#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
场景注册表 · 把不同题材统一成一个可渲染接口
================================================
服务端只需要知道「有哪些场景、支持哪些日期、怎么渲染一帧」，
不关心底下的数据是 CShapes 的真实国界，还是从《新唐书》重建出来的藩镇。

设计约束：**复用引擎，不在服务层重复实现渲染**。
  · 二战场景直接调 histmap_core（数据常驻内存，切换日期约 1 秒）
  · 唐代场景读预构建好的 GeoJSON（县→州→藩镇的合并很慢，必须预构建）

风格层两条路：
  · 预设（atlas / vintage / ink / modern）—— 纯代码，零成本
  · 参考图提取（用户上传一张图，量出它的色调/纹理/颗粒）—— 也是纯代码
模型风格化是可选项，不参与主流程（实测它会把地图数据一起美化掉）。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
for p in (os.path.join(ROOT, "packages", "core"), os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

PROC = os.path.join(ROOT, "data", "processed")
CACHE = os.path.join(ROOT, "data", "cache")

THEMES = ["dark", "light"]


# ── 二战欧洲 ────────────────────────────────────────────────
class WW2Scene:
    id = "ww2-europe"
    title = "二战欧洲"
    subtitle = "1939–1945 · 实际控制格局"
    desc = "数据源：CShapes 2.0 真实国界 + 事件式控制时间线。可精确到任意日期。"
    themes = THEMES
    _cache: dict = {}

    def dates(self):
        import make_ww2_video as M
        tl = M.ControlTimeline.load(M.TL_PATH)
        ds = M.build_dates(tl)
        return [d.isoformat() for d in ds]

    def default_date(self):
        return "1941-06-22"

    def _geometry(self, year: int):
        if year in self._cache:
            return self._cache[year]
        import make_ww2_video as M
        from histmap_core import Registry
        from histmap_core.datasets import cshapes
        ad = Registry.get("cshapes")
        if not getattr(ad, "_cached_gj", None):
            ad._cached_gj = ad.load()
            ad.load = lambda use_cache=True: ad._cached_gj
        s = ad.build(years=[year])
        fr = s.frames[0]
        lon0, lat0, lon1, lat1 = M.BBOX
        fr.regions = [r for r in fr.regions
                      if r.bbox() and not (r.bbox()[2] < lon0 or r.bbox()[0] > lon1
                                           or r.bbox()[3] < lat0 or r.bbox()[1] > lat1)]
        self._cache[year] = fr
        return fr

    def render(self, date: str, theme: str = "dark", size: str = "16x9") -> Image.Image:
        import make_ww2_video as M
        from datetime import date as _d
        from histmap_core import Renderer, Layout
        d = _d.fromisoformat(date)
        tl = M.ControlTimeline.load(M.TL_PATH, theme=theme)
        fr = self._geometry(d.year)
        tl.layer_at(date).apply(fr)

        beats = tl.events_between((d.replace(day=1)).isoformat(), date) if d.day > 1 else []
        fr.title = f"{d.year} 年 {d.month} 月" + ("" if d.day == 1 else f" {d.day} 日")
        fr.subtitle = f"{beats[-1]['date'][5:]} · {beats[-1]['label']}" if beats else ""

        W, H = (1920, 1080) if size == "16x9" else (1080, 1920)
        mode = "full" if size == "16x9" else "band"
        style = M.build_style(theme)
        lay = Layout(width=W, height=H, mode=mode,
                     band_top_ratio=0.20, band_max_height_ratio=0.52)
        r = Renderer(style, lay, projection="mercator", supersample=2)
        img = r.render_frame(fr, bbox=M.BBOX, legend_items=M.build_legend(theme),
                             legend_title="实际控制")
        rows = M.recent_lines(tl, d, 3)
        tb = M.ticker_box(W, H, r, [rows], style=style)
        M.draw_ticker(img, rows, tb)
        M.draw_footer(img, M.FOOTER, r, style)
        return img

    def year_range(self):
        return (1939, 1945)


# ── 唐 · 藩镇 ───────────────────────────────────────────────
class TangScene:
    id = "tang"
    title = "唐 · 藩镇割据"
    subtitle = "安史之乱后到唐末 · 藩镇辖境"
    desc = ("数据源：《新唐书·方镇表》州县隶属 + 本项目重建的州治坐标 "
            "+ geoBoundaries 现代县界。**几何是重建的，非史料记载的实际界线。**")
    themes = ["light"]
    _cache: dict = {}

    def dates(self):
        import glob
        ys = sorted(int(os.path.basename(p).split("_")[1])
                    for p in glob.glob(os.path.join(PROC, "tang_*_fanzhen.geojson")))
        return [f"{y}-01-01" for y in ys]

    def default_date(self):
        ds = self.dates()
        for d in ds:
            if d.startswith("807"):
                return d
        return ds[len(ds) // 2] if ds else "807-01-01"

    def _load(self, year: int):
        if year in self._cache:
            return self._cache[year]
        p = os.path.join(PROC, f"tang_{year}_fanzhen.geojson")
        if not os.path.exists(p):
            raise FileNotFoundError(
                f"缺少 {year} 年的唐图数据。先跑："
                f"python src/build_tang_map.py --year {year}")
        gj = json.load(open(p, encoding="utf-8"))
        self._cache[year] = gj
        return gj

    def render(self, date: str, theme: str = "light", size: str = "4x3") -> Image.Image:
        import render_tang as RT
        from histmap_core import Renderer, Layout
        # 注意别用 date[:4] 取年份：807 年是**三位数**，'807-01-01'[:4] == '807-'
        # （已经踩过一次，报 invalid literal for int()）。
        year = int(str(date).split("-")[0])
        gj = self._load(year)
        # 直接复用 render_tang 的取环逻辑，避免两处解析
        from histmap_core import Region, Frame
        regions = []
        for f in gj["features"]:
            pr = f["properties"]
            rings = RT.rings_of(f["geometry"])
            if not rings:
                continue
            regions.append(Region(
                id=pr["id"], name=pr["name"], rings=rings, color=pr["color"],
                label_pos=(pr["label_lon"], pr["label_lat"]) if pr.get("label_lon") else None,
                props={"zhou": pr.get("zhou") or []}))
        regions.sort(key=lambda r: -len(r.rings))
        fr = Frame(year=year, regions=regions,
                   title=f"唐 · {year} 年", subtitle="藩镇割据形势")

        W, H = {"4x3": (1600, 1200), "16x9": (1920, 1080), "1x1": (1400, 1400)}.get(
            size, (1600, 1200))
        lay = Layout(width=W, height=H, mode="full", title_ratio=0.11,
                     footer_ratio=0.07)
        r = Renderer(RT.TANG_STYLE, lay, projection="mercator", supersample=2)
        return r.render_frame(fr)

    def year_range(self):
        return (763, 880)


SCENES = {s.id: s for s in (WW2Scene(), TangScene())}


def get(scene_id: str):
    return SCENES.get(scene_id)


def list_scenes():
    out = []
    for s in SCENES.values():
        try:
            ds = s.dates()
        except Exception as e:
            ds = []
        out.append({
            "id": s.id, "title": s.title, "subtitle": s.subtitle,
            "desc": s.desc, "themes": s.themes,
            "dates": ds, "default_date": s.default_date() if ds else None,
            "n_dates": len(ds),
        })
    return out
