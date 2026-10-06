"""把题材的 bbox 从「人猜的」改成「从几何算出来的」。

跑法：
    python src/fit_bbox.py --topic mingqing --write     # 算完写回 topics.json
    python src/fit_bbox.py --all --write                # 所有 dynasty 类题材

为什么需要它：明清的 bbox 被写成了 [73,17,135,54]（整个中国加西北），
但 1644 那一年只有东部有数据 —— 新疆、西藏要到 1700/1820 才纳入。
结果那一帧的画面只占右下角一小块，左边一大片空白，标注也小到看不清。
**bbox 是从「这个朝代最大疆域」凭印象写的，不是从逐年的真实几何算的。**

正确做法：把该题材**所有年份**的几何取并集，留一点边距，就是 bbox。
这样既不裁掉任何一年的内容，也不会有大片永远空着的区域。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

PROC = os.path.join(ROOT, "data", "processed")
TOPICS = os.path.join(ROOT, "data", "topics", "topics.json")


def extent_of_topic(topic_id: str, years: list[int]) -> tuple | None:
    """并集范围。返回 (minLon, minLat, maxLon, maxLat) 或 None。"""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    boxes = []
    for y in years:
        p = os.path.join(PROC, f"{topic_id}_{y}_map.geojson")
        if not os.path.exists(p):
            continue
        gj = json.load(open(p, encoding="utf-8"))
        for f in gj.get("features", []):
            try:
                boxes.append(shape(f["geometry"]))
            except Exception:
                continue
        # 只算外框就够，不用真的并集（并集很慢）
        if boxes:
            b = unary_union(boxes).bounds
            boxes = [shape({"type": "Polygon",
                            "coordinates": [[[b[0], b[1]], [b[2], b[1]],
                                             [b[2], b[3]], [b[0], b[3]],
                                             [b[0], b[1]]]]})]
    if not boxes:
        return None
    b = unary_union(boxes).bounds
    return (b[0], b[1], b[2], b[3])


def fit(topic_id: str, years: list[int], pad_ratio: float = 0.03,
        min_pad: float = 0.4) -> list | None:
    e = extent_of_topic(topic_id, years)
    if not e:
        return None
    x0, y0, x1, y1 = e
    px = max(min_pad, (x1 - x0) * pad_ratio)
    py = max(min_pad, (y1 - y0) * pad_ratio)
    return [round(x0 - px, 2), round(y0 - py, 2),
            round(x1 + px, 2), round(y1 + py, 2)]


def data_fill_ratio(bbox: list, topic_id: str, years: list[int]) -> float:
    """数据实际占 bbox 面积的比例。太小说明 bbox 里大片是空的。"""
    e = extent_of_topic(topic_id, years)
    if not e:
        return 0.0
    a = max(1e-9, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    b = max(0.0, (e[2] - e[0]) * (e[3] - e[1]))
    return b / a


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true", help="写回 topics.json")
    ap.add_argument("--min-fill", type=float, default=0.55,
                    help="数据占 bbox 面积低于这个比例就报出来")
    args = ap.parse_args()

    doc = json.load(open(TOPICS, encoding="utf-8"))
    todo = [t for t in doc["topics"]
            if args.all or t.get("id") == args.topic]
    if not todo:
        raise SystemExit(f"没找到题材 {args.topic}")

    changed = 0
    for t in todo:
        if t.get("kind") == "boundary":
            print(f"  {t['id']:26s} boundary 类用现成国界，bbox 由数据源定，跳过")
            continue
        years = [int(y) for y in (t.get("years") or [])]
        if not years:
            continue
        old = t.get("bbox")
        new = fit(t["id"], years)
        if not new:
            print(f"  {t['id']:26s} 没找到几何文件，跳过")
            continue
        fill_old = data_fill_ratio(old, t["id"], years) if old else 0.0
        fill_new = data_fill_ratio(new, t["id"], years)
        flag = "!! 太空" if fill_old < args.min_fill else "ok"
        print(f"  {t['id']:26s} 旧 {old}")
        print(f"  {'':26s} 新 {new}   数据占比 {fill_old:.0%} -> {fill_new:.0%}  {flag}")
        if args.write and old != new:
            t["bbox"] = new
            changed += 1

    if args.write and changed:
        json.dump(doc, open(TOPICS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"\n已写回 {changed} 个题材的 bbox -> {TOPICS}")
        print("注意：bbox 变了，但已算好的几何不用重算（几何与 bbox 无关，只是取景框）。")
    elif args.write:
        print("\n没有需要改的")


if __name__ == "__main__":
    main()
