"""核心功能与不变量测试。

注意：系统的持久映射会跨批次保留，因此各测试使用 sample_dicom 自动生成的
唯一源标识（基于 tmp_path），断言时动态读取实际 UID，避免测试间数据耦合。
"""
from __future__ import annotations

import io
import threading
import uuid
import zipfile

import pydicom
from pydicom.dataset import Dataset

from tests.conftest import upload

SOP_CLASS_CT = "1.2.840.10008.5.1.4.1.1.2"


def test_health_and_projects(client):
    assert client.get("/api/health").json() == {"status": "ok"}
    projects = client.get("/api/projects").json()
    assert len(projects) >= 2


def test_end_to_end_batch_publish_and_download(client, project_id, sample_dicom, tmp_path):
    f1 = sample_dicom(tmp_path / "f1.dcm")
    # 先读出 f1 的实际标识，供 f2 构造嵌套引用
    ds1 = pydicom.dcmread(str(f1))
    f2 = sample_dicom(
        tmp_path / "f2.dcm",
        patient_id=ds1.PatientID,
        study_uid=ds1.StudyInstanceUID,
        series_uid=f"{ds1.StudyInstanceUID}.2",
        sop_uid=f"{ds1.StudyInstanceUID}.2.1",
        refs={"ref_sop": (str(ds1.SOPInstanceUID), SOP_CLASS_CT),
              "ref_patient": ds1.PatientID},
    )
    resp = upload(client, project_id, [f1, f2])
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert batch["status"] == "PUBLISHED"
    assert batch["file_count"] == 2

    dl = client.get(f"/api/batches/{batch['id']}/download")
    assert dl.status_code == 200
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        assert sorted(zf.namelist()) == ["f1.dcm", "f2.dcm"]
        ds2 = pydicom.dcmread(io.BytesIO(zf.read("f2.dcm")))

    mappings = {(m["id_kind"], m["source_value"]): m["target_value"]
                for m in client.get("/api/mappings", params={"project_id": project_id}).json()}
    # 顶层标识全部替换
    assert ds2.PatientID == mappings[("patient_id", ds1.PatientID)]
    assert str(ds2.StudyInstanceUID) == mappings[("study_uid", str(ds1.StudyInstanceUID))]
    assert str(ds2.SOPInstanceUID) == mappings[("sop_uid", f"{ds1.StudyInstanceUID}.2.1")]
    # 嵌套引用同步替换
    assert str(ds2.ReferencedStudySequence[0].ReferencedSOPSequence[0]
               .ReferencedSOPInstanceUID) == mappings[("sop_uid", str(ds1.SOPInstanceUID))]
    assert ds2.ReferencedPatientSequence[0].PatientID == mappings[("patient_id", ds1.PatientID)]
    # 受保护内容不变
    assert str(ds2.SOPClassUID) == SOP_CLASS_CT
    assert str(ds2.ReferencedStudySequence[0].ReferencedSOPSequence[0]
               .ReferencedSOPClassUID) == SOP_CLASS_CT
    assert str(ds2.file_meta.MediaStorageSOPClassUID) == SOP_CLASS_CT
    assert bytes(ds2.PixelData) == bytes(range(32))
    # file_meta 的 SOP Instance UID 同步
    assert str(ds2.file_meta.MediaStorageSOPInstanceUID) == \
        mappings[("sop_uid", f"{ds1.StudyInstanceUID}.2.1")]


def test_cross_batch_reuses_mapping(client, project_id, sample_dicom, tmp_path):
    f1 = sample_dicom(tmp_path / "f1.dcm")
    ds1 = pydicom.dcmread(str(f1))
    assert upload(client, project_id, [f1]).status_code == 201
    first = {(m["id_kind"], m["source_value"]): m["target_value"]
             for m in client.get("/api/mappings", params={"project_id": project_id}).json()}

    f2 = sample_dicom(
        tmp_path / "f2.dcm",
        patient_id=ds1.PatientID,
        study_uid=ds1.StudyInstanceUID,
        refs={"ref_sop": (str(ds1.SOPInstanceUID), SOP_CLASS_CT)},
    )
    assert upload(client, project_id, [f2]).status_code == 201
    second = {(m["id_kind"], m["source_value"]): m["target_value"]
              for m in client.get("/api/mappings", params={"project_id": project_id}).json()}
    # 第一批的全部标识在第二批得到相同目标
    for key, target in first.items():
        assert second[key] == target


def test_forward_reference_gets_preallocated(client, project_id, sample_dicom, tmp_path):
    future_sop = f"1.2.840.1001.99.77.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(tmp_path / "f.dcm",
                     refs={"ref_sop": (future_sop, SOP_CLASS_CT)})
    assert upload(client, project_id, [f]).status_code == 201
    mappings = client.get("/api/mappings", params={"project_id": project_id}).json()
    assert any(m["source_value"] == future_sop and m["status"] == "PUBLISHED" for m in mappings)


