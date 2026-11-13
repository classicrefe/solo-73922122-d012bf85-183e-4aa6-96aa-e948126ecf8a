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
) -> None:
    """原子发布：本批次 + 本批次新建 PENDING 映射一起转 PUBLISHED。"""
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
