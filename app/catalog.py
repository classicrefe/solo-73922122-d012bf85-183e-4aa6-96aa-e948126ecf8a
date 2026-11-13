"""实例目录 / 引用目录的提取、随批发布与历史批次后台补建。

两类持久目录（表结构见 :mod:`app.db`）：

* ``instances``：已发布 ZIP 内每个实例一行，按**重编码后**的 target
  SOP Instance UID 登记；
* ``instance_references``：每个文件中每个 ``ReferencedSOPInstanceUID``
  标签出现点一行（递归进入任意 VR=SQ，不限嵌套深度），记录来源批次、
  来源文件与标签路径。

关联规则：引用的 ``target_referenced_uid`` 与同项目 ``instances.target_sop_uid``
相等才算“到齐”。仅映射表存在、其他项目存在实例都不算到齐；来源批次未发布时
引用行根本不存在，也不可能产生虚假结论。

后台补建的并发约束：

* 读取 ZIP 与 pydicom 解析是纯文件/CPU 操作，**不持有任何数据库写锁**，
  期间上传流水线照常保留/发布；
* 每个文件解析完成后才开启毫秒级短事务写入，WAL + busy_timeout 串行化写者；
* 全部插入使用 ``INSERT OR IGNORE`` + 唯一约束，中断重启后重做同一批次
  不会产生重复实例或重复引用（续建幂等）；
* 缺 ZIP / 解析失败把批次标 FAILED 并跳过，页面显示“未完成”，可显式重试；
* 补建线程全局单例，扫描到没有 NULL/PENDING 批次后自行退出。
"""
from __future__ import annotations

import io
import threading
import zipfile
from dataclasses import dataclass, field

import pydicom

from . import db
from .config import settings

REF_TAG_KEYWORD = "ReferencedSOPInstanceUID"


# --------------------------------------------------------------------------- #
# 提取：递归遍历任意嵌套序列，记录标签路径
# --------------------------------------------------------------------------- #
def _element_name(elem) -> str:
    keyword = getattr(elem, "keyword", "") or ""
    return keyword or str(elem.tag)  # 私有/未知标签回退为 (gggg,eeee)


def walk_referenced_sop(ds, prefix: str = ""):
    """递归产出 ``(标签路径, 引用 UID)``，进入任意深度的任意 SQ。

    标签路径形如：
    ``ReferencedStudySequence[0]/ReferencedSOPSequence[0]/ReferencedSOPInstanceUID``
    只遍历数据集本体；file_meta 中的 MediaStorageSOPInstanceUID 是文件自身
    标识而非引用，不在遍历范围内。
    """
    for elem in ds:
        name = _element_name(elem)
        path = f"{prefix}/{name}" if prefix else name
        if elem.VR == "SQ":
            for idx, item in enumerate(elem.value or []):
                yield from walk_referenced_sop(item, prefix=f"{path}[{idx}]")
        elif name == REF_TAG_KEYWORD and elem.value not in (None, ""):
            yield path, str(elem.value)


def extract_view(raw: bytes) -> dict:
    """从一份（重编码后的）DICOM 字节提取自身 SOP 与全部引用点。"""
    ds = pydicom.dcmread(io.BytesIO(raw))
    refs = {path: uid for path, uid in walk_referenced_sop(ds)}
    return {
        "target_sop_uid": str(ds.SOPInstanceUID),
        "refs": refs,
    }


@dataclass
class SourceView:
    """重编码前从 ParsedFile 采集的源标识视图（重写会就地修改数据集，须先采集）。"""

    source_sop_uid: str
    refs: dict = field(default_factory=dict)


def extract_source_view(parsed) -> SourceView:
    refs = {path: uid for path, uid in walk_referenced_sop(parsed.dataset)}
    return SourceView(source_sop_uid=str(parsed.dataset.SOPInstanceUID), refs=refs)


def build_publish_catalog_entry(source_view: SourceView, encoded: bytes) -> dict:
    """配对重编码前后视图，生成单文件的目录记录。

    标签路径在同一数据集结构内唯一，前后视图按路径配对；目标侧缺失某路径
    属于重写器缺陷，直接抛出以阻止发布（失败不留可见引用）。
    """
    target = extract_view(encoded)
    references = []
    for path, source_uid in source_view.refs.items():
        if path not in target["refs"]:
            raise RuntimeError(
                f"重编码后引用标签路径丢失: {path}，拒绝发布以保证引用目录完整"
            )
        references.append(
            {
                "tag_path": path,
                "source_uid": source_uid,
                "target_uid": target["refs"][path],
            }
        )
    return {
        "instance": {
            "source_sop_uid": source_view.source_sop_uid,
            "target_sop_uid": target["target_sop_uid"],
        },
        "references": references,
    }


