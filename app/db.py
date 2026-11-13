"""SQLite 持久层：项目、批次、映射与并发保留协议。

并发模型（保证“并发批次不能给同一源标识分配不同结果”）：
  1. 保留阶段在单条 ``BEGIN IMMEDIATE`` 事务（持有写锁，毫秒级）内：
     - 登记 PENDING 批次；
     - 对每个源标识 ``INSERT OR IGNORE`` 一条 PENDING 映射；
     - 已存在但属他批次且仍 PENDING 时，本批次稍后轮询等待；
     - 已存在且 PUBLISHED 时直接复用。
  2. 转换阶段不持任何数据库事务/写锁（纯文件 CPU 工作）。
  3. 发布阶段再用单条写事务把本批次与本批次新建映射一并置为 PUBLISHED。
任何失败/中断：本批次 PENDING 映射与 PENDING 批次被回收（启动恢复或即时失败处理），
已 PUBLISHED 的映射永不修改。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import ensure_dirs, settings
from .ids import ID_KINDS, new_patient_id, new_uid

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS batches (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id),
    status       TEXT NOT NULL CHECK (status IN ('PENDING','PUBLISHED','FAILED')),
    error        TEXT,
    file_count   INTEGER NOT NULL,
    total_bytes  INTEGER NOT NULL,
    zip_name     TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id         TEXT NOT NULL REFERENCES batches(id),
    original_name    TEXT NOT NULL,
    archive_entry    TEXT NOT NULL,
    size_bytes       INTEGER NOT NULL,
    UNIQUE(batch_id, original_name)
);

CREATE TABLE IF NOT EXISTS id_mappings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(id),
    id_kind          TEXT NOT NULL CHECK (id_kind IN ('patient_id','study_uid','series_uid','sop_uid')),
    source_value     TEXT NOT NULL,
    target_value     TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('PENDING','PUBLISHED')),
    created_batch_id TEXT NOT NULL REFERENCES batches(id),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project_id, id_kind, source_value)
);

CREATE INDEX IF NOT EXISTS idx_mappings_lookup
    ON id_mappings(project_id, id_kind, source_value);
CREATE INDEX IF NOT EXISTS idx_mappings_batch ON id_mappings(created_batch_id);
CREATE INDEX IF NOT EXISTS idx_batches_project ON batches(project_id);

-- 实例目录：已发布 ZIP 内每个 DICOM 文件（实例）一行，按“重编码后”的
-- target SOP Instance UID 登记。file_id 唯一保证补建幂等（同一条目永不重复）。
CREATE TABLE IF NOT EXISTS instances (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id     TEXT NOT NULL REFERENCES projects(id),
    batch_id       TEXT NOT NULL REFERENCES batches(id),
    file_id        INTEGER NOT NULL REFERENCES batch_files(id),
    source_sop_uid TEXT NOT NULL,
    target_sop_uid TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(file_id)
);
CREATE INDEX IF NOT EXISTS idx_instances_lookup
    ON instances(project_id, target_sop_uid);
CREATE INDEX IF NOT EXISTS idx_instances_batch ON instances(batch_id);

-- 引用目录：每个文件中每个 ReferencedSOPInstanceUID 标签出现点一行，
-- 记录来源批次/文件/标签路径与重编码前后 UID。
-- (source_file_id, tag_path) 唯一：标签路径在同一文件内是结构唯一位置，
-- 补建中断重启后靠该约束 + INSERT OR IGNORE 实现“不重复记引用”。
CREATE TABLE IF NOT EXISTS instance_references (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id             TEXT NOT NULL REFERENCES projects(id),
    source_batch_id        TEXT NOT NULL REFERENCES batches(id),
    source_file_id         INTEGER NOT NULL REFERENCES batch_files(id),
    source_file_name       TEXT NOT NULL,
    tag_path               TEXT NOT NULL,
    source_referenced_uid  TEXT NOT NULL,
    target_referenced_uid  TEXT NOT NULL,
    created_at             TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_file_id, tag_path)
);
CREATE INDEX IF NOT EXISTS idx_refs_lookup
    ON instance_references(project_id, target_referenced_uid);
CREATE INDEX IF NOT EXISTS idx_refs_batch ON instance_references(source_batch_id);

-- 目录补建进度：每个已发布批次一行。
--   PENDING：补建进行中或进程中断（可续建）
--   DONE   ：实例与引用目录已随发布事务写入或补建完成
--   FAILED ：缺 ZIP/解析失败，可重试
CREATE TABLE IF NOT EXISTS catalog_builds (
    batch_id        TEXT PRIMARY KEY REFERENCES batches(id),
    project_id      TEXT NOT NULL REFERENCES projects(id),
    status          TEXT NOT NULL CHECK (status IN ('PENDING','DONE','FAILED')),
    error           TEXT,
    total_files     INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SEED_PROJECTS = ("肺癌筛查研究", "脑卒中影像队列")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path or settings.db_path),
        timeout=30.0,
        isolation_level=None,  # 手动事务
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={settings.lock_wait_seconds * 1000}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """显式事务上下文。``immediate`` 一开启即获取写锁，避免死锁与升级失败。"""
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db() -> None:
    ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        with transaction(conn) as tx:
            for name in SEED_PROJECTS:
                tx.execute(
                    "INSERT OR IGNORE INTO projects(id, name) VALUES (?, ?)",
                    (str(uuid.uuid4()), name),
                )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 项目
# --------------------------------------------------------------------------- #
def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY name").fetchall()


def get_project(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()


def create_project(conn: sqlite3.Connection, name: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    if row:
        return row
    project_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO projects(id, name) VALUES (?, ?)", (project_id, name)
    )
    return conn.execute(
        "SELECT * FROM projects WHERE id = ?", (project_id,)
    ).fetchone()


# --------------------------------------------------------------------------- #
# 映射保留协议
# --------------------------------------------------------------------------- #
def _generate_target(kind: str) -> str:
    if kind == "patient_id":
        return new_patient_id()
    return new_uid()


def reserve_mappings(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
    requested: dict[str, list[str]],
) -> dict[str, dict[str, str]]:
    """为批次保留全部源标识的映射。

    ``requested``: {id_kind: [去重后的源值...]}
    返回 {id_kind: {source_value: target_value}}，包含复用的已发布映射与新保留映射。

    转换期不持锁：本函数返回后即释放事务，调用方拿返回表做纯文件转换。
    他批次持有的 PENDING 冲突会在锁内/锁后轮询等待，保证同一源标识永远得到同一目标。
    """
    deadline = time.monotonic() + settings.lock_wait_seconds
    result: dict[str, dict[str, str]] = {kind: {} for kind in ID_KINDS}
    pending: dict[str, list[str]] = {
        kind: list(dict.fromkeys(requested.get(kind, []))) for kind in ID_KINDS
    }

    while True:
        blocked: list[tuple[str, str]] = []
        with transaction(conn):
            conn.execute(
                "INSERT INTO batches(id, project_id, status, file_count, total_bytes) "
                "VALUES (?, ?, 'PENDING', 0, 0) "
                "ON CONFLICT(id) DO NOTHING",
                (batch_id, project_id),
            )
            for kind in ID_KINDS:
                remaining: list[str] = []
                for source in pending[kind]:
                    existing = conn.execute(
                        "SELECT target_value, status, created_batch_id FROM id_mappings "
                        "WHERE project_id=? AND id_kind=? AND source_value=?",
                        (project_id, kind, source),
                    ).fetchone()
                    if existing is None:
                        target = _generate_target(kind)
                        conn.execute(
                            "INSERT OR IGNORE INTO id_mappings"
                            "(project_id, id_kind, source_value, target_value, status, created_batch_id) "
                            "VALUES (?,?,?,?, 'PENDING', ?)",
                            (project_id, kind, source, target, batch_id),
                        )
                        row = conn.execute(
                            "SELECT target_value, status FROM id_mappings "
                            "WHERE project_id=? AND id_kind=? AND source_value=?",
                            (project_id, kind, source),
                        ).fetchone()
                        result[kind][source] = row["target_value"]
                        if row["status"] == "PUBLISHED":
                            # 竞态：插入被唯一约束忽略且他端刚发布，直接复用
                            continue
                    elif existing["status"] == "PUBLISHED":
                        result[kind][source] = existing["target_value"]
                    else:
                        # 他批次 PENDING：本批次等待
                        blocked.append((kind, source))
                        remaining.append(source)
                pending[kind] = remaining

        if not blocked:
            return result

        # 不持锁轮询，等待持有者发布或失败回收
        while time.monotonic() < deadline:
            time.sleep(0.1)
            still_blocked: list[tuple[str, str]] = []
            for kind, source in blocked:
                row = conn.execute(
                    "SELECT target_value, status FROM id_mappings "
                    "WHERE project_id=? AND id_kind=? AND source_value=?",
                    (project_id, kind, source),
                ).fetchone()
                if row is None:
                    # 持有者失败、PENDING 行已回收 -> 下一轮自行保留
                    continue
                if row["status"] == "PUBLISHED":
                    result[kind][source] = row["target_value"]
                    continue
                still_blocked.append((kind, source))
            blocked = still_blocked
            if not blocked:
                break
        else:
            raise TimeoutError(
                "等待并发批次的标识映射超时；为保证同一源标识不产生不同结果，本批次中止"
            )
        # 仍 blocked 且未超时（理论上 blocked 为空才会 break），继续下一轮保留


def publish_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    file_records: list[dict],
    zip_name: str,
    total_bytes: int,
    catalog: dict | None = None,
) -> None:
    """原子发布：本批次 + 本批次新建 PENDING 映射 + 实例/引用目录一起提交。

    目录与批次在同一事务内落库：发布失败整体回滚，绝不会出现“批次可见但
    引用目录缺失/半成品”的状态（失败不留可见引用）。
    """
    with transaction(conn):
        conn.execute(
            "UPDATE batches SET status='PUBLISHED', zip_name=?, file_count=?, "
            "total_bytes=?, published_at=datetime('now') WHERE id=?",
            (zip_name, len(file_records), total_bytes, batch_id),
        )
        conn.executemany(
            "INSERT INTO batch_files(batch_id, original_name, archive_entry, size_bytes) "
            "VALUES (?,?,?,?)",
            [
                (
                    batch_id,
                    rec["original_name"],
                    rec["archive_entry"],
                    rec["size_bytes"],
                )
                for rec in file_records
            ],
        )
        conn.execute(
            "UPDATE id_mappings SET status='PUBLISHED' "
            "WHERE created_batch_id=? AND status='PENDING'",
            (batch_id,),
        )
        if catalog is not None:
            project_id = conn.execute(
                "SELECT project_id FROM batches WHERE id=?", (batch_id,)
            ).fetchone()["project_id"]
            insert_catalog_records(
                conn, project_id, batch_id, file_records, catalog, mark_done=True
            )


def fail_batch(conn: sqlite3.Connection, batch_id: str, error: str) -> None:
    """失败回收：删除本批次尚未发布的 PENDING 映射并标记批次 FAILED。

    已 PUBLISHED 的映射永不触碰；PENDING 行删除后，等待中的并发批次下一轮
    会自行保留，绝不会继承本批次的失败结果。
    """
    with transaction(conn):
        conn.execute(
            "DELETE FROM id_mappings WHERE created_batch_id=? AND status='PENDING'",
            (batch_id,),
        )
        conn.execute(
            "UPDATE batches SET status='FAILED', error=? WHERE id=? AND status='PENDING'",
            (error, batch_id),
        )


def recover_pending(conn: sqlite3.Connection) -> list[str]:
    """启动恢复：把进程中断遗留的 PENDING 批次/映射回滚为 FAILED。

    返回被回收的批次 id 列表（供文件层清理对应暂存目录）。
    """
    rows = conn.execute(
        "SELECT id FROM batches WHERE status='PENDING'"
    ).fetchall()
    stale_ids = [r["id"] for r in rows]
    with transaction(conn):
        conn.execute(
            "DELETE FROM id_mappings WHERE status='PENDING' AND created_batch_id IN "
            "(SELECT id FROM batches WHERE status='PENDING')"
        )
        conn.execute(
            "UPDATE batches SET status='FAILED', error='进程中断，启动时自动回滚' "
            "WHERE status='PENDING'"
        )
    return stale_ids


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #
def list_batches(conn: sqlite3.Connection, project_id: str | None = None) -> list[sqlite3.Row]:
    if project_id:
        return conn.execute(
            "SELECT * FROM batches WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        ).fetchall()
    return conn.execute("SELECT * FROM batches ORDER BY created_at DESC").fetchall()


def get_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()


def list_batch_files(conn: sqlite3.Connection, batch_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM batch_files WHERE batch_id=? ORDER BY id", (batch_id,)
    ).fetchall()


def list_mappings(
    conn: sqlite3.Connection, project_id: str | None = None
) -> list[sqlite3.Row]:
    if project_id:
        return conn.execute(
            "SELECT * FROM id_mappings WHERE project_id=? AND status='PUBLISHED' "
            "ORDER BY id",
            (project_id,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM id_mappings WHERE status='PUBLISHED' ORDER BY id"
    ).fetchall()


# --------------------------------------------------------------------------- #
# 实例目录 / 引用目录（项目引用核查）
# --------------------------------------------------------------------------- #
def get_batch_file_ids(
    conn: sqlite3.Connection, batch_id: str
) -> dict[str, int]:
    """批次内 archive_entry -> batch_files.id 映射。"""
    rows = conn.execute(
        "SELECT id, archive_entry FROM batch_files WHERE batch_id=?",
        (batch_id,),
    ).fetchall()
    return {r["archive_entry"]: r["id"] for r in rows}


def insert_catalog_records(
    conn: sqlite3.Connection,
    project_id: str,
    batch_id: str,
    file_records: list[dict],
    catalog: dict[str, dict],
    mark_done: bool,
) -> None:
    """在调用方的事务内写入一个批次的实例与引用目录，并登记补建进度。

    ``catalog``: {archive_entry: {"instance": {...}, "references": [{...}, ...]}}。
    全部写入使用 INSERT OR IGNORE，配合唯一约束实现幂等：补建重试/中断续建
    重复执行不会产生重复实例或重复引用行。
    ``mark_done=True`` 时把 catalog_builds 置 DONE（发布事务及补建完成路径）。
    """
    file_ids = get_batch_file_ids(conn, batch_id)
    for rec in file_records:
        entry = rec["archive_entry"]
        file_id = file_ids[entry]
        item = catalog.get(entry)
        if item is None:  # pragma: no cover - 防御：调用方应覆盖每个文件
            continue
        inst = item["instance"]
        conn.execute(
            "INSERT OR IGNORE INTO instances"
            "(project_id, batch_id, file_id, source_sop_uid, target_sop_uid) "
            "VALUES (?,?,?,?,?)",
            (project_id, batch_id, file_id,
             inst["source_sop_uid"], inst["target_sop_uid"]),
        )
        for ref in item.get("references", []):
            conn.execute(
                "INSERT OR IGNORE INTO instance_references"
                "(project_id, source_batch_id, source_file_id, source_file_name, "
                "tag_path, source_referenced_uid, target_referenced_uid) "
                "VALUES (?,?,?,?,?,?,?)",
                (project_id, batch_id, file_id, entry,
                 ref["tag_path"], ref["source_uid"], ref["target_uid"]),
            )
    if mark_done:
        conn.execute(
            "INSERT INTO catalog_builds(batch_id, project_id, status, total_files, "
            "processed_files) VALUES (?,?, 'DONE', ?, ?) "
            "ON CONFLICT(batch_id) DO UPDATE SET status='DONE', error=NULL, "
            "total_files=excluded.total_files, processed_files=excluded.processed_files, "
            "updated_at=datetime('now')",
            (batch_id, project_id, len(file_records), len(file_records)),
        )
    else:
        conn.execute(
            "INSERT INTO catalog_builds(batch_id, project_id, status, total_files, "
            "processed_files) VALUES (?,?, 'PENDING', ?, 0) "
            "ON CONFLICT(batch_id) DO UPDATE SET status='PENDING', error=NULL, "
            "updated_at=datetime('now')",
            (batch_id, project_id, len(file_records)),
        )


def upsert_catalog_build(
    conn: sqlite3.Connection,
    batch_id: str,
    project_id: str,
    status: str,
    total_files: int,
    processed_files: int,
    error: str | None = None,
) -> None:
    """登记/更新单个批次的目录补建进度。"""
    conn.execute(
        "INSERT INTO catalog_builds(batch_id, project_id, status, error, total_files, "
        "processed_files) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(batch_id) DO UPDATE SET status=excluded.status, error=excluded.error, "
        "total_files=excluded.total_files, processed_files=excluded.processed_files, "
        "updated_at=datetime('now')",
        (batch_id, project_id, status, error, total_files, processed_files),
    )


def list_catalog_builds(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT cb.* FROM catalog_builds cb ORDER BY cb.batch_id"
    ).fetchall()


def list_published_batches_missing_catalog(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """已发布但目录未完成（无记录 / PENDING / FAILED）的批次，按创建时间排序。

    已 PUBLISHED 但目录尚未开始的历史批次（无 catalog_builds 行）也会被选出，
    这是“历史目录补建”的入口集合。
    """
    return conn.execute(
        "SELECT b.* FROM batches b "
        "LEFT JOIN catalog_builds cb ON cb.batch_id = b.id "
        "WHERE b.status='PUBLISHED' "
        "AND (cb.status IS NULL OR cb.status IN ('PENDING','FAILED')) "
        "ORDER BY b.created_at, b.id"
    ).fetchall()


def catalog_coverage(conn: sqlite3.Connection) -> dict:
    """目录补建覆盖度：已发布批次总数与各补建状态计数。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM batches WHERE status='PUBLISHED'"
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM catalog_builds GROUP BY status"
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    done = by_status.get("DONE", 0)
    return {
        "published_total": total,
        "catalog_done": done,
        "catalog_pending": by_status.get("PENDING", 0),
        "catalog_failed": by_status.get("FAILED", 0),
        "catalog_unknown": max(total - done - by_status.get("PENDING", 0)
                               - by_status.get("FAILED", 0), 0),
        "complete": total > 0 and done == total,
    }


