"""把题材数据写成提示词，交给豆包出图。**不做代码渲染。**

这是用户要的链路，只有四步：

    数据源  →  数据  →  写好提示词  →  豆包出图

为什么不是"代码画地图"：用户要的就是**模型出的图**。代码这边只负责
把数据整理成一段**能读懂的描述**（谁、在哪、什么年代、什么范围），
剩下的交给图像模型去画。

数据怎么进提示词：几何坐标没法直接写进文字（几千个点），但**政体名 +
大致方位 + 相对大小 + 年代**是可以的 —— 而这些恰好是模型需要的全部信息。
它不需要精确到公里的边界，它需要知道"这张图上该有哪几块、各在什么位置"。
"""
from __future__ import annotations

import json
import os

# 方位词：把经纬度变成人能读的方位。模型对"在西北"这种描述的理解
# 远好于对一串坐标。
_DIRS = [("西北", -1, 1), ("北", 0, 1), ("东北", 1, 1),
         ("西", -1, 0), ("中", 0, 0), ("东", 1, 0),
         ("西南", -1, -1), ("南", 0, -1), ("东南", 1, -1)]


def _dir_of(cx, cy, box) -> str:
    x0, y0, x1, y1 = box
    rx = (cx - x0) / max(1e-6, x1 - x0)
    ry = (cy - y0) / max(1e-6, y1 - y0)
    dx = -1 if rx < 0.38 else (1 if rx > 0.62 else 0)
    dy = -1 if ry < 0.38 else (1 if ry > 0.62 else 0)
    for name, ax, ay in _DIRS:
        if ax == dx and ay == dy:
            return name
    return "中"


# ── 敏感词替换 ────────────────────────────────────────────────
# **这是豆包一直不给出图的原因，找了很多轮才发现。**
# 用户的原话：「割据这个词是敏感词，修改为军事藩镇描述」。
# 中国大陆平台的内容审核对"割据/军阀/分裂"这类词极敏感 ——
# 它们读起来是"国家分裂"，于是直接回
# `InputImageSensitiveContentDetected` / `OutputImageSensitiveContentDetected`，
# 而且**跟图片尺寸、跟题材都无关**，只看文字。
# 我们自己的题材标题里就带「藩镇割据」，所以每条提示词都中招。
#
# 替换原则：**换成同时代的中性军政术语**，不改变史实含义。
# 唐的"藩镇"本身就是当时正式的军事编制（节度使领藩镇），
# 用"军事藩镇"既准确又不触发审核。
SENSITIVE = [
    ("藩镇割据", "唐代军事藩镇建制"),
    ("割据", "军事藩镇分驻"),
    ("军阀", "军事长官"),
    ("武装割据", "军事分驻"),
    ("地方割据", "地方军事分驻"),
    ("分裂", "分治"),
    ("内战", "内部战事"),
    ("叛乱", "兵变"),
    ("暴动", "民变"),      # 民变是史学术语，中性
    ("沦陷", "易手"),
    ("占领区", "控制区"),
    ("傀儡政权", "地方政权"),
    ("殖民", "海外领地"),
    ("起义", "起事"),      # 保留史实，换中性词
]


def sanitize(text: str) -> tuple:
    """把敏感词换成中性军政术语。返回 (新文本, 替换了哪些)。"""
    hit = []
    out = text
    for bad, good in SENSITIVE:
        if bad in out:
            out = out.replace(bad, good)
            hit.append(f"{bad}→{good}")
    return out, hit


# ── 年代 → 历史背景 ────────────────────────────────────────────
# **这是提示词里最要紧的一段，之前我漏了。**
# 用户实测：「强调唐朝古代背景是可以生成的」。原来我的提示词开头是
# 「A historical atlas plate showing …」，没有朝代、没有年代、没有地域 ——
# 模型根本没有"这是哪个时代哪个文明"的抓手，只能按通用古地图的模板糊一张。
# 把朝代/世纪/地域写清楚，模型才知道该调动哪一类视觉知识。
_ERAS = [
    ((618, 907), ("唐朝", "Tang Dynasty China"), "唐代",
     "ancient imperial China of the Tang Dynasty, 7th–10th century"),
    ((960, 1279), ("宋朝", "Song Dynasty China"), "宋代",
     "ancient imperial China of the Song Dynasty, 10th–13th century"),
    ((1271, 1368), ("元朝", "Yuan Dynasty China"), "元代",
     "imperial China under the Yuan (Mongol) dynasty, 13th–14th century"),
    ((1368, 1644), ("明朝", "Ming Dynasty China"), "明代",
     "imperial China of the Ming Dynasty, 14th–17th century"),
    ((1636, 1912), ("清朝", "Qing Dynasty China"), "清代",
     "imperial China of the Qing Dynasty, 17th–20th century"),
    ((1912, 1949), ("中华民国时期", "Republican-era China"), "民国",
     "Republican-era China, early 20th century"),
    ((-500, 618), ("上古至隋", "early imperial China"), "上古",
     "ancient China before the Tang, 1st millennium"),
]


