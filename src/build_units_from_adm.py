"""从 geoBoundaries 的行政区划文件里裁出一个「单元多边形表」，供 partition 类题材用。

跑法：
    python src/build_units_from_adm.py --src data/cache/gb_usa_adm1.geojson \
        --out data/processed/usa_states.geojson --keep "Alabama,Arkansas,..."

为什么要这一步：唐宋题材的单元几何是「州治坐标 + 把现代县就近归并」重建出来的，
但美国内战的单元**本来就是现成的州多边形** —— 再走一遍「就近归并」既没必要也会失真。
所以题材可以声明 `units_file`（现成多边形）来跳过坐标表，这里就负责把那份文件裁出来。

裁完的文件很小（简化过），可以直接入库，于是这个题材 clone 下来就能用，
不必先下载 5 MB 的原始数据。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="geoBoundaries 的 ADM geojson")
    ap.add_argument("--out", required=True)
    ap.add_argument("--name-field", default="shapeName")
    ap.add_argument("--keep", default="", help="逗号分隔的白名单；留空则全部保留")
    ap.add_argument("--rename", default="", help="重命名，形如 旧名=新名,旧名2=新名2")
    ap.add_argument("--simplify", type=float, default=0.02, help="简化容差（度）")
    ap.add_argument("--bbox", default="", help="minLon,minLat,maxLon,maxLat 之外的丢掉")
    args = ap.parse_args()

    src = args.src if os.path.isabs(args.src) else os.path.join(ROOT, args.src)
    out = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    keep = {s.strip() for s in args.keep.split(",") if s.strip()}
    ren = {}
    for pair in args.rename.split(","):
        if "=" in pair:
            a, b = pair.split("=", 1)
            ren[a.strip()] = b.strip()
    bbox = [float(x) for x in args.bbox.split(",")] if args.bbox else None

    gj = json.load(open(src, encoding="utf-8"))
    from shapely.geometry import shape, mapping
    from shapely.ops import unary_union

    feats, skipped = [], 0
    for f in gj["features"]:
        pr = f.get("properties") or {}
        name = str(pr.get(args.name_field) or "").strip()
        if not name or (keep and name not in keep):
            skipped += 1
            continue
        try:
            g = shape(f["geometry"])
        except Exception:
            skipped += 1
            continue
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            skipped += 1
            continue
        if args.simplify > 0:
            g = g.simplify(args.simplify, preserve_topology=True)
        c = g.centroid
        if bbox and not (bbox[0] <= c.x <= bbox[2] and bbox[1] <= c.y <= bbox[3]):
            skipped += 1
            continue
        nm = ren.get(name, name)
        feats.append({
            "type": "Feature",
            "properties": {"id": nm, "name": nm},
            "geometry": mapping(g)})

    if not feats:
        raise SystemExit("一个要素都没留下，检查 --keep 里的名字是否和数据里的对得上")

    dst = {"type": "FeatureCollection",
           "_meta": {"source": os.path.basename(src),
                     "simplify": args.simplify,
                     "count": len(feats),
                     "skipped": skipped},
           "features": feats}
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(dst, open(out, "w", encoding="utf-8"), ensure_ascii=False)
    kb = os.path.getsize(out) / 1024
    print(f"-> {out}  {len(feats)} 个单元，{kb:.0f} KB（跳过 {skipped} 个）")
    print("   单元名:", ", ".join(f["properties"]["name"] for f in feats))


if __name__ == "__main__":
    main()
