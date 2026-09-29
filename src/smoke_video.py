#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冒烟测试：时间线求值 + 单帧渲染 + ticker，不启动 ffmpeg。"""
import os
import sys
from datetime import date

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from histmap_core import Registry, ControlTimeline  # noqa: E402
from histmap_core.datasets import cshapes           # noqa: E402
import make_ww2_video as M                          # noqa: E402

tl = ControlTimeline.load(M.TL_PATH)
ds = M.build_dates(tl)
print(f"关键帧 {len(ds)} 个: {ds[0]} → {ds[-1]}")
print(f"事件 {len(tl.events)} / 标记 {len(tl.data.get('markers') or [])} / 基线 {len(tl.baseline)}")

for probe in ("1939-01-01", "1939-09-02", "1940-06-22", "1941-06-22",
              "1943-09-08", "1944-08-25", "1945-05-08"):
    s = tl.state_at(probe)
    lay = tl.layer_at(probe)
    pl = {k: v.get("control_by") for k, v in s.items() if k in
          ("Poland", "France", "Germany (Prussia)", "Italy/Sardinia",
           "Estonia", "Libya", "Russia (Soviet Union)")}
    print(f"  {probe}  波兰={pl.get('Poland')} 法国={pl.get('France')} "
          f"德={pl.get('Germany (Prussia)')} 意={pl.get('Italy/Sardinia')} "
          f"爱沙尼亚={pl.get('Estonia')} 利比亚={pl.get('Libya')}")
print("\n口径检查（颜色解析）:")
for probe in ("1939-01-01", "1940-06-22", "1945-05-08"):
    lay = tl.layer_at(probe)
    for name in ("Poland", "France", "Sweden", "Russia (Soviet Union)", "Hungary"):
        _, rule = lay.lookup(name)
        if rule:
            print(f"  {probe} {name:24s} {str(rule.get('control_type')):15s} "
                  f"{str(rule.get('control_by')):8s} -> {lay.color_for(rule)}")

# 单帧渲染
ad = Registry.get("cshapes")
gj = ad.load()
ad.load = lambda use_cache=True: gj
s = ad.build(years=[1941])
fr = s.frames[0]
lon0, lat0, lon1, lat1 = M.BBOX
keep = [r for r in fr.regions
        if r.bbox() and not (r.bbox()[2] < lon0 or r.bbox()[0] > lon1
                             or r.bbox()[3] < lat0 or r.bbox()[1] > lat1)]
fr.regions = keep
print(f"\n裁剪后实体 {len(keep)} 个")

lay = tl.layer_at("1941-06-22")
st = lay.apply(fr)
print(f"控制层匹配 {st.matched} / 未匹配 {st.unmatched}")
print(f"未匹配: {st.unmatched_names}")

fr.title = "1941 年 6 月 22 日"
fr.subtitle = "06-22 · 巴巴罗萨行动·德国入侵苏联"
r = M.Renderer(M.STYLE, M.Layout(width=1920, height=1080, mode="full"),
               projection="mercator", supersample=2)
r.debug_labels = bool(os.environ.get("DEBUG_LABELS"))
img = r.render_frame(fr, bbox=M.BBOX, legend_items=M.LEGEND, legend_title="实际控制")
M.draw_ticker(img, M.recent_lines(tl, date(1941, 6, 22), 3), r)
p = os.path.join(M.MAP_DIR, "_smoke_1941_event.png")
img.save(p)
print(f"\n冒烟帧 -> {p}")
print("OK")
