#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方镇变化记录的「操作」分类器。

把《新唐书·方镇表》的文言记录切成子句，逐条归类为：
  NEW       置X，领A、B、C N州，治D
  ADD       X增领/复领/领 Y州
  DEL       X罢领 Y州
  TRANSFER  以A、B(N州)隶C   /   A、B(N州)隶C
  ABOLISH   废X
  RENAME    X更名Y / 升X为Y / 赐号Y / 更X曰Y
  SEAT      X治Y / 徙治Y
  NONTERR   兼xx使 / 带职衔 —— 非领土变更，忽略
  OTHER     未识别

输出: data/processed/ops_<year>.json  +  ops-report.txt
"""
import json
import os
import re
import sys
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")
OUT = os.path.join(ROOT, "data", "processed")
NUM = "一二三四五六七八九十百千万"

HAN = re.compile(r"[\u4e00-\u9fff]")
# 枚举 + 可选计数 + 州: 例「夏、鹽、綏、銀、豐、勝六州」「邠州」
ENUM_ZHOU = r"((?:[\u4e00-\u9fff]、)*[\u4e00-\u9fff])(?:[" + NUM + r"]*州)"


def has_han(s: str) -> bool:
    return bool(HAN.search(s))


def strip_noise(s: str) -> str:
    """去掉解析残留，如孤立的 | 或多余空白。"""
    s = s.replace("|", " ")
    return re.sub(r"\s+", " ", s).strip()


def extract_zhou(text: str):
    """抽出州名列表。"""
    res = []
    for m in re.finditer(ENUM_ZHOU, text):
        for c in m.group(1).split("、"):
            if c:
                res.append(c)
    # 「N州：a、b、c」
    for m in re.finditer(r"[" + NUM + r"]*州[：:]\s*((?:[\u4e00-\u9fff]、)*[\u4e00-\u9fff])", text):
        for c in m.group(1).split("、"):
            if c:
                res.append(c)
    seen, out = set(), []
    for z in res:
        if z and z not in seen:
            seen.add(z)
            out.append(z)
    return out


def split_clauses(text: str):
    t = text
    for w in ("是年", "未幾", "未几", "尋", "寻", "又"):
        t = t.replace(w + "，", "。").replace(w + ",", "。")
    t = t.replace("；", "。").replace(";", "。")
    return [c.strip(" ，,、") for c in re.split(r"[。；;]", t) if has_han(c)]


def classify(cl: str, col: str, year: int):
    """对单个子句分类，返回操作列表（可能多条）。"""
    ops = []
    zhou = extract_zhou(cl)
    has_zhou = "州" in cl
    has_enum = bool(re.search(r"[\u4e00-\u9fff]、[\u4e00-\u9fff]", cl))

    # ── 先抽 TRANSFER（可能一句多条）────────────────────────────
    transfers = []
    FILLER = r"[復复皆並并又寻尋]?"
    # 枚举式: A、B、C(州)?(虚词)?隸D
    for m in re.finditer(
            r"((?:[\u4e00-\u9fff]、)+[\u4e00-\u9fff])(?:[" + NUM + r"]*州)?" + FILLER +
            r"隸([\u4e00-\u9fff]{2,8})", cl):
        transfers.append(([c for c in m.group(1).split("、") if c], m.group(2)))
    # 单选式: X州(虚词)?隸Y
    for m in re.finditer(r"([\u4e00-\u9fff])[" + NUM + r"]?州" + FILLER +
                         r"隸([\u4e00-\u9fff]{2,8})", cl):
        transfers.append(([m.group(1)], m.group(2)))
    if transfers:
        for zh, to in transfers:
            ops.append({"op": "TRANSFER", "target": col, "zhou": zh,
                        "to": to, "raw": cl, "year": year})
        # 同一子句若还带「廢」，追加废除
        if re.search(r"[廢废罷罢]", cl) and not re.search(r"置|領|领", cl):
            ops.append({"op": "ABOLISH", "target": col, "raw": cl, "year": year})
        return ops

    # ── 非领土判定：无「州」且无枚举 ───────────────────────────
    if not has_zhou and not has_enum:
        if re.search(r"[廢废]", cl) and re.search(r"[置立]", cl) is None:
            return [{"op": "ABOLISH", "target": col, "raw": cl, "year": year}]
        if re.search(r"更名|改名|升|賜號|赐号|更[^\s]{1,8}[為为曰]", cl):
            return [{"op": "RENAME", "target": col, "raw": cl, "year": year}]
        return [{"op": "NONTERR", "target": col, "raw": cl, "year": year}]

    # ── 废除 ──────────────────────────────────────────────────
    if re.search(r"[廢废罷罢]", cl) and not re.search(r"置|領|领", cl):
        return [{"op": "ABOLISH", "target": col, "raw": cl, "year": year}]

    # ── 新建 ──────────────────────────────────────────────────
    if re.search(r"[置立]", cl) and re.search(r"領|领", cl):
        mseat = re.search(r"治([\u4e00-\u9fff]{1,3})州", cl)
        return [{"op": "NEW", "target": col, "zhou": zhou,
                 "seat": mseat.group(1) if mseat else None, "raw": cl, "year": year}]

    # ── 增领 / 罢领 ───────────────────────────────────────────
    if re.search(r"罷領|罢领", cl):
        return [{"op": "DEL", "target": col, "zhou": zhou, "raw": cl, "year": year}]
    if re.search(r"增領|增领|復領|复领|領|领", cl) and zhou:
        return [{"op": "ADD", "target": col, "zhou": zhou, "raw": cl, "year": year}]

    # ── 改名 ──────────────────────────────────────────────────
    if re.search(r"更名|改名|升[^\s]{1,8}[為为]|賜號|赐号|更[^\s]{1,8}[為为曰]", cl):
        return [{"op": "RENAME", "target": col, "raw": cl, "year": year}]

    # ── 治所 ──────────────────────────────────────────────────
    m = re.search(r"徙?治([\u4e00-\u9fff]{1,3})州", cl)
    if m:
        return [{"op": "SEAT", "target": col, "seat": m.group(1), "raw": cl, "year": year}]

    return [{"op": "OTHER", "target": col, "raw": cl, "year": year}]


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 807
    with open(SRC, encoding="utf-8") as f:
        vols = json.load(f)

    events = []
    for v in vols:
        for r in v["rows"]:
            if r["year"] > target:
                continue
            for col, txt in r["cells"].items():
                t = strip_noise(txt)
                if has_han(t):
                    events.append((r["year"], col, t, v["volume"]))
    events.sort(key=lambda x: x[0])

    ops = []
    for y, col, txt, vol in events:
        for cl in split_clauses(txt):
            ops.extend(classify(cl, col, y))

    c = Counter(o["op"] for o in ops)
    L = [f"目标年份: {target}",
         f"有效事件数: {len(events)}（已剔除无中文的解析残留）",
         f"子句/操作数: {len(ops)}", "",
         "=== 操作类型分布 ==="]
    for k, n in c.most_common():
        L.append(f"  {k:10s} {n:5d}  ({n/len(ops)*100:.1f}%)")
    L += ["", f"=== OTHER 共 {c['OTHER']} 条（前 30）==="]
    for o in [x for x in ops if x["op"] == "OTHER"][:30]:
        L.append(f"  [{o['year']} {o['target']}] {o['raw'][:95]}")
    L += ["", f"=== NONTERR 样例（前 8，这类应忽略）==="]
    for o in [x for x in ops if x["op"] == "NONTERR"][:8]:
        L.append(f"  [{o['year']} {o['target']}] {o['raw'][:70]}")

    with open(os.path.join(ROOT, "ops-report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(os.path.join(OUT, f"ops_{target}.json"), "w", encoding="utf-8") as f:
        json.dump(ops, f, ensure_ascii=False, indent=1)
    print("OK")


if __name__ == "__main__":
    main()
