#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
T3 · 唐藩镇格局图构建（县 → 州 → 藩镇）
============================================
## 为什么这么做

唐代史料**不提供几何**。《新唐书·方镇表》只告诉我们「某年某藩镇辖某几州」，
不告诉我们那些州的边界在哪。所以几何必须重建。

重建方案（见 docs/tang-data-plan.md 的取舍对比）：

    《新唐书·方镇表》原文
        ↓ T1 解析                      → 藩镇 ← [州] 的逐年隶属
    唐州治所坐标表（T2，329 条）
        ↓ 每个现代县归到最近的州治      ← geoBoundaries 3099 个县多边形
    州的几何（= 所属县的并集）
        ↓ 按藩镇-州隶属合并
    藩镇面

**为什么不用 Voronoi 直接生成州**：Voronoi 出来是直线多边形，合成感极强，
而且不会自动沿海岸线切断。用「县归属」得到的边界是**真实县界**，
形状自然、天然贴海岸线。

## 数据性质（必须写进成图口径）

这是**重建**，不是史料记载的实际界线：
  · 行政层级古今不对应（唐州 ≈ 现代县/市，数量级相近但非一一对应）
  · 归属判据是「县治到州治的直线距离最近」，不是史料记载的隶属
  · 唐实际控制范围与现代国界不同（如安西、安南）

## 许可

  · geoBoundaries CHN ADM2 — PDDL（≈公有领域）
  · geoBoundaries VNM ADM2 — CC BY 3.0 IGO
  · 坐标表 — 本项目重建
  三者都可商用；越南部分需署名。

用法
    python src/build_tang_map.py --year 807
    python src/build_tang_map.py --year 807 --max-km 220
