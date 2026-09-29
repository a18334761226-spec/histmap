#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据下载器 · 一键把项目需要的公开数据源拉到本地
======================================================
**为什么是「下载器模式」**：数据集不进仓库。原因有两个——

1. **许可**：CShapes 要求学术引用、商用条款未确认；geoBoundaries 虽可再分发，
   但几 MB 的几何放仓库里会让 clone 变慢、也让许可归属变得含糊。
2. **体积**：CShapes 25 MB + 两个 geoBoundaries 共 12 MB。

本脚本只依赖标准库，走 HTTPS_PROXY（若有）。

用法
    set HTTPS_PROXY=http://127.0.0.1:7897     # 国内直连 GitHub 很慢，建议开代理
    python src/fetch_data.py --list
    python src/fetch_data.py --all
    python src/fetch_data.py --only gb_chn gb_vnm
"""
import argparse
import os
import sys
import time
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "cache")
UA = "histmap/0.1 (+https://github.com/; data fetcher)"

SOURCES = {
    "gb_chn": {
        "file": "gb_chn_adm2.geojson",
        "url": ("https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
                "releaseData/gbOpen/CHN/ADM2/geoBoundaries-CHN-ADM2_simplified.geojson"),
        "license": "PDDL v1.0 (public domain dedication)",
        "note": "中国县级多边形 2391 个",
    },
    "gb_vnm": {
        "file": "gb_vnm_adm2.geojson",
        "url": ("https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
                "releaseData/gbOpen/VNM/ADM2/geoBoundaries-VNM-ADM2_simplified.geojson"),
        "license": "CC BY 3.0 IGO（须署名 OCHA ROAP / Government of Viet Nam）",
        "note": "越南县级多边形 708 个（唐安南都护府）",
    },
    "cshapes": {
        "file": "CShapes-2.0.geojson",
        "url": "https://icr.ethz.ch/data/cshapes/CShapes-2.0.geojson",
        "license": "学术引用要求；**商用条款未确认**（Schvitz et al. 2022, JCR 66(1):144-61）",
        "note": "近现代国界 1886–2019，25 MB",
    },
}


def fetch(key, force=False):
    s = SOURCES[key]
    os.makedirs(CACHE, exist_ok=True)
    dst = os.path.join(CACHE, s["file"])
    if os.path.exists(dst) and not force:
        mb = os.path.getsize(dst) / 1024 / 1024
        print(f"  – {key:8s} 已存在 {mb:.2f} MB，跳过（--force 可重下）")
        return True
    print(f"  ↓ {key:8s} {s['note']}")
    print(f"      许可: {s['license']}")
    t0 = time.time()
    try:
        req = urllib.request.Request(s["url"], headers={"User-Agent": UA})
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        opener = (urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            if proxy else urllib.request.build_opener())
        with opener.open(req, timeout=600) as r, open(dst, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        mb = os.path.getsize(dst) / 1024 / 1024
        print(f"      ✓ {mb:.2f} MB  {time.time()-t0:.1f}s  -> {dst}")
        return True
    except Exception as e:
        print(f"      ✗ 失败: {e}")
        if not proxy:
            print("        国内直连 GitHub / ETH 常常超时，试试设置 HTTPS_PROXY")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", nargs="*", choices=list(SOURCES))
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if args.list or not (args.all or args.only):
        print("可用数据源：\n")
        for k, s in SOURCES.items():
            have = "✓ 已有" if os.path.exists(os.path.join(CACHE, s["file"])) else "  未下载"
            print(f"  {k:8s} {have}  {s['note']}")
            print(f"           {s['license']}")
        print(f"\n缓存目录: {CACHE}")
        print("用法: python src/fetch_data.py --all")
        return

    keys = list(SOURCES) if args.all else args.only
    print(f"下载 {len(keys)} 个数据源 -> {CACHE}\n")
    ok = sum(fetch(k, args.force) for k in keys)
    print(f"\n完成 {ok}/{len(keys)}")
    sys.exit(0 if ok == len(keys) else 1)


if __name__ == "__main__":
    main()
