#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测 AtlasPI API 的 schema、几何支持与年份覆盖。"""
import json
import os
import ssl
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "atlaspi-probe.txt")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897"
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
    urllib.request.HTTPSHandler(context=CTX))
opener.addheaders = [("User-Agent", "dsh-agent")]


def get(path):
    url = "https://atlaspi.it" + path
    try:
        with opener.open(url, timeout=60) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return None, str(e)


L = []

# 1) 实体 schema
st, j = get("/v1/snapshot/1941")
L.append(f"=== /v1/snapshot/1941  status={st} ===")
if isinstance(j, dict):
    L.append(f"顶层字段: {list(j.keys())}")
    L.append(f"count: {j.get('count')}")
    ents = j.get("entities") or []
    if ents:
        e0 = ents[0]
        L.append(f"\n实体字段: {list(e0.keys())}")
        L.append("\n第一个实体（截断）:")
        L.append(json.dumps(e0, ensure_ascii=False)[:1200])
        # 检查几何
        geom_keys = [k for k in e0 if "geom" in k.lower() or "bound" in k.lower()
                     or "poly" in k.lower() or "coord" in k.lower()]
        L.append(f"\n含几何语义的字段: {geom_keys}")
        # 统计
        L.append(f"非空实体: {len(ents)}")
        names = [e.get("name") or e.get("name_en") or e.get("title") or "?" for e in ents[:12]]
        L.append(f"前 12 个名称: {names}")

# 2) 年份覆盖
L.append("\n\n=== 年份覆盖测试 ===")
for y in [800, 807, 900, 1000, 1200, 1300, 1500, 1700, 1800, 1900, 1939, 1941, 1945, 2000]:
    st, j = get(f"/v1/snapshot/{y}")
    if isinstance(j, dict):
        L.append(f"  {y}: count={j.get('count')}")
    else:
        L.append(f"  {y}: 失败 -> {str(j)[:80]}")

# 3) 找 GeoJSON 端点
L.append("\n\n=== 端点探测 ===")
for p in ["/v1/snapshot/1941?format=geojson",
          "/v1/geojson/1941",
          "/v1/entities/1",
          "/v1/entity/1",
          "/openapi.json"]:
    st, j = get(p)
    if isinstance(j, dict):
        L.append(f"  {p}: status={st} keys={list(j.keys())[:8]}")
        if p.endswith("openapi.json"):
            paths = list((j.get("paths") or {}).keys())
            L.append(f"    OpenAPI 路径数: {len(paths)}")
            L.append("    路径样例:")
            for pp in paths[:45]:
                L.append(f"      {pp}")
    else:
        L.append(f"  {p}: status={st} err={str(j)[:60]}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("OK")
