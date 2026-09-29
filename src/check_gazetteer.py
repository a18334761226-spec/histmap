#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
州治所坐标校验 · 用真实县界验证每个点
==========================================
**为什么这个工具是整条链路的关键**

唐代州治所坐标没有许可证干净的权威来源（CHGIS 禁商用、Wikidata 覆盖不足）。
所以坐标必须「拼」出来：Wikidata 有的用它，没有的靠重建。

但重建就会有错，而错误坐标在地图上表现为「某个州飞到了海里/隔壁省」——
不做校验根本发现不了。本工具用 geoBoundaries 的 2391 个真实县多边形
（PDDL 公有领域）做**点在多边形内**判定，把每个坐标落到具体某个县：

  · 落在中国境内 → 通过，并报出所在县（供人工核对是否是该州的治所）
  · 落在境外/海上 → **判失败**，说明坐标错了

这样「AI 重建」就有了客观的兜底，而不是靠人逐条看出来。

用法
    python src/check_gazetteer.py data/processed/tang_zhou_gazetteer.json
    python src/check_gazetteer.py --selftest        # 用已知正确的坐标自检
"""
import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GB = os.path.join(ROOT, "data", "cache", "gb_chn_adm2.geojson")
# 唐代疆域不止现代中国：安南都护府（交、爱、驩、演、峰、陆、长等州）在**今越南**。
# 只加载 CHN 会让这些点被判成「错」，所以按需并入越南的县级多边形。
GB_MORE = {
    "VNM": os.path.join(ROOT, "data", "cache", "gb_vnm_adm2.geojson"),
}


def load_counties(path=GB, extra=None):
    """载入县多边形，同时算出每个县的包围盒，用于先做粗筛。"""
    files = [path] + [p for p in (extra or GB_MORE).values() if os.path.exists(p)]
    out = []
    for fp in files:
        out.extend(_load_one(fp))
    return out


def _load_one(path):
    g = json.load(open(path, encoding="utf-8"))
    out = []
    for f in g["features"]:
        geom = f["geometry"]
        poly = geom["coordinates"]
        rings = poly if geom["type"] == "MultiPolygon" else [poly]
        # rings: MultiPolygon -> [[outer, hole...], ...]；Polygon -> [outer, hole...]
        outers = []
        for part in rings:
            if not part:
                continue
            outer = part[0] if isinstance(part[0][0], (list, tuple)) else part
            outers.append([(float(p[0]), float(p[1])) for p in outer])
        if not outers:
            continue
        xs = [p[0] for r in outers for p in r]
        ys = [p[1] for r in outers for p in r]
        out.append({"name": f["properties"].get("shapeName", "?"),
                    "outers": outers,
                    "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return out


def _in_ring(x, y, ring):
    """射线法。ring 是 [(lon,lat), ...]。"""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x < xin:
                inside = not inside
    return inside


def locate(lon, lat, counties):
    """返回包含该点的县名，找不到返回 None。先用包围盒粗筛。"""
    for c in counties:
        b = c["bbox"]
        if not (b[0] <= lon <= b[2] and b[1] <= lat <= b[3]):
            continue
        for ring in c["outers"]:
            if _in_ring(lon, lat, ring):
                return c["name"]
    return None


def check(gaz, counties, verbose=True):
    """gaz: [{name, lon, lat, ...}]。返回 (通过列表, 失败列表)。"""
    ok, bad = [], []
    for g in gaz:
        loc = locate(g["lon"], g["lat"], counties)
        rec = dict(g)
        rec["county"] = loc
        if loc:
            ok.append(rec)
        else:
            bad.append(rec)
    if verbose:
        print(f"总数 {len(gaz)}   落在中国境内 {len(ok)}   "
              f"落在境外/海上 {len(bad)}")
        if bad:
            print("\n⚠️  以下坐标不在任何中国县境内，几乎可以确定是错的：")
            for r in bad:
                print(f"    {r['name']:<8} ({r['lon']:.4f}, {r['lat']:.4f})"
                      + (f"  来源={r.get('source','')}" if r.get("source") else ""))
        if ok:
            print("\n通过的点（所在县供人工核对是否是该州治所）：")
            for r in ok[:20]:
                print(f"    {r['name']:<8} -> {r['county']}")
            if len(ok) > 20:
                print(f"    … 其余 {len(ok)-20} 条见输出文件")
    return ok, bad


def selftest():
    """用一组已知正确的唐代州治坐标自检工具本身。

    坐标取现代同名/近名城市，误差在几公里内，用来验证
    「点在多边形内」判定与数据载入都是对的。
    """
    known = [
        {"name": "延州", "lon": 109.4900, "lat": 36.5953},   # 今延安
        {"name": "邠州", "lon": 108.0764, "lat": 35.0364},   # 今彬州
        {"name": "华州", "lon": 109.7500, "lat": 34.5100},   # 今华州区
        {"name": "京兆", "lon": 108.9400, "lat": 34.2600},   # 今西安
        {"name": "太原", "lon": 112.5500, "lat": 37.8700},   # 今太原
        # 故意放两个错的，验证工具能抓出来
        {"name": "错点-海上", "lon": 126.0000, "lat": 30.0000},
        {"name": "错点-东京", "lon": 139.6900, "lat": 35.6900},
    ]
    counties = load_counties()
    print(f"载入县多边形 {len(counties)} 个\n")
    ok, bad = check(known, counties)
    print()
    if len(bad) == 2 and len(ok) == 5:
        print("✓ 自检通过：5 个正确坐标全部落在境内，2 个错误坐标全部被抓出")
        return 0
    print(f"✗ 自检未通过：期望 5 通过 / 2 失败，实得 {len(ok)} / {len(bad)}")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gazetteer", nargs="?", help="州治所 JSON: [{name, lon, lat}]")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.selftest:
        sys.exit(selftest())

    if not args.gazetteer:
        ap.error("需要 gazetteer JSON，或用 --selftest")
    gaz = json.load(open(args.gazetteer, encoding="utf-8"))
    if isinstance(gaz, dict):
        gaz = gaz.get("points") or gaz.get("zhou") or []
    counties = load_counties()
    print(f"载入县多边形 {len(counties)} 个\n")
    ok, bad = check(gaz, counties)
    out = args.out or os.path.splitext(args.gazetteer)[0] + "_checked.json"
    json.dump({"ok": ok, "bad": bad}, open(out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n-> {out}")
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