def test_project_isolation(client, sample_dicom, tmp_path):
    projects = client.get("/api/projects").json()
    p1, p2 = projects[0]["id"], projects[1]["id"]
    f = sample_dicom(tmp_path / "f.dcm")
    ds = pydicom.dcmread(str(f))
    assert upload(client, p1, [f]).status_code == 201
    assert upload(client, p2, [f]).status_code == 201
    m1 = {m["source_value"]: m["target_value"] for m in
          client.get("/api/mappings", params={"project_id": p1}).json()
          if m["id_kind"] == "sop_uid"}
    m2 = {m["source_value"]: m["target_value"] for m in
          client.get("/api/mappings", params={"project_id": p2}).json()
          if m["id_kind"] == "sop_uid"}
    assert m1[str(ds.SOPInstanceUID)] != m2[str(ds.SOPInstanceUID)]


def test_concurrent_same_source_single_mapping(client, project_id, sample_dicom, tmp_path):
    files = [sample_dicom(tmp_path / "f.dcm")]
    ds = pydicom.dcmread(str(files[0]))
    results = []
    errors = []

    def worker():
        try:
            results.append(upload(client, project_id, files))
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(r.status_code for r in results) == [201] * 4
    mappings = client.get("/api/mappings", params={"project_id": project_id}).json()
    sop = [m for m in mappings if m["id_kind"] == "sop_uid"
           and m["source_value"] == str(ds.SOPInstanceUID)]
    # 同一源 SOP 在任意并发度下只允许存在一条映射
    assert len(sop) == 1
    assert sop[0]["status"] == "PUBLISHED"


def test_bad_file_whole_batch_rejected(client, project_id, sample_dicom, tmp_path):
    good = sample_dicom(tmp_path / "good.dcm")
    ds = pydicom.dcmread(str(good))
    bad = tmp_path / "bad.dcm"
    bad.write_bytes(b"DICM" + b"\x00" * 200)
    resp = upload(client, project_id, [good, bad])
    assert resp.status_code == 400
    batches = client.get("/api/batches", params={"project_id": project_id}).json()
    assert all(b["status"] != "PENDING" for b in batches)
    # 好文件的标识也不得落库（整批原子）
    mappings = client.get("/api/mappings", params={"project_id": project_id}).json()
    assert all(m["source_value"] != str(ds.SOPInstanceUID) for m in mappings)
    # 暂存目录已清理
    from app.config import settings
    assert not any(settings.staging_dir.iterdir())


def test_compressed_transfer_syntax_rejected(client, project_id, sample_dicom, tmp_path):
    f = sample_dicom(tmp_path / "deflated.dcm", tsyntax="1.2.840.10008.1.2.2")
    resp = upload(client, project_id, [f])
    assert resp.status_code == 400
    assert "压缩" in resp.json()["detail"]


def test_limits_file_count_and_size(client, project_id, sample_dicom, tmp_path):
    from app import config
    object.__setattr__(config.settings, "max_files", 20)
    object.__setattr__(config.settings, "max_total_bytes", 16 * 1024 * 1024)
    f = sample_dicom(tmp_path / "f.dcm")
    too_many = [f] * 21
    resp = upload(client, project_id, too_many)
    assert resp.status_code in (400, 413)

    # 限额设为小于样例实际大小，路由层总量检查必须拒绝
    object.__setattr__(config.settings, "max_total_bytes", 256)
    resp = upload(client, project_id, [f])
    assert resp.status_code in (400, 413)
    object.__setattr__(config.settings, "max_total_bytes", 16 * 1024 * 1024)


def test_duplicate_sop_within_batch_rejected(client, project_id, sample_dicom, tmp_path):
    f1 = sample_dicom(tmp_path / "f1.dcm")
    f2 = sample_dicom(tmp_path / "f2.dcm")  # 默认参数 -> 同一组 UID
    ds = pydicom.dcmread(str(f1))
    resp = upload(client, project_id, [f1, f2])
    assert resp.status_code == 400
    mappings = client.get("/api/mappings", params={"project_id": project_id}).json()
    assert all(m["source_value"] != str(ds.SOPInstanceUID) for m in mappings)