# --------------------------------------------------------------------------- #
# 历史批次补建
# --------------------------------------------------------------------------- #
class BackfillState:
    """补建线程的运行态（内存态，重启后从 catalog_builds 表重建进度）。"""

    def __init__(self) -> None:
        # RLock：start_backfill 持锁后调用 snapshot() 会再次取锁，
        # 可重入锁避免自死锁；锁内只做内存字段读写，持有时间极短。
        self.lock = threading.RLock()
        self.running = False
        self.thread: threading.Thread | None = None
        self.started_at: str | None = None
        self.stop_requested = False
        self.current_batch_id: str | None = None
        self.processed_batches = 0
        self.failed_batches = 0
        self.last_error: str | None = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "current_batch_id": self.current_batch_id,
                "processed_batches": self.processed_batches,
                "failed_batches": self.failed_batches,
                "last_error": self.last_error,
            }


_state = BackfillState()


class BackfillError(RuntimeError):
    """单个批次补建失败（缺 ZIP / 解析失败等），不影响其他批次继续补建。"""


def _reverse_sop_mapping(conn, project_id: str) -> dict[str, str]:
    """项目内 target SOP UID -> source SOP UID 反查表（仅取已发布映射）。"""
    rows = conn.execute(
        "SELECT source_value, target_value FROM id_mappings "
        "WHERE project_id=? AND id_kind='sop_uid' AND status='PUBLISHED'",
        (project_id,),
    ).fetchall()
    return {r["target_value"]: r["source_value"] for r in rows}


def _backfill_one_batch(conn, batch) -> None:
    """补建单个批次：先离线读 ZIP/解析（不持写锁），再按文件开短事务写入。"""
    batch_id = batch["id"]
    project_id = batch["project_id"]
    zip_file = settings.published_dir / f"{batch_id}.zip"
    if not zip_file.is_file():
        raise BackfillError(f"已发布 ZIP 缺失: {zip_file.name}")

    reverse_map = _reverse_sop_mapping(conn, batch["project_id"])

    # ---- 离线阶段：读 ZIP + pydicom 解析，全程不持有数据库事务/写锁 ---------- #
    parsed_entries: list[tuple[str, dict]] = []
    try:
        with zipfile.ZipFile(zip_file, "r") as zf:
            names = sorted(
                n for n in zf.namelist() if not n.endswith("/")
            )
            for name in names:
                raw = zf.read(name)
                try:
                    view = extract_view(raw)
                except Exception as exc:
                    raise BackfillError(
                        f"文件 {name} 解析失败: {exc}"
                    ) from exc
                parsed_entries.append((name, view))
    except zipfile.BadZipFile as exc:
        raise BackfillError(f"ZIP 损坏无法读取: {exc}") from exc

    file_rows = conn.execute(
        "SELECT id, archive_entry FROM batch_files WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    file_ids = {r["archive_entry"]: r["id"] for r in file_rows}

    total = len(parsed_entries)
    processed = 0
    # ---- 写入阶段：每个文件一条毫秒级短事务，锁外已完成全部 CPU 工作 -------- #
    for entry_name, view in parsed_entries:
        file_id = file_ids.get(entry_name)
        if file_id is None:  # pragma: no cover - 发布一致性保证不会发生
            raise BackfillError(f"ZIP 条目在 batch_files 无登记: {entry_name}")
        target_sop = view["target_sop_uid"]
        source_sop = reverse_map.get(target_sop, target_sop)
        references = [
            {
                "tag_path": path,
                # 历史 ZIP 内只有重编码值；源值经持久映射反查，查不到则回退为目标值
                "source_uid": reverse_map.get(target_uid, target_uid),
                "target_uid": target_uid,
            }
            for path, target_uid in sorted(view["refs"].items())
        ]
        with db.transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO instances"
                "(project_id, batch_id, file_id, source_sop_uid, target_sop_uid) "
                "VALUES (?,?,?,?,?)",
                (project_id, batch_id, file_id, source_sop, target_sop),
            )
            for ref in references:
                conn.execute(
                    "INSERT OR IGNORE INTO instance_references"
                    "(project_id, source_batch_id, source_file_id, source_file_name, "
                    "tag_path, source_referenced_uid, target_referenced_uid) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (project_id, batch_id, file_id, entry_name,
                     ref["tag_path"], ref["source_uid"], ref["target_uid"]),
                )
            processed += 1
            db.upsert_catalog_build(
                conn, batch_id, project_id, "PENDING", total, processed
            )

    with db.transaction(conn):
        db.upsert_catalog_build(
            conn, batch_id, project_id, "DONE", total, total
        )


