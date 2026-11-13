"""DICOM 解析、校验与标识重写（pydicom）。

关键约束：
  * 只接受未压缩传输语法（见 :data:`app.ids.ALLOWED_TRANSFER_SYNTAXES`）；
  * SOP Class UID 类标签与 PixelData 字节原样保留；
  * 嵌套序列（含 Referenced SOP/Study/Patient Sequence 等）中的引用同步替换；
  * 目标标识尚未入库时由保留阶段预分配，本模块只负责套用映射表。
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field

import pydicom
from pydicom.dataelem import DataElement
from pydicom.dataset import Dataset

from .ids import (
    ALLOWED_TRANSFER_SYNTAXES,
    ID_KINDS,
    ID_PATIENT,
    ID_SERIES,
    ID_SOP,
    ID_STUDY,
    PROTECTED_TAGS,
    TAG_TO_KIND,
    validate_uid,
)

#: 数据集顶层四种源标识的必备标签
REQUIRED_ELEMENTS = {
    ID_PATIENT: "PatientID",
    ID_STUDY: "StudyInstanceUID",
    ID_SERIES: "SeriesInstanceUID",
    ID_SOP: "SOPInstanceUID",
}


class DicomValidationError(ValueError):
    """文件内容不满足入库条件。"""


@dataclass
class ParsedFile:
    filename: str
    raw: bytes
    dataset: Dataset
    # kind -> set(source values)，含嵌套序列中出现的全部值
    identifiers: dict[str, set[str]] = field(default_factory=lambda: {k: set() for k in ID_KINDS})
    pixel_bytes: bytes | None = None
    # 改写前快照：受保护标签值（含嵌套，按“标签->值集合”）与传输语法
    protected_snapshot: dict = field(default_factory=dict)
    transfer_syntax: str | None = None


def _read_dataset(raw: bytes, filename: str) -> Dataset:
    try:
        ds = pydicom.dcmread(io.BytesIO(raw), force=False)
    except Exception as exc:  # pydicom 抛出的异常类型较多，统一包装
        raise DicomValidationError(f"{filename}: 无法解析为 DICOM 文件（{exc}）") from exc
    if not getattr(ds, "file_meta", None):
        raise DicomValidationError(f"{filename}: 缺少 DICOM 文件元信息（File Meta Information）")
    return ds


def _check_transfer_syntax(ds: Dataset, filename: str) -> None:
    tsyntax = getattr(ds.file_meta, "TransferSyntaxUID", None)
    if tsyntax is None:
        raise DicomValidationError(f"{filename}: 缺少 Transfer Syntax UID")
    ts = str(tsyntax)
    if ts not in ALLOWED_TRANSFER_SYNTAXES:
        raise DicomValidationError(
            f"{filename}: 传输语法 {ts} 为压缩/不支持格式，仅接受未压缩的 "
            "Explicit/Implicit VR Little Endian"
        )


def _check_required_uids(ds: Dataset, filename: str) -> None:
    for kind, keyword in REQUIRED_ELEMENTS.items():
        if keyword not in ds:
            raise DicomValidationError(f"{filename}: 缺少必备标识 {keyword}")
        value = str(getattr(ds, keyword))
        if not value:
            raise DicomValidationError(f"{filename}: {keyword} 为空")
        if kind != ID_PATIENT and not validate_uid(value):
            raise DicomValidationError(f"{filename}: {keyword} 不是合法 DICOM UID: {value!r}")


def parse_and_validate(raw: bytes, filename: str) -> ParsedFile:
    """解析单文件并执行全部入库前校验，同时收集（含嵌套的）全部源标识。"""
    ds = _read_dataset(raw, filename)
    _check_transfer_syntax(ds, filename)
    _check_required_uids(ds, filename)

    parsed = ParsedFile(filename=filename, raw=raw, dataset=ds)
    if "PixelData" in ds:
        # 原始字节快照，重写后用于逐字节比对，确保像素不被改动
        parsed.pixel_bytes = bytes(ds.PixelData)

    parsed.transfer_syntax = str(ds.file_meta.TransferSyntaxUID)
    _collect_protected(ds, parsed.protected_snapshot)
    _collect_identifiers(ds, parsed.identifiers)
    return parsed


def _collect_protected(ds: Dataset, out: dict) -> None:
    """递归收集所有受保护标签（SOP Class UID 类）的改写前取值，按标签聚合。"""
    for elem in ds:
        if elem.VR == "SQ":
            for item in elem.value or []:
                _collect_protected(item, out)
        elif elem.tag in PROTECTED_TAGS and elem.value not in (None, ""):
            out.setdefault(elem.tag, set()).add(str(elem.value))
    meta = getattr(ds, "file_meta", None)
    if meta is not None:
        for elem in meta:
            if elem.tag in PROTECTED_TAGS and elem.value not in (None, ""):
                out.setdefault(elem.tag, set()).add(str(elem.value))


def _collect_identifiers(ds: Dataset, out: dict[str, set[str]]) -> None:
    """递归遍历数据集（含任意深度嵌套序列），收集四类源标识。

    所有 VR=SQ 的元素都会被递归，而不限于固定序列名，确保任意嵌套结构里的
    实例引用都被收集，进而在保留阶段得到（含前向引用的）映射。
    """
    for elem in ds:
        if elem.VR == "SQ":
            for item in elem.value or []:
                _collect_identifiers(item, out)
            continue
        kind = TAG_TO_KIND.get(elem.tag)
        if kind is not None and elem.value not in (None, ""):
            out[kind].add(str(elem.value))
    # 文件元信息中的 Media Storage SOP Instance UID (0002,0003)
    meta = getattr(ds, "file_meta", None)
    if meta is not None:
        for elem in meta:
            kind = TAG_TO_KIND.get(elem.tag)
            if kind is not None and elem.VR != "SQ" and elem.value not in (None, ""):
                out[kind].add(str(elem.value))


def _remap_element(elem: DataElement, mapping: dict[str, dict[str, str]]) -> None:
    if elem.tag in PROTECTED_TAGS:
        # 双保险：SOP Class UID 类标签即使误配也绝不重写
        return
    if elem.VR == "SQ":
        # 递归进入任意序列的每个 item
        for item in elem.value or []:
            remap_dataset(item, mapping)
        return
    kind = TAG_TO_KIND.get(elem.tag)
    if kind is None:
        return
    source = str(elem.value) if elem.value not in (None, "") else ""
    target = mapping.get(kind, {}).get(source)
    if target is not None:
        elem.value = target


def remap_dataset(ds: Dataset, mapping: dict[str, dict[str, str]]) -> None:
    """就地重写数据集中所有可映射标签（递归进入嵌套序列）。"""
    for elem in ds:
        _remap_element(elem, mapping)
    meta = getattr(ds, "file_meta", None)
    if meta is not None:
        for elem in meta:
            _remap_element(elem, mapping)


def _assert_pixel_unchanged(before: bytes | None, after_ds: Dataset, filename: str) -> None:
    if before is None:
        return
    if "PixelData" not in after_ds:
        raise RuntimeError(f"{filename}: 重写后 PixelData 标签丢失")
    if bytes(after_ds.PixelData) != before:
        raise RuntimeError(f"{filename}: 重写后像素字节发生变化")


def _assert_protected_unchanged(parsed: ParsedFile, after: Dataset) -> None:
    """以解析时（改写前）快照为基线，核对全部受保护标签与传输语法未变。"""
    after_snapshot: dict = {}
    _collect_protected(after, after_snapshot)
    if after_snapshot != parsed.protected_snapshot:
        raise RuntimeError(
            f"{parsed.filename}: 受保护标签（SOP Class UID 类）被意外修改"
        )
    after_ts = str(after.file_meta.TransferSyntaxUID)
    if after_ts != parsed.transfer_syntax:
        raise RuntimeError(f"{parsed.filename}: Transfer Syntax UID 被意外修改")


def remap_to_bytes(parsed: ParsedFile, mapping: dict[str, dict[str, str]]) -> bytes:
    """套用映射并序列化为新的 DICOM 字节流，附像素/保护标签完整性校验。"""
    ds = parsed.dataset
    remap_dataset(ds, mapping)

    # 顶层四个标识必须全部命中映射（保留阶段已保证，这里防御性校验）
    for kind, keyword in REQUIRED_ELEMENTS.items():
        new_value = str(getattr(ds, keyword))
        if not new_value or new_value not in mapping.get(kind, {}).values():
            raise RuntimeError(
                f"{parsed.filename}: {keyword} 未命中持久映射，拒绝发布"
            )

    buffer = io.BytesIO()
    # enforce_file_format=True：写出标准 DICOM 格式（含 128 字节前导与 DICM），
    # 不添加/重编码像素，仅按原传输语法封装。
    ds.save_as(buffer, enforce_file_format=True)
    encoded = buffer.getvalue()

    check = pydicom.dcmread(io.BytesIO(encoded))
    _assert_pixel_unchanged(parsed.pixel_bytes, check, parsed.filename)
    _assert_protected_unchanged(parsed, check)
    return encoded
