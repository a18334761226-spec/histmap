"""
AtlasPI 数据集适配器

AtlasPI (atlaspi.it) 是历史地理的公开 REST API：
  * 1038 个历史政体，覆盖约公元前 4500 – 2024 年
  * `/v1/export/geojson?year=YYYY` 返回**真实边界多边形**
  * 每条记录带 confidence_score 与 status(confirmed/uncertain/disputed)
  * Apache-2.0，免费、无需注册、无需 API Key

这是本系统的**宏观层**主力数据源（帝国/王国/政体级，全球覆盖）。
中国断代史的藩镇/州级细粒度需另有适配器。
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.request

from ..contract import Frame, Region, Series
from .base import DatasetAdapter, Manifest, register

API = "https://atlaspi.it"


def _opener():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or "http://127.0.0.1:7897")
    op = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        urllib.request.HTTPSHandler(context=ctx))
    op.addheaders = [("User-Agent", "histmap/0.1")]
    return op


@register
class AtlasPIAdapter(DatasetAdapter):
    manifest = Manifest(
        id="atlaspi",
        title="AtlasPI 世界历史政体",
        source="AtlasPI (atlaspi.it) — 公开 REST API",
        license="Apache-2.0",
        commercial_ok=True,
        redistribution_ok=False,   # 不随仓库分发，运行时按需拉取
        coverage_years=(-4500, 2024),
        region="全球",
        granularity="政体（帝国/王国/城邦）",
        confidence=0.75,
        fields=["name_original", "entity_type", "status", "confidence_score",
                "year_start", "year_end", "capital"],
        notes="边界为学术近似；每条记录自带 confidence_score 与 status，"
              "争议领土会同时给出多个版本。",
    )

    # ── 抓取 ─────────────────────────────────────────────────
    def fetch_year(self, year: int, use_cache: bool = True) -> dict:
        """取某年的 GeoJSON FeatureCollection（带本地缓存）。"""
        name = f"atlaspi_{year}.geojson"
        path = self.cache_path(name)
        if use_cache and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        url = f"{API}/v1/export/geojson?year={year}"
        with _opener().open(url, timeout=120) as r:
            data = json.loads(r.read().decode("utf-8"))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return data

    def fetch_entity(self, entity_id: int) -> dict:
        url = f"{API}/v1/entity/{entity_id}"
        with _opener().open(url, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    # ── 构建 Series ──────────────────────────────────────────
    def build(self, years=None, bbox=None, min_confidence: float = 0.0,
              types=None, continents=None, **kwargs) -> Series:
        """构建跨年份的 Series。

        years        要出图的年份列表
        min_confidence  过滤低于该置信度的实体（默认为 0，全部保留）
        types        只要这些 entity_type（如 ["empire","kingdom","republic"]）
        continents   只要这些大洲（如 ["Europe"]）
        """
        years = list(years or [1941])
        frames = []
        for y in sorted(years):
            gj = self.fetch_year(y)
            regions = []
            for feat in gj.get("features", []):
                props = feat.get("properties", {}) or {}
                conf = props.get("confidence_score") or 0.0
                if conf < min_confidence:
                    continue
                if types and props.get("entity_type") not in types:
                    continue
                if continents and props.get("continent") not in continents:
                    continue

                geom = feat.get("geometry") or {}
                rings = self._rings(geom)
                if not rings:
                    continue

                name = (props.get("name_original") or "").strip() or f"#{feat.get('id')}"
                regions.append(Region(
                    id=str(feat.get("id") or name),
                    name=name,
                    rings=rings,
                    props={
                        "entity_type": props.get("entity_type"),
                        "status": props.get("status"),
                        "confidence_score": conf,
                        "year_start": props.get("year_start"),
                        "year_end": props.get("year_end"),
                        "name_lang": props.get("name_original_lang"),
                    },
                ))

            frames.append(Frame(year=y, regions=regions,
                                title=f"{y} 年",
                                subtitle=f"{len(regions)} 个政治实体"))

        meta = {
            "total_entities": sum(len(f.regions) for f in frames),
            "source": "AtlasPI",
            "license": "Apache-2.0",
        }
        return Series(dataset=self.manifest.id, frames=frames,
                      title="世界历史政体", bbox=bbox, meta=meta)

    @staticmethod
    def _rings(geom: dict):
        """GeoJSON 几何 -> 环列表（打平所有多边形）。"""
        gtype = geom.get("type")
        coords = geom.get("coordinates") or []
        rings = []
        if gtype == "Polygon":
            for ring in coords:
                rings.append([(float(p[0]), float(p[1])) for p in ring])
        elif gtype == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    rings.append([(float(p[0]), float(p[1])) for p in ring])
        return rings

    # ── 可用性自检（供可行性评估器）──────────────────────────
    def availability(self, years=None) -> dict:
        years = list(years or [1941])
        info = {
            "dataset": self.manifest.id,
            "commercial_ok": self.manifest.commercial_ok,
            "license": self.manifest.license,
            "years_checked": {},
            "issues": [],
        }
        for y in years:
            try:
                gj = self.fetch_year(y)
                feats = gj.get("features", [])
                scores = [(f.get("properties") or {}).get("confidence_score") or 0
                          for f in feats]
                low = sum(1 for s in scores if s < 0.6)
                info["years_checked"][y] = {
                    "entities": len(feats),
                    "avg_confidence": round(sum(scores) / len(scores), 3) if scores else 0,
                    "low_confidence": low,
                }
            except Exception as e:
                info["issues"].append(f"{y}: {e}")
        return info
