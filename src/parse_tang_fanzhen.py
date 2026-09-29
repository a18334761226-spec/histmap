#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T1 ·《新唐书·方镇表》解析器
================================
把六个卷的方镇表原文解析成「藩镇 × 年份 → 辖州列表」。

## 三次踩坑记录（都写在代码里，免得重走）

**坑 1 · 单字正则切碎多字名**
旧版 `classify_ops.py` 用 `((?:\\u4e00-\\u9fff、)*\\u4e00-\\u9fff)(?:[数]*州)`，
`[\\u4e00-\\u9fff]` 只吃一个字，于是「領**京兆**、同、岐、金、商五州」
被解析成 `兆、同、岐、金、商`——「京」丢了。

**坑 2 · 从整句扫字典字符**
换成「先建词典再扫描」之后，如果对**整个子句**扫，就会把藩镇名与使职名
里的字也当成州：「**劍南**」→ 劍、南；「**西**川」→ 西；「**河**中」→ 河。
结果每个藩镇都多出十几个假州。
→ 必须**只从「、分隔且以州/府结尾」的枚举片段**里取名。

**坑 3 · 转移范围失控**
`head = cl[:m.start()]` 取了「隸」之前的整个子句，于是
「…領松、當、悉、柘…環、真九州，以四州隸山南西道」会把前面提到的
十几个州全搬走，造成州在藩镇间大规模泄漏。
→ head 只取「隸」前面紧邻的那一段（上一个句读之后）。

产出
    data/processed/tang_zhou_dict.json         州/府名词典
    data/processed/tang_fanzhen_timeline.json  藩镇 × 年份 × 州

用法
    python src/parse_tang_fanzhen.py --year 807
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, OrderedDict, defaultdict

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "processed", "fangzhen_raw.json")
OUT = os.path.join(ROOT, "data", "processed")

HAN = re.compile(r"[\u4e00-\u9fff]")
NUM = "一二三四五六七八九十百千"

# 会被误当成州名首字的动词/虚词
STOP = set("領领增復复罷罢廢废置以隸隶治徙為为及等諸诸軍军都"
           "升省更析取行降割分賜赐併并合入出")

# 唐代 742–758 年曾改州为郡，方镇表早期用**郡名**记州。
# 「置淮南西道節度使，領義陽、弋陽、潁川、滎陽、汝南五郡」——
# 五个郡名一个都匹配不上州词典，整批州就丢了（蔡州就是这么丢的）。
JUN_TO_ZHOU = {
    "義陽": "申", "弋陽": "光", "潁川": "許", "滎陽": "鄭", "汝南": "蔡",
    "北海": "青", "張掖": "甘", "上洛": "商", "天水": "秦",
    "安化": "慶", "濮陽": "濮", "河間": "瀛", "清河": "貝",
    "鉅鹿": "邢", "趙郡": "趙", "常山": "恒", "平原": "德",
    "樂安": "棣", "博平": "博", "魏郡": "魏", "汲郡": "衛",
    "鄴郡": "相", "范陽": "幽", "漁陽": "薊", "北平": "平",
    "河東": "河中", "太原": "太原", "上黨": "潞", "高平": "澤",
    "陽翟": "許", "陳留": "汴", "滎澤": "鄭",
}

