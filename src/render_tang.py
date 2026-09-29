#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
渲染唐藩镇格局图（宣纸古籍风格）
====================================
把 T3 产出的藩镇多边形喂进现有渲染引擎，验证整条链路。

风格取向：二十四史题材要的是**古籍地图**的味道 ——
米白宣纸底、淡彩晕染、深墨勾边、地名用简体（观众读简体，
但史源是繁体，两边都保留在数据里）。
"""
import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

from histmap_core import Region, Frame, Renderer, Layout, Style   # noqa: E402

PROC = os.path.join(ROOT, "data", "processed")
MAPS = os.path.join(ROOT, "output", "maps")

# 唐图的版面比 WWII 紧凑：中国本体接近方形，用 4:3 更合适
SIZES = {"4x3": (1600, 1200, "full"), "16x9": (1920, 1080, "band"),
         "1x1": (1400, 1400, "full")}


def rings_of(geom):
    """把 Polygon / MultiPolygon 拍平成环列表（契约要求）。"""
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


def build_frame(path):
    gj = json.load(open(path, encoding="utf-8"))
    regions = []
    for f in gj["features"]:
        p = f["properties"]
        rings = rings_of(f["geometry"])
        if not rings:
            continue
        regions.append(Region(
            id=p["id"], name=p["name"], rings=rings, color=p["color"],
            label_pos=(p["label_lon"], p["label_lat"]) if p.get("label_lon") else None,
            props={"zhou": p.get("zhou") or [], "label": p["name"]},
        ))
    regions.sort(key=lambda r: -len(r.rings))
    return Frame(year=gj["_meta"]["year"], regions=regions,
                 title=f"唐 · 元和二年（{gj['_meta']['year']}）",
                 subtitle="藩镇割据形势"), gj["_meta"]


TANG_STYLE = Style.from_dict({
    "id": "tang_xuanzhi",
    "theme": "light",
    "canvas": {"background": "#efe7d6"},          # 米白宣纸
    "borders": {"color": "#6b5f4a", "width": 1.0},  # 深墨勾边
    "labels": {"size": 19, "color": "#2b2620", "halo": "#f7f2e8",
               "halo_width": 4, "min_area_ratio": 0.0006},
    "title_style": {"size": 50, "color": "#241f1a",
                    "subtitle_size": 24, "subtitle_color": "#6b5f50"},
    "legend": {"enabled": False, "position": "bottom-left", "size": 15},
})

FOOTER = ("示意地图：藩镇辖境据《新唐书·方镇表》州县隶属关系重建，"
          "几何以现代县界为底、按治所邻近度归属，非史料记载的实际界线")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=807)
    ap.add_argument("--size", default="4x3", choices=list(SIZES))
    ap.add_argument("--out", default=None)
    ap.add_argument("--supersample", type=int, default=2)
    args = ap.parse_args()

    src = os.path.join(PROC, f"tang_{args.year}_fanzhen.geojson")
    if not os.path.exists(src):
        print(f"缺少 {src}，先跑 src/build_tang_map.py"); sys.exit(1)

    frame, meta = build_frame(src)
    print(f"{frame.title}  藩镇 {len(frame.regions)} 个")
    zs = sum(len(r.props["zhou"]) for r in frame.regions)
    print(f"  辖州合计 {zs}")

    W, H, mode = SIZES[args.size]
    lay = Layout(width=W, height=H, mode=mode, title_ratio=0.11,
                 footer_ratio=0.07, band_top_ratio=0.14, band_max_height_ratio=0.72)
    r = Renderer(TANG_STYLE, lay, projection="mercator", supersample=args.supersample)
    img = r.render_frame(frame, legend_items=None)
    os.makedirs(MAPS, exist_ok=True)
    out = args.out or os.path.join(MAPS, f"tang_{args.year}_{args.size}.png")
    img.save(out)
    print(f"  -> {out}   {img.size[0]}x{img.size[1]}")


if __name__ == "__main__":
    main()
