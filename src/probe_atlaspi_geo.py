#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试 AtlasPI 的 GeoJSON 导出，确认是否有边界多边形。"""
import json
import os
import ssl
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "atlaspi-geo.txt")
CACHE = os.path.join(ROOT, "data", "cache")
os.makedirs(CACHE, exist_ok=True)

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:7897"
opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}),
    urllib.request.HTTPSHandler(context=CTX))
opener.addheaders = [("User-Agent", "dsh-agent")]


def get_text(path):
    try:
        with opener.open("https://atlaspi.it" + path, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


L = []

# 1) OpenAPI 里 export/geojson 的参数
st, txt = get_text("/openapi.json")
if st == 200:
    spec = json.loads(txt)
    paths = spec.get("paths", {})
    for p in ["/v1/export/geojson", "/v1/snapshot/{year}", "/v1/entities/{entity_id}/evolution"]:
        if p in paths:
            L.append(f"=== {p} ===")
            for method, meta in paths[p].items():
                if method in ("get", "post"):
                    L.append(f"  {method.upper()}  {meta.get('summary','')}")
                    for prm in meta.get("parameters", []):
                        sch = prm.get("schema", {})
                        L.append(f"    参数 {prm.get('name')} ({prm.get('in')}) "
                                 f"required={prm.get('required')} type={sch.get('type')} "
                                 f"default={sch.get('default')}")
            L.append("")

# 2) 实际请求 geojson 导出
L.append("=== 实测 /v1/export/geojson ===")
for q in ["/v1/export/geojson",
          "/v1/export/geojson?year=1941",
          "/v1/export/geojson?year=1941&limit=5"]:
    st, txt = get_text(q)
    L.append(f"\n--- {q}  status={st}  len={len(txt) if txt else 0} ---")
    if st == 200 and txt:
        try:
            j = json.loads(txt)
            if isinstance(j, dict):
                L.append(f"  顶层: {list(j.keys())[:10]}")
                feats = j.get("features") or []
                L.append(f"  features 数: {len(feats)}")
                if feats:
                    f0 = feats[0]
                    L.append(f"  feature 字段: {list(f0.keys())}")
                    g = f0.get("geometry") or {}
                    L.append(f"  geometry.type: {g.get('type')}")
                    c = g.get("coordinates")
                    if c:
                        s = json.dumps(c)[:200]
                        L.append(f"  coordinates 前 200 字符: {s}")
                    L.append(f"  properties: {json.dumps(f0.get('properties',{}), ensure_ascii=False)[:300]}")
        except Exception as e:
            L.append(f"  非 JSON 或解析失败: {e}")
            L.append(f"  前 300 字符: {txt[:300]}")
    else:
        L.append(f"  错误: {str(txt)[:120]}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(L))
print("OK")