def era_of(year: int) -> dict:
    """给年份配一段**历史背景**（朝代 + 世纪 + 地域）。

    模型要的是"这是哪个时代哪个文明"，不是"这是哪一年"。
    """
    for (a, b), (cn, en), tag, blurb in _ERAS:
        if a <= year <= b:
            century = (year - 1) // 100 + 1
            return {"cn": cn, "en": en, "tag": tag, "blurb": blurb,
                    "century": century,
                    "when": f"{century} 世纪（{year} 年）" if century <= 21
                            else f"{year} 年"}
    return {"cn": "", "en": "", "tag": "", "blurb": "", "century": 0,
            "when": f"{year} 年"}


# ── 经纬度 → 中国历史地理分区 ─────────────────────────────────
# **提示词里必须有真实地名，不能只有"中/东/东北"。**
# 用户的原话：「背景朝代还有地理位置这些你都没有，豆包怎么生成，靠猜么？」
# 对 —— 原来我只给方位词和面积百分比，模型只能猜。
# 这张表把经纬度翻译成中国历史上的地理分区名（并附今地名），
# 模型才知道"这一块是在关中、在河西、在岭南"。
_REGIONS = [
    ("关中（今陕西中部）", 105, 33, 111, 36.5),
    ("陕北", 107, 35.5, 111, 39),
    ("河东（今山西）", 110, 34, 114, 40.5),
    ("河北（今河北）", 113, 36, 120, 42.5),
    ("河南（今河南）", 110, 32, 117, 36),
    ("山东（今山东）", 114.5, 34, 123, 38.5),
    ("淮北（今皖北苏北）", 114, 32.5, 121, 34.5),
    ("江淮（今江苏安徽）", 115.5, 30, 122, 33),
    ("江南（今苏南浙北）", 117, 29, 122, 32),
    ("两浙（今浙江）", 117.5, 27, 123, 30.5),
    ("江西（今江西）", 113, 24, 118.5, 30),
    ("荆湖（今湖北湖南）", 108, 24, 117, 32.5),
    ("四川（今四川重庆）", 101, 26, 110, 33.5),
    ("汉中", 104.5, 31.5, 109.5, 34),
    ("陇右（今甘肃东部）", 101, 33, 107, 37.5),
    ("河西（今甘肃西部）", 93, 36, 104, 43),
    ("朔方（今宁夏内蒙）", 103, 36.5, 113, 43),
    ("西域（今新疆）", 73, 34, 96, 49),
    ("青藏（今青海西藏）", 78, 26, 103, 39),
    ("云南（今云南）", 97, 21, 106, 29),
    ("贵州", 103, 24, 110, 29.5),
    ("岭南（今广东广西）", 104, 20, 117, 25.5),
    ("福建（今福建）", 115.5, 23, 120.5, 28.5),
    ("台湾", 119.5, 21.5, 122.5, 25.5),
    ("辽东（今辽宁）", 118, 38, 126, 43.5),
    ("松漠（今内蒙东部）", 117, 41, 126, 50),
    ("朝鲜半岛", 124, 33, 130, 43),
    ("日本", 129, 30, 146, 46),
    ("中南半岛", 92, 5, 110, 24),
    ("印度", 68, 8, 92, 32),
    ("中亚", 46, 35, 75, 55),
    ("蒙古高原", 87, 41, 122, 52),
    ("西伯利亚", 60, 50, 180, 75),
]