# 藩镇改名：方镇表**列名用初名**，正文里却用后来的号。
# 「淮南西道」这一列的正文写的是「淮西節度」「彰義軍節度」「申光蔡節度」。
# 不建别名表，「所管州皆隸淮西節度」就找不到落点，蔡、申诸州全丢。
FANZHEN_ALIAS = {
    "淮西": "淮南西道", "彰義軍": "淮南西道", "彰義": "淮南西道",
    "申光蔡": "淮南西道", "淮寧軍": "淮南西道", "蔡汝": "淮南西道",
    "淮南西": "淮南西道",
    "西川": "劍南", "劍南西川": "劍南",
    "東都畿": "東畿", "都畿": "東畿",
    "浙江西道": "江東", "浙江東道": "浙東", "浙西": "江東",
    "平盧": "青密", "淄青": "青密", "淄沂": "青密",
    "永平軍": "滑衛", "宣武軍": "滑衛", "義成軍": "滑衛",
    "忠武軍": "鄭陳", "陳許": "鄭陳", "淮寧": "淮南西道",
    "天平軍": "鄆", "泰寧軍": "徐海沂密", "武寧軍": "徐海沂密",
    "河陽": "河中", "陝虢": "陝", "陝州": "陝",
    "振武": "朔方", "天德軍": "朔方", "靈鹽": "朔方",
    "鳳翔": "興鳳隴", "隴右": "隴右",
    "荊南": "荊南", "山南東道": "南陽", "襄陽": "南陽",
    "福建": "福建", "江西": "洪吉", "鄂岳": "鄂岳沔",
    "湖南": "衡州", "桂管": "桂管", "邕管": "邕管", "容管": "容管",
    "安南": "安南", "嶺南": "嶺南", "黔中": "黔州",
}


def resolve_fanzhen(name: str, cols) -> str | None:
    """把一个正文里出现的藩镇号/别名解析成方镇表的列名。"""
    if not name:
        return None
    for cand in (name, FANZHEN_ALIAS.get(name)):
        if not cand:
            continue
        if cand in cols:
            return cand
    # 去掉「節度使」等后缀再试
    base = re.sub(r"(節度使|節度|觀察使|觀察|防禦使|團練使|經略使|留後|軍)$", "", name)
    if base in cols:
        return base
    if FANZHEN_ALIAS.get(base) in cols:
        return FANZHEN_ALIAS[base]
    # 子串匹配（正文里常写成「XX節度」而列名是「XX道」）
    for c in cols:
        if c and (c in name or name in c):
            return c
    return FANZHEN_ALIAS.get(name)


# 唐代的「府」总共只有十来个（州升府）。它们在语料里往往只以枚举形式出现
# （「領京兆、同、岐、金、商五州」），过不了「必须有 X州/X府 显式见证」这条
# 过滤，于是「京兆」被整条丢掉。直接白名单，比放宽规则安全。
KNOWN_FU = {"京兆", "河南", "太原", "河中", "江陵", "成都", "鳳翔", "凤翔",
            "興元", "兴元", "興德", "兴德", "興唐", "兴唐", "京兆府"}

# 枚举片段：名字(、名字)+ 数字? (州|府|郡)
#   例「領京兆、同、岐、金、商五州」「夏、鹽、綏、銀、豐、勝六州」
#       「領義陽、弋陽、潁川、滎陽、汝南五郡」 ← 必须含「郡」！
# 漏掉「郡」的代价：756 年淮南西道那句整条枚举匹配不上、zhou=[]，
# 五个州（含蔡州）从源头就没进系统，蔡州此后永远找不到归属。
ENUM_SPAN = re.compile(r"([\u4e00-\u9fff]{1,2}(?:、[\u4e00-\u9fff]{1,3})+"
                       r"(?:[" + NUM + r"]*)(?:州|府|郡))")
# 单发片段：分隔符/句首 + 名字(1~2字) + (州|府|郡)
SINGLE_SPAN = re.compile(r"(?:^|[、，。；：領领增復复罷罢廢废以隸隶治及等並并]"
                         r"|置|升|降|賜|赐)([\u4e00-\u9fff]{1,2})[州府郡]")


def clean_token(tok: str) -> str:
    """剥掉名字首尾的动词、数字、后缀（州/府/郡）。"""
    t = tok
    for suf in ("州", "府", "郡"):
        if t.endswith(suf):
            t = t[:-len(suf)]
    while t and (t[-1] in NUM or t[-1] == "等"):
        t = t[:-1]
    while t and t[0] in STOP:
        t = t[1:]
    for suf in ("州", "府", "郡"):
        if t.endswith(suf):
            t = t[:-len(suf)]
    return t


def load_cells():
    vols = json.load(open(SRC, encoding="utf-8"))
    out = []
    for v in vols:
        for r in v["rows"]:
            for col, txt in r["cells"].items():
                if txt and HAN.search(txt):
                    out.append({"year": r["year"], "era": r["era"], "col": col,
                                "text": txt.replace("|", " ").strip(),
                                "vol": v["volume"]})
    out.sort(key=lambda x: x["year"])
    return out


