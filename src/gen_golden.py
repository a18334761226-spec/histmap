#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M1 门禁 · 生成投影黄金向量
================================
浏览器端渲染必须和 Python 端给出同一个像素结果，否则同一个 scene
在两端出来的图不一样，用户会当成 bug。

本脚本用 Python 实现算出「给定 bbox/视口/采样点 -> 像素坐标」的基准值，
写进 web/golden-projection.json；web/verify.mjs 用 JS 实现跑同一组输入
并逐条比对，不一致就退出码非零 —— 可以挂进 CI。

用法
    python src/gen_golden.py
    node   web/verify.mjs
"""
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

from histmap_core import Viewport, get_projection   # noqa: E402

OUT = os.path.join(ROOT, "web", "golden-projection.json")

# (投影, bbox, 视口, padding, 采样点)
CASES = [
    ("mercator", [-11.0, 33.0, 62.0, 62.0], [0, 0, 1920, 1080], 16,
     [[-11, 33], [62, 62], [0, 50], [20, 55], [30, 45], [-5, 60]]),
    ("mercator", [-180.0, -60.0, 180.0, 75.0], [0, 0, 1080, 1920], 8,
     [[0, 0], [-180, -60], [180, 75], [100, 40], [-70, -30]]),
    ("equirectangular", [-11.0, 33.0, 62.0, 62.0], [0, 0, 1920, 1080], 16,
     [[-11, 33], [62, 62], [0, 50], [20, 55]]),
    ("lambert", [73.0, 18.0, 135.0, 54.0], [0, 0, 1600, 1200], 12,
     [[73, 18], [135, 54], [104, 36], [120, 45]]),
]


def main():
    out = {"tolerance": 1e-6, "note": "Python 端基准；JS 端必须逐条吻合", "cases": []}
    for i, (name, bbox, vp, pad, pts) in enumerate(CASES):
        proj = get_projection(name)(bbox, Viewport(*vp), padding=pad)
        rows = []
        for lon, lat in pts:
            x, y = proj(lon, lat)
            rows.append({"lon": lon, "lat": lat, "x": x, "y": y})
        out["cases"].append({
            "id": f"{name}-{i}", "projection": name, "bbox": bbox,
            "viewport": vp, "padding": pad, "points": rows,
        })
        print(f"  {name:16s} bbox={bbox} vp={vp} pad={pad}  {len(rows)} 点")

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n黄金向量 -> {OUT}")
    print("下一步：node web/verify.mjs")


if __name__ == "__main__":
    main()
