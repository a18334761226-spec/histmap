#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
方镇格局状态重建器（接入领域知识表）。

把 classify_ops.py 产出的操作按年施加到 42 个藩镇槽位，
推算目标年份「每个藩镇辖有哪些州」的格局。

用法: python build_state.py [目标年份]
输出: data/processed/state_<年>.json  +  state-report.txt
"""
import json
import os
import re
import sys
from collections import OrderedDict, Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from domain_knowledge import is_valid_zhou, resolve_col, FANZHEN_ALIAS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")
OUT = os.path.join(ROOT, "data", "processed")

SUFFIX = ["節度大使", "都防禦經略使", "都團練觀察使", "都防禦使", "都團練使",
          "節度使", "觀察使", "防禦使", "團練使", "經略使", "守捉使",
          "節度", "觀察", "防禦", "團練", "經略", "軍使", "大使", "留後", "尹"]


def norm_name(s: str) -> str:
    s = re.sub(r"[\s，,。、；;：:（）()〈〉《》]", "", s or "")
    for suf in SUFFIX:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    s = re.sub(r"^(以|於|于|為|为|升|置|改|更|復|复|又)", "", s)
    return s


def main():
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 807
    with open(os.path.join(OUT, f"ops_{target}.json"), encoding="utf-8") as f:
        ops = json.load(f)
    with open(SRC, encoding="utf-8") as f:
        vols = json.load(f)

    cols = []
    for v in vols:
        for c in v["headers"][2:]:
            if c and c not in cols:
                cols.append(c)
    norm2col = {}
    for c in cols:
        norm2col.setdefault(norm_name(c), c)

    state = OrderedDict((c, {"name": c, "zhou": [], "seat": None,
                             "active": False, "since": None}) for c in cols)
    unresolved = Counter()
    dropped = Counter()

    ops.sort(key=lambda o: o["year"])

    def addz(st, zs):
        for z in zs:
            if not is_valid_zhou(z):
                dropped[z] += 1
                continue
            if z not in st["zhou"]:
                st["zhou"].append(z)

    for o in ops:
        col = o.get("target")
        if col not in state:
            continue
        st = state[col]
        yr, op = o["year"], o["op"]

        if op == "NEW":
            st["active"] = True
            st["since"] = st["since"] or yr
            addz(st, o.get("zhou", []))
            if o.get("seat") and is_valid_zhou(o["seat"]):
                st["seat"] = o["seat"]

        elif op == "ADD":
            st["active"] = True
            addz(st, o.get("zhou", []))

        elif op == "DEL":
            for z in o.get("zhou", []):
                if z in st["zhou"]:
                    st["zhou"].remove(z)

        elif op == "TRANSFER":
            zs = [z for z in o.get("zhou", []) if is_valid_zhou(z)]
            for z in o.get("zhou", []):
                if not is_valid_zhou(z):
                    dropped[z] += 1
            tgt = resolve_col(norm_name(o.get("to", "")), norm2col)
            if tgt is None:
                tgt = resolve_col(o.get("to", ""), norm2col)
            if tgt is None:
                unresolved[o.get("to", "")] += 1
            for s2 in state.values():
                for z in zs:
                    if z in s2["zhou"]:
                        s2["zhou"].remove(z)
            if tgt:
                state[tgt]["active"] = True
                addz(state[tgt], zs)

        elif op == "ABOLISH":
            st["active"] = False

        elif op == "RENAME":
            st["active"] = True

        elif op == "SEAT":
            if o.get("seat") and is_valid_zhou(o["seat"]):
                st["seat"] = o["seat"]

    active = {c: s for c, s in state.items() if s["active"] and s["zhou"]}

    L = [f"=== 公元 {target} 年 藩镇格局（重建结果 v2，已过滤虚词+别名归一） ===",
         f"方镇表槽位: {len(cols)}    有效藩镇: {len(active)}    "
         f"辖州合计: {sum(len(s['zhou']) for s in active.values())}", ""]
    for c, s in sorted(active.items(), key=lambda kv: -len(kv[1]["zhou"])):
        L.append(f"  {c:6s} 治{(s['seat'] or '?'):3s} {len(s['zhou']):2d}州  {'、'.join(s['zhou'])}")
    L += ["", f"未解析转移目标: {len(unresolved)} 种"]
    for k, n in unresolved.most_common(20):
        L.append(f"    {k} x{n}")
    L += ["", f"被过滤的非州名 token: {len(dropped)} 种"]
    L.append("    " + "、".join(f"{k}({n})" for k, n in dropped.most_common(30)))

    with open(os.path.join(ROOT, "state-report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    with open(os.path.join(OUT, f"state_{target}.json"), "w", encoding="utf-8") as f:
        json.dump({"year": target, "fanzhen": active}, f, ensure_ascii=False, indent=1)
    print("OK")


if __name__ == "__main__":
    main()
