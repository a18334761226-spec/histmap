"""内置数据集适配器。

导入本包即完成注册 —— 引擎通过 Registry 按 id 取用。
新增题材：在本目录加一个模块，实现 DatasetAdapter 并 @register，
然后在 __init__ 中 import 一次即可。
"""
from .base import Registry, DatasetAdapter, Manifest, register  # noqa: F401

# ── 内置数据集（导入即注册）─────────────────────────────────
from . import atlaspi  # noqa: F401,E402  # 全球历史政体（宏观覆盖/事件/谱系）
from . import cshapes  # noqa: F401,E402  # 近现代真实国界（二战/冷战主力）
