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
for p in (os.path.join(ROOT, "packages", "core"),
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


def stable_color(name: str, all_names: list) -> str:
    """按名字稳定分配颜色 —— 不用 hash()，那个跨进程不稳定。"""
    h = int(hashlib.md5(name.encode("utf-8")).hexdigest()[:8], 16)
    return PALETTE[h % len(PALETTE)]


def build(topic_id: str, year: int, max_km: float = 260.0,
          simplify: float = 0.02, force: bool = False, quiet: bool = False):
    from histmap_server import topics as T

    t = T.get(topic_id)
    if not t:
        raise SystemExit(f"没有这个题材: {topic_id}")
    if t.kind != "dynasty":
        raise SystemExit(f"题材 {topic_id} 不是 dynasty 类（kind={t.kind}）")

    dst = os.path.join(PROC, f"{topic_id}_{year}_map.geojson")
    if os.path.exists(dst) and not force:
        if not quiet:
            print(f"已存在 {dst}（--force 可重建）")
        return dst

    units_path = t.raw.get("units")
    if not units_path:
        raise SystemExit(f"题材 {topic_id} 没声明 units 文件")
    up = units_path if os.path.isabs(units_path) else os.path.join(PROC, units_path)
    if not os.path.exists(up):
        raise SystemExit(f"找不到单元表: {up}")
    ctrl = t.control_path()
    if not ctrl:
        raise SystemExit(f"题材 {topic_id} 没声明 control 文件")

    units = load_units(up)
    raw = json.load(open(ctrl, encoding="utf-8"))
    owner = parse_owner_table(raw, year)
    if not owner:
        raise SystemExit(f"控制表里没有 {year} 年")

    if not quiet:
        print(f"=== {t.title} · {year} 年 ===")
        print(f"[1] 单元坐标 {len(units)} 条；控制表给出 {len(owner)} 个单元")
        print(f"    归属方 {len(set(owner.values()))} 个")

    # 只保留今年有归属的单元，且必须在坐标表里（两边都归一到简体再比）
    by_name = {_norm(u["name"]): u for u in units}
    active = {_norm(n): o for n, o in owner.items() if _norm(n) in by_name}
    missing = [n for n in owner if _norm(n) not in by_name]
    if not quiet:
        print(f"[2] 可定位单元 {len(active)} 个；缺坐标 {len(missing)} 个"
              + (f": {' '.join(sorted(missing)[:15])}" if missing else ""))

    # 归属方显示名（方镇表列名用初名，正文用后来的号）
    disp = raw.get("_display") or {}
    # 配色覆盖：同色系政权靠调色板凑不出稳定区分时，直接在数据里点名要什么色。
    # 例：北宋/南宋要同色（同一政权延续），金要和西夏的灰蓝拉开。
    pal = raw.get("_palette") or {}
    # 换色键：让「南宋」沿用「宋」的颜色，颜色跟政权走而不是跟名字走
    ckey = raw.get("_color_key") or {}

    counties = load_counties()
    if not quiet:
        print(f"[3] 县多边形 {len(counties)} 个")

    # ── 县 → 单元（只在当年存在的单元里选最近） ──
    cand = [(n, by_name[n]["lon"], by_name[n]["lat"]) for n in active]
    assigned, far = {}, 0
    for c in counties:
        best, bd = None, 1e18
        for n, lon, lat in cand:
            d = haversine_km(c["cx"], c["cy"], lon, lat)
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
            color = pal.get(own) or stable_color(ckey.get(own, own), list(groups))
        else:
            us = sorted(u for u, o in active.items() if o == grp)
            name = disp.get(grp, grp)
            color = pal.get(grp) or stable_color(ckey.get(grp, grp), list(groups))
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
    return dst


def sha1_of(path: str) -> str:
    """JSON 的**语义**指纹：重排键序、改缩进、动换行都不算变化。

    和 topics._file_sha1 必须一致，否则服务端会把刚算好的图当成过期。
    """
    obj = json.load(open(path, encoding="utf-8"))
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(canon).hexdigest()[:16]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--year", type=int, default=None,
                    help="单年构建；用 --all-years 时可不填")
    ap.add_argument("--max-km", type=float, default=260.0)
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


if __name__ == "__main__":
    main()
