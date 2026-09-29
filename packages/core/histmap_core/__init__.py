"""histmap-core · 统一数据契约 / 投影 / 样式 / 渲染 / 数据集 / 质量校验"""
from .contract import Region, Frame, Series
from .style import Style
from .render import Renderer, Layout
from .projection import (Projection, Viewport, Equirectangular, Mercator,
                         LambertConformal, get_projection)
from .datasets.base import DatasetAdapter, Manifest, Registry, register
from .quality import (GeometryQuality, assess_region, assess_series,
                      filter_regions, REAL, COARSE, SYNTHETIC, INVALID,
                      LEVEL_LABEL)
from .control import ControlLayer, ControlStats, ControlTimeline, build_legend

__all__ = [
    "Region", "Frame", "Series",
    "Style", "Renderer", "Layout",
    "Projection", "Viewport", "Equirectangular", "Mercator",
    "LambertConformal", "get_projection",
    "DatasetAdapter", "Manifest", "Registry", "register",
    "GeometryQuality", "assess_region", "assess_series", "filter_regions",
    "REAL", "COARSE", "SYNTHETIC", "INVALID", "LEVEL_LABEL",
    "ControlLayer", "ControlStats", "ControlTimeline", "build_legend",
]

__version__ = "0.1.0"
