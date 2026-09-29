#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Wikidata 覆盖度探测 · 唐州治所坐标
======================================
回答的问题：**237 个唐州里，有多少能从 Wikidata 拿到可用坐标？**

为什么要单独探：Wikidata 的 SPARQL 有两条坑
  1. 通过代理访问会被拦（要求描述性 User-Agent），且重查询会 504
  2. 同名条目很多——「庆州」同时有唐代的州、宋代的州、朝鲜的庆州、
     还有消歧义页。直接按名字取坐标会取错。

本脚本按名字批量查，把命中的条目全部拉回来让人工/规则筛选，
并统计「有坐标的州」占比，作为是否采用 Wikidata 的决策依据。

用法
    python src/probe_wikidata_gazetteer.py
"""
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
STATE = os.path.join(ROOT, "data", "processed", "state_807.json")
OUT = os.path.join(ROOT, "data", "processed", "wikidata_probe.json")

UA = ("histmap-research/0.1 (local historical-map project; "
      "one-off gazetteer coverage check; contact: local)")
PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897"
ENDPOINT = "https://query.wikidata.org/sparql"


def zhou_names():
    """从 807 年状态里取州名。

    注意：早期解析器把「州」后缀剥掉了，所以单字名要补回「州」才能查。
    多字名（如被截断的「京兆」→「兆」）补出来是错的，脚本会把它们标出来。
    """
    st = json.load(open(STATE, encoding="utf-8"))
    names = set()
    for v in st["fanzhen"].values():
        for z in v.get("zhou") or []:
            names.add(z)
    cand = []
    for n in sorted(names):
        cand.append((n, n + "州"))
    return cand


def query(names):
    vals = " ".join(f'"{n}"@zh' for n in names)
    q = f"""
SELECT ?item ?itemLabel ?classLabel ?coord ?adminLabel WHERE {{
  VALUES ?nm {{ {vals} }}
  ?item rdfs:label ?nm ;
        wdt:P31 ?class .
  OPTIONAL {{ ?item wdt:P625 ?coord }}
  OPTIONAL {{ ?item wdt:P131 ?admin }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "zh,en". }}
}}
"""
    url = ENDPOINT + "?format=json&query=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/sparql-results+json"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    with opener.open(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))["results"]["bindings"]


def main():
    cand = zhou_names()
    print(f"807 年州名 {len(cand)} 个（单字名已补「州」后缀用于检索）\n")

    # 名字太长或明显不是「X州」形态的，标出来人工看
    weird = [n for n, q in cand if len(n) > 2]
    if weird:
        print(f"⚠️  名字异常（可能是解析截断导致），共 {len(weird)} 个: {weird}\n")

    all_rows = []
    B = 60
    # Wikidata 在故障期会把匿名查询限到 1 请求/分钟，且返回 429。
    # 所以批次间隔必须够长，并对 429 做退避重试，否则统计会严重偏低
    # （踩过：间隔 1 秒时后面 3 批全被拒，覆盖率被算成 4%）。
    DELAY = float(os.environ.get("WDQS_DELAY") or 65)
    for i in range(0, len(cand), B):
        batch = [q for _, q in cand[i:i + B]]
        rows = None
        for attempt in range(3):
            try:
                rows = query(batch)
                break
            except Exception as e:
                code = getattr(e, "code", None)
                print(f"  批次 {i//B+1} 第 {attempt+1} 次失败: {e}")
                if code == 429 or "429" in str(e):
                    time.sleep(DELAY)          # 限流：等满一个窗口再试
                else:
                    time.sleep(5)
        if rows is not None:
            all_rows += rows
            print(f"  批次 {i//B+1}: 查 {len(batch)} 名 → 命中 {len(rows)} 条")
        if i + B < len(cand):
            time.sleep(DELAY)

    json.dump(all_rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 统计：每个州名是否有「带坐标且类为州」的条目
    from collections import defaultdict
    by_name = defaultdict(list)
    for r in all_rows:
        nm = r["itemLabel"]["value"]
        cls = r.get("classLabel", {}).get("value", "")
        coord = r.get("coord", {}).get("value")
        by_name[nm].append({"cls": cls, "coord": coord,
                            "qid": r["item"]["value"].rsplit("/", 1)[-1]})

    good = {n: v for n, v in by_name.items()
            if any(x["coord"] and "州" in x["cls"] for x in v)}
    has_item = {n: v for n, v in by_name.items() if v}
    print(f"\n命中有条目的州名: {len(has_item)}/{len(cand)} "
          f"({len(has_item)/max(len(cand),1)*100:.0f}%)")
    print(f"**有可用坐标的州名: {len(good)}/{len(cand)} "
          f"({len(good)/max(len(cand),1)*100:.0f}%)**")

    missing = [n for n, q in cand if q not in by_name]
    print(f"\n完全没命中的 {len(missing)} 个（前 30）: {missing[:30]}")
    print(f"\n详细结果 -> {OUT}")


if __name__ == "__main__":
    main()
