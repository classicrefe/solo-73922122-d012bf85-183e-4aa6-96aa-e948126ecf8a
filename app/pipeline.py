"""批次处理流水线：校验 -> 映射保留（短写锁）-> 转换（不持锁）-> 原子发布。

暂存与发布目录布局::

    <data>/staging/<batch_id>/<安全文件名>      转换产物 + 临时 zip
    <data>/published/<batch_id>.zip             发布后整批下载件（原子 rename）

任何环节失败或进程中断：
  * 已 PUBLISHED 的映射与 zip 永不回滚；
  * 本批次 PENDING 映射由 :func:`app.db.fail_batch` / 启动恢复回收；
  * 暂存目录在失败时与启动时双重清理，published 下不会出现孤儿 zip。
"""
from __future__ import annotations

import os
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import db
from .catalog import build_publish_catalog_entry, extract_source_view
from .config import settings
from .dicom import DicomValidationError, ParsedFile, parse_and_validate, remap_to_bytes
from .ids import ID_KINDS

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    """把上传文件名规整为 zip/文件系统安全名，防止路径穿越。"""
    base = os.path.basename(name.replace("\\", "/")).strip() or "unnamed.dcm"
    cleaned = _SAFE_NAME_RE.sub("_", base)
    if cleaned in (".", "..") or not cleaned:
        cleaned = "unnamed.dcm"
    return cleaned[:180]


@dataclass
class UploadedFile:
    filename: str
    content: bytes


class UploadLimitError(ValueError):
    """文件数量/总大小超限。"""


def validate_upload_bundle(files: list[UploadedFile]) -> None:
    if not files:
        raise UploadLimitError("批次为空，请至少上传 1 份 DICOM 文件")
    if len(files) > settings.max_files:
        raise UploadLimitError(
            f"单批最多 {settings.max_files} 份文件，本次为 {len(files)} 份"
        )
    total = sum(len(f.content) for f in files)
    if total > settings.max_total_bytes:
        raise UploadLimitError(
            f"单批合计不得超过 {settings.max_total_bytes // (1024 * 1024)} MiB，"
            f"本次为 {total / (1024 * 1024):.2f} MiB"
        )
    if any(not f.content for f in files):
        raise UploadLimitError("存在空文件，已拒绝整批")
    names = [safe_filename(f.filename) for f in files]
    if len(set(names)) != len(names):
        raise UploadLimitError("批次内存在同名文件（规整后），已拒绝整批")


def _staging_dir(batch_id: str) -> Path:
    return settings.staging_dir / batch_id


def cleanup_staging(batch_id: str) -> None:
    shutil.rmtree(_staging_dir(batch_id), ignore_errors=True)


def recover_on_startup() -> list[str]:
    """启动恢复：回滚中断批次的 PENDING 数据，并清理暂存/孤儿发布文件。"""
    conn = db.connect()
    try:
        stale = db.recover_pending(conn)
    finally:
        conn.close()
    for batch_id in stale:
        cleanup_staging(batch_id)

    # 仅保留数据库中已 PUBLISHED 批次对应的 zip，其余视为孤儿删除
    conn = db.connect()
    try:
        published = {
            f"{row['id']}.zip"
            for row in conn.execute(
                "SELECT id FROM batches WHERE status='PUBLISHED' AND zip_name IS NOT NULL"
            )
        }
    finally:
        conn.close()
    for entry in settings.published_dir.iterdir():
        if entry.is_file() and entry.name not in published:
            entry.unlink(missing_ok=True)
    return stale


def process_batch(project_id: str, files: list[UploadedFile]) -> str:
    """同步处理一个批次，返回 batch_id。任何异常都保证不留部分批次。"""
    validate_upload_bundle(files)
    batch_id = str(uuid.uuid4())
    work_dir = _staging_dir(batch_id)
    work_dir.mkdir(parents=True, exist_ok=False)

    # ---- 阶段 1：纯内存解析校验，数据库尚未写入 ----------------------------- #
    parsed_files: list[ParsedFile] = []
    seen_sop: set[str] = set()
    try:
        for uploaded in files:
            parsed = parse_and_validate(uploaded.content, uploaded.filename)
            # 重复检测只针对各文件“自身”的顶层 SOP Instance UID；
            # 嵌套序列里引用的他人 SOP 属于合法引用，不计入重复。
            own_sop = str(parsed.dataset.SOPInstanceUID)
            if own_sop in seen_sop:
                raise DicomValidationError(
                    f"{uploaded.filename}: 批次内出现重复 SOP Instance UID {own_sop}"
                )
            seen_sop.add(own_sop)
            parsed_files.append(parsed)
    except Exception:
        cleanup_staging(batch_id)
        raise

    requested = {
        kind: sorted({value for parsed in parsed_files for value in parsed.identifiers[kind]})
        for kind in ID_KINDS
    }

    # ---- 阶段 2：短写锁内保留映射（含对并发批次的等待），返回后立即放锁 ------ #
    conn = db.connect()
    try:
        mapping = db.reserve_mappings(conn, project_id, batch_id, requested)
    except Exception:
        conn.close()
        cleanup_staging(batch_id)
        raise

    # ---- 阶段 3：转换，不持有任何数据库事务/写锁 ----------------------------- #
    file_records: list[dict] = []
    total_out = 0
    catalog: dict[str, dict] = {}
    try:
        for parsed in parsed_files:
            # 重写会就地修改 parsed.dataset，必须先采集重编码前的源引用视图
            source_view = extract_source_view(parsed)
            encoded = remap_to_bytes(parsed, mapping)
            entry = safe_filename(parsed.filename)
            out_path = work_dir / entry
            out_path.write_bytes(encoded)
            # 从重编码产物提取目标侧视图并配对，生成实例/引用目录记录
            catalog[entry] = build_publish_catalog_entry(source_view, encoded)
            file_records.append(
                {
                    "original_name": parsed.filename,
                    "archive_entry": entry,
                    "size_bytes": len(encoded),
                }
            )
            total_out += len(encoded)

        zip_name = f"{batch_id}.zip"
        temp_zip = work_dir / zip_name
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for rec in file_records:
                zf.write(work_dir / rec["archive_entry"], arcname=rec["archive_entry"])
    except Exception as exc:
        conn.close()
        _abort(batch_id, f"转换失败: {exc}")
        raise

    # ---- 阶段 4：原子发布。先落盘 zip（原子 rename），再单事务提交 DB ------- #
    final_zip = settings.published_dir / zip_name
    try:
        os.replace(temp_zip, final_zip)
        db.publish_batch(conn, batch_id, file_records, zip_name, total_out, catalog)
    except Exception as exc:
        # DB 提交失败：zip 还未被任何已提交批次引用，作为孤儿在下次启动清理；
        # 这里立即删除以保证“不留部分批次”，并回收本批次 PENDING 映射。
        final_zip.unlink(missing_ok=True)
        _abort_with_conn(conn, batch_id, f"发布失败: {exc}")
        raise
    finally:
        conn.close()

    cleanup_staging(batch_id)
    return batch_id


def _abort(batch_id: str, error: str) -> None:
    conn = db.connect()
    try:
        _abort_with_conn(conn, batch_id, error)
    finally:
        conn.close()


def _abort_with_conn(conn, batch_id: str, error: str) -> None:
    db.fail_batch(conn, batch_id, error)
    cleanup_staging(batch_id)


def zip_path(batch_id: str) -> Path:
    return settings.published_dir / f"{batch_id}.zip"
