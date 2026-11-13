"""运行期配置。所有路径与限额均可用环境变量覆盖，便于容器化与测试。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    # 环境变量相关字段使用 default_factory：dataclass 的普通默认值在类定义
    # （import）时只求值一次并被所有实例共享，reload_settings() 将因此永远读到
    # 首次启动的环境变量。工厂函数保证每次构造 Settings() 都重新读取环境。
    data_dir: Path = field(
        default_factory=lambda: Path(os.environ.get("DICOM_REMAP_DATA", "/data"))
    )
    max_files: int = field(
        default_factory=lambda: _int_env("DICOM_REMAP_MAX_FILES", 20)
    )
    # 16 MiB
    max_total_bytes: int = field(
        default_factory=lambda: _int_env(
            "DICOM_REMAP_MAX_TOTAL_BYTES", 16 * 1024 * 1024
        )
    )
    # 并发批次等待对端提交/放弃的最长轮询时间（秒）
    lock_wait_seconds: int = field(
        default_factory=lambda: _int_env("DICOM_REMAP_LOCK_WAIT", 60)
    )
    db_filename: str = "registry.db"

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def published_dir(self) -> Path:
        return self.data_dir / "published"


settings = Settings()


def reload_settings() -> "Settings":
    """根据当前环境变量刷新全局配置。

    关键：原地修改同一个 Settings 对象（frozen dataclass 借助 object.__setattr__），
    而不是重绑定模块变量——其他模块以 ``from .config import settings`` 持有的引用
    才能立即看到新值。data_dir 相关路径均为 property，会动态反映新目录。
    """
    fresh = Settings()
    # 注意：必须遍历 dataclasses.fields() 得到实际 Field 对象并取 .name，
    # 不能直接遍历 __dataclass_fields__（那是名称->Field 的字典，迭代出来的是
    # 字符串），否则 object.__setattr__ 拿到字符串属性名写入无效，data_dir
    # 会永远停在首次启动的值，导致多数据目录（测试/多租户）串库。
    for fld in fields(fresh):
        object.__setattr__(settings, fld.name, getattr(fresh, fld.name))
    return settings


def ensure_dirs() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    settings.published_dir.mkdir(parents=True, exist_ok=True)
