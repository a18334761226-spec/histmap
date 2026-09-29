# -*- coding: utf-8 -*-
"""统计累积到目标年份需要处理多少条变化记录。"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")

TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 807

with open(SRC, encoding="utf-8") as f:
    vols = json.load(f)

L = []
total = 0
upto = 0
for v in vols:
    for r in v["rows"]:
        for name, txt in r["cells"].items():
            if txt.strip():
                total += 1
                if r["year"] <= TARGET:
                    upto += 1

L.append(f"目标年份: {TARGET}")
L.append(f"全部变化记录: {total}")
L.append(f"需累积到 {TARGET} 年的记录数: {upto}")
L.append(f"占比: {upto/total*100:.1f}%")

# 按年分布（前 20 年）
from collections import Counter
c = Counter()
for v in vols:
    for r in v["rows"]:
        if r["year"] <= TARGET:
            for name, txt in r["cells"].items():
                if txt.strip():
                    c[r["year"]] += 1
L.append("")
L.append(f"变化最密集的 15 年（<= {TARGET}）:")
for y, n in c.most_common(15):
    L.append(f"  {y}: {n} 条")

with open(os.path.join(ROOT, "workload.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("OK")
