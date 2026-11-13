"""生成演示用合成 DICOM 文件（未压缩，含像素与嵌套序列引用）。

输出目录结构（out_dir 由命令行参数指定，默认 ./data/samples）::

    batch_a/   研究项目 A 的第一批：2 个实例（同 Patient/Study，不同 Series/SOP），
               其中实例 2 通过 ReferencedSOPSequence 引用实例 1，
               并通过 ReferencedStudySequence 引用本研究。
    batch_b/   第二批：新实例引用 batch_a 的 Study/Series/SOP（跨批次引用场景）。
    compressed/ 1 份 Deflated 压缩 DICOM，用于验证“压缩文件整批拒绝”。
    bad/       1 份损坏文件，用于验证“坏文件整批拒绝且不留部分批次”。

注意：本脚本只写合成数据，不读取任何外部不可信输入。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    generate_uid,
)
from pydicom.sequence import Sequence

# 固定源 UID，保证演示可重复（真实系统里这些来自导出设备）
PATIENT_ID = "PAT-DEMO-0001"
STUDY_UID = "1.2.840.1001.99.1.1"
SERIES_A1_UID = "1.2.840.1001.99.1.1.1"
SERIES_A2_UID = "1.2.840.1001.99.1.1.2"
SOP_A1_UID = "1.2.840.1001.99.1.1.1.1"
SOP_A2_UID = "1.2.840.1001.99.1.1.2.1"
# 第二批中“目标尚未入库”的前向引用
SOP_FUTURE_UID = "1.2.840.1001.99.99.0.0.1"

SOP_CLASS_CT = "1.2.840.10008.5.1.4.1.1.2"  # CT Image Storage
SOP_CLASS_SR = "1.2.840.10008.5.1.4.1.1.88.11"  # Basic Text SR


def _file_meta(media_sop_uid: str, sop_class: str, tsyntax: str) -> FileMetaDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = media_sop_uid
    meta.TransferSyntaxUID = tsyntax
    meta.FileMetaInformationVersion = b"\x00\x01"
    meta.ImplementationClassUID = generate_uid()
    return meta


def _base_ds(patient_id: str, study_uid: str, series_uid: str, sop_uid: str,
             sop_class: str, tsyntax: str) -> Dataset:
    ds = Dataset()
    ds.file_meta = _file_meta(sop_uid, sop_class, tsyntax)
    ds.PatientID = patient_id
    ds.PatientName = "Demo^Patient"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.SOPInstanceUID = sop_uid
    ds.SOPClassUID = sop_class
    ds.Modality = "CT"
    ds.InstanceNumber = "1"
    # 未压缩像素：4x4 单帧，位深 16
    ds.Rows = 4
    ds.Columns = 4
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(range(32))  # 16 个 uint16
    return ds


def _ref_sop_item(sop_uid: str, sop_class: str) -> Dataset:
    item = Dataset()
    item.ReferencedSOPClassUID = sop_class  # 必须保持不变
    item.ReferencedSOPInstanceUID = sop_uid  # 必须被重映射
    return Dataset(item)


def build_batch_a(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)

    ds1 = _base_ds(PATIENT_ID, STUDY_UID, SERIES_A1_UID, SOP_A1_UID,
                   SOP_CLASS_CT, ExplicitVRLittleEndian)
    ds1.save_as(out / "a1_instance.dcm", enforce_file_format=True)

    ds2 = _base_ds(PATIENT_ID, STUDY_UID, SERIES_A2_UID, SOP_A2_UID,
                   SOP_CLASS_CT, ImplicitVRLittleEndian)

    # 嵌套序列 1：ReferencedSOPSequence -> 引用同批实例 a1
    ref_sop = Dataset()
    ref_sop.ReferencedSOPSequence = Sequence([_ref_sop_item(SOP_A1_UID, SOP_CLASS_CT)])

    # 嵌套序列 2：ReferencedStudySequence（含 StudyInstanceUID 引用）
    ref_study = Dataset()
    ref_study.ReferencedSOPClassUID = SOP_CLASS_SR
    ref_study.ReferencedSOPInstanceUID = SOP_FUTURE_UID  # 前向引用：目标尚未入库
    ref_study_item = Dataset()
    ref_study_item.StudyInstanceUID = STUDY_UID
    ref_study_item.ReferencedSOPSequence = Sequence([ref_study])
    ref_study_seq = Sequence([ref_study_item])
    ds2.ReferencedStudySequence = ref_study_seq

    # 嵌套序列 3：ReferencedPatientSequence（含 PatientID 引用）
    ref_patient_item = Dataset()
    ref_patient_item.PatientID = PATIENT_ID
    ds2.ReferencedPatientSequence = Sequence([ref_patient_item])

    # 额外的顶层直接引用
    ds2.ReferencedSOPInstanceUID = SOP_A1_UID
    ds2.ReferencedSOPClassUID = SOP_CLASS_CT

    ds2.save_as(out / "a2_referencing.dcm", enforce_file_format=True)


def build_batch_b(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # 同一患者/研究的新系列，引用第一批的 Series 与 SOP（跨批次引用）
    series_b = "1.2.840.1001.99.1.1.3"
    sop_b = "1.2.840.1001.99.1.1.3.1"
    ds = _base_ds(PATIENT_ID, STUDY_UID, series_b, sop_b,
                  SOP_CLASS_CT, ExplicitVRLittleEndian)

    ref_series_item = Dataset()
    ref_series_item.SeriesInstanceUID = SERIES_A1_UID  # 跨批次 Series 引用
    ref_series_item.ReferencedSOPSequence = Sequence(
        [_ref_sop_item(SOP_A1_UID, SOP_CLASS_CT)]  # 跨批次 SOP 引用
    )
    ds.ReferencedSeriesSequence = Sequence([ref_series_item])
    ds.save_as(out / "b1_cross_batch.dcm", enforce_file_format=True)


def build_compressed(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    ds = _base_ds(PATIENT_ID, STUDY_UID, "1.2.840.1001.99.2.1",
                  "1.2.840.1001.99.2.1.1", SOP_CLASS_CT, "1.2.840.10008.1.2.2")
    # Deflated Explicit VR 属压缩传输语法，服务端必须拒绝
    ds.save_as(out / "compressed_deflated.dcm", enforce_file_format=True)


def build_bad(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "broken.dcm").write_bytes(b"DICM" + b"\x00" * 128 + b"not-a-real-dicom")


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DICOM 演示样例")
    parser.add_argument("out_dir", nargs="?", default="./data/samples",
                        help="输出目录（默认 ./data/samples）")
    args = parser.parse_args()
    root = Path(args.out_dir).resolve()
    build_batch_a(root / "batch_a")
    build_batch_b(root / "batch_b")
    build_compressed(root / "compressed")
    build_bad(root / "bad")
    print(f"样例已生成到 {root}")
    for path in sorted(root.rglob("*")):
        if path.is_file():
            print(f"  {path.relative_to(root)}  ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
