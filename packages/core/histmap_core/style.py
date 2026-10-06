"""
histmap-core · 样式层

样式完全外置：同一份数据换一个 style 文件就能变成完全不同的观感。
这是「客户改需求不改代码」的基础。

支持 YAML（推荐，便于手写）与 JSON 两种格式。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

try:
    import yaml  # 可选依赖
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False


# 默认调色板：高对比、暗底友好，适合短视频
DEFAULT_PALETTE = [
    "#c0392b", "#2980b9", "#27ae60", "#8e44ad", "#d35400",
    "#16a085", "#c2185b", "#f39c12", "#2c3e50", "#7f8c8d",
    "#e74c3c", "#3498db", "#1abc9c", "#9b59b6", "#e67e22",
    "#34495e", "#95a5a6", "#ff6b6b", "#4ecdc4", "#ffe66d",
]


@dataclass
class Style:
    id: str = "default"
    title: str = "默认样式"

    # 画布
    background: str = "#0a0e14"
    # 调色板（按 region 出现顺序或 id 稳定分配）
    palette: list[str] = field(default_factory=lambda: list(DEFAULT_PALETTE))
    color_overrides: dict[str, str] = field(default_factory=dict)

    # 边界与描边
    border_color: str = "#000000"
    border_width: float = 1.0
    region_alpha: float = 1.0

    # 地名标注
    label_font: str = r"C:\Windows\Fonts\msyh.ttc"
    label_size: int = 22
    label_color: str = "#f5f5f5"
    label_halo: str = "#000000"
    label_halo_width: int = 3
    label_min_area_ratio: float = 0.0   # 小于此面积占比的区域不标名

    # 标题
    title_size: int = 46
    title_color: str = "#ffffff"
    subtitle_size: int = 26
    subtitle_color: str = "#bdc3c7"

    # ── 图例 ─────────────────────────────────────────────────
    # legend_max_items：**作者可选的硬上限，0 = 按可用空间自动**。
    # 默认必须是 0：早先默认 14，图的分类多于 14 个时图例就被静默截断，
    # 于是「颜色 = 所属政权」这张图上有一半颜色没有解释（唐 807 有 37 个藩镇、
    # 只有 12 条图例）。截断是**说谎**，不能当默认行为。
    # 注意：即使作者设了这个上限，超出时也会画出「等 N 个」那一格。
    legend_enabled: bool = True
    legend_position: str = "bottom-left"
    legend_size: int = 18
    legend_max_items: int = 0

    # ── 主题（明 / 暗） ─────────────────────────────────────
    # 早期版本的图例/大事记/页脚配色是硬编码的深色值，导致「换样式」只换了地图、
    # 面板还是黑底白字。这里把面板配色也外置，才能做出浅色主题
    # —— 二十四史/古籍题材要的正是米白宣纸底 + 深墨字。
    theme: str = "dark"                 # dark | light
    ink: str = ""                       # 文字与线条主色（空则由 theme 推导）
    panel_bg: str = ""                  # 图例/大事记面板底色
    panel_border: str = ""
    muted: str = ""                     # 页脚/次要文字

    # 额外任意配置（供扩展）
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve_theme(self):
        """把 theme 推导成具体颜色；显式填过的字段不覆盖。"""
        presets = {
            "dark": {"ink": "#ffffff", "panel_bg": "#0b1016",
                     "panel_border": "#4a5568", "muted": "#8b97a3",
                     "label_halo": "#000000"},
            "light": {"ink": "#2b2620", "panel_bg": "#f5efe3",
                      "panel_border": "#b9ad97", "muted": "#7a6f5f",
                      "label_halo": "#f7f2e8"},
        }
        p = presets.get(self.theme, presets["dark"])
        if not self.ink:
            self.ink = p["ink"]
        if not self.panel_bg:
            self.panel_bg = p["panel_bg"]
        if not self.panel_border:
            self.panel_border = p["panel_border"]
        if not self.muted:
            self.muted = p["muted"]
        if self.theme == "light":
            # 浅底上标注描边若还是黑色，字会糊成一团
            if self.label_halo == "#000000":
                self.label_halo = p["label_halo"]
            if self.label_color == "#f5f5f5":
                self.label_color = self.ink
            if self.title_color == "#ffffff":
                self.title_color = self.ink
            if self.subtitle_color == "#bdc3c7":
                self.subtitle_color = self.muted
        return self

    # ── 颜色分配 ─────────────────────────────────────────────
    def color_for(self, region, index: int = 0) -> str:
        if region.color:
            return region.color
        for key in (region.id, region.name):
            if key in self.color_overrides:
                return self.color_overrides[key]
        if not self.palette:
            return "#888888"
        return self.palette[index % len(self.palette)]

    # ── 载入 ─────────────────────────────────────────────────
    @staticmethod
    def load(path: str) -> "Style":
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if path.lower().endswith((".yaml", ".yml")):
            if not _HAS_YAML:
                raise RuntimeError("需要 PyYAML 才能读取 YAML 样式；或改用 JSON")
            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        return Style.from_dict(data or {})

    @staticmethod
    def from_dict(d: dict) -> "Style":
        s = Style()
        s.id = d.get("id", s.id)
        s.title = d.get("title", s.title)

        canvas = d.get("canvas", {}) or {}
        s.background = canvas.get("background", s.background)

        pal = d.get("palette") or {}
        if isinstance(pal, list):
            s.palette = pal
        elif isinstance(pal, dict):
            if pal.get("colors"):
                s.palette = pal["colors"]
            s.color_overrides = pal.get("overrides", {}) or {}

        borders = d.get("borders", {}) or {}
        s.border_color = borders.get("color", s.border_color)
        s.border_width = float(borders.get("width", s.border_width))
        s.region_alpha = float(borders.get("region_alpha", s.region_alpha))

        lb = d.get("labels", {}) or {}
        s.label_font = lb.get("font", s.label_font)
        s.label_size = int(lb.get("size", s.label_size))
        s.label_color = lb.get("color", s.label_color)
        s.label_halo = lb.get("halo", s.label_halo)
        s.label_halo_width = int(lb.get("halo_width", s.label_halo_width))
        s.label_min_area_ratio = float(lb.get("min_area_ratio", s.label_min_area_ratio))

        ti = d.get("title_style", d.get("title", {})) or {}
        if isinstance(ti, dict):
            s.title_size = int(ti.get("size", s.title_size))
            s.title_color = ti.get("color", s.title_color)
            s.subtitle_size = int(ti.get("subtitle_size", s.subtitle_size))
            s.subtitle_color = ti.get("subtitle_color", s.subtitle_color)

        lg = d.get("legend", {}) or {}
        if isinstance(lg, dict):
            s.legend_enabled = bool(lg.get("enabled", s.legend_enabled))
            s.legend_position = lg.get("position", s.legend_position)
            s.legend_size = int(lg.get("size", s.legend_size))
            s.legend_max_items = int(lg.get("max_items", s.legend_max_items))

        th = d.get("theme") or {}
        if isinstance(th, str):
            s.theme = th
        elif isinstance(th, dict):
            s.theme = th.get("mode", s.theme)
            s.ink = th.get("ink", s.ink)
            s.panel_bg = th.get("panel_bg", s.panel_bg)
            s.panel_border = th.get("panel_border", s.panel_border)
            s.muted = th.get("muted", s.muted)

        s.extra = d.get("extra", {}) or {}
        return s.resolve_theme()

    def to_dict(self):
        return {
            "id": self.id, "title": self.title,
            "canvas": {"background": self.background},
            "palette": {"colors": self.palette, "overrides": self.color_overrides},
            "borders": {"color": self.border_color, "width": self.border_width,
                        "region_alpha": self.region_alpha},
            "labels": {"font": self.label_font, "size": self.label_size,
                       "color": self.label_color, "halo": self.label_halo,
                       "halo_width": self.label_halo_width,
                       "min_area_ratio": self.label_min_area_ratio},
            "title_style": {"size": self.title_size, "color": self.title_color,
                            "subtitle_size": self.subtitle_size,
                            "subtitle_color": self.subtitle_color},
            "legend": {"enabled": self.legend_enabled, "position": self.legend_position,
                       "size": self.legend_size, "max_items": self.legend_max_items},
            "theme": {"mode": self.theme, "ink": self.ink, "panel_bg": self.panel_bg,
                      "panel_border": self.panel_border, "muted": self.muted},
            "extra": self.extra,
        }


def list_styles(styles_dir: str):
    """列出样式库中的所有样式文件。"""
    out = []
    if not os.path.isdir(styles_dir):
        return out
    for fn in sorted(os.listdir(styles_dir)):
        if fn.lower().endswith((".yaml", ".yml", ".json")):
            out.append(os.path.join(styles_dir, fn))
    return out