def build_dict(cells):
    """州/府名词典。

    两个来源都限定在**有明确边界的片段**里：
      · 枚举片段（、分隔 + 州/府 收尾）
      · 单发片段（前有分隔符或动词，后有州/府）
    多字名还必须有「X州」「X府」的显式见证，否则是短语误捕。
    """
    cand = Counter()
    witnessed = set()
    for c in cells:
        t = c["text"]
        for m in ENUM_SPAN.finditer(t):
            for tok in m.group(1).split("、"):
                x = clean_token(tok)
                if x:
                    cand[x] += 1
        for m in SINGLE_SPAN.finditer(t):
            x = clean_token(m.group(1))
            if x:
                cand[x] += 1
        for m in re.finditer(r"([\u4e00-\u9fff]{2})[州府]", t):
            witnessed.add(m.group(1))

    names = {}
    for nm, n in cand.items():
        if not nm or nm in STOP or len(nm) > 2:
            continue
        if all(ch in NUM for ch in nm):
            continue
        if len(nm) == 2 and nm not in witnessed and nm not in KNOWN_FU:
            continue
        names[nm] = {"count": n}
    # 府名兜底：即便没被任何片段捕到，也要在词典里，
    # 否则枚举里的「京兆」会被当成不在词典而丢弃
    for fu in KNOWN_FU:
        names.setdefault(fu, {"count": 0, "from": "whitelist"})
    # 郡名也算合法州名，其值（州名）由调用方按 JUN_TO_ZHOU 还原
    for jun in JUN_TO_ZHOU:
        names.setdefault(jun, {"count": 0, "from": "jun"})
    return names


def normalize_jun(z: str) -> str:
    """郡名 → 州名。方镇表早期用郡名记州。"""
    return JUN_TO_ZHOU.get(z, z)


def extract_zhou(text, dic):
    """**只从枚举片段与单发片段**取州名。

    绝不对整句扫词典——那样会把藩镇名（劍南）、使职名（支度營田）里的字
    也当成州。郡名（義陽、潁川…）会按 JUN_TO_ZHOU 还原成州名。
    """
    out = []
    for m in ENUM_SPAN.finditer(text):
        for tok in m.group(1).split("、"):
            x = clean_token(tok)
            if x in dic:
                x = normalize_jun(x)
                if x not in out:
                    out.append(x)
    for m in SINGLE_SPAN.finditer(text):
        x = clean_token(m.group(1))
        if x in dic:
            x = normalize_jun(x)
            if x not in out:
                out.append(x)
    return out


