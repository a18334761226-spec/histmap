"""
CShapes 2.0 数据集适配器

来源：ETH Zürich, International Conflict Research
      Schvitz, Guy, et al. 2022. "Mapping The International System, 1886-2017:
      The CShapes 2.0 Dataset." Journal of Conflict Resolution 66(1): 144-61.

特点
  * 覆盖 1886–2019（CShapes-Europe 上溯 1816）
  * **真实边界多边形**（加拿大 3.2 万顶点、美国 1 万顶点），非合成占位
  * 每条记录是一个「国家-时段」片段，字段 gwsyear/gweyear 界定生效区间
  * 许可：学术引用要求；商用条款需与作者确认（见 manifest.notes）

适用：近现代题材（二战、冷战、殖民体系）。中国断代史不适用。
"""
from __future__ import annotations

import json
import os

from ..contract import Frame, Region, Series
from .base import DatasetAdapter, Manifest, register

DEFAULT_FILE = "CShapes-2.0.geojson"


@register
class CShapesAdapter(DatasetAdapter):
    manifest = Manifest(
        id="cshapes",
        title="CShapes 2.0 近现代国界",
        source="ETH Zürich ICR — CShapes 2.0 (Schvitz et al. 2022, JCR)",
        license="Academic citation required; commercial terms to be confirmed",
        commercial_ok=False,          # 商用前须与作者确认
        redistribution_ok=False,      # 下载器模式
        coverage_years=(1886, 2019),
        region="全球",
        granularity="国家（含附属地）",
        confidence=0.9,               # 几何为真实边界，置信度高
        fields=["cntry_name", "area", "capname", "caplong", "caplat",
                "gwcode", "gwsyear", "gweyear"],
        notes="**真实边界多边形**，与 AtlasPI 的合成圆饼形成鲜明对比。"
              "引用：Schvitz et al. 2022, Journal of Conflict Resolution 66(1):144-61。"
              "商用交付前需确认授权。",
    )

    def __init__(self, cache_dir: str | None = None, filename: str | None = None):
        super().__init__(cache_dir)
        self.filename = filename or DEFAULT_FILE

    # ── 载入 ─────────────────────────────────────────────────
    def load(self, use_cache: bool = True):
        path = self.cache_path(self.filename)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"未找到 {path}\n"
                f"请先下载：curl -L -o {path} "
                f"https://icr.ethz.ch/data/cshapes/{self.filename}")
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _active(props: dict, year: int) -> bool:
        try:
            s = int(props.get("gwsyear") or 0)
            e = int(props.get("gweyear") or 9999)
        except Exception:
            return False
        return s <= year <= e

    @staticmethod
    def _rings(geom: dict):
        gtype = (geom or {}).get("type")
        coords = (geom or {}).get("coordinates") or []
        rings = []
        if gtype == "Polygon":
            for r in coords:
                rings.append([(float(p[0]), float(p[1])) for p in r])
        elif gtype == "MultiPolygon":
            for poly in coords:
                for r in poly:
                    rings.append([(float(p[0]), float(p[1])) for p in r])
        return rings

    # ── 构建 Series ──────────────────────────────────────────
    def build(self, years=None, bbox=None, merges=None, **kwargs) -> Series:
        """按年份构建 Series。

        merges  可选：把若干 cntry_name 合并为一个实体（如殖民地并入宗主国），
                形如 {"British Empire": ["British Raj", "British Burma", ...]}
        """
        years = [int(y) for y in (years or [1941])]
        gj = self.load()
        feats = gj.get("features", [])

        # 预建反向索引：名字 -> 合并组
        name2group = {}
        for gname, members in (merges or {}).items():
            for m in members:
                name2group[m] = gname

        frames = []
        for y in years:
            groups: dict[str, Region] = {}
            for feat in feats:
                p = feat.get("properties") or {}
                if not self._active(p, y):
                    continue
                name = (p.get("cntry_name") or "").strip()
                if not name:
                    continue
                rings = self._rings(feat.get("geometry"))
                if not rings:
                    continue

                key = name2group.get(name, name)
                if key not in groups:
                    groups[key] = Region(
                        id=str(key), name=key, rings=[],
                        props={"members": [], "area": 0, "status_source": "cshapes"},
                    )
                r = groups[key]
                r.rings.extend(rings)
                r.props["members"].append(name)
                try:
                    r.props["area"] += float(p.get("area") or 0)
                except Exception:
                    pass

            regions = sorted(groups.values(), key=lambda r: -len(r.rings))
            frames.append(Frame(
                year=y, regions=regions,
                title=f"{y} 年",
                subtitle=f"{len(regions)} 个政治实体",
            ))

        return Series(dataset=self.manifest.id, frames=frames,
                      title="近现代国界", bbox=bbox,
                      meta={"source": "CShapes 2.0",
                            "citation": "Schvitz et al. 2022, JCR 66(1):144-61"})

    def availability(self, years=None) -> dict:
        years = [int(y) for y in (years or [1941])]
        info = {"dataset": self.manifest.id,
                "commercial_ok": self.manifest.commercial_ok,
                "license": self.manifest.license, "years_checked": {},
                "issues": []}
        try:
            gj = self.load()
            feats = gj.get("features", [])
            for y in years:
                act = [f for f in feats if self._active(f.get("properties") or {}, y)]
                info["years_checked"][y] = {"segments": len(act),
                                            "distinct_countries":
                                                len({(f["properties"] or {}).get("cntry_name")
                                                     for f in act})}
        except Exception as e:
            info["issues"].append(str(e))
        return info