def project_catalog_coverage(conn: sqlite3.Connection, project_id: str) -> dict:
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM batches WHERE status='PUBLISHED' AND project_id=?",
        (project_id,),
    ).fetchone()["n"]
    rows = conn.execute(
        "SELECT cb.status, COUNT(*) AS n FROM catalog_builds cb "
        "JOIN batches b ON b.id = cb.batch_id "
        "WHERE b.project_id=? GROUP BY cb.status",
        (project_id,),
    ).fetchall()
    by_status = {r["status"]: r["n"] for r in rows}
    done = by_status.get("DONE", 0)
    return {
        "published_total": total,
        "catalog_done": done,
        "catalog_pending": by_status.get("PENDING", 0),
        "catalog_failed": by_status.get("FAILED", 0),
        "complete": total > 0 and done == total,
    }


def project_missing_references(
    conn: sqlite3.Connection, project_id: str
) -> list[sqlite3.Row]:
    """已分配目标 UID 但本项目内没有任何已发布实例的引用（缺项清单）。

    判定规则（全部在同一条只读事务/快照内执行）：
      * 引用目录已随批次发布，引用侧一定有记录；
      * “到齐”仅当目标 target UID 在**同一项目**的实例目录中存在
        （其他项目有实例不算、仅映射表存在不算、来源批次未发布则根本无记录）。
    """
    return conn.execute(
        "SELECT r.target_referenced_uid, "
        "       MIN(r.source_referenced_uid) AS source_referenced_uid, "
        "       COUNT(*) AS reference_count, "
        "       GROUP_CONCAT(DISTINCT r.source_batch_id) AS source_batch_ids, "
        "       GROUP_CONCAT(DISTINCT r.source_file_name) AS source_file_names "
        "FROM instance_references r "
        "WHERE r.project_id=? "
        "  AND NOT EXISTS ( "
        "      SELECT 1 FROM instances i "
        "      WHERE i.project_id=r.project_id "
        "        AND i.target_sop_uid=r.target_referenced_uid) "
        "GROUP BY r.target_referenced_uid "
        "ORDER BY r.target_referenced_uid",
        (project_id,),
    ).fetchall()


def project_missing_reference_sources(
    conn: sqlite3.Connection, project_id: str, target_uid: str
) -> list[sqlite3.Row]:
    """某个缺失目标 UID 的全部引用来源（批次/文件/标签路径）。"""
    return conn.execute(
        "SELECT r.*, b.created_at AS batch_created_at "
        "FROM instance_references r "
        "JOIN batches b ON b.id = r.source_batch_id "
        "WHERE r.project_id=? AND r.target_referenced_uid=? "
        "ORDER BY r.id",
        (project_id, target_uid),
    ).fetchall()
