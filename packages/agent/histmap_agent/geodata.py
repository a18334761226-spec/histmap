"""几何数据源：从 geoBoundaries 按国家代码拉真实行政区，以及名字匹配。

从 src/new_topic.py 搬过来的，搬的理由是**依赖方向**：题材起草图既要
「让模型定来源」也要「系统去拉真名」，如果图放在 new_topic 里、
又把 new_topic 的取数函数反向 import 回来，就成了循环。
放到中间这一层，new_topic 和 agent 都往下依赖它，方向是单向的。
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

CACHE = os.path.join(ROOT, "data", "cache")
GB_API = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/{adm}/"
UA = {"User-Agent": "histmap/0.1 (+https://github.com/; topic builder)"}

# 许可声明必须由系统按数据源给出，**不能采信模型**。
# 实测模型会把 geoBoundaries 说成 CC BY-SA 4.0 —— 既不是它真实的许可
# （gbOpen 多为 CC BY 4.0），而 BY-SA 还是传染性许可，本项目明确避开。
# 这种错误写在界面上就是法律风险，所以一律覆盖。
GB_LICENSE = {
    "CHN": "geoBoundaries CHN 为 PDDL v1.0（≈公有领域）",
    "VNM": "geoBoundaries VNM 为 CC BY 3.0 IGO（须署名 OCHA ROAP / 越南政府）",
    "USA": "geoBoundaries USA 为 CC BY 4.0（须署名 geoBoundaries / Wikimedia）",
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).lower()
    s = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", s)
    # 常见后缀：数据集里叫 "Alabama"，人会说 "State of Alabama" / "Alabama State"
    for junk in ("stateof", "provinceof", "republicof", "oblast", "krai",
                 "prefecture", "province", "state", "region", "county",
                 "省", "市", "府", "州", "道", "路"):
        if s.endswith(junk) and len(s) > len(junk) + 1:
            s = s[: -len(junk)]
    return s


def match_names(want: list[str], have: list[str]) -> tuple[dict, list]:
    """把规格里的单元名对到数据集里的真实名字。返回 (映射, 对不上的)。"""
    idx = {_norm(h): h for h in have}
    out, bad = {}, []
    for w in want:
        k = _norm(w)
        if k in idx:
            out[w] = idx[k]
            continue
        # 退化匹配：互相包含（"Dakota" 对 "North Dakota" 之类不做，容易错配；
        # 只做「数据集名以它开头/结尾」这一种，且要求唯一）
        cand = [h for h in have if _norm(h).startswith(k) or _norm(h).endswith(k)]
        if len(cand) == 1:
            out[w] = cand[0]
        else:
            bad.append(w)
    return out, bad


def fetch_adm(iso: str, adm: str, force: bool = False) -> str:
    """按国家代码+层级从 geoBoundaries 拉行政区文件，返回本地路径。"""
    iso, adm = iso.upper(), adm.upper()
    dst = os.path.join(CACHE, f"gb_{iso.lower()}_{adm.lower()}.geojson")
    if os.path.exists(dst) and not force and os.path.getsize(dst) > 2000:
        return dst
    os.makedirs(CACHE, exist_ok=True)
    api = GB_API.format(iso=iso, adm=adm)
    try:
        meta = json.loads(urllib.request.urlopen(
            urllib.request.Request(api, headers=UA), timeout=60).read())
    except Exception as e:
        raise SystemExit(f"取 {iso} {adm} 的元数据失败：{type(e).__name__}: {e}\n"
                         f"（检查国家代码是否为三字母 ISO3，层级是否形如 ADM1/ADM2）")
    url = meta.get("simplifiedGeometryGeoJSON") or meta.get("gjDownloadURL")
    if not url:
        raise SystemExit(f"{iso} {adm} 没有可下载的几何")
    print(f"  下载 {meta.get('boundaryName')} {adm} …")
    req = urllib.request.Request(url, headers=UA)
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        op = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        data = op.open(req, timeout=600).read()
    else:
        data = urllib.request.urlopen(req, timeout=600).read()
    open(dst, "wb").write(data)
    print(f"  -> {dst}  {len(data)/1024/1024:.1f} MB")
    return dst


def units_of(path: str, name_field: str = "shapeName") -> list[str]:
    gj = json.load(open(path, encoding="utf-8"))
    out = []
    for f in gj.get("features", []):
        n = str((f.get("properties") or {}).get(name_field) or "").strip()
        if n:
            out.append(n)
    return sorted(set(out))


def gb_license(sources: list[dict]) -> str:
    isos = [(g.get("iso") or "").upper() for g in sources if g.get("iso")]
    if not isos:
        return "见各数据源官方许可"
    parts = [GB_LICENSE.get(i, f"geoBoundaries {i} 为 CC BY 4.0（须署名）")
             for i in isos]
    return "；".join(dict.fromkeys(parts))