"""
import argparse
import json
import math
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from shapely.geometry import shape, mapping, Point
from shapely.ops import unary_union
from shapely.strtree import STRtree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache")
PROC = os.path.join(ROOT, "data", "processed")

COUNTY_FILES = [
    ("CHN", os.path.join(CACHE, "gb_chn_adm2.geojson")),
    ("VNM", os.path.join(CACHE, "gb_vnm_adm2.geojson")),
]
GAZ = os.path.join(PROC, "tang_zhou_gazetteer.json")
TIMELINE = os.path.join(PROC, "tang_fanzhen_timeline.json")

# 藩镇配色：muted 传统色，浅色宣纸底上可读、彼此可辨。
# 按藩镇序号稳定分配（不随年份变），这样同一藩镇在整条时间轴上颜色不变。
FANZHEN_COLORS = [
    "#b5675a", "#7c8fa8", "#8a9a6b", "#b08a5c", "#9a7ba8", "#6fa39a",
    "#c08a7a", "#8a8fb0", "#a89a5c", "#7fa08a", "#b07a92", "#6b8ba8",
    "#a08a6b", "#8fa87c", "#b59a7a", "#7c9ab0", "#ab7f6b", "#96a8b0",
    "#c0a05c", "#8a7ca8", "#6f9a7c", "#b09a8a", "#7a8fa0", "#a8945c",
]


# 方镇表**列名用初名**，正文里用后来的号。出图时应当显示当时的号，
# 否则 807 年的图上会出现「淮南西道」这种 762 年就不用的名字。
FANZHEN_DISPLAY = {
    "淮南西道": "淮西", "劍南": "劍南西川", "南陽": "山南東道",
    "江東": "浙西", "衡州": "湖南", "洪吉": "江西", "鄂岳沔": "鄂岳",
    "青密": "平盧", "滑衛": "宣武", "鄭陳": "忠武", "徐海沂密": "武寧",
    "東畿": "東都畿", "興鳳隴": "鳳翔", "渭北鄜坊": "鄜坊",
    "山南西道": "山南西道", "東川": "劍南東川", "黔州": "黔中",
    "安西": "安西四鎮", "桂管": "桂管", "邕管": "邕管", "容管": "容管",
    "京畿": "京畿", "河中": "河中", "澤潞沁": "昭義", "成德": "成德",
    "魏博": "魏博", "幽州": "盧龍", "橫海": "橫海", "義武": "義武",
    "朔方": "朔方", "河西": "河西", "隴右": "隴右", "涇原": "涇原",
    "邠寧": "邠寧", "北都": "河東", "河南": "河南", "淮南": "淮南",
    "福建": "福建", "浙東": "浙東", "嶺南": "嶺南", "安南": "安南",
    "荆南": "荊南", "荊南": "荊南",
}


def display_name(fz: str) -> str:
    return FANZHEN_DISPLAY.get(fz, fz)


def haversine_km(lon1, lat1, lon2, lat2):
    R = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def load_counties():
    out = []
    for iso, path in COUNTY_FILES:
        if not os.path.exists(path):
            print(f"  ! 缺少 {path}，跳过")
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
                geom = geom.buffer(0)          # 简化后的多边形常有自交
            if geom.is_empty:
                continue
            c = geom.centroid
            out.append({"iso": iso, "name": f["properties"].get("shapeName", "?"),
                        "geom": geom, "cx": c.x, "cy": c.y})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=807)
    ap.add_argument("--max-km", type=float, default=260.0,
                    help="县治到州治的最大归属距离；超过则视为唐疆域之外")
    ap.add_argument("--simplify", type=float, default=0.02,
                    help="输出简化容差（度）；0 表示不简化")
    args = ap.parse_args()

    print(f"=== T3 唐藩镇图  {args.year} 年 ===")
    counties = load_counties()
    print(f"[1] 县多边形 {len(counties)} 个（CHN + VNM）")

    gaz = json.load(open(GAZ, encoding="utf-8"))["points"]
    coords = [(p["name"], p["lon"], p["lat"]) for p in gaz]
    print(f"[2] 州治所 {len(coords)} 条")

    tl = json.load(open(TIMELINE, encoding="utf-8"))
    snap = tl.get(str(args.year)) or tl.get(args.year)
    if not snap:
        print(f"  ! 时间线里没有 {args.year} 年"); sys.exit(1)

    # 州名归一到简体再匹配。
    # 时间线来自维基文库《新唐書》（**繁体**：儀、兗、劍、臺），
    # 坐标表按今地名写成**简体**（仪、兖、剑、台）。
    # 不做这步会有一百多个州匹配不上、分不到县（踩过）。
    try:
        import zhconv
        norm = lambda s: zhconv.convert(s, "zh-cn")
    except ImportError:
        norm = lambda s: s

    zhou2fz = {}
    for fz, v in snap.items():
        for z in v["zhou"]:
            s = norm(z)
            if s in zhou2fz and zhou2fz[s] != fz:
                print(f"    ! 州名冲突（繁简归一后）: {z} -> {s}  "
                      f"已属 {zhou2fz[s]}，{fz} 也声称拥有")
            zhou2fz.setdefault(s, fz)
    print(f"[3] {args.year} 年：{len(snap)} 藩镇 / {len(zhou2fz)} 州（已归一到简体）")

    # 藩镇配色按名字稳定分配（排序后取模），保证跨年份同色
    fz_names = sorted(snap)
    fz_color = {n: FANZHEN_COLORS[i % len(FANZHEN_COLORS)]
                for i, n in enumerate(fz_names)}

    # ── 县 → 州 ─────────────────────────────────────────────
    # 直接用「县治到州治的球面距离最近」判定。州治数量（329）远小于县数（3099），
    # 暴力算 3099×329 ≈ 100 万次距离完全可接受，不必上 KD-tree。
    #
    # **关键：只在「今年真实存在的州」里选最近**。
    # 初版在全部 329 个州治里选，于是「最近的是虢州、但虢州不在 807 名单」
    # 的县被直接丢弃 —— 地图上成了空洞（宣州、宿州、郑州、河南府整片没了）。
    # 限定在 active 州里选，虢州的县会归到同属陝虢的陝州，图面完整。
    active = [(n, lon, lat) for n, lon, lat in coords if n in zhou2fz]
    print(f"[4] 归属候选：{len(active)} 个州（今年存在的）；"
          f"另有 {len(coords)-len(active)} 个州今年不在名单，不参与归属")

    assigned = {}
    far = []
    for c in counties:
        best, bd = None, 1e18
        for name, lon, lat in active:
            d = haversine_km(c["cx"], c["cy"], lon, lat)
            if d < bd:
                bd, best = d, name
        if bd > args.max_km:
            far.append((c["name"], round(bd)))
            continue
        assigned.setdefault(best, []).append(c)

    placed = sum(len(v) for v in assigned.values())
    print(f"[4] 归属：{placed} 个县 -> {len(assigned)} 个州；"
          f"超出 {args.max_km:.0f} km 判为唐疆域外 {len(far)} 个")

    missing = [z for z in zhou2fz if z not in assigned]
    print(f"    今年有隶属关系但没分到县的州 {len(missing)} 个: "
          f"{' '.join(sorted(missing))}")

    # ── 州 → 藩镇（合并） ────────────────────────────────────
    print("[5] 合并多边形 …")
    fz_geoms = {}
    for z, cs in assigned.items():
        try:
            g = unary_union([c["geom"] for c in cs])
        except Exception as e:
            print(f"    ! {z} 合并失败: {e}")
            continue
        fz = zhou2fz[z]
        fz_geoms.setdefault(fz, []).append(g)

    feats, summary = [], []
    for fz, geoms in fz_geoms.items():
        try:
            merged = unary_union(geoms)
        except Exception:
            continue
        if merged.is_empty:
            continue
        if args.simplify > 0:
            merged = merged.simplify(args.simplify, preserve_topology=True)
        zs = sorted(z for z in zhou2fz if zhou2fz[z] == fz)
        c = merged.centroid
        feats.append({
            "type": "Feature",
            "properties": {"id": fz, "name": display_name(fz), "raw_name": fz,
                           "color": fz_color[fz],
                           "zhou": zs, "label_lon": round(c.x, 3),
                           "label_lat": round(c.y, 3)},
            "geometry": mapping(merged),
        })
        summary.append((fz, len(zs), merged.area))

    out = {
        "type": "FeatureCollection",
        "_meta": {
            "year": args.year,
            "method": ("藩镇辖境据《新唐书·方镇表》州县隶属关系重建；"
                       "几何以现代县界（geoBoundaries CHN/VNM）为底、"
                       "按县治至州治最近距离归属，**非史料记载的实际界线**"),
            "licenses": ["geoBoundaries CHN ADM2 — PDDL v1.0",
                         "geoBoundaries VNM ADM2 — CC BY 3.0 IGO",
                         "唐州治所坐标表 — 本项目重建"],
            "max_km": args.max_km,
        },
        "features": feats,
    }
    dst = os.path.join(PROC, f"tang_{args.year}_fanzhen.geojson")
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"[6] -> {dst}  ({os.path.getsize(dst)/1024:.0f} KB)")

    summary.sort(key=lambda x: -x[2])
    print(f"\n{args.year} 年藩镇（按面积）:")
    for fz, nz, a in summary[:15]:
        print(f"    {fz:8s} {nz:2d}州")


if __name__ == "__main__":
    main()
