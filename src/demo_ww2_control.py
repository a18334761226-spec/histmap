#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二战 1941 欧洲 · 真实边界 + 控制层（可交付版）
================================================
三层数据合成：
    1. CShapes 2.0  —— 真实主权边界几何（不是圆饼）
    2. 控制状态表   —— 史料整理的占领/控制关系（修正「波兰是盟国」这类失实）
    3. 样式层       —— 统一视觉规范

并把几何质量闸门跑在交付前。
"""
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

from histmap_core import (Renderer, Layout, Style, Registry,          # noqa: E402
                          assess_series, ControlLayer, build_legend)
from histmap_core.datasets import atlaspi, cshapes                     # noqa: F401,E402

OUT = os.path.join(ROOT, "output", "maps")
CTRL = os.path.join(ROOT, "data", "control", "ww2.json")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(ROOT, "ww2-control-report.txt")
lines = []


def log(s):
    lines.append(str(s))
    print(s)


# 覆盖范围：含苏联西部
BBOX = (-11.0, 33.0, 62.0, 62.0)


def run(year: int):
    log("=" * 70)
    log(f"  二战 {year} 年欧洲 · 真实边界 + 控制层")
    log("=" * 70)

    ds = Registry.get("cshapes")
    t0 = time.time()
    series = ds.build(years=[year])
    fr = series.frames[0]
    log(f"\n[1] 拉取 CShapes: {time.time()-t0:.1f}s，实体 {len(fr.regions)} 个")

    # 几何质量闸门（交付前必过）
    rep = assess_series(series, attach=True)
    d = rep["distribution"]
    log(f"[2] 几何质量闸门: 真实 {d.get('real',0)} / 粗略 {d.get('coarse',0)} / "
        f"合成 {d.get('synthetic',0)}    真实率 {rep['real_ratio']*100:.1f}%")
    log(f"    {rep['verdict']}")

    # 控制层
    if not os.path.exists(CTRL):
        log(f"[3] 控制表缺失: {CTRL}")
        return
    layer = ControlLayer.load(CTRL, year)
    stats = layer.apply(fr)
    log(f"[3] 控制层已叠加: 匹配 {stats.matched} / 未匹配 {stats.unmatched}")
    log(f"    控制方分布: {stats.by_control}")
    if stats.unmatched_names:
        log(f"    未匹配实体（前 12）: {', '.join(stats.unmatched_names[:12])}")

    # 裁剪到 bbox（按包围盒重叠判定，不能只抽样前若干顶点——
    # 苏联主环跨经度 21°~180°，抽样会把整个苏联误裁掉）
    lon0, lat0, lon1, lat1 = BBOX
    keep = []
    for r in fr.regions:
        b = r.bbox()
        if not b:
            continue
        if b[2] < lon0 or b[0] > lon1 or b[3] < lat0 or b[1] > lat1:
            continue
        keep.append(r)
    fr.regions = keep
    log(f"[4] 裁剪到欧洲+苏联西部: {len(keep)} 个")

    # 图例
    legend = build_legend(stats, layer)
    # 去重保序
    seen, items = set(), []
    for label, color in legend:
        if label in seen:
            continue
        seen.add(label)
        items.append((label, color))
    log(f"[5] 图例项 {len(items)} 条")

    fr.title = f"{year} 年的欧洲"
    fr.subtitle = "实际控制格局"

    style = Style.from_dict({
        "id": "ww2_control",
        "canvas": {"background": "#0b1016"},
        "borders": {"color": "#1a2028", "width": 0.9},
        "labels": {"size": 19, "color": "#ffffff", "halo": "#000000",
                   "halo_width": 4, "min_area_ratio": 0.0009},
        "title_style": {"size": 56, "color": "#ffffff",
                        "subtitle_size": 28, "subtitle_color": "#b8c2cc"},
        "legend": {"enabled": True, "position": "bottom-left",
                   "size": 17, "max_items": 16},
    })

    log("\n[6] 渲染")
    for name, (w, h), mode in [("vertical_9x16", (1080, 1920), "band"),
                               ("horizontal_16x9", (1920, 1080), "full")]:
        lay = Layout(width=w, height=h, mode=mode,
                     band_top_ratio=0.20, band_max_height_ratio=0.50)
        r = Renderer(style, lay, projection="mercator", supersample=2)
        t0 = time.time()
        img = r.render_frame(fr, bbox=BBOX, legend_items=items,
                             legend_title="实际控制")
        p = os.path.join(OUT, f"ww2_{year}_control_{name}.png")
        img.save(p)
        log(f"    {name:16s} {w}x{h}  {time.time()-t0:.1f}s -> {os.path.basename(p)}")


run(1941)
log("")
run(1939)

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("\n完成")
