#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
导出「浏览器端渲染」所需的静态数据产物
==========================================
对应 docs/design-browser-render.md 的 M1：把 Python 端的数据切成
浏览器可直接 fetch 的静态文件，云端只托管这些文件，不跑任何渲染服务。

产物结构（可直接扔进 CDN / GitHub Pages）
    data/web/scene/{scene_id}.json     场景入口（浏览器只认这个）
    data/web/geometry/{ds}-{year}.json 年度几何切片（已按 scene.bbox 裁剪）
    data/web/control/{topic}.json      控制时间线
    data/web/style/{style_id}.json     样式

许可隔离：CShapes 原始数据不进仓库，本脚本是**构建期**工具，
生成的产物同样不入库（.gitignore），由使用者本地/CI 生成。
"""
import json
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))
sys.path.insert(0, os.path.join(ROOT, "src"))

from histmap_core import Registry, ControlTimeline, Style   # noqa: E402
from histmap_core.datasets import cshapes                   # noqa: E402

WEB = os.path.join(ROOT, "data", "web")
SCENE_FILE = os.path.join(ROOT, "scenes", "ww2-europe.json")
TL_SRC = os.path.join(ROOT, "data", "control", "ww2_timeline.json")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    return os.path.getsize(path)


def main():
    scene = json.load(open(SCENE_FILE, encoding="utf-8"))
    sid = scene["id"]
    bbox = scene["bbox"]
    lon0, lat0, lon1, lat1 = bbox
    years = list(range(scene["years"][0], scene["years"][1] + 1))

    print(f"scene={sid} bbox={bbox} years={years[0]}–{years[-1]}")

    # ── 1. 几何切片 ────────────────────────────────────────
    ad = Registry.get("cshapes")
    gj = ad.load()                      # 25 MB JSON 只解析一次
    ad.load = lambda use_cache=True: gj
    total = 0
    for y in years:
        s = ad.build(years=[y])
        fr = s.frames[0]
        feats = []
        for r in fr.regions:
            b = r.bbox()
            if not b:
                continue
            if b[2] < lon0 or b[0] > lon1 or b[3] < lat0 or b[1] > lat1:
                continue
            feats.append({
                "id": r.id,
                # 原始英文名：控制层要用它做匹配（中文名在控制表里）
                "name": r.name,
                "rings": [[[round(x, 4), round(y2, 4)] for x, y2 in ring]
                          for ring in r.rings],
                "props": {"members": (r.props or {}).get("members") or []},
            })
        out = {
            "dataset": "cshapes", "year": y,
            "bbox": bbox,
            "source": "CShapes 2.0 (ETH Zürich) — 学术引用要求，商用需确认",
            "features": feats,
        }
        p = os.path.join(WEB, "geometry", f"cshapes-{y}.json")
        n = write_json(p, out)
        total += n
        print(f"  geometry {y}: {len(feats):3d} 实体  {n/1024:8.1f} KB")

    # ── 2. 控制时间线 ──────────────────────────────────────
    tl = ControlTimeline.load(TL_SRC)
    tl_out = {
        "id": "ww2_timeline",
        "title": "二战欧洲控制状态",
        "baseline": tl.baseline,
        "events": tl.events,
        "markers": tl.data.get("markers") or [],
        "palette": tl.palette,
    }
    n = write_json(os.path.join(WEB, "control", "ww2_timeline.json"), tl_out)
    total += n
    print(f"  control: {len(tl.events)} 事件 / {len(tl.baseline)} 基线  {n/1024:.1f} KB")

    # ── 3. 样式 ────────────────────────────────────────────
    # 样式必须与 Python 端**完全同一份**，否则两端渲染结果无法对齐（M1 要求）。
    # 优先读 styles/{id}.yaml，没有就回落到视频脚本里用的那套 STYLE。
    style_path = os.path.join(ROOT, "styles", f"{scene['style']}.yaml")
    if os.path.exists(style_path):
        st = Style.load(style_path)
    else:
        from make_ww2_video import STYLE as st
    style_obj = st.to_dict()
    n = write_json(os.path.join(WEB, "style", f"{scene['style']}.json"), style_obj)
    total += n
    print(f"  style: {n/1024:.1f} KB  (source={'yaml' if os.path.exists(style_path) else 'STYLE'})")

    # ── 4. 场景入口 ────────────────────────────────────────
    n = write_json(os.path.join(WEB, "scene", f"{sid}.json"), scene)
    total += n
    print(f"  scene: {n/1024:.1f} KB")

    print(f"\n合计 {total/1024/1024:.2f} MB -> {WEB}")


if __name__ == "__main__":
    t0 = time.time()
    main()
    print(f"用时 {time.time()-t0:.1f}s")
