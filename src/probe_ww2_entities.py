#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探查二战欧洲裁剪框内的实体名，用于补全控制时间线基线。"""
import os
import sys
import json

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

from histmap_core.datasets import cshapes  # noqa: E402

BBOX = (-11.0, 33.0, 62.0, 62.0)
lon0, lat0, lon1, lat1 = BBOX

ad = cshapes.CShapesAdapter()
gj = ad.load()
feats = gj.get("features", [])

TL = os.path.join(ROOT, "data", "control", "ww2_timeline.json")
tl = json.load(open(TL, encoding="utf-8"))
known = set(tl.get("_baseline_1939", {}).keys())
for e in tl.get("events", []):
    if e.get("entity"):
        known.add(e["entity"])

seen = {}
for y in range(1939, 1946):
    for f in feats:
        p = f.get("properties") or {}
        if not ad._active(p, y):
            continue
        name = (p.get("cntry_name") or "").strip()
        if not name:
            continue
        rings = ad._rings(f.get("geometry"))
        if not rings:
            continue
        xs = [pt[0] for r in rings for pt in r]
        ys = [pt[1] for r in rings for pt in r]
        if not xs:
            continue
        if max(xs) < lon0 or min(xs) > lon1 or max(ys) < lat0 or min(ys) > lat1:
            continue
        seen.setdefault(name, set()).add(y)

print(f"裁剪框内实体 {len(seen)} 个")
missing = sorted(n for n in seen if n not in known)
print(f"\n未在时间线中定义（{len(missing)} 个）:")
for n in missing:
    print(f"  - {n}")
print(f"\n已定义 {len(seen) - len(missing)} 个")

out = os.path.join(ROOT, "output", "ww2_crop_entities.json")
os.makedirs(os.path.dirname(out), exist_ok=True)
json.dump({n: sorted(v) for n, v in sorted(seen.items())},
          open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print(f"\n-> {out}")