def parse_cell(text, col, year, dic, cols=None):
    ops = []
    t = text
    for w in ("是年", "未幾", "未几", "尋", "寻", "又"):
        t = t.replace(w + "，", "。").replace(w + ",", "。")
    clauses = [c.strip(" ，,、") for c in re.split(r"[。；;]", t) if HAN.search(c)]

    for cl in clauses:
        zhou = extract_zhou(cl, dic)

        # ── 指代转移：「以所管七州隸朔方」「所管州皆隸淮西節度」 ──
        # 这类句子**不列出具体州名**，只说「所有辖州归某镇」。
        # 初版遇到指代直接跳过，结果蔡州永远到不了淮西手里
        # （「廢蔡汝節度使，所管州皆隸淮西節度」是蔡州唯一的去向）。
        # 必须在 build_timeline 里执行——只有那时才知道「所管」是哪些州。
        anaphora = False
        for m in re.finditer(r"所管[^，。；]*?隸([\u4e00-\u9fff]{1,6})", cl):
            to_raw = m.group(1)
            to = resolve_fanzhen(to_raw, cols) if cols else None
            ops.append({"op": "TRANSFER_ALL", "target": col,
                        "to": to or to_raw, "to_raw": to_raw,
                        "raw": cl, "year": year})
            anaphora = True
        if anaphora:
            continue

        # 转移（范围限制在「隸」前紧邻的一段）
        moved_any = False
        for m in re.finditer(r"隸([\u4e00-\u9fff]{1,6})", cl):
            tgt = m.group(1)
            sep = max(cl.rfind("，", 0, m.start()), cl.rfind("。", 0, m.start()),
                      cl.rfind("；", 0, m.start()))
            head = cl[sep + 1:m.start()]
            moved = extract_zhou(head, dic)
            # 目标先按州名解析，再按藩镇别名解析
            tgt_names = extract_zhou(tgt, dic)
            tgt_fz = resolve_fanzhen(tgt, cols) if cols else None
            if not moved:
                continue
            moved_any = True
            ops.append({"op": "TRANSFER", "target": col, "zhou": moved,
                        "to": tgt_names[0] if tgt_names else (tgt_fz or tgt),
                        "to_raw": tgt, "raw": cl, "year": year})

        if re.search(r"罷領|罢领", cl):
            ops.append({"op": "DEL", "target": col, "zhou": zhou,
                        "raw": cl, "year": year})
            continue
        if re.search(r"[廢废罷罢]", cl) and not re.search(r"領|领|隸|隶|置", cl):
            ops.append({"op": "ABOLISH", "target": col, "raw": cl, "year": year})
            continue
        if moved_any:
            continue

        seat = None
        ms = re.search(r"徙?治([\u4e00-\u9fff]{1,3})州", cl)
        if ms:
            seat = ms.group(1)

        if re.search(r"[置立]", cl) and re.search(r"領|领", cl):
            ops.append({"op": "NEW", "target": col, "zhou": zhou, "seat": seat,
                        "raw": cl, "year": year})
        elif re.search(r"增領|增领|復領|复领|並領|并领|領|领", cl) and zhou:
            ops.append({"op": "ADD", "target": col, "zhou": zhou, "seat": seat,
                        "raw": cl, "year": year})
        elif seat:
            ops.append({"op": "SEAT", "target": col, "seat": seat,
                        "raw": cl, "year": year})
        elif not zhou and "州" not in cl and "府" not in cl:
            ops.append({"op": "NONTERR", "target": col, "raw": cl, "year": year})
        else:
            ops.append({"op": "OTHER", "target": col, "zhou": zhou,
                        "raw": cl, "year": year})
    return ops


