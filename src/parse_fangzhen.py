#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
解析《新唐书》方镇表（卷64-69）的 wikitext。
正确处理 wikitext 表格规则：
  - '|-'            行分隔
  - '|' / '!'       单元格起头
  - '||' / '!!'     同一行内的内联单元格分隔（关键！否则会列错位）
  - '|+'            表题（跳过）
  - '|}'            表格结束
输出: data/processed/fangzhen_raw.json
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "data", "processed")
os.makedirs(OUT, exist_ok=True)


def strip_attrs(cell: str) -> str:
    """去掉 'ATTR | CONTENT' 的属性前缀。"""
    cell = cell.strip()
    idx = cell.find("|")
    if idx >= 0 and "=" in cell[:idx]:
        return cell[idx + 1:].strip()
    return cell


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[\[([^\]|]+)\|([^\]]+)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]]+)\]\]", r"\1", s)
    s = re.sub(r"\{\{[^}]*\}\}", "", s)
    s = s.replace("'''", "").replace("''", "")
    s = s.replace("\u3000", " ")
    return re.sub(r"\s+", " ", s).strip()


def parse_table(table: str):
    """返回 rows: [[cell, cell, ...], ...]；第一行通常是表头。"""
    rows = []
    cur = None
    for raw in table.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("|-"):                 # 行分隔（含 '|-----'）
            if cur is not None:
                rows.append(cur)
            cur = []
            continue
        if line.startswith("|}"):                 # 表格结束
            break
        if line.startswith("|+"):                 # 表题
            continue
        if cur is None:
            cur = []
        if line[0] in "|!":
            sep = "!!" if line[0] == "!" else "||"
            for part in line[1:].split(sep):
                cur.append(part)
        else:                                     # 续行 -> 接上一个单元格
            if cur:
                cur[-1] += " " + line
    if cur:
        rows.append(cur)
    return rows


def parse_volume(path: str):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    wt = data["parse"]["wikitext"]
    title = data["parse"]["title"]

    start = wt.find("{|")
    end = wt.find("|}", start)
    if start < 0 or end < 0:
        return None
    rows = parse_table(wt[start:end])
    if not rows:
        return None

    # 表头 = 第一个「含年份特征之外」的行；实测第一行就是表头
    header_idx = None
    for i, r in enumerate(rows):
        joined = "".join(clean_text(strip_attrs(c)) for c in r)
        if "西元" in joined or "公元" in joined:
            header_idx = i
            break
    if header_idx is None:
        header_idx = 0

    headers = [clean_text(strip_attrs(c)) for c in rows[header_idx]]

    out_rows = []
    for r in rows[header_idx + 1:]:
        cells = [clean_text(strip_attrs(c)) for c in r]
        if not cells:
            continue
        m = re.search(r"(\d{3,4})", cells[0])
        if not m:
            continue
        year = int(m.group(1))
        era = cells[1] if len(cells) > 1 else ""
        body = cells[2:]
        entry = {"year": year, "era": era, "cells": {}}
        cols = headers[2:]
        for j, name in enumerate(cols):
            nm = name if name else f"col{j}"
            entry["cells"][nm] = body[j] if j < len(body) else ""
        out_rows.append(entry)

    return {"volume": title, "headers": headers, "rows": out_rows}


def main():
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".json"))
    all_data, report = [], []
    for fn in files:
        r = parse_volume(os.path.join(RAW, fn))
        if not r:
            report.append(f"[跳过] {fn}")
            continue
        cols = [c for c in r["headers"][2:] if c]
        yrs = [x["year"] for x in r["rows"]]
        report.append(f"{fn}: {r['volume']}  年份 {min(yrs)}~{max(yrs)}  "
                      f"{len(r['rows'])} 行  {len(cols)} 列")
        report.append(f"    列: {'、'.join(cols)}")
        all_data.append(r)

    with open(os.path.join(OUT, "fangzhen_raw.json"), "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=1)

    ncells = sum(1 for d in all_data for r in d["rows"] for v in r["cells"].values() if v.strip())
    report.append(f"\n合计: {len(all_data)} 卷, "
                  f"{sum(len(d['rows']) for d in all_data)} 行, "
                  f"{sum(len([c for c in d['headers'][2:] if c]) for d in all_data)} 列, "
                  f"{ncells} 个非空变化记录")
    with open(os.path.join(ROOT, "parse-report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report))
    print("OK")


if __name__ == "__main__":
    main()
