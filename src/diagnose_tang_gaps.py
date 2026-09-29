#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断 T3 归属后地图上的「空格」
==================================
空格 = 有县多边形却没被画出来的地方。三种可能，必须分清楚：

  A. 该县到最近州治超过 max_km     → 判为唐疆域外（阈值问题）
  B. 该县最近的那个州，今年不在任何藩镇名下 → 州已失（如河西失陷）
  C. 该县最近的那个州治是「别名重复」的州（蒲/河中、辽/仪、隋/随）
     → 县全给了同名那个，另一个州名下没县，但它仍占着 807 的空位

本脚本把空格按 A/B/C 分类列出，便于对症修。
"""
import json
import os
import sys
from collections import Counter

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_tang_map import load_counties, haversine_km     # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROC = os.path.join(ROOT, "data", "processed")
MAX_KM = 260.0

try:
    import zhconv
    norm = lambda s: zhconv.convert(s, "zh-cn")
except ImportError:
    norm = lambda s: s


def main():
    counties = load_counties()
    gaz = json.load(open(os.path.join(PROC, "tang_zhou_gazetteer.json"),
                        encoding="utf-8"))["points"]
    coords = [(p["name"], p["lon"], p["lat"]) for p in gaz]

    tl = json.load(open(os.path.join(PROC, "tang_fanzhen_timeline.json"),
                        encoding="utf-8"))
    snap = tl["807"]
    zhou2fz = {}
    for fz, v in snap.items():
        for z in v["zhou"]:
            zhou2fz.setdefault(norm(z), fz)

    kinds = Counter()
    samples = {"A": [], "B": [], "C": []}
    # 每个州治分到多少个县
    per_zhou = Counter()

    for c in counties:
        best, bd = None, 1e18
        for name, lon, lat in coords:
            d = haversine_km(c["cx"], c["cy"], lon, lat)
            if d < bd:
                bd, best = d, name
        if bd > MAX_KM:
            kinds["A 超出阈值"] += 1
            if len(samples["A"]) < 8:
                samples["A"].append((c["name"], round(c["cx"], 1), round(c["cy"], 1), round(bd)))
            continue
        if best not in zhou2fz:
            kinds["B 该州不在今年名单"] += 1
            if len(samples["B"]) < 8:
                samples["B"].append((c["name"], best))
            continue
        per_zhou[best] += 1

    # C 类：今年有隶属关系、但一个县都没分到的州
    no_county = [z for z in zhou2fz if per_zhou.get(z, 0) == 0]
    print(f"县总数 {len(counties)}")
    for k, v in kinds.most_common():
        print(f"  {k}: {v}")
    print(f"  被画出来的县: {sum(per_zhou.values())}")
    print()
    if samples["A"]:
        print("A 类样例（离任何州治都太远）：")
        for n, x, y, d in samples["A"]:
            print(f"    {n:22s} ({x},{y})  最近 {d} km")
    if samples["B"]:
        print("B 类样例（最近的州今年不在名单）：")
        for n, z in samples["B"]:
            print(f"    {n:22s} -> 最近的州是「{z}」")
    print(f"\nC 类：{len(no_county)} 个州今年有隶属关系却分不到县：")
    print("   " + " ".join(sorted(no_county)))
    print("\n  这些就是图上最大的空格来源——它们占着 807 的位置，却没有几何。")
    print("  典型是别名重复：蒲州＝河中府、辽州＝仪州、鄚州＝莫州、隋州＝随州。")


if __name__ == "__main__":
    main()
