"""
histmap-core · 数据集适配器基类与注册表

**这是开源项目的核心扩展点。**
任何人想加一个新题材，只需继承 DatasetAdapter 并注册。

约定
----
* adapter 的职责：把某个史料/数据源，转成统一契约 Series
* adapter 不负责样式、渲染、排版 —— 那是引擎的事
* adapter 必须声明 manifest（来源/许可证/覆盖范围/置信度），
  系统据此做「可行性评估」与「合规检查」
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

from ..contract import Series


@dataclass
class Manifest:
    """数据集元信息 —— 决定它能否被用于商业交付。"""

    id: str
    title: str
    source: str                       # 数据来源说明
    license: str                      # 许可证（SPDX 或描述）
    commercial_ok: bool = False       # 是否可用于商业交付
    redistribution_ok: bool = False   # 是否可随开源仓库分发
    coverage_years: tuple[int, int] | None = None
    region: str = ""
    granularity: str = ""             # 州 / 省 / 国 / 势力
    confidence: float = 1.0           # 数据置信度 0~1，影响报价与免责声明
    fields: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self):
        d = asdict(self)
        if self.coverage_years:
            d["coverage_years"] = list(self.coverage_years)
        return d


class DatasetAdapter:
    """所有题材适配器的基类。"""

    manifest: Manifest

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = cache_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "data", "cache")
        os.makedirs(self.cache_dir, exist_ok=True)

    # ── 必须实现 ─────────────────────────────────────────────
    def build(self, years: Iterable[int] | None = None, **kwargs) -> Series:
        """构建 Series。子类实现。"""
        raise NotImplementedError

    # ── 可选覆盖 ─────────────────────────────────────────────
    def availability(self, years=None) -> dict:
        """数据可用性自检，供「可行性评估器」使用。"""
        return {
            "dataset": self.manifest.id,
            "confidence": self.manifest.confidence,
            "commercial_ok": self.manifest.commercial_ok,
            "coverage_years": self.manifest.coverage_years,
            "issues": [],
        }

    # ── 工具 ─────────────────────────────────────────────────
    def cache_path(self, name: str) -> str:
        return os.path.join(self.cache_dir, name)

    def has_cache(self, name: str) -> bool:
        return os.path.exists(self.cache_path(name))


class Registry:
    """数据集注册表：id -> adapter 类。"""

    _adapters: dict[str, type[DatasetAdapter]] = {}

    @classmethod
    def register(cls, adapter_cls: type[DatasetAdapter]):
        m = getattr(adapter_cls, "manifest", None)
        if m is None:
            raise ValueError(f"{adapter_cls.__name__} 缺少 manifest")
        cls._adapters[m.id] = adapter_cls
        return adapter_cls

    @classmethod
    def get(cls, dataset_id: str) -> DatasetAdapter:
        if dataset_id not in cls._adapters:
            raise KeyError(f"未注册的数据集: {dataset_id}；"
                           f"可用: {', '.join(cls._adapters)}")
        return cls._adapters[dataset_id]()

    @classmethod
    def all(cls):
        """返回所有数据集的 manifest（不实例化重活）。"""
        out = []
        for aid, cls in sorted(cls._adapters.items()):
            out.append(cls.manifest.to_dict())
        return out

    @classmethod
    def ids(cls):
        return sorted(cls._adapters.keys())


# 便于 adapter 文件顶部使用
def register(adapter_cls):
    return Registry.register(adapter_cls)
