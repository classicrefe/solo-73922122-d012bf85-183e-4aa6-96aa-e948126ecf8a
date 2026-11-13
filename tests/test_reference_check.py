"""项目引用核查 / 历史目录后台补建的端到端测试。

覆盖需求要点：
  * 发布事务同时写实例/引用目录，失败不留可见引用；
  * 按重编码后 target UID 关联，同项目到库即解除缺项；
  * 仅映射存在、其他项目有实例均不算到齐；
  * 查询走 SQLite 不重读 ZIP；后台补建可续建、幂等、失败可重试；
  * 补建期间照常上传；全量目录完成前不给结论，完成后结论随目标到库自动解除。
"""
from __future__ import annotations

import io
import threading
import time
import uuid
import zipfile

import pydicom
import pytest

from tests.conftest import upload

SOP_CLASS_CT = "1.2.840.10008.5.1.4.1.1.2"


@pytest.fixture(autouse=True)
def _stop_backfill_between_tests():
    """补建线程是模块级单例，每个测试结束后强制停掉，防止它继续写入
    下一个测试的独立数据目录（跨测试污染）。"""
    yield
    from app.catalog import stop_backfill
    stop_backfill()


def _wait_backfill_idle(client, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = client.get("/api/catalog/backfill").json()
        if not st["running"]:
            return st
        time.sleep(0.1)
    raise AssertionError("补建线程未在预期时间内结束")


def _mapping(client, project_id):
    return {
        (m["id_kind"], m["source_value"]): m["target_value"]
        for m in client.get("/api/mappings", params={"project_id": project_id}).json()
    }


def test_published_batch_writes_instance_and_reference_catalog(
    client, project_id, sample_dicom, tmp_path
):
    f1 = sample_dicom(tmp_path / "f1.dcm")
    ds1 = pydicom.dcmread(str(f1))
    f2 = sample_dicom(
        tmp_path / "f2.dcm",
        series_uid=f"{ds1.StudyInstanceUID}.2",
        sop_uid=f"{ds1.StudyInstanceUID}.2.1",
        refs={"ref_sop": (str(ds1.SOPInstanceUID), SOP_CLASS_CT)},
    )
    resp = upload(client, project_id, [f1, f2])
    assert resp.status_code == 201, resp.text

    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    # 新批次目录随发布事务写入，发布完成即“目录完整”，无需补建
    assert check["complete"] is True
    assert check["coverage"]["catalog_done"] == 1
    # f2 引用的 f1 同批到库，不应报缺项
    assert check["missing_count"] == 0

    st = client.get("/api/catalog/backfill").json()
    assert st["coverage"]["catalog_done"] == 1
    assert st["builds"][0]["status"] == "DONE"


def test_forward_reference_listed_as_missing_with_source_path(
    client, project_id, sample_dicom, tmp_path
):
    future_sop = f"1.2.840.1001.99.77.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "fwd.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    assert upload(client, project_id, [f]).status_code == 201

    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert check["complete"] is True
    assert check["missing_count"] == 1
    missing = check["missing"][0]
    mapping = _mapping(client, project_id)
    # 关联键是重编码后的 target UID
    assert missing["target_referenced_uid"] == mapping[("sop_uid", future_sop)]
    assert missing["source_referenced_uid"] == future_sop
    # 来源批次/文件/标签路径齐全，且路径体现嵌套序列
    assert len(missing["sources"]) == 1
    src = missing["sources"][0]
    assert src["source_file_name"] == "fwd.dcm"
    assert src["tag_path"].endswith("/ReferencedSOPInstanceUID")
    assert "ReferencedSOPSequence" in src["tag_path"]


def test_mapping_alone_does_not_count_as_arrived(
    client, project_id, sample_dicom, tmp_path
):
    """前向引用已预分配映射（映射表存在），但实例未到库，必须仍判缺项。"""
    future_sop = f"1.2.840.1001.99.78.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "fwd.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    upload(client, project_id, [f])
    mapping = _mapping(client, project_id)
    assert ("sop_uid", future_sop) in mapping  # 映射确实已分配且发布

    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    targets = {m["target_referenced_uid"] for m in check["missing"]}
    assert mapping[("sop_uid", future_sop)] in targets


def test_instance_in_other_project_does_not_resolve(
    client, sample_dicom, tmp_path
):
    projects = client.get("/api/projects").json()
    p1, p2 = projects[0]["id"], projects[1]["id"]

    # 项目 2 先发布实例 X（使用独立 UID，避免与项目 1 自身实例撞号）
    x_sop = f"1.2.840.1001.99.90.{uuid.uuid4().int % 10**12}"
    fx = sample_dicom(tmp_path / "x.dcm", sop_uid=x_sop)
    assert upload(client, p2, [fx]).status_code == 201

    # 项目 1 的文件引用该 SOP；各项目独立重编码，项目 2 的实例不算到齐
    f = sample_dicom(
        tmp_path / "ref.dcm",
        refs={"ref_sop": (x_sop, SOP_CLASS_CT)},
    )
    assert upload(client, p1, [f]).status_code == 201

    check1 = client.get(
        "/api/reference-check", params={"project_id": p1}
    ).json()
    assert check1["complete"] is True
    assert check1["missing_count"] == 1  # 项目 2 的实例不算到齐

    check2 = client.get(
        "/api/reference-check", params={"project_id": p2}
    ).json()
    assert check2["missing_count"] == 0  # 项目 2 自身到库


def test_missing_auto_resolved_after_target_instance_arrives(
    client, project_id, sample_dicom, tmp_path
):
    future_sop = f"1.2.840.1001.99.79.{uuid.uuid4().int % 10**12}"
    referencer = sample_dicom(
        tmp_path / "ref.dcm",
        refs={"ref_sop": (future_sop, SOP_CLASS_CT)},
    )
    assert upload(client, project_id, [referencer]).status_code == 201
    before = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert before["missing_count"] == 1

    # 目标实例随后到库（另一批次，源 SOP 即此前的前向引用）
    target = sample_dicom(tmp_path / "target.dcm", sop_uid=future_sop)
    assert upload(client, project_id, [target]).status_code == 201

    after = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert after["complete"] is True
    assert after["missing_count"] == 0
    # 引用行仍在（目录不删改），只是同项目实例已到库；本项目两个批次均已建目录
    assert after["coverage"]["catalog_done"] == after["coverage"]["published_total"]
    assert after["coverage"]["published_total"] >= 2


def test_backfill_rebuilds_historical_catalog_without_reread_on_query(
    client, project_id, sample_dicom, tmp_path, monkeypatch
):
    # 发布一个带前向引用的批次（发布路径已写目录，先手工删除模拟“历史无目录”）
    future_sop = f"1.2.840.1001.99.80.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "h.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    batch = upload(client, project_id, [f]).json()

    from app import db
    conn = db.connect()
    with db.transaction(conn):
        conn.execute("DELETE FROM instance_references")
        conn.execute("DELETE FROM instances")
        conn.execute("DELETE FROM catalog_builds")
    conn.close()

    # 目录缺失：核查不得给出“到齐/缺失”结论，必须显示未完成
    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert check["complete"] is False
    assert check["missing"] == []

    # 查询本身绝不读 ZIP：给 ZIP 读取埋点，核查期间不应被调用
    import app.catalog as catalog_mod
    real_zip = zipfile.ZipFile
    reads = {"n": 0}

    class TrackingZip(real_zip):
        def __init__(self, *args, **kwargs):
            reads["n"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(catalog_mod.zipfile, "ZipFile", TrackingZip)
    client.get("/api/reference-check", params={"project_id": project_id})
    assert reads["n"] == 0
    monkeypatch.undo()

    # 启动后台补建并等待完成
    r = client.post("/api/catalog/backfill")
    assert r.status_code == 202
    st = _wait_backfill_idle(client)
    assert st["coverage"]["complete"] is True
    assert st["coverage"]["catalog_done"] == 1

    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert check["complete"] is True
    assert check["missing_count"] == 1
    assert check["missing"][0]["source_referenced_uid"] == future_sop
    # 补建产物包含来源批次信息
    assert check["missing"][0]["sources"][0]["source_batch_id"] == batch["id"]


def test_backfill_idempotent_no_duplicate_references(
    client, project_id, sample_dicom, tmp_path
):
    future_sop = f"1.2.840.1001.99.81.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "h.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    upload(client, project_id, [f])

    from app import db
    conn = db.connect()
    with db.transaction(conn):
        conn.execute("DELETE FROM instance_references")
        conn.execute("DELETE FROM instances")
        conn.execute("DELETE FROM catalog_builds")
    conn.close()

    client.post("/api/catalog/backfill")
    _wait_backfill_idle(client)
    # 再触发一次：已全部 DONE，没有可补批次，应立即空闲且不产生重复
    client.post("/api/catalog/backfill")
    _wait_backfill_idle(client)

    conn = db.connect()
    ref_count = conn.execute(
        "SELECT COUNT(*) AS n FROM instance_references"
    ).fetchone()["n"]
    distinct = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT source_file_id, tag_path "
        "FROM instance_references)"
    ).fetchone()["n"]
    inst_count = conn.execute("SELECT COUNT(*) AS n FROM instances").fetchone()["n"]
    conn.close()
    assert ref_count == distinct  # 无重复引用
    assert inst_count == 1


def test_backfill_resumes_after_interruption(
    client, project_id, sample_dicom, tmp_path
):
    """模拟中断：批次目录停在 PENDING，重启后续建且不重复记引用。"""
    future_sop = f"1.2.840.1001.99.82.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "h.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    upload(client, project_id, [f])

    from app import db
    conn = db.connect()
    with db.transaction(conn):
        conn.execute("DELETE FROM instance_references")
        conn.execute("DELETE FROM instances")
        conn.execute(
            "UPDATE catalog_builds SET status='PENDING', processed_files=0"
        )
    conn.close()

    # 启动恢复钩子应自动续建 PENDING
    from app.catalog import auto_resume_on_startup
    auto_resume_on_startup()
    st = _wait_backfill_idle(client)
    assert st["coverage"]["complete"] is True

    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert check["complete"] is True
    assert check["missing_count"] == 1


def test_backfill_failed_batch_retriable(
    client, project_id, sample_dicom, tmp_path
):
    future_sop = f"1.2.840.1001.99.83.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "h.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    batch = upload(client, project_id, [f]).json()

    from app import db
    from app.config import settings
    conn = db.connect()
    with db.transaction(conn):
        conn.execute("DELETE FROM instance_references")
        conn.execute("DELETE FROM instances")
        conn.execute("DELETE FROM catalog_builds")
    conn.close()
    # 移走 ZIP 模拟缺包：补建应标 FAILED 且核查保持未完成
    zip_file = settings.published_dir / f"{batch['id']}.zip"
    hidden = settings.published_dir / f"{batch['id']}.zip.hidden"
    zip_file.rename(hidden)
    try:
        client.post("/api/catalog/backfill")
        st = _wait_backfill_idle(client)
        assert st["coverage"]["catalog_failed"] == 1
        assert st["coverage"]["complete"] is False
        check = client.get(
            "/api/reference-check", params={"project_id": project_id}
        ).json()
        assert check["complete"] is False
        assert any(b["status"] == "FAILED" for b in check["builds"])

        # 普通补建不会反复重试 FAILED
        client.post("/api/catalog/backfill")
        st2 = _wait_backfill_idle(client)
        assert st2["coverage"]["catalog_failed"] == 1

        # 恢复 ZIP 后显式重试成功
        hidden.rename(zip_file)
        client.post("/api/catalog/backfill?retry_failed=true")
        st3 = _wait_backfill_idle(client)
        assert st3["coverage"]["catalog_failed"] == 0
        assert st3["coverage"]["complete"] is True
    finally:
        if hidden.exists():
            hidden.rename(zip_file)

    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert check["complete"] is True
    assert check["missing_count"] == 1


def test_backfill_runs_concurrently_with_uploads(
    client, project_id, sample_dicom, tmp_path
):
    """补建进行中上传流水线照常工作，且新批次目录随发布自动就绪。"""
    from app import catalog as catalog_mod

    # 先造一个无目录历史批次
    f0 = sample_dicom(tmp_path / "old.dcm")
    upload(client, project_id, [f0])
    from app import db
    conn = db.connect()
    with db.transaction(conn):
        conn.execute("DELETE FROM instance_references")
        conn.execute("DELETE FROM instances")
        conn.execute("DELETE FROM catalog_builds")
    conn.close()

    gate = threading.Event()
    # 只拦截补建路径的 ZIP 读取（发布流水线从内存字节解析，不经过 ZipFile），
    # 从而把补建钉在“读 ZIP/解析”的无锁窗口内，期间照常验证上传。
    real_zip = catalog_mod.zipfile.ZipFile

    class GatedZip(real_zip):
        def __init__(self, *args, **kwargs):
            gate.wait(timeout=5)
            super().__init__(*args, **kwargs)

    catalog_mod.zipfile.ZipFile = GatedZip
    try:
        client.post("/api/catalog/backfill")
        time.sleep(0.3)  # 确保补建线程已进入解析窗口
        # 补建在跑的同时上传新批次，必须成功发布
        f1 = sample_dicom(tmp_path / "new.dcm")
        resp = upload(client, project_id, [f1])
        assert resp.status_code == 201, resp.text
    finally:
        gate.set()
        catalog_mod.zipfile.ZipFile = real_zip

    st = _wait_backfill_idle(client)
    assert st["coverage"]["complete"] is True
    assert st["coverage"]["catalog_done"] == 2
    check = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    assert check["complete"] is True


def test_failed_upload_leaves_no_visible_references(
    client, project_id, sample_dicom, tmp_path
):
    good = sample_dicom(tmp_path / "good.dcm")
    bad = tmp_path / "bad.dcm"
    bad.write_bytes(b"DICM" + b"\x00" * 200)
    resp = upload(client, project_id, [good, bad])
    assert resp.status_code == 400

    from app import db
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) AS n FROM instances").fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM instance_references"
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM catalog_builds"
    ).fetchone()["n"] == 0
    conn.close()


def test_query_uses_single_committed_snapshot(
    client, project_id, sample_dicom, tmp_path
):
    """核查接口内部多次读使用同一快照：同一请求内 coverage 与缺项一致。"""
    future_sop = f"1.2.840.1001.99.84.{uuid.uuid4().int % 10**12}"
    f = sample_dicom(
        tmp_path / "f.dcm", refs={"ref_sop": (future_sop, SOP_CLASS_CT)}
    )
    upload(client, project_id, [f])
    # 连续多次查询结果稳定（同一已提交状态可重复读）
    first = client.get(
        "/api/reference-check", params={"project_id": project_id}
    ).json()
    for _ in range(3):
        again = client.get(
            "/api/reference-check", params={"project_id": project_id}
        ).json()
        assert again["missing_count"] == first["missing_count"]
        assert again["coverage"] == first["coverage"]
