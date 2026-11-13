"""FastAPI 入口：项目选择、DICOM 批次上传、批次列表与整批 ZIP 下载。"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, db
from .config import settings
from .dicom import DicomValidationError
from .ids import ID_KINDS
from .pipeline import (
    UploadLimitError,
    UploadedFile,
    process_batch,
    recover_on_startup,
    zip_path,
)

WEB_DIR = Path(__file__).parent / "web"

# SQLite 连接不能跨线程共享；处理线程各自取连接（check_same_thread=False
# 配合“每线程一连接”的使用方式，连接对象不会并发使用）。
_init_lock = threading.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with _init_lock:
        db.init_db()
        recover_on_startup()
        # 仅续建历史遗留的未完成目录（NULL/PENDING）；FAILED 等用户显式重试
        catalog.auto_resume_on_startup()
    yield


app = FastAPI(
    title="科研影像 DICOM 统一标识入库服务",
    version="1.0.0",
    description="按研究项目隔离的 DICOM UID/PatientID 持久重映射与整批 ZIP 下载",
    lifespan=lifespan,
)


def _get_project_or_404(conn, project_id: str):
    project = db.get_project(conn, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail=f"研究项目不存在: {project_id}")
    return project


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/projects")
def get_projects():
    conn = db.connect()
    try:
        return [dict(row) for row in db.list_projects(conn)]
    finally:
        conn.close()


@app.post("/api/projects", status_code=201)
def post_project(name: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    conn = db.connect()
    try:
        row = db.create_project(conn, name)
        return dict(row)
    finally:
        conn.close()


@app.post("/api/batches")
async def create_batch(
    project_id: str = Form(...),
    files: list[UploadFile] = File(..., description="最多 20 份、合计 ≤16 MiB 的未压缩 DICOM"),
):
    conn = db.connect()
    try:
        _get_project_or_404(conn, project_id)
    finally:
        conn.close()

    # 先在请求层做总量限制，避免超大上传耗尽内存
    payload: list[UploadedFile] = []
    total = 0
    for upload in files:
        content = await upload.read()
        total += len(content)
        if total > settings.max_total_bytes:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"批次合计超过 {settings.max_total_bytes // (1024 * 1024)} MiB 上限，"
                    "整批已拒绝，未写入任何数据"
                ),
            )
        payload.append(UploadedFile(filename=upload.filename or "unnamed.dcm", content=content))

    if len(payload) > settings.max_files:
        raise HTTPException(
            status_code=413,
            detail=f"单批最多 {settings.max_files} 份文件，本次为 {len(payload)} 份",
        )

    try:
        # 转换为 CPU/文件密集型同步流水线，放到线程池执行，避免阻塞事件循环，
        # 保证多批次 HTTP 请求能真正并发进入保留协议。
        batch_id = await run_in_threadpool(process_batch, project_id, payload)
    except UploadLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except DicomValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # 防御：任何意外错误都不允许部分落库
        raise HTTPException(status_code=500, detail=f"批次处理失败且已回滚: {exc}") from exc

    conn = db.connect()
    try:
        row = db.get_batch(conn, batch_id)
        return JSONResponse(status_code=201, content=dict(row))
    finally:
        conn.close()


@app.get("/api/batches")
def get_batches(project_id: str | None = None):
    conn = db.connect()
    try:
        if project_id:
            _get_project_or_404(conn, project_id)
        rows = db.list_batches(conn, project_id)
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    conn = db.connect()
    try:
        row = db.get_batch(conn, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        result = dict(row)
        result["files"] = [dict(r) for r in db.list_batch_files(conn, batch_id)]
        return result
    finally:
        conn.close()


@app.get("/api/batches/{batch_id}/download")
def download_batch(batch_id: str):
    conn = db.connect()
    try:
        row = db.get_batch(conn, batch_id)
        if row is None:
            raise HTTPException(status_code=404, detail="批次不存在")
        if row["status"] != "PUBLISHED":
            raise HTTPException(status_code=409, detail=f"批次状态为 {row['status']}，不可下载")
    finally:
        conn.close()

    path = zip_path(batch_id)
    if not path.is_file():
        # 已发布但文件缺失属于存储事故，明确报错而不是返回空包
        raise HTTPException(status_code=410, detail="发布的 ZIP 文件缺失，请联系管理员")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"remapped-{batch_id}.zip",
    )


@app.get("/api/mappings")
def get_mappings(project_id: str | None = None):
    conn = db.connect()
    try:
        if project_id:
            _get_project_or_404(conn, project_id)
        rows = db.list_mappings(conn, project_id)
        return [dict(row) for row in rows]
    finally:
        conn.close()


@app.get("/api/meta")
def get_meta():
    return {
        "max_files": settings.max_files,
        "max_total_bytes": settings.max_total_bytes,
        "max_total_mib": settings.max_total_bytes / (1024 * 1024),
        "id_kinds": ID_KINDS,
    }


# --------------------------------------------------------------------------- #
# 项目引用核查：实例/引用目录状态、后台补建、缺项查询
# --------------------------------------------------------------------------- #
@app.get("/api/catalog/backfill")
def get_backfill_status():
    """后台补建运行态 + 全量目录覆盖度 + 各批次补建进度。"""
    return catalog.backfill_status()


@app.post("/api/catalog/backfill", status_code=202)
async def start_catalog_backfill(retry_failed: bool = False):
    """启动历史目录后台补建（已在运行则幂等返回）。

    读 ZIP 与 pydicom 解析在后台线程执行且不持数据库写锁，上传流水线照常工作；
    ``retry_failed=true`` 时同时重试此前因缺包/解析失败标记的批次。
    """
    state = await run_in_threadpool(catalog.start_backfill, retry_failed)
    return {"accepted": True, "backfill": state}


@app.get("/api/reference-check")
def reference_check(project_id: str):
    """项目引用核查结论。每次查询在同一已提交快照（单条只读事务）内完成。

    覆盖全部历史批次及补建期间发布的批次（目录均为 DONE）后才给出缺项结论；
    目录未完成（含缺包/解析失败的批次）时 ``complete=false``，不返回缺项清单，
    页面据此只显示“未完成”，避免把“尚未补建”误报为“到齐/缺失”。
    """
    conn = db.connect()
    try:
        _get_project_or_404(conn, project_id)
        # 显式只读事务（DEFERRED：首个读语句才开始，WAL 下固定读快照，
        # 纯读不会升级为写锁，即便补建/发布线程正在写也不阻塞）：本次查询内
        # 所有读语句都基于同一已提交状态，读不到进行到一半的数据。
        conn.execute("BEGIN DEFERRED")
        try:
            coverage = db.project_catalog_coverage(conn, project_id)
            if not coverage["complete"]:
                builds = [
                    dict(r)
                    for r in conn.execute(
                        "SELECT cb.* FROM catalog_builds cb "
                        "JOIN batches b ON b.id = cb.batch_id "
                        "WHERE b.project_id=? ORDER BY cb.batch_id",
                        (project_id,),
                    ).fetchall()
                ]
                result = {
                    "project_id": project_id,
                    "complete": False,
                    "coverage": coverage,
                    "builds": builds,
                    "missing": [],
                }
            else:
                missing_rows = db.project_missing_references(conn, project_id)
                missing = []
                for row in missing_rows:
                    sources = [
                        dict(r)
                        for r in db.project_missing_reference_sources(
                            conn, project_id, row["target_referenced_uid"]
                        )
                    ]
                    missing.append(
                        {
                            "target_referenced_uid": row["target_referenced_uid"],
                            "source_referenced_uid": row["source_referenced_uid"],
                            "reference_count": row["reference_count"],
                            "source_batch_ids": (
                                row["source_batch_ids"].split(",")
                                if row["source_batch_ids"]
                                else []
                            ),
                            "source_file_names": (
                                row["source_file_names"].split(",")
                                if row["source_file_names"]
                                else []
                            ),
                            "sources": sources,
                        }
                    )
                result = {
                    "project_id": project_id,
                    "complete": True,
                    "coverage": coverage,
                    "missing": missing,
                    "missing_count": len(missing),
                }
        except Exception:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
        return result
    finally:
        conn.close()


# 静态前端（挂载在最后，避免覆盖 /api 路由）
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
