"""标识生成与 DICOM 标签重写规则。

重写范围（四种源标识）：
  * PatientID          (0010,0020)
  * StudyInstanceUID   (0020,000D)
  * SeriesInstanceUID  (0020,000E)
  * SOPInstanceUID     (0008,0018)

SOP Class UID（含 Referenced SOP Class UID、Media Storage SOP Class UID）
与像素字节一律不参与映射。
"""
from __future__ import annotations

import uuid

from pydicom.tag import Tag

#: 参与持久映射的标识类型
ID_PATIENT = "patient_id"
ID_STUDY = "study_uid"
ID_SERIES = "series_uid"
ID_SOP = "sop_uid"

ID_KINDS = (ID_PATIENT, ID_STUDY, ID_SERIES, ID_SOP)

#: 标识类型 -> 需要同步替换的“标量” DICOM 标签。
#: 序列（VR=SQ）不在这里：重写器会递归进入“任意”序列的每个 item，
#: 因此 ReferencedStudy/Series/SOP/Patient Sequence、OriginalAttributesSequence、
#: SourceImageSequence 等任意名称、任意嵌套深度的引用都会被覆盖。
REMAPPABLE_TAGS: dict[str, tuple[Tag, ...]] = {
    ID_PATIENT: tuple(Tag(name) for name in ("PatientID",)),
    ID_STUDY: tuple(Tag(name) for name in ("StudyInstanceUID",)),
    ID_SERIES: tuple(Tag(name) for name in ("SeriesInstanceUID",)),
    ID_SOP: tuple(
        Tag(name)
        for name in (
            "SOPInstanceUID",
            "MediaStorageSOPInstanceUID",  # 文件元信息 (0002,0003)
            "ReferencedSOPInstanceUID",
        )
    ),
}

#: 标签 -> 标识类型的反向索引，遍历时 O(1) 判定（只含标量标签）
TAG_TO_KIND: dict[Tag, str] = {
    tag: kind for kind, tags in REMAPPABLE_TAGS.items() for tag in tags
}

#: 明确不允许改写的标签（保护白名单，供测试与代码审查核对）
PROTECTED_TAGS = frozenset(
    Tag(name)
    for name in (
        "SOPClassUID",
        "MediaStorageSOPClassUID",
        "ReferencedSOPClassUID",
    )
)

#: 仅接受未压缩传输语法：Implicit / Explicit VR Little Endian。
#: Deflated (1.2.840.10008.1.2.2) 与所有 JPEG/RLE 等封装语法一律拒绝。
ALLOWED_TRANSFER_SYNTAXES = frozenset(
    (
        "1.2.840.10008.1.2",    # Implicit VR Little Endian
        "1.2.840.10008.1.2.1",  # Explicit VR Little Endian
    )
)

#: DICOM UID 根：2.25 为 ISO 注册的“基于 UUID 的 UID”分支，全局唯一且无需机构前缀
UID_ROOT = "2.25"
MAX_UID_LENGTH = 64


def new_uid() -> str:
    """生成 2.25.<uuid-as-decimal> 形式的 DICOM UID（长度恒为 44，合法）。"""
    uid = f"{UID_ROOT}.{uuid.uuid4().int}"
    if len(uid) > MAX_UID_LENGTH:  # pragma: no cover
        raise RuntimeError("generated UID exceeds DICOM 64-byte limit")
    return uid


def new_patient_id() -> str:
    """生成替换用 PatientID（LO 短字符串，非 UID，故不用点分形式）。"""
    return f"RMPID-{uuid.uuid4().hex}"


def validate_uid(value: str) -> bool:
    """校验 DICOM UID 的字符集（数字与点）与长度。"""
    if not value or len(value) > MAX_UID_LENGTH:
        return False
    return all(ch.isdigit() or ch == "." for ch in value)
