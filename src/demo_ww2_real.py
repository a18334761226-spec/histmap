#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二战 1941 欧洲战场 · 真实边界版
================================
对比演示：
  * AtlasPI  —— 德/意/日/苏 是合成圆饼或极粗块
  * CShapes  —— 真实边界多边形（加拿大 3.2 万顶点）

同时跑几何质量闸门，验证 CShapes 能通过验收。
"""
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
                          assess_series, filter_regions)
from histmap_core.datasets import atlaspi, cshapes                     # noqa: F401,E402

OUT = os.path.join(ROOT, "output", "maps")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(ROOT, "ww2-real-report.txt")
lines = []


def log(s):
    lines.append(str(s))
    print(s)


# ── 阵营判定（CShapes 用英文国名）─────────────────────────────
AXIS = ["Germany", "Italy", "Japan", "Hungary", "Romania", "Bulgaria",
        "Finland", "Slovakia", "Croatia", "Austria", "Albania"]
ALLIES = ["United Kingdom", "Soviet Union", "Russia", "United States",
          "China", "France", "Poland", "Netherlands", "Belgium", "Norway",
          "Yugoslavia", "Greece", "Czechoslovakia", "Denmark", "Luxembourg",
          "Ethiopia", "Brazil", "Canada", "Australia", "New Zealand",
          "South Africa", "India", "British", "Egypt"]
NEUTRAL = ["Sweden", "Switzerland", "Spain", "Portugal", "Ireland",
           "Turkey", "Ottoman", "Afghanistan", "Iran", "Persia", "Argentina",
           "Chile", "Mexico", "Venezuela", "Colombia", "Peru"]

C_AXIS = "#a03434"
C_ALLIES = "#2f6fa8"
C_NEUTRAL = "#5a6570"
C_OTHER = "#3a4048"
C_SOVIET = "#a83f6f"   # 苏联单列，视觉上区别于西方盟国


def faction_of(name: str):
    n = name or ""
    if "Soviet" in n or n == "Russia":
        return "苏联", C_SOVIET
    for k in AXIS:
        if k in n:
            return "轴心国", C_AXIS
    for k in ALLIES:
        if k in n:
            return "同盟国", C_ALLIES
    for k in NEUTRAL:
        if k in n:
            return "中立国", C_NEUTRAL
    return "其他", C_OTHER


# ── 场景 ─────────────────────────────────────────────────────
BBOX = (-11.0, 34.0, 32.0, 61.0)      # 欧洲战场
YEAR = 1941

log("=" * 66)
log("  二战 1941 欧洲战场 · 真实边界版")
log("=" * 66)

# 1) 数据源对比
log("\n=== 数据源几何质量对比 ===")
for ds_id, ds_label in [("atlaspi", "AtlasPI"), ("cshapes", "CShapes 2.0")]:
    try:
        ds = Registry.get(ds_id)
        s = ds.build(years=[YEAR])
        rep = assess_series(s, attach=True)
        d = rep["distribution"]
        log(f"  {ds_label:12s} 实体 {rep['total']:4d}  真实 {d.get('real',0):3d}  "
            f"粗略 {d.get('coarse',0):3d}  合成 {d.get('synthetic',0):3d}  "
            f"真实率 {rep['real_ratio']*100:5.1f}%")
    except Exception as e:
        log(f"  {ds_label:12s} 失败: {e}")

# 2) 用 CShapes 建图
log("\n=== 构建 CShapes 1941 ===")
ds = Registry.get("cshapes")
t0 = time.time()
series = ds.build(years=[YEAR])
fr = series.frames[0]
log(f"  拉取 {time.time()-t0:.1f}s   实体 {len(fr.regions)} 个")

# 质量闸门
rep = assess_series(series, attach=True)
d = rep["distribution"]
log(f"  几何质量: 真实 {d.get('real',0)} / 粗略 {d.get('coarse',0)} / "
    f"合成 {d.get('synthetic',0)}")
log(f"  判定: {rep['verdict']}")

# 3) 裁剪到欧洲
lon0, lat0, lon1, lat1 = BBOX
keep = []
for r in fr.regions:
    if not r.rings:
        continue
    main = max(r.rings, key=len)
    cx = sum(p[0] for p in main) / len(main)
    cy = sum(p[1] for p in main) / len(main)
    # 用小国也保留：只要有点落在框内
    inside = any(lon0 <= p[0] <= lon1 and lat0 <= p[1] <= lat1
                 for p in main[:400])
    if inside or (lon0 <= cx <= lon1 and lat0 <= cy <= lat1):
        keep.append(r)
log(f"  裁剪到欧洲: {len(keep)} 个")

# 4) 阵营着色
from collections import Counter
fac = Counter()
final = []
for r in keep:
    label, color = faction_of(r.name)
    r.color = color
    r.props["faction"] = label
    fac[label] += 1
    # 剔除极小附属地（环数与面积都很小）
    area = r.props.get("area") or 0
    if label == "其他" and area < 5000:
        continue
    final.append(r)
fr.regions = final
fr.title = "1941 年的欧洲"
fr.subtitle = "轴心国扩张 · 同盟国与苏联对峙"
log(f"  最终 {len(final)} 个；阵营分布: {dict(fac)}")
log("  样例:")
for r in sorted(final, key=lambda x: -(x.props.get("area") or 0))[:12]:
    log(f"    [{r.props.get('faction','?'):4s}] {r.name[:36]:38s} "
        f"环{len(r.rings):3d}")

# 5) 样式
style = Style.from_dict({
    "id": "ww2_faction",
    "canvas": {"background": "#0d1219"},
    "borders": {"color": "#f0f4f8", "width": 0.9},
    "labels": {"size": 20, "color": "#ffffff", "halo": "#000000",
               "halo_width": 4, "min_area_ratio": 0.0006},
    "title_style": {"size": 58, "color": "#ffffff",
                    "subtitle_size": 30, "subtitle_color": "#b8c2cc"},
    "legend": {"enabled": False},
})

# 6) 渲染
log("\n=== 渲染 ===")
for name, (w, h), mode in [("vertical_9x16", (1080, 1920), "band"),
                           ("horizontal_16x9", (1920, 1080), "full")]:
    lay = Layout(width=w, height=h, mode=mode)
    r = Renderer(style, lay, projection="mercator", supersample=2)
    t0 = time.time()
    img = r.render_frame(fr, bbox=BBOX)
    p = os.path.join(OUT, f"ww2_1941_real_{name}.png")
    img.save(p)
    log(f"  {name:16s} {w}x{h}  {time.time()-t0:.1f}s -> {os.path.basename(p)}")

log("\n完成")
with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
