#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通用朝代地图构建器 · 单元 → 县合并
========================================
中国古代断代（唐、宋、元、明、清…）**没有**许可证干净的现成边界数据，
几何必须从史料重建。这个脚本把那套重建方法**抽成通用的**：

    单元坐标表（单元名 → 今地名 + 经纬度）
        +  控制表（年份 → 单元 → 归属方）
        +  现代县多边形（geoBoundaries）
        ↓
    每个县归到最近的**当年存在的**单元
        ↓
    按归属方合并单元 → 政权/藩镇的面
        ↓
    data/processed/<topic>_<year>_map.geojson

**支持两种控制表格式**（不同史料整理出来的结构不一样）：
  按归属方分组：{ "807": { "魏博": ["魏","博","相",...], "成德": [...] } }
  按单元列表：  { "807": { "魏": "魏博", "博": "魏博", "冀": "成德", ... } }

**加一个新朝代要做什么**：写一份单元坐标表 + 一份控制表，加一条 topic 记录。
零 Python。坐标表用 `check_gazetteer.py` 做逐点几何校验。

用法
    python src/build_dynasty_map.py --topic song --year 1080
    python src/build_dynasty_map.py --topic tang --year 807 --force
"""
import argparse
import hashlib
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from shapely.geometry import shape, mapping
from shapely.ops import unary_union

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE,
          os.path.join(ROOT, "packages", "core"),
          os.path.join(ROOT, "packages", "server")):
    if p not in sys.path:
        sys.path.insert(0, p)

PROC = os.path.join(ROOT, "data", "processed")
CACHE = os.path.join(ROOT, "data", "cache")

COUNTY_FILES = [("CHN", os.path.join(CACHE, "gb_chn_adm2.geojson")),
                ("VNM", os.path.join(CACHE, "gb_vnm_adm2.geojson"))]

# 配色：按归属方名稳定分配，保证跨年份同一个政权颜色不变
PALETTE = [
    "#b5675a", "#7c8fa8", "#8a9a6b", "#b08a5c", "#9a7ba8", "#6fa39a",
    "#c08a7a", "#8a8fb0", "#a89a5c", "#7fa08a", "#b07a92", "#6b8ba8",
    "#a08a6b", "#8fa87c", "#b59a7a", "#7c9ab0", "#ab7f6b", "#96a8b0",
    "#c0a05c", "#8a7ca8", "#6f9a7c", "#b09a8a", "#7a8fa0", "#a8945c",
    "#d08a6a", "#6a9ab0", "#9ab06a", "#b06a8a", "#8a9ab0", "#a0b08a",
]


def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def load_counties():
    out = []
    for iso, path in COUNTY_FILES:
        if not os.path.exists(path):
            continue
        g = json.load(open(path, encoding="utf-8"))
        for f in g["features"]:
            try:
                geom = shape(f["geometry"])
            except Exception:
                continue
            if geom.is_empty:
                continue
            if not geom.is_valid:
                geom = geom.buffer(0)
            if geom.is_empty:
                continue
            c = geom.centroid
            out.append({"name": f["properties"].get("shapeName", "?"),
                        "geom": geom, "cx": c.x, "cy": c.y})
    return out


def load_units(path: str):
    """单元坐标表。支持 {points:[...]} 与顶层数组两种。"""
    d = json.load(open(path, encoding="utf-8"))
    pts = d.get("points") if isinstance(d, dict) else d
    out = []
    for p in pts or []:
        try:
            out.append({"name": str(p["name"]), "lon": float(p["lon"]),
                        "lat": float(p["lat"]), "modern": p.get("modern", "")})
        except Exception:
            continue
    return out


def parse_owner_table(raw: dict, year: int):
    """把控制表的一行统一成 {单元: 归属方}。

    三种格式都吃（史料整理出来的结构本来就不统一）：
      {"魏博": ["魏","博"]}                    按归属方分组（扁平列表）
      {"魏博": {"zhou": ["魏","博"], ...}}     按归属方分组（带元信息的嵌套）
      {"魏": "魏博", "博": "魏博"}              按单元列表
    """
    row = raw.get(str(year)) or raw.get(year)
    if not row:
        return None
    out = {}
    for owner, units in row.items():
        if str(owner).startswith("_"):
            continue
        if isinstance(units, dict):
            # 嵌套格式：取里面的单元列表字段
            us = (units.get("units") or units.get("zhou")
                  or units.get("lus") or units.get("members") or [])
            for u in us:
                out[str(u)] = str(owner)
        elif isinstance(units, (list, tuple)):
            for u in units:
                out[str(u)] = str(owner)
        elif isinstance(units, str):
            out[str(units)] = str(owner)
    return out


def _norm(s: str) -> str:
    """州名归一到简体。

    史料来自古籍（**繁体**：劍、勝、儀、臺），而坐标表按今地名写成简体。
    不归一就有上百个单元匹配不上（唐图踩过）。"""
    try:
        import zhconv
        return zhconv.convert(str(s), "zh-cn")
    except ImportError:
        return str(s)


def all_owners(raw: dict) -> list:
    """控制表里**所有年份**出现过的归属方。

    颜色必须按全体分配，不能只按当年：某一年多冒出一个藩镇，若按当年分配，
    其余藩镇会整体换色 —— 跨年份同一个政权颜色就变了，观众看到的是「换了人」。
    """
    names = []
    for k in raw:
        if str(k).startswith("_"):
            continue
        row = parse_owner_table(raw, int(k) if str(k).isdigit() else k)
        if not row:
            continue
        for o in row.values():
            if o and o not in names:
                names.append(o)
    return names


# ── 配色：按分类个数现算，而不是从固定调色板里取模 ──────────────
# 踩过的坑：早先是 md5(名字) % 30，从 30 色调色板取一个。唐 807 有 **37 个藩镇**，
# 结果只用到 21 种颜色 —— **28 个藩镇和别的藩镇同色**（幽州/朔方/江東 一模一样）。
# 地图上同色就等于「同一个政权」，这是**图在说谎**；而图例又上限 12 条，把大部分
# 颜色藏起来了。任何图像模型都救不了这种错 —— 它只会把这张错的图渲染得很漂亮。
#
# 正确做法：在 OKLab（感知均匀）里按**黄金角**铺色相，明度分 6 档错开。
# 黄金角逐次落点是低差异序列，让 N 个分类的色相间隔尽量均匀；明度分档负责
# 把色相撞在一起的几个再拉开。这两者的档数必须让它实测最优 ——
# 试过 3/4/5/6/7/9 档明度与「分层均匀铺色相」，42 个分类下最小感知距离：
#     3档 0.0120 · 4档 0.0314 · 5档 0.0485 · **6档 0.0510** · 7档 0.0049
# 7 档反而崩掉（黄金角与档数共振，存在色相几乎相同又在同一档的配对）。
# 最终取「明度 0.80–0.45 分 6 档 + 固定彩度」，实测 3/5/8/12/20/30/42 个
# 分类都能稳定在 ≥0.0687，是从 0.0000（同色）到 0.0687 的量级提升。
# 彩度**不**随序号变化：加了彩度循环后最小距离反而从 0.0120 掉到 0.0070。
_GOLDEN = 137.508
_L_HI, _L_LO, _L_TIERS = 0.80, 0.45, 6
_CHROMA = 0.09
_COLOR_CACHE: dict = {}


def _oklch_hex(i: int, n: int) -> str:
    """第 i 个（共 n 个）分类的颜色。OKLCh 取色相 + 明度分档。"""
    import math
    from style_from_image import _oklab_to_srgb, _srgb_to_oklab, _rgb01_to_hex

    L = _L_HI - (_L_HI - _L_LO) * (i % _L_TIERS) / (_L_TIERS - 1)
    h = math.radians((i * _GOLDEN) % 360.0)
    C = _CHROMA
    rgb = (0.5, 0.5, 0.5)
    for _ in range(18):
        lab = (L, C * math.cos(h), C * math.sin(h))
        rgb = _oklab_to_srgb(lab)
        # 出了 sRGB 色域就会被裁，裁完色相就跑了 —— 降彩度重试，别硬裁
        back = _srgb_to_oklab(rgb)
        if sum((x - y) ** 2 for x, y in zip(lab, back)) < 1e-4:
            break
        C *= 0.8
    return _rgb01_to_hex(rgb)


def topic_palette(names) -> dict:
    """一组归属方 → {名字: 颜色}。确定性，同题材跨年份一致。

    名字先排序，所以分配**只取决于这一组名字**：换台机器、换 Python 版本、
    重新构建，颜色都一样。
    """
    key = tuple(sorted({str(n) for n in names if n}))
    got = _COLOR_CACHE.get(key)
    if got is None:
        got = {nm: _oklch_hex(i, len(key)) for i, nm in enumerate(key)}
        _COLOR_CACHE[key] = got
    return got


def stable_color(name: str, all_names: list = ()) -> str:
    """单个名字的颜色。走 topic_palette，保证与全体分配是同一套。"""
    names = list(all_names)
    if name not in names:
        names.append(name)
    return topic_palette(names).get(name) or _oklch_hex(0, 1)


def palette_collisions(names) -> dict:
    """{颜色: [同色的名字]}，只留撞色的。用来在构建时把问题报出来。"""
    inv: dict = {}
    for nm, c in topic_palette(names).items():
        inv.setdefault(c, []).append(nm)
    return {c: v for c, v in inv.items() if len(v) > 1}


# 模型给的配色，两个颜色靠得比这个还近就认为「图上分不出来」。
# 阈值是**量出来的**，不是拍的。实测各题材现有手工配色（绝大多数是模型起草时
# 给的）的最小感知距离：
#     明/南明那类       0.0966      良好
#     印巴/孟加拉国      0.0881
#     联邦/邦联         0.0803
#     普鲁士/奥地利      0.0722
#     宋/辽/西夏/金      0.0556      偏挤但还能分
#     两个政权同色       0.0000      完全分不出（这是要拦的）
# 0.045 落在「偏挤但能分」和「真的分不出」之间，不会误伤现有数据。
MIN_PALETTE_GAP = 0.045


def validate_palette(pal: dict, owners: list) -> tuple[dict, list[str]]:
    """挑出**真正能用**的模型配色，并报告它有什么毛病。

    为什么要验：`_palette` 是模型起草题材时给的，而它在赋值处是
    `color = pal.get(own) or stable_color(...)` —— **模型给的颜色会直接盖掉
    自动配色**。也就是说我做的「按分类数算最大感知分离」在有色板时完全绕过。
    模型给的色板不受任何约束，于是可能出现：

      · 两个政权拿到几乎一样的颜色（图上分不出谁是谁）—— 这是最常见的
      · 同一个颜色给两个不同政权（撞色）
      · 值根本不是颜色：`_palette` 里混进注释，比如
        ww2-europe 的 `_divided_note = "被多国瓜分/分区占领……"`
        （实测存在，会让任何遍历色板的代码崩在 int(x, 16) 上）
      · 嵌套结构（二战那种 `{政权: {状态: 颜色}}`）—— 这套格式只给
        boundary 类用，dynasty 类拿到会是一个 dict 而不是字符串

    策略：只要有一项不过，就**整份丢掉**，改用自动配色，并把原因报出来。
    不用「挑好的、补坏的」—— 那会得到一半模型色一半机器色，更难看也难解释。
    """
    issues: list[str] = []
    clean: dict = {}
    for k, v in (pal or {}).items():
        if str(k).startswith("_"):          # 注释键，不是颜色
            continue
        if isinstance(v, dict):
            issues.append(f"「{k}」的值是嵌套字典而不是颜色（那种格式只给 boundary 类用）")
            continue
        if not isinstance(v, str):
            issues.append(f"「{k}」的值不是字符串：{type(v).__name__}")
            continue
        h = v.strip()
        if not (len(h) == 7 and h.startswith("#")):
            issues.append(f"「{k}」不是 #rrggbb 格式：{h[:40]!r}")
            continue
        try:
            int(h[1:], 16)
        except ValueError:
            issues.append(f"「{k}」不是合法十六进制：{h!r}")
            continue
        clean[k] = h.lower()

    # 撞色 / 太近
    try:
        from style_from_image import _hex_to_rgb01, _srgb_to_oklab
        labs = {k: _srgb_to_oklab(_hex_to_rgb01(v)) for k, v in clean.items()}
        inv: dict = {}
        for k, v in clean.items():
            inv.setdefault(v, []).append(k)
        for c, ks in inv.items():
            if len(ks) > 1:
                issues.append(f"{'、'.join(ks)} 是同一个颜色 {c}")
        keys = sorted(labs)
        worst = None
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                d = sum((x - y) ** 2 for x, y in zip(labs[a], labs[b])) ** 0.5
                if worst is None or d < worst[0]:
                    worst = (d, a, b)
        if worst and worst[0] < MIN_PALETTE_GAP:
            issues.append(f"「{worst[1]}」与「{worst[2]}」的颜色太接近"
                          f"（感知距离 {worst[0]:.3f} < {MIN_PALETTE_GAP}），图上分不出来")
    except Exception as e:
        issues.append(f"配色校验本身失败：{type(e).__name__}: {e}")

    if issues:
        return {}, issues
    return clean, []


def build(topic_id: str, year: int, max_km: float | None = None,
          simplify: float = 0.02, force: bool = False, quiet: bool = False):
    from histmap_server import topics as T

    t = T.get(topic_id)
    if not t:
        raise SystemExit(f"没有这个题材: {topic_id}")
    if t.kind != "dynasty":
        raise SystemExit(f"题材 {topic_id} 不是 dynasty 类（kind={t.kind}）")

    # 归并半径由**题材数据**决定，不写死：唐宋的州治密（约 150 km 一个），
    # 260 km 够用；明清的省治能隔上千公里，260 km 会把中间地带全丢掉，
    # 结果新疆、西藏各飘一块孤岛（实测）。
    if max_km is None:
        max_km = float(t.raw.get("max_km") or 260.0)

    dst = os.path.join(PROC, f"{topic_id}_{year}_map.geojson")
    if os.path.exists(dst) and not force:
        if not quiet:
            print(f"已存在 {dst}（--force 可重建）")
        return dst

    # 两种几何来源：
    #   units      —— 坐标表（州治经纬度）→ 把现代县就近归并成单元的面（唐、宋、明清）
    #   units_file —— 现成多边形表，单元本来就是一个个面（美国内战：州就是单元）
    # 下游完全一样：按归属方合并 → 上色 → 写 geojson。所以两条来源在这里分叉一次就够。
    units_path = t.raw.get("units")
    units_file = t.raw.get("units_file")
    if not units_path and not units_file:
        raise SystemExit(f"题材 {topic_id} 既没声明 units 也没声明 units_file")
    up = None
    if units_path:
        up = units_path if os.path.isabs(units_path) else os.path.join(PROC, units_path)
        if not os.path.exists(up):
            raise SystemExit(f"找不到单元表: {up}")
    uf = None
    if units_file:
        uf = units_file if os.path.isabs(units_file) else os.path.join(PROC, units_file)
        if not os.path.exists(uf):
            raise SystemExit(f"找不到单元多边形表: {uf}")
    ctrl = t.control_path()
    if not ctrl:
        raise SystemExit(f"题材 {topic_id} 没声明 control 文件")

    raw = json.load(open(ctrl, encoding="utf-8"))
    owner = parse_owner_table(raw, year)
    if not owner:
        raise SystemExit(f"控制表里没有 {year} 年")

    if uf:
        # 现成多边形：跳过坐标表与县归并，直接把面按归属方分组
        return build_from_polygons(t, year, uf, raw, owner, dst, force,
                                   simplify, quiet)

    units = load_units(up)

    if not quiet:
        print(f"=== {t.title} · {year} 年 ===")
        print(f"[1] 单元坐标 {len(units)} 条；控制表给出 {len(owner)} 个单元")
        print(f"    归属方 {len(set(owner.values()))} 个")

    # 只保留今年有归属的单元，且必须在坐标表里（两边都归一到简体再比）
    # 一个单元可以有**多个锚点**：新疆一个乌鲁木齐代表不了 166 万平方公里。
    # 靠放大归并半径去够剩下的地方，会把明朝也一起放大到吞掉整个中国（实测）；
    # 正解是给大省多写几个治所，判据仍是「离该单元任一锚点最近」。
    by_name: dict = {}
    for u in units:
        by_name.setdefault(_norm(u["name"]), []).append((u["lon"], u["lat"]))
    active = {_norm(n): o for n, o in owner.items() if _norm(n) in by_name}
    missing = [n for n in owner if _norm(n) not in by_name]
    if not quiet:
        multi = sum(1 for v in by_name.values() if len(v) > 1)
        print(f"[2] 可定位单元 {len(active)} 个（其中 {multi} 个有多个锚点）；"
              f"缺坐标 {len(missing)} 个"
              + (f": {' '.join(sorted(missing)[:15])}" if missing else ""))

    # 归属方显示名（方镇表列名用初名，正文用后来的号）
    disp = raw.get("_display") or {}
    # 配色覆盖：同色系政权靠调色板凑不出稳定区分时，直接在数据里点名要什么色。
    # 例：北宋/南宋要同色（同一政权延续），金要和西夏的灰蓝拉开。
    # **但模型给的要先验**：它在赋值处会直接盖掉自动配色（见 validate_palette 的说明）
    pal, pal_issues = validate_palette(raw.get("_palette") or {}, [])
    if pal_issues and not quiet:
        print(f"    ！控制表里给的配色不能用，已改用自动配色：")
        for s in pal_issues:
            print(f"        {s}")
    # 换色键：让「南宋」沿用「宋」的颜色，颜色跟政权走而不是跟名字走
    ckey = raw.get("_color_key") or {}
    # 自动配色按**全题材全年龄**的归属方一次性分配，跨年份才不会换色
    owners_all = all_owners(raw)

    counties = load_counties()
    if not quiet:
        print(f"[3] 县多边形 {len(counties)} 个")

    # ── 县 → 单元（只在当年存在的单元里选最近；多锚点取最近的那个锚） ──
    cand = [(n, by_name[n]) for n in active]
    assigned, far = {}, 0
    for c in counties:
        best, bd = None, 1e18
        for n, pts in cand:
            d = min(haversine_km(c["cx"], c["cy"], lo, la) for lo, la in pts)
            if d < bd:
                bd, best = d, n
        if bd > max_km:
            far += 1
            continue
        assigned.setdefault(best, []).append(c)
    placed = sum(len(v) for v in assigned.values())
    if not quiet:
        print(f"[4] 归属：{placed} 个县 -> {len(assigned)} 个单元；"
              f"超出 {max_km:.0f} km 判为疆域外 {far} 个")
        no_c = [n for n in active if n not in assigned]
        if no_c:
            print(f"    今年有归属但没分到县的单元 {len(no_c)} 个: "
                  f"{' '.join(sorted(no_c)[:20])}")

    # ── 单元 → 归属方（合并） ──
    # merge_by 决定输出粒度：
    #   "owner" 把同属一方的单元并成一块 → 疆域图（唐藩镇就属这类）
    #   "unit"  保留每个单元独立成面，只是**颜色按归属方**上 →
    #           行政区划图（宋的路制要的就是这个：看得到路界，又分得清朝代）
    merge_by = t.raw.get("merge_by", "owner")

    groups = {}
    for unit, cs in assigned.items():
        try:
            g = unary_union([c["geom"] for c in cs])
        except Exception:
            continue
        key = unit if merge_by == "unit" else active[unit]
        groups.setdefault(key, []).append(g)

    feats, summary = [], []
    for grp, geoms in groups.items():
        try:
            merged = unary_union(geoms)
        except Exception:
            continue
        if merged.is_empty:
            continue
        if simplify > 0:
            merged = merged.simplify(simplify, preserve_topology=True)
        c = merged.centroid
        if merge_by == "unit":
            us = [grp]
            name = disp.get(grp, grp)
            own = active.get(grp, grp)
            color = pal.get(own) or stable_color(ckey.get(own, own), owners_all)
        else:
            us = sorted(u for u, o in active.items() if o == grp)
            name = disp.get(grp, grp)
            color = pal.get(grp) or stable_color(ckey.get(grp, grp), owners_all)
        feats.append({
            "type": "Feature",
            "properties": {"id": name, "name": name, "color": color,
                           "owner": active.get(grp, grp) if merge_by == "unit" else grp,
                           "units": us, "label_lon": round(c.x, 3),
                           "label_lat": round(c.y, 3)},
            "geometry": mapping(merged)})
        summary.append((name, len(us), merged.area))

    out = {"type": "FeatureCollection",
           "_meta": {"topic": topic_id, "year": year,
                     "method": t.source_note or
                               ("辖境据史料单元隶属关系重建；几何以现代县界为底、"
                                "按单元治所邻近度归属，**非史料记载的实际界线**"),
                     "max_km": max_km,
                     "units_total": len(units), "units_matched": len(active),
                     "units_missing": missing,
                     "counties_placed": placed, "counties_outside": far,
                     # 记下当时的输入指纹：控制表或坐标表改了但没重跑构建时，
                     # 服务端能据此发现「图是旧的」并提醒，而不是让人对着旧图找 bug。
                     "control_sha1": sha1_of(ctrl),
                     "gazetteer_sha1": sha1_of(up),
                     "built_from": {"control": os.path.basename(ctrl),
                                    "units": os.path.basename(up)}},
           "features": feats}
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
    if not quiet:
        print(f"[5] -> {dst}  ({os.path.getsize(dst)/1024:.0f} KB)")
        print(f"\n{year} 年归属方（按面积）:")
        for g, n, a in sorted(summary, key=lambda x: -x[2])[:12]:
            print(f"    {g:12s} {n:3d} 单元")
        # 撞色 = 图上两个政权长得一样，观众会读成一家。自动配色本身不会撞，
        # 但控制表里的 _palette 覆盖（比如刻意让南宋沿用宋的颜色）可能撞，
        # 所以还是报出来 —— 是不是故意的，只有写数据的人知道。
        got = [(f["properties"]["name"], f["properties"]["color"]) for f in feats]
        inv: dict = {}
        for nm, c in got:
            inv.setdefault(c, []).append(nm)
        same = {c: v for c, v in inv.items() if len(v) > 1}
        if same:
            print(f"    注意：{len(same)} 种颜色被多个归属方共用（是否故意？）")
            for c, v in list(same.items())[:6]:
                print(f"        {c} <- {'、'.join(v)}")
    return dst


def refit_topic_bbox(topic_id: str, quiet: bool = True) -> list | None:
    """几何一变，取景框就得跟着重算。

    这是个**必须自动化**的环节：bbox 是几何的外接框，改了 max_km、加了锚点、
    改了控制表，范围就变了。早先是靠人记得手动跑 fit_bbox —— 结果改完明清的
    多锚点后忘了跑，自查时才发现画面被裁掉 130%。

    只在**声明年份的几何全部齐了**才写回：fit_bbox 会跳过缺失年份，
    拿一年的几何去拟合会把框收得过紧，反而把别的年份裁掉。
    """
    try:
        import fit_bbox
    except Exception:
        return None
    p = os.path.join(ROOT, "data", "topics", "topics.json")
    doc = json.load(open(p, encoding="utf-8"))
    for t in doc.get("topics", []):
        if t.get("id") != topic_id or t.get("kind") == "boundary":
            continue
        years = [int(y) for y in (t.get("years") or [])]
        if not years:
            return None
        missing = [y for y in years
                   if not os.path.exists(os.path.join(PROC, f"{topic_id}_{y}_map.geojson"))]
        if missing:
            if not quiet:
                print(f"[6] 取景框暂不更新：{missing} 还没建")
            return None
        old = t.get("bbox")
        new = fit_bbox.fit(topic_id, years)
        if new and old != new:
            t["bbox"] = new
            json.dump(doc, open(p, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            if not quiet:
                print(f"[6] 取景框跟着几何更新 {old} -> {new}")
        elif not quiet:
            print(f"[6] 取景框核对无误 {new}")
        return new
    return None


def sha1_of(path: str) -> str:
    """JSON 的**语义**指纹：重排键序、改缩进、动换行都不算变化。

    和 topics._file_sha1 必须一致，否则服务端会把刚算好的图当成过期。
    """
    obj = json.load(open(path, encoding="utf-8"))
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(canon).hexdigest()[:16]


def build_from_polygons(t, year: int, uf: str, raw: dict, owner: dict,
                        dst: str, force: bool, simplify: float, quiet: bool):
    """单元本来就是多边形的那种题材（美国内战：州即单元）。

    不经过「县 → 最近单元」这一步 —— 现成的面比就近归并更准，
    再走一遍只会把边界啃坏。
    """
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union
    import hashlib
    import json as _json
    import os as _os

    gj = _json.load(open(uf, encoding="utf-8"))
    disp = raw.get("_display") or {}
    pal, pal_issues = validate_palette(raw.get("_palette") or {}, [])
    if pal_issues and not quiet:
        print("    ！控制表里给的配色不能用，已改用自动配色：")
        for s in pal_issues:
            print(f"        {s}")
    ckey = raw.get("_color_key") or {}
    owners_all = all_owners(raw)
    merge_by = t.raw.get("merge_by", "owner")

    by_name = {}
    for f in gj["features"]:
        nm = str((f.get("properties") or {}).get("name") or "").strip()
        if not nm:
            continue
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        if not g.is_empty:
            by_name[nm] = g

    active = {n: o for n, o in owner.items() if n in by_name}
    missing = [n for n in owner if n not in by_name]
    if not quiet:
        print(f"=== {t.title} · {year} 年 ===")
        print(f"[1] 多边形单元 {len(by_name)} 个；控制表给出 {len(owner)} 个")
        print(f"[2] 命中 {len(active)} 个" + (f"；对不上的 {missing[:10]}" if missing else ""))
    if not active:
        raise SystemExit(f"{year} 年一个单元都没对上，检查控制表里的名字")

    groups = {}
    for unit, own in active.items():
        key = unit if merge_by == "unit" else own
        groups.setdefault(key, []).append(by_name[unit])

    feats = []
    for grp, geoms in groups.items():
        try:
            merged = unary_union(geoms)
        except Exception:
            continue
        if merged.is_empty:
            continue
        if simplify > 0:
            merged = merged.simplify(simplify, preserve_topology=True)
        c = merged.centroid
        if merge_by == "unit":
            us, own = [grp], active.get(grp, grp)
            name = disp.get(grp, grp)
        else:
            us = sorted(u for u, o in active.items() if o == grp)
            own = grp
            name = disp.get(grp, grp)
        color = pal.get(own) or stable_color(ckey.get(own, own), owners_all)
        feats.append({
            "type": "Feature",
            "properties": {"id": name, "name": name, "color": color,
                           "owner": own, "units": us,
                           "label_lon": round(c.x, 3), "label_lat": round(c.y, 3)},
            "geometry": mapping(merged)})

    def sha1_of(p):
        obj = _json.load(open(p, encoding="utf-8"))
        canon = _json.dumps(obj, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8")
        return hashlib.sha1(canon).hexdigest()[:16]

    out = {"type": "FeatureCollection",
           "_meta": {"topic": t.id, "year": year, "units_source": _os.path.basename(uf),
                     "method": t.source_note or
                               "单元为现成行政区多边形；归属据控制表，非当年实际界线",
                     "units_total": len(by_name), "units_matched": len(active),
                     "units_missing": missing,
                     "control_sha1": sha1_of(t.control_path()),
                     "gazetteer_sha1": sha1_of(uf),
                     "built_from": {"control": _os.path.basename(t.control_path()),
                                    "units": _os.path.basename(uf)}},
           "features": feats}
    _json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
    if not quiet:
        print(f"[5] -> {dst}  ({_os.path.getsize(dst)/1024:.0f} KB)")
        for g, n in sorted(((f['properties']['owner'], len(f['properties']['units']))
                            for f in feats), key=lambda x: -x[1]):
            print(f"    {g:8s} {n:3d} 个单元")
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--year", type=int, default=None,
                    help="单年构建；用 --all-years 时可不填")
    ap.add_argument("--max-km", type=float, default=None,
                    help="归并半径；缺省读题材数据里的 max_km，再缺省 260")
    ap.add_argument("--simplify", type=float, default=0.02)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只报错误，不打印过程")
    ap.add_argument("--all-years", action="store_true",
                    help="构建该题材声明的全部年份")
    args = ap.parse_args()
    if not args.all_years and args.year is None:
        ap.error("要么给 --year，要么给 --all-years")

    if args.all_years:
        from histmap_server import topics as T
        t = T.get(args.topic)
        for y in (t.raw.get("years") or []):
            build(args.topic, int(y), args.max_km, args.simplify, args.force, args.quiet)
            if not args.quiet:
                print()
    else:
        build(args.topic, args.year, args.max_km, args.simplify, args.force, args.quiet)

    # 建完几何就把取景框对齐到几何（年份不齐时本函数会自己跳过）
    refit_topic_bbox(args.topic, args.quiet)


if __name__ == "__main__":
    main()
