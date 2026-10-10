#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""意图识别：**先判断该用哪个数据源**，找不着再让大模型兜底。

用户的原话是「先意图识别 如果实在是找不到就写好提示词 大模型兜底」——
这里说的"找"是**找数据源**，不是找已有题材。

系统里能用的几何来源（都不是"凑合"，各有各的适用面）：

  atlaspi        AtlasPI · 真实历史政体多边形，**按年查询**，公元前 4500–2024
                 Apache-2.0 可商用。政体级（帝国/王国/汗国）。
                 优点：给的是**当年的真实边界**，且任意年份都有。
                 缺点：细到"州/县"就不行了。

  geoboundaries  geoBoundaries · 现代行政区（ADM1/ADM2），PDDL 公有领域。
                 优点：单元级细粒度（藩镇、九边、州、路、道都能落到县）。
                 缺点：底图是**现代**行政界，只能按治所归并反推历史疆域 ——
                       所以必须显式声明"不是当年的界线"。

  cshapes        CShapes 2.0 · 1886–2019 的国家边界（学术许可，禁商用）。
                 适合一战/二战这种"主权国家"层面且年份落在覆盖内的题材。

选择规则（**确定性的关键词判断，不花模型调用**）：
  1. 出现细粒度单元词（藩镇/军镇/九边/州/路/道/县/府/卫所/节度使…）
     → geoboundaries：只有它能落到单元级。
  2. 出现政体/宏观词（疆域/版图/格局/帝国/王国/各国/势力范围…）
     → atlaspi：真实历史边界。
  3. 只给了年份、没给领域 → atlaspi（任意年份都有，且是真实边界）。
  4. 落在 1886–2019 且是主权国家层面 → cshapes 优先（更权威），否则 atlaspi。
  5. 什么都不像 → **不猜**，交给大模型：给一段写好的提示词让它定数据源。