def _run_backfill(include_failed: bool) -> None:
    """补建主循环。

    启动时快照本次需要处理的批次集合（NULL/PENDING，重试时另含 FAILED），
    每个批次在本轮**恰好处理一次**：成功置 DONE，缺包/解析失败置 FAILED 后
    不再重选，杜绝坏批次造成忙等死循环。补建期间新发布的批次目录已随发布
    事务写入（DONE），不在待补集合内；若其发布晚于本快照且目录缺失，会在
    下次启动/下次手动补建时被覆盖。
    """
    conn = db.connect()
    try:
        query = (
            "SELECT b.* FROM batches b "
            "LEFT JOIN catalog_builds cb ON cb.batch_id = b.id "
            "WHERE b.status='PUBLISHED' AND ("
            "  cb.status IS NULL OR cb.status='PENDING'"
            + (" OR cb.status='FAILED'" if include_failed else "")
            + ") ORDER BY b.created_at, b.id"
        )
        pending_batches = conn.execute(query).fetchall()
        seen: set[str] = set()
        for batch in pending_batches:
            if _state.stop_requested:
                break
            # 快照内去重，保证一个批次本轮只处理一次
            if batch["id"] in seen:
                continue
            seen.add(batch["id"])
            with _state.lock:
                _state.current_batch_id = batch["id"]
            try:
                # 先把“无记录/FAILED”的批次登记为 PENDING（短事务），
                # 中断重启后该行依然指示续建。
                with db.transaction(conn):
                    db.upsert_catalog_build(
                        conn, batch["id"], batch["project_id"],
                        "PENDING", batch["file_count"], 0,
                    )
                _backfill_one_batch(conn, batch)
                with _state.lock:
                    _state.processed_batches += 1
                    _state.last_error = None
            except BackfillError as exc:
                # 终态失败：标记 FAILED 后本轮不再触碰，等待显式重试
                with db.transaction(conn):
                    db.upsert_catalog_build(
                        conn, batch["id"], batch["project_id"],
                        "FAILED", batch["file_count"], 0, error=str(exc),
                    )
                with _state.lock:
                    _state.failed_batches += 1
                    _state.last_error = f"批次 {batch['id']}: {exc}"
            except Exception as exc:  # 防御：意外错误也不能让线程静默死掉
                with _state.lock:
                    _state.last_error = f"批次 {batch['id']} 补建异常: {exc}"
    finally:
        conn.close()
        with _state.lock:
            _state.running = False
            _state.thread = None
            _state.current_batch_id = None


def start_backfill(retry_failed: bool = False) -> dict:
    """启动后台补建（已在运行则直接返回当前状态，保证单例）。"""
    with _state.lock:
        if _state.running:
            return _state.snapshot()
        conn = db.connect()
        try:
            query = (
                "SELECT b.id FROM batches b "
                "LEFT JOIN catalog_builds cb ON cb.batch_id = b.id "
                "WHERE b.status='PUBLISHED' AND ("
                "cb.status IS NULL OR cb.status='PENDING'"
                + (" OR cb.status='FAILED'" if retry_failed else "")
                + ")"
            )
            pending = conn.execute(query).fetchall()
        finally:
            conn.close()
        if not pending:
            return _state.snapshot()

        from datetime import datetime

        _state.running = True
        _state.stop_requested = False
        _state.started_at = datetime.utcnow().isoformat(timespec="seconds")
        _state.processed_batches = 0
        _state.failed_batches = 0
        _state.last_error = None
        thread = threading.Thread(
            target=_run_backfill,
            args=(retry_failed,),
            name="catalog-backfill",
            daemon=True,
        )
        _state.thread = thread
        thread.start()
        return _state.snapshot()


def backfill_status() -> dict:
    """补建线程运行态 + 全量目录覆盖度（各自独立短连接，不阻塞写者）。"""
    state = _state.snapshot()
    conn = db.connect()
    try:
        state["coverage"] = db.catalog_coverage(conn)
        state["builds"] = [dict(r) for r in db.list_catalog_builds(conn)]
    finally:
        conn.close()
    return state


def auto_resume_on_startup() -> None:
    """启动钩子：仅续建 NULL/PENDING 批次；FAILED 需用户在页面显式重试。"""
    conn = db.connect()
    try:
        has_pending = conn.execute(
            "SELECT 1 FROM batches b "
            "LEFT JOIN catalog_builds cb ON cb.batch_id = b.id "
            "WHERE b.status='PUBLISHED' AND "
            "(cb.status IS NULL OR cb.status='PENDING') LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if has_pending:
        start_backfill(retry_failed=False)


def stop_backfill(timeout: float = 5.0) -> None:
    """停止后台补建线程并等待其退出（主要用于测试隔离与优雅停机）。"""
    with _state.lock:
        thread = _state.thread
        _state.stop_requested = True
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    with _state.lock:
        _state.running = False
        _state.thread = None
        _state.current_batch_id = None
        _state.stop_requested = False
        _state.processed_batches = 0
        _state.failed_batches = 0
        _state.last_error = None
        _state.started_at = None