def build_timeline(ops, cols):
    state = OrderedDict((c, {"name": c, "zhou": set(), "seat": None,
                             "since": None, "active": False}) for c in cols)
    timeline = {}
    by_year = defaultdict(list)
    for o in ops:
        by_year[o["year"]].append(o)
    for year in sorted(by_year):
        for o in by_year[year]:
            col = o.get("target")
            if col not in state:
                continue
            st, op = state[col], o["op"]
            if op in ("NEW", "ADD"):
                zs = o.get("zhou") or []
                # 一个州在同一时刻只能隶属一个藩镇。
                # 不摘掉的话会出现「慈、晉、隰」同时挂在北京畿与河中名下的
                # 幽灵归属（实测 807 年有 16 个州被重复占用），地图上表现为
                # 两块领地重叠、颜色互相吃。
                for other, s2 in state.items():
                    if other == col:
                        continue
                    for z in zs:
                        s2["zhou"].discard(z)
                st["since"] = st["since"] or year
                st["zhou"].update(zs)
                st["active"] = True
                if o.get("seat"):
                    st["seat"] = o["seat"]
            elif op == "DEL":
                for z in o.get("zhou") or []:
                    st["zhou"].discard(z)
            elif op == "TRANSFER":
                for s2 in state.values():
                    for z in o.get("zhou") or []:
                        s2["zhou"].discard(z)
                if o.get("to") in state:
                    state[o["to"]]["zhou"].update(o.get("zhou") or [])
            elif op == "TRANSFER_ALL":
                # 「所管州皆隸X」：把本镇**当前全部**辖州交给 X。
                # 只能在这里做——解析阶段不知道「所管」是哪些州。
                zs = list(st["zhou"])
                for s2 in state.values():
                    for z in zs:
                        s2["zhou"].discard(z)
                if o.get("to") in state:
                    state[o["to"]]["zhou"].update(zs)
            elif op == "ABOLISH":
                # 关键修正：**不清空辖州**。
                #
                # 《方镇表》里的「廢/罷」多半指**官职类型变化**（节度使降为观察使、
                # 罢某军号），不是地盘消失。初版在这里 `st["zhou"] = set()`，
                # 结果 762 年一句「廢節度使」就把京畿的州全清空了，
                # 到 807 年京畿整个藩镇从图上消失。
                #
                # 地盘只会因为显式的 NEW/ADD/DEL/TRANSFER 而变。
                # 真被裁撤的藩镇，其州会被后续的 TRANSFER 带走而自然变空。
                st["active"] = False
            elif op == "SEAT" and o.get("seat"):
                st["seat"] = o["seat"]
        timeline[year] = {c: {"zhou": sorted(s["zhou"]), "seat": s["seat"],
                              "since": s["since"], "active": s["active"]}
                          for c, s in state.items() if s["zhou"]}
    return timeline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=807)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    cells = load_cells()
    print(f"有效单元格 {len(cells)}（{cells[0]['year']}–{cells[-1]['year']}）")
    dic = build_dict(cells)
    lens = Counter(len(k) for k in dic)
    print(f"州/府词典 {len(dic)} 条   长度分布 {dict(sorted(lens.items()))}")
    print(f"  多字名: {sorted(k for k in dic if len(k) == 2)}")

    cols = []
    for c in cells:
        if c["col"] not in cols:
            cols.append(c["col"])

    ops = []
    for c in cells:
        ops.extend(parse_cell(c["text"], c["col"], c["year"], dic, cols))
    cnt = Counter(o["op"] for o in ops)
    print(f"\n操作数 {len(ops)}")
    for k, n in cnt.most_common():
        print(f"    {k:18s} {n:5d}  ({n/len(ops)*100:.1f}%)")

    if args.verbose:
        print("\n=== OTHER 样例（前 15）===")
        for o in [x for x in ops if x["op"] == "OTHER"][:15]:
            print(f"  [{o['year']} {o['target']}] {o['raw'][:78]}")

    tl = build_timeline(ops, cols)
    snap = tl.get(args.year, {})
    total = sum(len(v["zhou"]) for v in snap.values())
    print(f"\n=== {args.year} 年 ===")
    print(f"有效藩镇 {len(snap)} 个，辖州合计 {total}")
    for c, v in sorted(snap.items(), key=lambda kv: -len(kv[1]["zhou"]))[:12]:
        print(f"  {c:8s} 治{(v['seat'] or '?'):3s} {len(v['zhou']):2d}州  "
              f"{'、'.join(v['zhou'])}")

    write_keep_overlay(os.path.join(OUT, "tang_zhou_dict.json"), dic)
    write_keep_overlay(os.path.join(OUT, "tang_fanzhen_timeline.json"), tl)
    print(f"\n-> {OUT}\\tang_zhou_dict.json")
    print(f"-> {OUT}\\tang_fanzhen_timeline.json")


def write_keep_overlay(path: str, obj):
    """写盘时把已有文件里下划线开头的键原样带回来。

    _display（藩镇改名）、_footer（口径声明）、_baseline/_legend/_palette
    这些是手工加的覆盖层，解析器不产出它们。不保留的话，
    重跑一次解析就把这些人工成果冲掉了。
    """
    keep = {}
    if os.path.exists(path):
        try:
            old = json.load(open(path, encoding="utf-8"))
            if isinstance(old, dict):
                keep = {k: v for k, v in old.items() if k.startswith("_")}
        except Exception as e:
            print(f"  [警告] 读不回旧覆盖层（{type(e).__name__}: {e}），本次不保留")
    if keep:
        for k, v in keep.items():
            obj.setdefault(k, v)
        # 下划线键排在最前面，人看文件时先看到口径
        ordered = {k: obj[k] for k in keep}
        ordered.update({k: v for k, v in obj.items() if k not in ordered})
        obj = ordered
        print(f"  保留手工覆盖层 {len(keep)} 项: {' '.join(sorted(keep))}")
    json.dump(obj, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