"""
from __future__ import annotations

import re

# ── 细粒度单元词：只有 geoBoundaries 那条路能落到单元级 ──────────
UNIT_WORDS = [
    "藩镇", "方镇", "军镇", "九边", "边镇", "节度使", "观察使",
    "州", "路", "道", "县", "府", "郡", "卫所", "都司", "行省",
    "布政使", "按察使", "总督", "巡抚", "各镇", "诸路", "各路",
    "割据", "军阀", "辖区", "控制区", "归属",
]

# ── 政体/宏观词：AtlasPI 的真实历史边界正合适 ────────────────────
POLITY_WORDS = [
    "疆域", "版图", "格局", "势力范围", "帝国", "王国", "汗国", "王朝",
    "政体", "政权", "各国", "列国", "世界", "欧洲", "亚洲", "东亚",
    "对比", "对峙", "并立", "形势",
]

# ── 主权国家层面 + 这些年份区间，CShapes 最权威 ──────────────────
CSHAPES_HINT = ["国界", "边界", "主权", "独立", "殖民地", "大战", "一战",
                "二战", "冷战"]

# 明显是现代行政区/今地名，用 geoBoundaries 更贴切
MODERN_HINT = ["现代", "今天", "现在", "当前", "省会", "地级市", "县级"]


def _years_in(ask: str) -> list[int]:
    ys = [int(m.group(1)) for m in re.finditer(r"(?<!\d)(\d{3,4})(?!\d)", ask)]
    return [y for y in ys if 0 < y <= 2100]


def choose_source(ask: str) -> dict:
    """判断该用哪个数据源。**不调模型**。

    返回 {source, why, kind, need_llm, prompt_hint}
      source     atlaspi / geoboundaries / cshapes / ""（都定不了）
      kind       直接可用的题材类型（atlaspi / gazetteer / partition）
      need_llm   True 表示没定下来，该走大模型兜底
    """
    a = (ask or "").strip()
    if not a:
        return {"source": "", "need_llm": True, "why": "没说话",
                "prompt_hint": ""}

    unit = [w for w in UNIT_WORDS if w in a]
    polity = [w for w in POLITY_WORDS if w in a]
    modern = [w for w in MODERN_HINT if w in a]
    cs = [w for w in CSHAPES_HINT if w in a]
    ys = _years_in(a)

    # 1) 细粒度单元词优先 —— 这是 geoBoundaries 唯一能做、AtlasPI 做不了的
    if unit and not modern:
        return {
            "source": "geoboundaries", "kind": "gazetteer",
            "why": f"出现单元级词 {unit[:3]} —— 要落到州/县这一层，"
                   f"AtlasPI 只有政体级多边形，做不到",
            "years": ys,
            "note": "底图是现代行政区，必须在 source_note 里写明"
                    "「不是当年的界线」，不能假装是实测边界",
            "need_llm": False,
        }

    # 2) **主权国家层面 + 年份落在覆盖期 → CShapes 优先**。
    #    这一条必须在通用政体词之前判：问「一战欧洲的国界」时"欧洲"会命中
    #    政体词，但 1914 的国界用 CShapes（学界标准数据）比 AtlasPI 更权威。
    if cs and ys and all(1886 <= y <= 2019 for y in ys):
        return {
            "source": "cshapes", "kind": "boundary",
            "why": f"主权国家层面（{cs[:2]}）+ 年份 {ys} 落在 CShapes 的 "
                   f"1886–2019 区间，用它的国家边界（学界标准数据）",
            "years": ys, "need_llm": False,
            "note": "CShapes 学术许可禁商用；商用场景请改用 AtlasPI",
        }

    # 3) 政体/宏观词 → AtlasPI 的真实历史边界
    if polity:
        return {
            "source": "atlaspi", "kind": "atlaspi",
            "why": f"出现政体级词 {polity[:3]} —— AtlasPI 直接给"
                   f"**当年的真实边界多边形**，比现代县界反推准确得多",
            "years": ys or [1000, 1200, 1400, 1644, 1800, 1900],
            "need_llm": False,
        }

    # 4) 只给了年份、没给领域 → AtlasPI（任意年份都有，且是真实边界）
    if ys:
        return {
            "source": "atlaspi", "kind": "atlaspi",
            "why": f"只给了年份 {ys}，没给领域 —— AtlasPI 按年查询、"
                   f"覆盖公元前 4500–2024，**任意年份都有真实边界**，"
                   f"不用先建题材",
            "years": ys, "need_llm": False,
        }

    # 5) 定不下来 → 不猜，让大模型来定
    return {
        "source": "", "kind": "", "need_llm": True,
        "why": "没有能确定数据源的关键词（既没有单元级词，也没有政体级词，"
               "也没给年份）",
        "prompt_hint": (
            "请判断这张历史地图应该用哪个几何数据源，并只输出 JSON：\n"
            "  atlaspi       —— 政体级（帝国/王国/汗国），真实历史边界，"
            "任意年份（-4500…2024），Apache-2.0 可商用\n"
            "  geoboundaries —— 单元级（州/县/藩镇/军镇），现代行政区，"
            "必须声明「不是当年的界线」\n"
            "  cshapes       —— 主权国家边界，仅 1886–2019，学术许可禁商用\n"
            "判据：**问的是「谁占了哪一片」用 atlaspi；问的是「某某镇/州"
            "各自的范围」用 geoboundaries**。\n"
            "输出 {\"source\":\"...\",\"kind\":\"atlaspi|gazetteer|partition\","
            "\"why\":\"...\",\"years\":[...],\"bbox\":[...]}\n"
            "如果三者都不合适，输出 {\"source\":\"none\",\"why\":\"...\"}，"
            "不要硬套。"),
    }


# ══════════════════════════════════════════════════════════════
#  另一件事：用户那句话是不是在问**系统里已有的题材**
# ══════════════════════════════════════════════════════════════
# 和"选数据源"是两件事，但都该在叫模型之前先查一遍：
# 问「唐宪宗二年的藩镇图」时，系统里已经有 tang 题材、807 年现成能出图，
# 直接命中就行，不必花 191 秒去问模型（实测模型还答错过：805 而不是 807）。

ALIASES: dict[str, list[str]] = {
    "tang": ["唐", "唐朝", "唐代", "藩镇", "藩镇割据", "节度使", "安史",
             "宪宗", "玄宗", "德宗", "代宗", "肃宗", "中晚唐"],
    "song": ["宋", "宋朝", "宋代", "北宋", "南宋", "路制", "路府", "澶渊"],
    "mingqing": ["明清", "明", "明朝", "明代", "清", "清朝", "清代",
                 "两京十三司", "行省", "南明", "大顺", "准噶尔"],
    "ww1-europe": ["一战", "第一次世界大战", "一次大战"],
    "ww2-europe": ["二战", "第二次世界大战", "二次大战"],
    "us-civil-war": ["美国内战", "南北战争", "美内战"],
    "deu-unification": ["德意志统一", "德国统一", "普鲁士", "俾斯麦", "普法"],
    "india-pakistan-partition": ["印巴分治", "印巴", "克什米尔"],
    "french-revolution": ["法国大革命", "法国革命", "大革命", "拿破仑"],
    "china-atlaspi": ["历史政体", "世界格局", "东亚格局", "真实边界", "历史边界"],
}

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}

# 少量「年号 → 公元元年」的确定映射。不求全，只覆盖最常被问到的；
# 其余交给大模型（历史换算恰好是它的强项）。
ERA_YEARS = {
    ("唐", "宪宗"): 806, ("唐", "玄宗"): 712, ("唐", "德宗"): 780,
    ("唐", "代宗"): 763, ("唐", "肃宗"): 756,
    ("宋", "太祖"): 960, ("宋", "徽宗"): 1101, ("宋", "钦宗"): 1126,
    ("宋", "高宗"): 1127,
    ("明", "太祖"): 1368, ("明", "成祖"): 1403, ("明", "神宗"): 1573,
    ("明", "思宗"): 1628,
    ("清", "世祖"): 1644, ("清", "圣祖"): 1662, ("清", "高宗"): 1736,
}


def cn_number(s: str):
    """「二」「十二」「三十五」→ 整数。只到千位，年份场景够用。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, cur, seen = 0, 0, False
    for ch in s:
        if ch in _CN_DIGIT:
            cur = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            total += (cur or 1) * _CN_UNIT[ch]
            cur = 0
            seen = True
        else:
            return None
    return total + cur if seen else None


