"""pytest 夹具：每个测试独立临时数据目录 + 真实 uvicorn 子服务。"""
from __future__ import annotations

import socket
import sys
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DICOM_REMAP_DATA", str(tmp_path))
    # 不 purge 模块：所有 app.* 模块持有的是同一个 config.settings 引用，
    # reload_settings() 原地重建该对象后，data_dir 相关的 property 立即指向新目录。
    import app.config as config_module
    import app.db as db_module

    config_module.reload_settings()
    db_module.init_db()
    from app.pipeline import recover_on_startup
    recover_on_startup()

    from app.main import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/api/health", timeout=1)
            if r.status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.1)
    else:  # pragma: no cover
        raise RuntimeError("测试用 uvicorn 服务启动超时")

    with httpx.Client(base_url=base_url, timeout=30) as c:
        yield c

    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture()
def sample_dicom(tmp_path):
    """生成最小合法未压缩 DICOM 的工厂。

    每个测试的源 UID 带独立数字后缀，避免与其他测试已发布的持久映射相互干扰，
    同时保证 UID 仅含数字与点（DICOM VR=UI 合法字符集）。
    """
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    unique = uuid.uuid4().int % 10**10

    def _make(
        path: Path,
        patient_id: str | None = None,
        study_uid: str | None = None,
        series_uid: str | None = None,
        sop_uid: str | None = None,
        sop_class: str = "1.2.840.10008.5.1.4.1.1.2",
        tsyntax: str = ExplicitVRLittleEndian,
        pixel: bytes = bytes(range(32)),
        refs: dict | None = None,
    ) -> Path:
        patient_id = patient_id or f"PAT-{unique}"
        study_uid = study_uid or f"1.2.840.1001.99.{unique}.1"
        series_uid = series_uid or f"1.2.840.1001.99.{unique}.1.1"
        sop_uid = sop_uid or f"1.2.840.1001.99.{unique}.1.1.1"
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = sop_class
        meta.MediaStorageSOPInstanceUID = sop_uid
        meta.TransferSyntaxUID = tsyntax
        meta.FileMetaInformationVersion = b"\x00\x01"
        meta.ImplementationClassUID = "1.2.840.1001.99.1.99"
        ds = Dataset()
        ds.file_meta = meta
        ds.PatientID = patient_id
        ds.StudyInstanceUID = study_uid
        ds.SeriesInstanceUID = series_uid
        ds.SOPInstanceUID = sop_uid
        ds.SOPClassUID = sop_class
        ds.Modality = "CT"
        ds.Rows = 4
        ds.Columns = 4
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.PixelData = pixel
        if refs:
            from pydicom.sequence import Sequence
            if "ref_sop" in refs:
                item = Dataset()
                item.ReferencedSOPClassUID = refs["ref_sop"][1]
                item.ReferencedSOPInstanceUID = refs["ref_sop"][0]
                wrapper = Dataset()
                wrapper.ReferencedSOPSequence = Sequence([item])
                ds.ReferencedStudySequence = Sequence([wrapper])
            if "ref_patient" in refs:
                item = Dataset()
                item.PatientID = refs["ref_patient"]
                ds.ReferencedPatientSequence = Sequence([item])
        ds.save_as(path, enforce_file_format=True)
        return path

    return _make


@pytest.fixture()
def project_id(client):
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    return resp.json()[0]["id"]


def upload(client, project_id, paths):
    files = [("files", (p.name, p.read_bytes(), "application/dicom")) for p in paths]
    return client.post("/api/batches", data={"project_id": project_id}, files=files)
