"""
histmap-core · 统一数据契约

这是整个系统的枢纽：**任何题材的数据，最终都必须转成这里的结构**。
渲染引擎只认这个契约，因此新增题材时渲染层永不改动。

设计原则
--------
1. 坐标一律 WGS84 经纬度 (lon, lat)
2. 一个 Region 可含多个环（多边形 + 洞）
3. 颜色可留空 —— 由样式层决定，保证「同一数据换风格」可行
4. 所有对象可 JSON 往返（便于 API 传输与归档）
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Region:
    """一个地图要素：一块领土/势力范围。"""

    id: str
    name: str
    # 多环：rings[0] 为外环，其余为洞。每环为 [(lon, lat), ...]
    rings: list[list[tuple[float, float]]] = field(default_factory=list)
    # 无多边形时退化为点位（用于 Voronoi 或点标注）
    point: tuple[float, float] | None = None
    color: str | None = None
    label_pos: tuple[float, float] | None = None
    props: dict[str, Any] = field(default_factory=dict)

    def bbox(self):
        xs, ys = [], []
        for ring in self.rings:
            for lon, lat in ring:
                xs.append(lon)
                ys.append(lat)
        if self.point:
            xs.append(self.point[0])
            ys.append(self.point[1])
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

    def to_dict(self):
        d = asdict(self)
        d["rings"] = [[list(p) for p in ring] for ring in self.rings]
        return d

    @staticmethod
    def from_dict(d: dict) -> "Region":
        return Region(
            id=d["id"],
            name=d.get("name", d["id"]),
            rings=[[tuple(p) for p in ring] for ring in d.get("rings", [])],
            point=tuple(d["point"]) if d.get("point") else None,
            color=d.get("color"),
            label_pos=tuple(d["label_pos"]) if d.get("label_pos") else None,
            props=d.get("props", {}),
        )


@dataclass
class Frame:
    """一个时间切片。"""

    year: int
    regions: list[Region] = field(default_factory=list)
    title: str | None = None
    subtitle: str | None = None
    # 该帧的补充说明（用于字幕/注释）
    note: str | None = None

    def to_dict(self):
        return {
            "year": self.year,
            "regions": [r.to_dict() for r in self.regions],
            "title": self.title,
            "subtitle": self.subtitle,
            "note": self.note,
        }

    @staticmethod
    def from_dict(d: dict) -> "Frame":
        return Frame(
            year=d["year"],
            regions=[Region.from_dict(r) for r in d.get("regions", [])],
            title=d.get("title"),
            subtitle=d.get("subtitle"),
            note=d.get("note"),
        )


@dataclass
class Series:
    """一个完整的场景数据：若干帧 + 全局元信息。"""

    dataset: str
    frames: list[Frame] = field(default_factory=list)
    title: str | None = None
    # 全局视野范围 (min_lon, min_lat, max_lon, max_lat)；None 表示自动计算
    bbox: tuple[float, float, float, float] | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def auto_bbox(self):
        """对所有帧求并集范围。"""
        if self.bbox:
            return self.bbox
        xs, ys = [], []
        for f in self.frames:
            for r in f.regions:
                b = r.bbox()
                if b:
                    xs += [b[0], b[2]]
                    ys += [b[1], b[3]]
        if not xs:
            return (-180.0, -90.0, 180.0, 90.0)
        return (min(xs), min(ys), max(xs), max(ys))

    def to_dict(self):
        return {
            "dataset": self.dataset,
            "title": self.title,
            "bbox": list(self.bbox) if self.bbox else None,
            "meta": self.meta,
            "frames": [f.to_dict() for f in self.frames],
        }

    @staticmethod
    def from_dict(d: dict) -> "Series":
        return Series(
            dataset=d["dataset"],
            title=d.get("title"),
            bbox=tuple(d["bbox"]) if d.get("bbox") else None,
            meta=d.get("meta", {}),
            frames=[Frame.from_dict(f) for f in d.get("frames", [])],
        )

    # ── 便捷构造 ─────────────────────────────────────────────
    @staticmethod
    def from_geojson(dataset: str, year: int, geojson: dict,
                     name_field: str = "name", title: str | None = None) -> "Series":
        """从 GeoJSON FeatureCollection 构造单帧 Series。

        支持 Polygon 与 MultiPolygon；坐标顺序经 GeoJSON 规范为 (lon, lat)。
        """
        regions = []
        for feat in geojson.get("features", []):
            props = feat.get("properties", {}) or {}
            geom = feat.get("geometry") or {}
            gtype = geom.get("type")
            coords = geom.get("coordinates") or []

            rings: list[list[tuple[float, float]]] = []
            if gtype == "Polygon":
                for ring in coords:
                    rings.append([(float(x), float(y)) for x, y, *_ in ring])
            elif gtype == "MultiPolygon":
                for poly in coords:
                    for ring in poly:
                        rings.append([(float(x), float(y)) for x, y, *_ in ring])

            rid = str(props.get("id") or props.get(name_field) or len(regions))
            regions.append(Region(
                id=rid,
                name=str(props.get(name_field, rid)),
                rings=rings,
                props=props,
            ))

        return Series(dataset=dataset, title=title,
                      frames=[Frame(year=year, regions=regions)])