def find_year(ask: str) -> tuple:
    """抽年份。返回 (年份, 依据)。年号走确定表 —— 模型在这上面答错过。"""
    m = re.search(r"(唐|宋|明|清)?\s*([太宗高宗宪德代肃顺徽钦神思世圣])?宗?\s*"
                  r"([一二两三四五六七八九十百千]+)\s*年", ask)
    if m:
        n = cn_number(m.group(3))
        if n:
            for (dyn, who), base in ERA_YEARS.items():
                if who in ask or (m.group(2) and who.startswith(m.group(2))):
                    return base + n - 1, f"年号换算：{who}{m.group(3)}年"
    for m in re.finditer(r"(?<!\d)(\d{3,4})(?!\d)", ask):
        y = int(m.group(1))
        if 0 < y <= 2100:
            return y, f"公元纪年 {y}"
    return None, ""


def match_scene(ask: str, scenes: list) -> tuple:
    """在已有题材里找。命中最长的关键词优先。"""
    have = {s["id"]: s for s in scenes}
    best, best_len, best_word = None, 0, ""
    for sid, words in ALIASES.items():
        if sid not in have:
            continue
        for w in words:
            if w and w in ask and len(w) > best_len:
                best, best_len, best_word = sid, len(w), w
    if best:
        return best, f"关键词「{best_word}」→ {best}"
    for sid, s in have.items():
        core = str(s.get("title") or "").replace(" ", "").split("·")[0].strip()
        if core and len(core) >= 2 and core in ask.replace(" ", ""):
            return sid, f"标题「{core}」"
    return None, ""


