#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
单帧输出 · 给定日期渲染一张当前口径的底图
============================================
用途：封面/缩略图，以及给风格化层提供「精确底图」。

和视频用的是同一套代码路径（几何按年 → 控制时间线求值 → 标注避让 →
大事项面板 → 口径声明），所以出来的图与成片里的对应帧一致。

用法
    python src/still.py --date 1942-11-01
    python src/still.py --date 1941-06-22 --size 9x16
"""
import argparse
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

from histmap_core import Registry, ControlTimeline, Renderer, Layout   # noqa: E402
from histmap_core.datasets import cshapes                              # noqa: E402
import make_ww2_video as M                                             # noqa: E402

SIZES = {
    "16x9": (1920, 1080, "full"),
    "9x16": (1080, 1920, "band"),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="1942-11-01", help="YYYY-MM-DD")
    ap.add_argument("--size", default="16x9", choices=list(SIZES))
    ap.add_argument("--theme", default="dark", choices=["dark", "light"],
                    help="light = 宣纸古籍向（二十四史题材）")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    d = date.fromisoformat(args.date)
    W, H, mode = SIZES[args.size]

    tl = ControlTimeline.load(M.TL_PATH, theme=args.theme)
    ad = Registry.get("cshapes")
    gj = ad.load()
    ad.load = lambda use_cache=True: gj

    s = ad.build(years=[d.year])
    fr = s.frames[0]
    lon0, lat0, lon1, lat1 = M.BBOX
    fr.regions = [r for r in fr.regions
                  if r.bbox() and not (r.bbox()[2] < lon0 or r.bbox()[0] > lon1
                                       or r.bbox()[3] < lat0 or r.bbox()[1] > lat1)]

    st = tl.layer_at(d.isoformat()).apply(fr)
    fr.title = f"{d.year} 年 {d.month} 月" + ("" if d.day == 1 else f" {d.day} 日")
    beats = tl.events_between((d.replace(day=1)).isoformat(), d.isoformat()) if d.day > 1 else []
    fr.subtitle = f"{beats[-1]['date'][5:]} · {beats[-1]['label']}" if beats else ""

    style = M.build_style(args.theme)
    legend = M.build_legend(args.theme)
    lay = Layout(width=W, height=H, mode=mode,
                 band_top_ratio=0.20, band_max_height_ratio=0.52)
    r = Renderer(style, lay, projection="mercator", supersample=2)
    img = r.render_frame(fr, bbox=M.BBOX, legend_items=legend,
                         legend_title="实际控制")

    rows = M.recent_lines(tl, d, 3)
    tb = M.ticker_box(W, H, r, [rows], style=style)
    M.draw_ticker(img, rows, tb)
    M.draw_footer(img, M.FOOTER, r, style)

    out = args.out or os.path.join(
        M.MAP_DIR, f"still_{args.date}_{args.size}_{args.theme}.png")
    img.save(out)
    print(f"{args.date}  {W}x{H} mode={mode} theme={args.theme}")
    print(f"  控制层匹配 {st.matched} / 未匹配 {st.unmatched}   实体 {len(fr.regions)}")
    print(f"  -> {out}")


if __name__ == "__main__":
    main()
