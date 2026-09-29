#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
几何质量闸门 · 真实数据验证

用 AtlasPI 1941 年的真实数据跑一遍，确认闸门能准确揪出合成圆饼。
"""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

from histmap_core import Registry, assess_series, filter_regions  # noqa: E402
from histmap_core.datasets import atlaspi                          # noqa: F401,E402

LOG = os.path.join(ROOT, "quality-gate-report.txt")
lines = []


def log(s):
    lines.append(str(s))
    print(s)


ds = Registry.get("atlaspi")

for year in (1941, 1939, 1250):
    log(f"\n{'='*62}")
    log(f"  公元 {year} 年")
    log(f"{'='*62}")
    series = ds.build(years=[year])
    rep = assess_series(series, attach=True)

    log(f"  实体总数: {rep['total']}")
    d = rep["distribution"]
    log(f"  真实边界: {d.get('real',0):3d}   "
        f"粗略: {d.get('coarse',0):3d}   "
        f"合成占位: {d.get('synthetic',0):3d}   "
        f"无效: {d.get('invalid',0):3d}")
    log(f"  可用率: {rep['usable_ratio']*100:.1f}%    "
        f"真实率: {rep['real_ratio']*100:.1f}%")
    log(f"  判定: {rep['verdict']}")

    sus = rep["suspects"]
    if sus:
        log(f"\n  ⚠️ 检出 {len(sus)} 个可疑几何（前 15）:")
        log(f"    {'实体':38s} {'层级':8s} {'顶点':>6s} {'环':>4s} {'CV':>7s}")
        for s in sorted(sus, key=lambda x: x["verts"])[:15]:
            log(f"    {s['name'][:36]:38s} {s['label']:8s} "
                f"{s['verts']:6d} {s['rings']:4d} {s['cv']:7.3f}")

    # 演示过滤：只保留真实+粗略
    s2 = ds.build(years=[year])
    r = filter_regions(s2, min_level=("real", "coarse"))
    log(f"\n  过滤后保留 {r['kept']} 个，剔除 {r['dropped']} 个")

with open(LOG, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("\n完成")
