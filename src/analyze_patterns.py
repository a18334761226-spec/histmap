# -*- coding: utf-8 -*-
"""统计方镇表变化文本的语言模式，评估解析难度。"""
import json
import os
import re
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")
OUT = os.path.join(ROOT, "analyze.txt")

with open(SRC, encoding="utf-8") as f:
    vols = json.load(f)

cells = []
for v in vols:
    for r in v["rows"]:
        for name, txt in r["cells"].items():
            if txt.strip():
                cells.append((v["volume"][-3:], r["year"], name, txt.strip()))

L = []
L.append(f"非空单元格总数: {len(cells)}")
L.append(f"涉及藩镇列: {len(set(c[2] for c in cells))}")
L.append(f"年份跨度: {min(c[1] for c in cells)} ~ {max(c[1] for c in cells)}")
L.append("")

# 关键词频次
KW = ["領", "增領", "罷領", "廢", "置", "改", "更名", "隸", "割", "析", "省", "復", "升", "降", "治", "徙治", "賜號", "兼", "以"]
L.append("=== 关键词出现次数 ===")
for k in KW:
    n = sum(1 for c in cells if k in c[3])
    L.append(f"  {k:6s} {n}")

L.append("")
# 州的形态：X州 / X、Y、Z...州
allz = []
for c in cells:
    for m in re.findall(r"([\u4e00-\u9fff]{1,4}州)", c[3]):
        allz.append(m)
L.append(f"=== 州名出现总次数: {len(allz)} ===")
L.append(f"  不同州名: {len(set(allz))}")
L.append("  高频前 40:")
for name, n in Counter(allz).most_common(40):
    L.append(f"    {name} {n}")

L.append("")
L.append("=== 含『領』的句式样例（20 条）===")
n = 0
for c in cells:
    if "領" in c[3] and n < 20:
        L.append(f"  [{c[0]} {c[1]} {c[2]}] {c[3][:90]}")
        n += 1

L.append("")
L.append("=== 最长文本前 8 条（看复杂度）===")
for c in sorted(cells, key=lambda x: -len(x[3]))[:8]:
    L.append(f"  [{c[0]} {c[1]} {c[2]}] 长度{len(c[3])}")
    L.append(f"     {c[3][:220]}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("OK")
