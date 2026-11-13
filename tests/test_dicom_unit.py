"""DICOM 重写器的细粒度单元测试（不经过 HTTP）。"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolated_data_dir(monkeypatch):
    monkeypatch.setenv("DICOM_REMAP_DATA", tempfile.mkdtemp())
    from app.config import reload_settings
    reload_settings()


def _make_ds(sop_uid="1.2.840.1001.99.1.1.1"):
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    meta.MediaStorageSOPInstanceUID = sop_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.FileMetaInformationVersion = b"\x00\x01"
    meta.ImplementationClassUID = "1.2.840.1001.99.1.99"
    ds = Dataset()
    ds.file_meta = meta
    ds.PatientID = "PAT-X"
    ds.StudyInstanceUID = "1.2.840.1001.99.1"
    ds.SeriesInstanceUID = "1.2.840.1001.99.1.1"
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.Modality = "CT"
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(range(32))
    return ds


def test_protection_check_detects_sop_class_tamper():
    from app.dicom import parse_and_validate, remap_dataset, _assert_protected_unchanged

    buf = io.BytesIO()
    _make_ds().save_as(buf, enforce_file_format=True)
    parsed = parse_and_validate(buf.getvalue(), "x.dcm")

    mapping = {
        "patient_id": {"PAT-X": "RMPID-NEW"},
        "study_uid": {"1.2.840.1001.99.1": "2.25.11"},
        "series_uid": {"1.2.840.1001.99.1.1": "2.25.12"},
        "sop_uid": {"1.2.840.1001.99.1.1.1": "2.25.13"},
    }
    # 正常重写后保护校验必须通过
    remap_dataset(parsed.dataset, mapping)
    _assert_protected_unchanged(parsed, parsed.dataset)

    # 人为篡改 SOPClassUID，保护校验必须抛出
    parsed.dataset.SOPClassUID = "1.2.840.10008.5.1.4.1.1.99"
    with pytest.raises(RuntimeError, match="受保护标签"):
        _assert_protected_unchanged(parsed, parsed.dataset)


def test_remap_does_not_touch_unknown_scalar_tags():
    from app.dicom import parse_and_validate, remap_dataset

    ds = _make_ds()
    ds.Modality = "CT"  # 非映射标签
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    parsed = parse_and_validate(buf.getvalue(), "x.dcm")
    mapping = {
        "patient_id": {"PAT-X": "RMPID-NEW"},
        "study_uid": {"1.2.840.1001.99.1": "2.25.11"},
        "series_uid": {"1.2.840.1001.99.1.1": "2.25.12"},
        "sop_uid": {"1.2.840.1001.99.1.1.1": "2.25.13"},
    }
    remap_dataset(parsed.dataset, mapping)
    assert parsed.dataset.Modality == "CT"
    assert parsed.dataset.SOPInstanceUID == "2.25.13"
    assert parsed.dataset.file_meta.MediaStorageSOPInstanceUID == "2.25.13"


def test_generated_uids_are_valid_and_unique():
    from app.ids import new_patient_id, new_uid, validate_uid

    uids = {new_uid() for _ in range(1000)}
    assert len(uids) == 1000
    assert all(validate_uid(u) and len(u) <= 64 for u in uids)
    assert all(new_patient_id().startswith("RMPID-") for _ in range(5))