def test_persistence_across_reopen(client, project_id, sample_dicom, tmp_path):
    f = sample_dicom(tmp_path / "f.dcm")
    batch = upload(client, project_id, [f]).json()
    before = client.get("/api/mappings", params={"project_id": project_id}).json()

    # 模拟重启：启动恢复，数据与已发布 zip 必须保留
    from app.pipeline import recover_on_startup
    stale = recover_on_startup()
    assert stale == []
    dl = client.get(f"/api/batches/{batch['id']}/download")
    assert dl.status_code == 200
    after = client.get("/api/mappings", params={"project_id": project_id}).json()
    assert before == after


def test_recovery_of_interrupted_pending(client, project_id, sample_dicom, tmp_path):
    # 手工制造一个中断遗留：PENDING 批次 + PENDING 映射 + 暂存孤儿
    from app import db
    from app.config import settings
    ghost_sop = f"1.2.840.1001.99.66.{uuid.uuid4().int % 10**12}"
    conn = db.connect()
    bid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO batches(id, project_id, status, file_count, total_bytes) "
        "VALUES (?,?, 'PENDING', 0, 0)", (bid, project_id))
    conn.execute(
        "INSERT INTO id_mappings(project_id, id_kind, source_value, target_value, status, created_batch_id)"
        " VALUES (?, 'sop_uid', ?, '2.25.1', 'PENDING', ?)",
        (project_id, ghost_sop, bid))
    conn.close()
    orphan = settings.staging_dir / bid
    orphan.mkdir(parents=True)
    (orphan / "x.tmp").write_bytes(b"tmp")

    from app.pipeline import recover_on_startup
    recovered = recover_on_startup()
    assert bid in recovered
    assert not orphan.exists()
    conn = db.connect()
    row = conn.execute("SELECT status, error FROM batches WHERE id=?", (bid,)).fetchone()
    assert row["status"] == "FAILED"
    ghost = conn.execute(
        "SELECT * FROM id_mappings WHERE source_value=?", (ghost_sop,)).fetchall()
    assert ghost == []
    conn.close()

    # 回收后同标识可以被新批次正常保留
    f = sample_dicom(tmp_path / "f.dcm", sop_uid=ghost_sop)
    assert upload(client, project_id, [f]).status_code == 201


def test_arbitrary_nested_sequence_reference_remapped(client, project_id, sample_dicom, tmp_path):
    """白名单之外的嵌套序列（OriginalAttributesSequence）内的引用也必须替换。"""
    from pydicom.sequence import Sequence
    f = sample_dicom(tmp_path / "nested.dcm")
    ds = pydicom.dcmread(str(f))
    inner_sop = f"1.2.840.1001.99.55.{uuid.uuid4().int % 10**12}"
    inner = Dataset()
    inner.ReferencedSOPClassUID = SOP_CLASS_CT          # 受保护，不变
    inner.ReferencedSOPInstanceUID = inner_sop          # 必须替换
    inner.StudyInstanceUID = str(ds.StudyInstanceUID)   # 同研究引用，必须替换
    outer = Dataset()
    outer.OriginalAttributesSequence = Sequence([inner])
    ds.OriginalAttributesSequence = Sequence([outer])
    ds.save_as(str(f), enforce_file_format=True)

    assert upload(client, project_id, [f]).status_code == 201
    batch = client.get("/api/batches", params={"project_id": project_id}).json()[0]
    dl = client.get(f"/api/batches/{batch['id']}/download")
    with zipfile.ZipFile(io.BytesIO(dl.content)) as zf:
        out = pydicom.dcmread(io.BytesIO(zf.read("nested.dcm")))
    mappings = {(m["id_kind"], m["source_value"]): m["target_value"]
                for m in client.get("/api/mappings", params={"project_id": project_id}).json()}
    got = out.OriginalAttributesSequence[0].OriginalAttributesSequence[0]
    assert str(got.ReferencedSOPInstanceUID) == mappings[("sop_uid", inner_sop)]
    assert str(got.StudyInstanceUID) == mappings[("study_uid", str(ds.StudyInstanceUID))]
    assert str(got.ReferencedSOPClassUID) == SOP_CLASS_CT


def test_published_mapping_never_changes(client, project_id, sample_dicom, tmp_path):
    f1 = sample_dicom(tmp_path / "f1.dcm")
    upload(client, project_id, [f1])
    mappings_before = client.get("/api/mappings", params={"project_id": project_id}).json()

    # 第二批故意带坏文件导致整体失败（使用全新标识，确保失败前后映射表全等）
    good = sample_dicom(tmp_path / "f2.dcm",
                        sop_uid=f"1.2.840.1001.99.88.{uuid.uuid4().int % 10**12}")
    bad = tmp_path / "bad.dcm"
    bad.write_bytes(b"nope")
    assert upload(client, project_id, [good, bad]).status_code == 400

    mappings_after = client.get("/api/mappings", params={"project_id": project_id}).json()
    assert mappings_before == mappings_after