def recognize(ask: str, scenes: list) -> dict:
    """一句话能不能直接落到已有题材上。命中就不用叫模型。"""
    ask = (ask or "").strip()
    if not ask:
        return {"hit": False, "reason": "没说话", "need_llm": False}
    sid, how_scene = match_scene(ask, scenes)
    year, how_year = find_year(ask)
    if sid:
        s = next((x for x in scenes if x["id"] == sid), None) or {}
        dates = s.get("dates") or []

        def y_of(d):
            m = re.match(r"(\d+)", str(d))
            return int(m.group(1)) if m else None

        if year and dates:
            cand = [(abs((y_of(d) or 0) - year), d) for d in dates if y_of(d)]
            if cand:
                dist, d = min(cand)
                how = f"{how_scene}；{how_year}"
                if dist:
                    how += f"（没有 {year} 年，取最近的 {d[:4]} 年）"
                return {"hit": True, "scene": sid, "date": d, "how": how,
                        "exact_year": dist == 0}
        if dates:
            return {"hit": True, "scene": sid,
                    "date": s.get("default_date") or dates[0],
                    "how": how_scene + ("" if year else "；没提年份，用默认年份"),
                    "exact_year": False}

    # **关键词没命中时，用年份反查。**
    # 「给我出 1943 年的欧洲」这句话里没有"二战"两个字，按关键词匹配不到任何
    # 题材 —— 但 1943 落在 ww2-europe 的年份区间里。年份是个很强的信号。
    # **多个题材同时命中时取跨度最窄的那个**：1943 同时落在
    # ww2-europe(1938–1945) 和 china-atlaspi(100–1945) 里，
    # 显然 7 年跨度那个才是用户要的，1845 年跨度那个等于"什么都能对上"。
    if year:
        def y_of(d):
            m = re.match(r"(\d+)", str(d))
            return int(m.group(1)) if m else None

        by_year = []
        for s in scenes:
            ys = [y_of(d) for d in (s.get("dates") or [])]
            ys = [y for y in ys if y is not None]
            if not ys:
                continue
            if min(ys) - 2 <= year <= max(ys) + 2:
                by_year.append((max(ys) - min(ys), s))
        if by_year:
            by_year.sort(key=lambda x: x[0])
            span, s = by_year[0]
            dates = s.get("dates") or []
            cand = [(abs((y_of(d) or 0) - year), d) for d in dates if y_of(d)]
            if cand:
                dist, d = min(cand)
                extra = f"（取最近的 {d[:4]} 年）" if dist else ""
                return {"hit": True, "scene": s["id"], "date": d,
                        "how": f"没有关键词命中，但 {year} 年只落在「{s['id']}」"
                               f"（{min(y_of(x) for x in dates)}–"
                               f"{max(y_of(x) for x in dates)}）的区间里{extra}",
                        "exact_year": dist == 0}

    return {"hit": False, "need_llm": True,
            "reason": "认出了年份但没认出题材" if year else "没认出题材",
            "year": year, "how_year": how_year,
            "source": choose_source(ask)}


if __name__ == "__main__":
    import json
    import sys
    cases = sys.argv[1:] or [
        "唐宪宗二年的藩镇图",
        "明代九边",
        "唐朝和吐蕃的疆域对比",
        "1930 年的世界格局",
        "一战欧洲的国界",
        "中原大战各方势力范围",
        "给我出一张 1644 年的地图",
    ]
    for q in cases:
        r = choose_source(q)
        src = r["source"] or "（定不了 → 大模型）"
        print(f"  {q:<24} → {src:<14} {r['why'][:64]}")