def where(x0: float, y0: float, x1: float, y1: float, max_n: int = 3,
          min_share: float = 0.08) -> list:
    """区域的**外接框** → 它主要落在哪些地理分区里。

    第一版用"中心点 ± 固定 6°×4°"，结果大区域全错：嶺南（华南）被算成
    "岭南 + 中南半岛 + 荆湖"，浙西（浙江）被算成"荆湖 + 江西 + 江淮" ——
    因为中心点加固定窗口对几千公里宽的区域毫无意义。
    用户对提示词的第一条不满就是"地理位置你都没有"。

    现在按**实际重叠面积占该区域的比例**排序，小于 8% 的丢掉 ——
    只有真正占了相当篇幅的分区才会写进提示词。
    """
    area = max(1e-6, (x1 - x0) * (y1 - y0))
    hits = []
    for name, ax0, ay0, ax1, ay1 in _REGIONS:
        ox = min(x1, ax1) - max(x0, ax0)
        oy = min(y1, ay1) - max(y0, ay0)
        if ox > 0 and oy > 0:
            hits.append((ox * oy / area, name))
    hits.sort(reverse=True)
    out = [n for share, n in hits if share >= min_share][:max_n]
    return out


def describe(topic, year: int, top: int = 10) -> dict:
    """把某个题材某一年的数据整理成**给模型看的描述**。

    返回 {"title", "year", "regions":[{name, dir, share}], "summary", "prompt"}
    """
    raw = topic.raw
    kind = topic.kind
    regions = []

    if kind == "atlaspi":
        # AtlasPI：真实历史政体边界
        import sys as _sys
        _core = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "packages", "core")
        if _core not in _sys.path:
            _sys.path.insert(0, _core)
        from histmap_core.datasets.atlaspi import AtlasPIAdapter
        gj = AtlasPIAdapter().fetch_year(int(year))
        focus = raw.get("focus") or raw.get("bbox") or [70, 15, 140, 55]
        minc = float(raw.get("min_confidence", 0.65) or 0.65)
        for f in gj.get("features") or []:
            p = f.get("properties") or {}
            nm = str(p.get("name_original") or p.get("name") or "").strip()
            if not nm or float(p.get("confidence_score") or 0) < minc:
                continue
            rings = []
            g = f.get("geometry") or {}
            cs = g.get("coordinates") or []
            if g.get("type") == "Polygon":
                cs = [cs]
            for poly in cs:
                for ring in poly:
                    rings.append(ring)
            xs = [q[0] for r in rings for q in r]
            ys = [q[1] for r in rings for q in r]
            if not xs:
                continue
            cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
            if not (focus[0] <= cx <= focus[2] and focus[1] <= cy <= focus[3]):
                continue
            regions.append({"name": nm, "cx": cx, "cy": cy,
                            "bbox": [min(xs), min(ys), max(xs), max(ys)],
                            "size": (max(xs) - min(xs)) * (max(ys) - min(ys)),
                            "conf": float(p.get("confidence_score") or 0)})
        box = focus
    else:
        # dynasty：控制表里的归属
        ctrl = topic.control_path()
        if not ctrl:
            return {"error": "这个题材没有控制表数据"}
        data = json.load(open(ctrl, encoding="utf-8"))
        row = data.get(str(year)) or data.get(year) or {}
        allx, ally = [], []
        # 从已构建的几何里取每个区域的位置和大小
        gjp = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data", "processed",
            f"{topic.id}_{year}_map.geojson")
        if os.path.exists(gjp):
            geo = json.load(open(gjp, encoding="utf-8"))
            for f in geo.get("features") or []:
                pr = f.get("properties") or {}
                nm = pr.get("name") or pr.get("owner") or ""
                rings = []
                g = f.get("geometry") or {}
                cs = g.get("coordinates") or []
                if g.get("type") == "Polygon":
                    cs = [cs]
                for poly in cs:
                    for ring in poly:
                        rings.append(ring)
                xs = [q[0] for r in rings for q in r]
                ys = [q[1] for r in rings for q in r]
                if not xs:
                    continue
                allx += [min(xs), max(xs)]
                ally += [min(ys), max(ys)]
                regions.append({"name": nm, "cx": (min(xs) + max(xs)) / 2,
                                "cy": (min(ys) + max(ys)) / 2,
                                "bbox": [min(xs), min(ys), max(xs), max(ys)],
                                "size": (max(xs) - min(xs)) * (max(ys) - min(ys)),
                                "conf": 1.0})
        box = raw.get("bbox") or [min(allx or [70]), min(ally or [15]),
                                  max(allx or [140]), max(ally or [55])]

    # 过滤兜底块（"其他（未载）"不是政治实体，不该进提示词）
    fill = str(raw.get("unassigned_label") or "其他（未载）")
    regions = [r for r in regions if r["name"] != fill]
    # **画不出的名字也不要进提示词。** 让模型"写这些地名"等于请它画方框 ——
    # 阿拉伯文/蒙古文/天城文在中文字体里没有字形（回鹘 `ئۇيغۇر خانلىقى`、
    # 准噶尔 `ᠵᠡᠭᠦᠨᠭᠠᠷ`）。宁可少写一个名字。
    try:
        import sys as _sys
        _core = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "packages", "core")
        if _core not in _sys.path:
            _sys.path.insert(0, _core)
        from histmap_core.fonts import drawable_text
        dropped = [r["name"] for r in regions if not drawable_text(r["name"])]
        regions = [r for r in regions if drawable_text(r["name"])]
    except Exception:
        dropped = []
    regions.sort(key=lambda r: -r["size"])
    total = sum(r["size"] for r in regions) or 1.0
    for r in regions:
        r["dir"] = _dir_of(r["cx"], r["cy"], box)
        r["share"] = round(100 * r["size"] / total)
    regions = regions[:top]

    title = raw.get("title") or topic.id
    # **标题也要过一遍敏感词。** 我们自己起的题材名就带「藩镇割据」，
    # 直接写进提示词就会触发审核（实测每条都拦）。
    title_safe, title_hits = sanitize(title)
    era = era_of(int(year))
    for r in regions:
        bb = r.get("bbox")
        r["where"] = (where(*bb) if bb
                      else where(r["cx"] - 3, r["cy"] - 2,
                                 r["cx"] + 3, r["cy"] + 2))

    lines = []
    for r in regions:
        w = "、".join(r["where"]) if r["where"] else f"图面{r['dir']}部"
        lines.append(f"- {r['name']}：控制 {w}（约占 {r['share']}%）")
    summary = (f"{era['when']}，{era['cn']}。{title}。"
               + ("图上各势力：\n" + "\n".join(lines) if regions else ""))

    # ── 提示词 ────────────────────────────────────────────────
    # 三段必须有，缺一段模型就只能猜（用户实测过"强调唐朝古代背景能生成"）：
    #   ① 时代背景：哪个朝代、哪个世纪、哪个文明
    #   ② 地理位置：每个势力**控制哪些真实地理区域**（不是"中/东"这种废话）
    #   ③ 风格：古地图集的质感
    loc_en = "; ".join(
        f"{r['name']} controls " +
        (", ".join(r["where"]) if r["where"] else f"the {r['dir']} part")
        for r in regions)
    # **每个势力的真实坐标。**
    # 用户的要求：「提示词不够详细，没有定位坐标信息」。
    # 只写"唐控制四川"太虚 —— 模型需要一个能落地的锚点。
    # 给两样：中心点（定位）+ 经纬度范围（大小与形状方向）。
    coord_lines = []
    for r in regions:
        bb = r.get("bbox")
        if bb and len(bb) == 4:
            x0, y0, x1, y1 = bb
            coord_lines.append(
                f"{r['name']} centred near {r['cx']:.1f}E {r['cy']:.1f}N, "
                f"spanning {x0:.0f}-{x1:.0f}E / {y0:.0f}-{y1:.0f}N")
        else:
            coord_lines.append(
                f"{r['name']} centred near {r['cx']:.1f}E {r['cy']:.1f}N")
    prompt = (
        f"An antique Chinese historical atlas plate of {era['blurb']}, "
        f"showing the political situation in the year {year} AD. "
        f"This is {era['cn']}，{title_safe}。 "
        f"Geography — each power and the areas it controls: " + loc_en + ". "
        f"Coordinates — " + "; ".join(coord_lines) + ". "
        f"Redraw the provided reference map: keep every region's shape and "
        f"position exactly as in it, and place each region at the coordinates "
        f"listed above. "
        f"Style: aged ivory xuan paper with subtle foxing, fine engraved "
        f"hatching and hand-drawn mountain relief, muted earthy "
        f"low-saturation colours, thin dark ink outlines, a small engraved "
        f"vignette in one corner, printed like a 19th-century atlas plate. "
        f"Write these Chinese place names in their own areas: "
        + "、".join(r["name"] for r in regions) + "."
    )
    prompt, prompt_hits = sanitize(prompt)
    return {"topic": topic.id, "title": title, "title_safe": title_safe,
            "year": int(year),
            "regions": regions, "summary": summary, "prompt": prompt,
            "era": era,
            "sanitized": title_hits + prompt_hits,
            "dropped_unrenderable": dropped}
