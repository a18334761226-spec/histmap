# -*- coding: utf-8 -*-
"""
方镇格局累积推算引擎。

把《新唐书·方镇表》的文言变化记录，转成结构化操作并按年施加，
从而推算出任意年份「各藩镇辖有哪些州」的完整格局。

操作类型:
  NEW      置X节度/观察使，领A、B、C N州，治D
  ADD      X增领/复领 Y州
  DEL      X罢领 Y州
  TRANSFER 以A、B隶C
  ABOLISH  废X
  RENAME   X更名Y / 升X为Y
  SEAT     X治Y / 徙治Y  (仅影响治所，不影响辖州)

用法: python accumulate.py [目标年份]
输出: data/processed/state_<年>.json
"""
import json
import os
import re
import sys
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")
OUT = os.path.join(ROOT, "data", "processed")

NUM = "一二三四五六七八九十百"


def extract_zhou(text: str):
    """从一段文字里抽出州名列表（去掉末尾的计数）。"""
    res = []
    # 形如 「夏、鹽、綏、銀、豐、勝六州」 或 「邠州」 或 「領州九：邠、寧、慶」
    # 1) 顿号枚举 + 可选计数
    for m in re.finditer(r"((?:[\u4e00-\u9fff]、)+[\u4e00-\u9fff])(?:[" + NUM + r"]*州)", text):
        seq = m.group(1)
        res += [c for c in seq.split("、") if c]
    # 2) 「N州：a、b、c」
    for m in re.finditer(r"[" + NUM + r"]*州[：:]\s*((?:[\u4e00-\u9fff]、)*[\u4e00-\u9fff])", text):
        seq = m.group(1)
        res += [c for c in seq.split("、") if c]
    # 3) 单个 「X州」（非枚举、非计数）
    for m in re.finditer(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff])(?:[" + NUM + r"]?州)", text):
        res.append(m.group(1))
    seen, out = set(), []
    for z in res:
        if z and z not in seen:
            seen.add(z)
            out.append(z)
    return out


def split_clauses(text: str):
    """把长句切成子句，便于逐条解析。"""
    t = re.sub(r"是年[，,]?", "。", text)
    t = re.sub(r"未幾[，,]?", "。", t)
    t = re.sub(r"尋[，,]?", "。", t)
    t = t.replace("；", "。")
    return [c.strip() for c in re.split(r"[。；]", t) if c.strip()]


def parse_ops(cell_text: str, col: str, year: int):
    """把一条记录解析成操作列表。"""
    ops = []
    for cl in split_clauses(cell_text):
        zhou = extract_zhou(cl)

        # 废 / 罢
        if re.search(r"廢|罢|罷", cl) and not re.search(r"置|領", cl):
            m = re.search(r"[廢罢罷]([\u4e00-\u9fff]{1,6}(?:節度|觀察|防禦|團練)[\u4e00-\u9fff]*)", cl)
            ops.append({"op": "ABOLISH", "target": col, "raw": cl, "year": year})
            continue

        # 以A、B隸C  —— 转移
        m = re.search(r"以\s*((?:[\u4e00-\u9fff]、)*[\u4e00-\u9fff]{1,2}(?:、[\u4e00-\u9fff]{1,2})*)\s*隸\s*([\u4e00-\u9fff]{2,6})", cl)
        if m:
            ops.append({"op": "TRANSFER", "zhou": zhou, "to": m.group(2), "raw": cl, "year": year})
            continue

        # 置…節度使，領… ，治…
        if re.search(r"置|立", cl):
            m = re.search(r"治([\u4e00-\u9fff]{1,3})州", cl)
            seat = m.group(1) if m else None
            ops.append({"op": "NEW", "target": col, "zhou": zhou,
                        "seat": seat, "raw": cl, "year": year})
            continue

        # 增領 / 復領 / 領
        if re.search(r"領", cl):
            if re.search(r"罷領", cl):
                ops.append({"op": "DEL", "target": col, "zhou": zhou, "raw": cl, "year": year})
            elif re.search(r"增領|復領|領", cl):
                ops.append({"op": "ADD", "target": col, "zhou": zhou, "raw": cl, "year": year})
            continue

        # 治所
        m = re.search(r"徙?治([\u4e00-\u9fff]{1,3})州", cl)
        if m:
            ops.append({"op": "SEAT", "target": col, "seat": m.group(1), "raw": cl, "year": year})
            continue

        # 更名 / 升…為…
        if re.search(r"更名|改|升|賜號", cl):
            ops.append({"op": "RENAME", "target": col, "raw": cl, "year": year})
            continue

        ops.append({"op": "OTHER", "target": col, "raw": cl, "year": year})
    return ops


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 807

    with open(SRC, encoding="utf-8") as f:
        vols = json.load(f)

    # 收集所有 (year, col, text)
    events = []
    for v in vols:
        for r in v["rows"]:
            if r["year"] > target:
                continue
            for col, txt in r["cells"].items():
                if txt.strip():
                    events.append((r["year"], col, txt.strip(), v["volume"]))
    events.sort(key=lambda x: x[0])

    all_ops = []
    for y, col, txt, vol in events:
        all_ops += parse_ops(txt, col, y)

    from collections import Counter
    c = Counter(o["op"] for o in all_ops)

    L = []
    L.append(f"目标年份: {target}")
    L.append(f"处理事件数: {len(events)}")
    L.append(f"解析出操作数: {len(all_ops)}")
    L.append("")
    L.append("=== 操作类型分布 ===")
    for k, n in c.most_common():
        L.append(f"  {k:10s} {n}")
    L.append("")
    L.append(f"=== OTHER（未识别，需人工/LLM 处理）共 {c['OTHER']} 条 ===")
    for o in [x for x in all_ops if x["op"] == "OTHER"][:40]:
        L.append(f"  [{o['year']} {o['target']}] {o['raw'][:100]}")

    with open(os.path.join(ROOT, "ops-report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(os.path.join(OUT, f"ops_{target}.json"), "w", encoding="utf-8") as f:
        json.dump(all_ops, f, ensure_ascii=False, indent=1)
    print("OK")


if __name__ == "__main__":
    main()
