#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抓取《新唐书·方镇表》全 10 卷（卷 64–73）
==============================================
**为什么需要这个脚本**：早期只抓了卷 64–69（6 卷 / 42 个藩镇槽位），
漏掉的卷 70–73 里正是**淮西（彰義）、淄青（平盧）**这些宪宗朝的主角。
表现为「807 年蔡州、申州无人占有」「淮西藩镇根本不存在」——
不是解析错，是史料没下全。

本脚本从维基文库的 MediaWiki API 取 wikitext，存成
`data/raw/juan0NN.json`，格式与既有文件一致，供 parse_fangzhen.py 消费。

用法
    set HTTPS_PROXY=http://127.0.0.1:7897
    python src/fetch_fangzhen.py            # 只抓缺的
    python src/fetch_fangzhen.py --all      # 全部重抓
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
os.makedirs(RAW, exist_ok=True)

API = "https://zh.wikisource.org/w/api.php"
UA = ("histmap/0.1 (local historical-map project; "
      "fetching public-domain wikitext for the Tang fangzhen table)")
VOLUMES = list(range(64, 74))          # 方镇表 = 卷 64~73


def fetch(vol: int):
    page = f"新唐書/卷{vol:03d}"
    q = urllib.parse.urlencode({
        "action": "parse", "page": page, "prop": "wikitext",
        "format": "json", "formatversion": "1",
    })
    req = urllib.request.Request(f"{API}?{q}",
                                 headers={"User-Agent": UA,
                                          "Accept": "application/json"})
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=90) as r:
        data = json.loads(r.read().decode("utf-8"))
    if "error" in data:
        raise RuntimeError(data["error"].get("info", "unknown"))
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="全部重抓（默认只抓缺的）")
    args = ap.parse_args()

    got, skipped, failed = [], [], []
    for v in VOLUMES:
        dst = os.path.join(RAW, f"juan{v:03d}.json")
        if os.path.exists(dst) and not args.all:
            skipped.append(v)
            continue
        try:
            data = fetch(v)
            wt = (data.get("parse") or {}).get("wikitext") or ""
            if "{|" not in wt:
                failed.append((v, "wikitext 里没有表格"))
                continue
            json.dump(data, open(dst, "w", encoding="utf-8"), ensure_ascii=False)
            title = (data.get("parse") or {}).get("title", "?")
            print(f"  ✓ 卷{v:03d}  {title}  {len(wt)} 字节")
            got.append(v)
        except Exception as e:
            print(f"  ✗ 卷{v:03d}  失败: {e}")
            failed.append((v, str(e)))
        time.sleep(1.2)                # 对维基客气一点

    print(f"\n新抓 {len(got)} 卷，已存在 {len(skipped)} 卷，失败 {len(failed)} 卷")
    if skipped:
        print(f"  已存在: {skipped}")
    if failed:
        print(f"  失败: {failed}")
        print("  提示：维基文库可能需要走代理，设置 HTTPS_PROXY 后重试")
    print(f"\n下一步: python src/parse_fangzhen.py")


if __name__ == "__main__":
    main()
