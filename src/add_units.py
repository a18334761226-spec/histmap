#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""往一个题材已有的单元多边形表里**追加**另一个国家的行政区。

跑法：
    python src/add_units.py --topic india-pakistan-partition --iso BGD
    python src/add_units.py --topic india-pakistan-partition --iso BGD --adm ADM1 --dry-run

为什么要有它：题材的单元表一旦建好就落在 data/processed/<topic>_units.geojson，
想再补一个国家过去只能手工改 GeoJSON。而「跨国的历史题材漏了一个国家」
是很常见的事 —— 实测印巴分治那张图的单元表只有 IND 36 个 + PAK 6 个，
**完全没有东巴基斯坦（今孟加拉国）**，于是 1947 年的图里巴基斯坦缺了整个东巴，
这是史实错误，不只是「不够好看」。

幂等：已经存在的单元名会跳过，重复跑不会把面叠两份。
归属关系不在这里改 —— 加完单元要自己去控制表里指定它归谁，
这一步必须由人（或模型）按史实决定，脚本不猜。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "packages", "server"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--iso", required=True, help="ISO3，如 BGD")
    ap.add_argument("--adm", default="ADM1")
    ap.add_argument("--name-field", default="shapeName")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import new_topic as NT
    from histmap_server import topics as T

    t = T.get(args.topic)
    if not t:
        raise SystemExit(f"没有这个题材：{args.topic}")
    up = T._units_path(t)
    if not up or not os.path.exists(up):
        raise SystemExit(f"{args.topic} 没有单元多边形表（units_file），"
                         f"它是按坐标表+现代县界生成的，不需要补单元")
    doc = json.load(open(up, encoding="utf-8"))
    have = {(f.get("properties") or {}).get("name") for f in doc["features"]}

    src = NT.fetch_adm(args.iso.upper(), args.adm.upper())
    add = json.load(open(src, encoding="utf-8"))
    added, skipped = [], []
    for f in add["features"]:
        nm = str((f.get("properties") or {}).get(args.name_field) or "").strip()
        if not nm:
            continue
        if nm in have:
            skipped.append(nm)
            continue
        doc["features"].append({
            "type": "Feature",
            # 只留 id/name 两个字段 —— 跟这个表里原有的单元保持同一套结构，
            # 下游 build_from_polygons 只认 name，多带字段只会让文件变大。
            "properties": {"id": nm, "name": nm},
            "geometry": f["geometry"]})
        have.add(nm)
        added.append(nm)

    print(f"题材 {args.topic}  现有单元 {len(doc['features']) - len(added)} 个")
    print(f"从 {args.iso.upper()} {args.adm.upper()} 新增 {len(added)} 个：{added}")
    if skipped:
        print(f"已存在、跳过 {len(skipped)} 个：{skipped}")
    if not added:
        print("没有新增，什么都不用做")
        return 0
    meta = doc.setdefault("_meta", {})
    meta.setdefault("added_units", []).append(
        {"iso": args.iso.upper(), "adm": args.adm.upper(), "names": added})
    if args.dry_run:
        print("（--dry-run，没有写盘）")
        return 0
    json.dump(doc, open(up, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"已写回 {up}")
    print("\n下一步（必须做，脚本不猜归属）：")
    print(f"  在 {os.path.basename(t.control_path() or '控制表')} 里把这些单元")
    print(f"  按年份指定归属方，然后重建：")
    print(f"  python src/build_dynasty_map.py --topic {args.topic} --all-years --force")
    return 0


if __name__ == "__main__":
    sys.exit(main())
