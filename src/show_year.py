# -*- coding: utf-8 -*-
"""查看指定年份的方镇表原始记录。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")
OUT = os.path.join(ROOT, "check.txt")

YEAR = int(sys.argv[1]) if len(sys.argv) > 1 else 807

with open(SRC, encoding="utf-8") as f:
    vols = json.load(f)

lines = []
lines.append(f"=== 《新唐书》方镇表  公元 {YEAR} 年 ===\n")

# 统计所有藩镇列
allcols = []
for v in vols:
    for c in v["headers"][2:]:
        if c and c not in allcols:
            allcols.append(c)
lines.append(f"共 {len(allcols)} 个藩镇列: {'、'.join(allcols)}\n")

hit = 0
for v in vols:
    rows = [r for r in v["rows"] if r["year"] == YEAR]
    if not rows:
        continue
    r = rows[0]
    lines.append(f"\n--- {v['volume']}   年号: {r['era']} ---")
    for name, txt in r["cells"].items():
        if txt:
            lines.append(f"  【{name}】{txt}")
            hit += 1
        else:
            lines.append(f"  【{name}】(无变化)")

lines.append(f"\n该年有变动记录: {hit} 条")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("OK")
