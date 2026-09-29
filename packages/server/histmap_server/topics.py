#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
题材加载器 · 把「题材」变成数据
====================================
## 为什么重写

初版把每个题材写成一个 Python 类（`WW2Scene` / `TangScene`）。
后果是**题材 = 代码**：用户问「宋呢？清呢？一战呢？」，
每加一个都要写一套渲染逻辑。这不是系统，是几个 demo 拼在一起。

现在改成：**题材 = data/topics/*.json 里的一条记录**，渲染逻辑只有两种：

  · kind = "boundary"  用现成国界几何（CShapes）+ 控制时间线
                       —— 近现代战争、国际体系。换题材只换控制表。
  · kind = "dynasty"   史料构建：行政单元 → 今地名 → 合并现代县
                       —— 中国古代断代。换朝代只换单元表与控制表。

两种 kind 共用同一个渲染引擎，所以「统一风格」是天然成立的。

## 加一个新题材要做什么

  boundary 类：写一份控制时间线 JSON（事件式），加一条 topic 记录。**零 Python。**
  dynasty  类：写一份单元坐标表 + 一份控制表，加一条 topic 记录。**零 Python。**

坐标表可以用 `src/check_gazetteer.py` 做逐点几何校验，
控制表可以复用 `src/parse_tang_fanzhen.py` 的解析思路从史料生成。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date as _date, timedelta
from functools import lru_cache
from typing import Any

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
for p in (os.path.join(ROOT, "packages", "core"), os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

TOPICS_FILE = os.path.join(ROOT, "data", "topics", "topics.json")
CONTROL_DIR = os.path.join(ROOT, "data", "control")
PROC = os.path.join(ROOT, "data", "processed")
CACHE = os.path.join(ROOT, "data", "cache")


# ════════════════════════════════════════════════════════════
# 题材定义
# ════════════════════════════════════════════════════════════
@dataclass
class Topic:
    id: str
    title: str
    kind: str
    bbox: list
    themes: list
    raw: dict

    @property
    def subtitle(self) -> str:
        return self.raw.get("subtitle", "")

    @property
    def source_note(self) -> str:
        return self.raw.get("source_note", "")

    @property
    def default_size(self) -> str:
        return self.raw.get("default_size", "16x9")

    def control_path(self) -> str | None:
        c = self.raw.get("control")
        if not c:
            return None
        for d in (CONTROL_DIR, PROC):
            p = os.path.join(d, c)
            if os.path.exists(p):
                return p
        return None

    def dates(self) -> list[str]:
        """该题材**当前真的能渲染**的日期点。

        dynasty 类只返回已构建几何的年份 —— 控制表里有 150 年不代表
        150 年都出得了图（县→单元的合并是预构建的）。返回没构建的年份，
        界面会给出一个点了就报错的滑块。
        """
        if self.kind == "dynasty":
            ys = []
            for y in (self.raw.get("years") or []):
                if os.path.exists(os.path.join(PROC, f"{self.id}_{y}_map.geojson")):
                    ys.append(int(y))
            if ys:
                return [f"{y}-01-01" for y in sorted(ys)]
        ctrl = self.control_path()
        if ctrl:
            data = json.load(open(ctrl, encoding="utf-8"))
            if "events" in data:
                return _boundary_dates(data)
            return _dynasty_dates(data)
        ys = self.raw.get("years") or []
        return [f"{y}-01-01" for y in ys]

    def default_date(self) -> str:
        d = self.raw.get("default_date")
        ds = self.dates()
        if d and d in ds:
            return d
        return ds[-1] if ds else (f"{self.raw['years'][0]}-01-01"
                                  if self.raw.get("years") else "1000-01-01")


def _boundary_dates(data: dict) -> list[str]:
    """boundary 类：月度帧 ∪ 事件帧（与既有脚本一致）。"""
    y0, y1 = None, None
    for e in data.get("events", []):
        s = str(e.get("date") or "")[:10]
        if not s:
            continue
        y = int(s[:4])
        y0 = y if y0 is None else min(y0, y)
        y1 = y if y1 is None else max(y1, y)
    if y0 is None:
        return []
    ds = set()
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            ds.add(f"{y}-{m:02d}-01")
    for e in data.get("events", []):
        s = str(e.get("date") or "")[:10]
        if s:
            ds.add(s)
    for mk in data.get("markers", []) or []:
        s = str(mk.get("date") or "")[:10]
        if s:
            ds.add(s)
    return sorted(d for d in ds if f"{y0}-01-01" <= d <= f"{y1}-12-31")


def _dynasty_dates(data: dict) -> list[str]:
    """dynasty 类：控制表的顶层键就是年份。"""
    ys = []
    for k in data:
        if k.startswith("_"):
            continue
        try:
            ys.append(int(k))
        except ValueError:
            continue
    return [f"{y}-01-01" for y in sorted(ys)]


@lru_cache(maxsize=1)
def load_topics() -> dict[str, Topic]:
    if not os.path.exists(TOPICS_FILE):
        return {}
    data = json.load(open(TOPICS_FILE, encoding="utf-8"))
    out = {}
    for t in data.get("topics", []):
        out[t["id"]] = Topic(
            id=t["id"], title=t["title"], kind=t.get("kind", "boundary"),
            bbox=t.get("bbox") or [-180, -60, 180, 75],
            themes=t.get("themes") or ["dark", "light"], raw=t)
    return out


def get(topic_id: str) -> Topic | None:
    return load_topics().get(topic_id)


def list_topics() -> list[dict]:
    out = []
    for t in load_topics().values():
        try:
            ds = t.dates()
        except Exception:
            ds = []
        out.append({
            "id": t.id, "title": t.title, "subtitle": t.subtitle,
            "kind": t.kind, "themes": t.themes, "dates": ds,
            "n_dates": len(ds),
            "default_date": t.default_date() if ds else None,
            "default_size": t.default_size,
            "source_note": t.source_note,
            "license_note": t.raw.get("license_note", ""),
        })
    return out


# ════════════════════════════════════════════════════════════
# 两种渲染器
# ════════════════════════════════════════════════════════════
_SIZES = {"16x9": (1920, 1080, "full"), "9x16": (1080, 1920, "band"),
          "4x3": (1600, 1200, "full"), "1x1": (1400, 1400, "full")}


def _sizes(size: str):
    return _SIZES.get(size, _SIZES["16x9"])


# ── boundary 类 ─────────────────────────────────────────────
_geo_cache: dict = {}


def _boundary_geometry(topic: Topic, year: int):
    key = (topic.id, year)
    if key in _geo_cache:
        return _geo_cache[key]
    from histmap_core import Registry
    from histmap_core.datasets import cshapes
    ds = topic.raw.get("dataset", "cshapes")
    ad = Registry.get(ds)
    if not getattr(ad, "_cached_gj", None):
        ad._cached_gj = ad.load()
        ad.load = lambda use_cache=True: ad._cached_gj
    s = ad.build(years=[year])
    fr = s.frames[0]
    lon0, lat0, lon1, lat1 = topic.bbox
    fr.regions = [r for r in fr.regions
                  if r.bbox() and not (r.bbox()[2] < lon0 or r.bbox()[0] > lon1
                                       or r.bbox()[3] < lat0 or r.bbox()[1] > lat1)]
    _geo_cache[key] = fr
    return fr


def _render_boundary(topic: Topic, date: str, theme: str, size: str) -> Image.Image:
    from histmap_core import Renderer, Layout, ControlTimeline, Style
    from histmap_core.render import _hex_to_rgb
    ctrl = topic.control_path()
    if not ctrl:
        raise FileNotFoundError(f"题材 {topic.id} 缺少控制表")
    tl = ControlTimeline.load(ctrl, theme=theme)
    d = _date.fromisoformat(date)
    fr = _boundary_geometry(topic, d.year)
    tl.layer_at(date).apply(fr)

    beats = tl.events_between(d.replace(day=1).isoformat(), date) if d.day > 1 else []
    fr.title = f"{d.year} 年 {d.month} 月" + ("" if d.day == 1 else f" {d.day} 日")
    fr.subtitle = f"{beats[-1]['date'][5:]} · {beats[-1]['label']}" if beats else ""

    style = _topic_style(topic, theme)
    W, H, mode = _sizes(size)
    lay = Layout(width=W, height=H, mode=mode,
                 band_top_ratio=0.20, band_max_height_ratio=0.52)
    r = Renderer(style, lay, projection="mercator", supersample=2)
    legend = _topic_legend(topic, theme)
    img = r.render_frame(fr, bbox=topic.bbox, legend_items=legend,
                         legend_title="实际控制")
    _draw_overlays(img, r, tl, d, style, W, H, topic)
    return img


def _topic_style(topic: Topic, theme: str) -> "Style":
    from histmap_core import Style
    if theme == "light":
        return Style.from_dict({
            "id": f"{topic.id}_light", "theme": "light",
            "canvas": {"background": "#efe7d6"},
            "borders": {"color": "#6b5f4a", "width": 0.9},
            "labels": {"size": 19, "color": "#2b2620", "halo": "#f7f2e8",
                       "halo_width": 4, "min_area_ratio": 0.0011},
            "title_style": {"size": 50, "color": "#241f1a",
                            "subtitle_size": 24, "subtitle_color": "#6b5f50"},
            "legend": {"enabled": True, "position": "bottom-left",
                       "size": 16, "max_items": 16}})
    return Style.from_dict({
        "id": f"{topic.id}_dark",
        "canvas": {"background": "#0b1016"},
        "borders": {"color": "#161c24", "width": 0.8},
        "labels": {"size": 20, "color": "#ffffff", "halo": "#000000",
                   "halo_width": 4, "min_area_ratio": 0.0011},
        "title_style": {"size": 54, "color": "#ffffff",
                        "subtitle_size": 26, "subtitle_color": "#b8c2cc"},
        "legend": {"enabled": True, "position": "bottom-left",
                   "size": 17, "max_items": 16}})


def _topic_legend(topic: Topic, theme: str):
    """控制表自带 _legend 就用它，否则从调色板推导。"""
    ctrl = topic.control_path()
    if ctrl:
        data = json.load(open(ctrl, encoding="utf-8"))
        lg = data.get("_legend")
        if lg:
            from histmap_core.control import to_light
            if theme == "light":
                return [(n, to_light(c)) for n, c in lg]
            return [(n, c) for n, c in lg]
    return None


def _draw_overlays(img, renderer, tl, d, style, W, H, topic: Topic):
    """右下角大事记 + 底部口径声明。题材没提供就跳过。"""
    import make_ww2_video as M
    rows = []
    evs = getattr(tl, "events", []) or []
    mks = getattr(tl, "data", {}).get("markers") or []
    cand = [(str(e.get("date") or "")[:10], e.get("label") or "") for e in evs]
    cand += [(str(m.get("date") or "")[:10], m.get("label") or "") for m in mks]
    ds = d.isoformat()
    cand = [c for c in cand if c[0] and c[0] <= ds and c[1]]
    cand.sort(key=lambda x: x[0])
    seen, picked = set(), []
    for dt, lab in reversed(cand):
        if lab in seen or any(lab in s or s in lab for s in seen):
            continue
        seen.add(lab)
        picked.append((_date.fromisoformat(dt), lab))
        if len(picked) >= 3:
            break
    picked.reverse()
    if picked:
        tb = M.ticker_box(W, H, renderer, [picked], style=style)
        M.draw_ticker(img, picked, tb)
    note = tl.data.get("_footer") or topic.source_note
    if note:
        M.draw_footer(img, note, renderer, style)


# ── dynasty 类 ──────────────────────────────────────────────
_dyn_cache: dict = {}


def _dynasty_geometry(topic: Topic, year: int) -> dict:
    key = (topic.id, year)
    if key in _dyn_cache:
        return _dyn_cache[key]
    p = os.path.join(PROC, f"{topic.id}_{year}_map.geojson")
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"缺少 {topic.id} {year} 年的几何。先跑：\n"
            f"  python src/build_dynasty_map.py --topic {topic.id} --year {year}")
    gj = json.load(open(p, encoding="utf-8"))
    _dyn_cache[key] = gj
    return gj


def rings_of(geom: dict) -> list:
    t = geom.get("type")
    c = geom.get("coordinates") or []
    out = []
    if t == "Polygon":
        for r in c:
            out.append([(float(p[0]), float(p[1])) for p in r])
    elif t == "MultiPolygon":
        for poly in c:
            for r in poly:
                out.append([(float(p[0]), float(p[1])) for p in r])
    return out


def _render_dynasty(topic: Topic, date: str, theme: str, size: str) -> Image.Image:
    from histmap_core import Renderer, Layout, Region, Frame, Style
    year = int(str(date).split("-")[0])          # 别用 [:4]，807 是三位数
    gj = _dynasty_geometry(topic, year)
    regions = []
    for f in gj["features"]:
        pr = f["properties"]
        rings = rings_of(f["geometry"])
        if not rings:
            continue
        regions.append(Region(
            id=pr["id"], name=pr["name"], rings=rings, color=pr["color"],
            label_pos=(pr["label_lon"], pr["label_lat"]) if pr.get("label_lon") else None,
            props={"units": pr.get("units") or pr.get("zhou") or []}))
    regions.sort(key=lambda r: -len(r.rings))
    fr = Frame(year=year, regions=regions,
               title=f"{topic.title} · {year} 年", subtitle=topic.subtitle)

    W, H, mode = _sizes(size)
    style = Style.from_dict({
        "id": f"{topic.id}_xuan", "theme": "light",
        "canvas": {"background": "#efe7d6"},
        "borders": {"color": "#6b5f4a", "width": 1.0},
        "labels": {"size": 19, "color": "#2b2620", "halo": "#f7f2e8",
                   "halo_width": 4, "min_area_ratio": 0.0006},
        "title_style": {"size": 50, "color": "#241f1a",
                        "subtitle_size": 24, "subtitle_color": "#6b5f50"},
        "legend": {"enabled": False}})
    lay = Layout(width=W, height=H, mode=mode, title_ratio=0.11, footer_ratio=0.07)
    r = Renderer(style, lay, projection="mercator", supersample=2)
    img = r.render_frame(fr, bbox=topic.bbox)
    note = gj.get("_meta", {}).get("method") or topic.source_note
    if note:
        import make_ww2_video as M
        M.draw_footer(img, note, r, style)
    return img


# ════════════════════════════════════════════════════════════
def render(topic_id: str, date: str, theme: str = "dark", size: str = "16x9"):
    t = get(topic_id)
    if not t:
        raise KeyError(f"没有这个题材: {topic_id}")
    if theme not in t.themes:
        theme = t.themes[0]
    if t.kind == "dynasty":
        return _render_dynasty(t, date, theme, size)
    return _render_boundary(t, date, theme, size)
