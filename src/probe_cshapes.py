# -*- coding: utf-8 -*-
"""检查 CShapes 2.0 的字段结构与年份覆盖。"""
import json
import os
from collections import Counter

PATH = r"D:\history-map\data\cache\CShapes-2.0.geojson"
OUT = r"D:\history-map\cshapes-probe.txt"

L = []
with open(PATH, encoding="utf-8") as f:
    gj = json.load(f)

feats = gj.get("features", [])
L.append(f"features: {len(feats)}")
if feats:
    L.append(f"\n字段: {list(feats[0]['properties'].keys())}")
    L.append("\n第一个 feature 的 properties:")
    L.append(json.dumps(feats[0]["properties"], ensure_ascii=False, indent=1))
    L.append(f"\ngeometry.type: {feats[0]['geometry']['type']}")

# 年份覆盖：找 start/end 字段
keys = list(feats[0]["properties"].keys())
start_key = next((k for k in keys if k.lower() in ("start", "year_start", "from")), None)
end_key = next((k for k in keys if k.lower() in ("end", "year_end", "to")), None)
name_key = next((k for k in keys if "name" in k.lower()), None)

L.append(f"\n识别出的字段: start={start_key} end={end_key} name={name_key}")

# 测试若干年份
def active(p, y):
    try:
        s = int(str(p.get(start_key, "0"))[:4])
        e = int(str(p.get(end_key, "9999"))[:4]) if p.get(end_key) else 9999
        return s <= y <= e
    except Exception:
        return False

L.append("\n=== 各年份的实体数 ===")
for y in [800, 807, 1000, 1250, 1500, 1800, 1900, 1939, 1941, 1945, 2000]:
    n = sum(1 for f in feats if active(f["properties"], y))
    L.append(f"  {y}: {n}")

# 1941 欧洲样例
L.append("\n=== 1941 年实体（前 40）===")
n = 0
for f in feats:
    p = f["properties"]
    if not active(p, 1941):
        continue
    geom = f["geometry"]
    c = geom["coordinates"]
    if geom["type"] == "Polygon":
        rings = 1
        verts = len(c[0])
    else:
        rings = sum(len(poly) for poly in c)
        verts = sum(len(r) for poly in c for r in poly)
    L.append(f"  {str(p.get(name_key))[:36]:38s} 环{rings:4d} 顶点{verts:6d} "
             f"status={p.get('status')}")
    n += 1
    if n >= 40:
        break

open(OUT, "w", encoding="utf-8").write("\n".join(L))
print("OK")
