"""
histmap-core · 几何质量闸门

**为什么必须有这个模块**
历史 GIS 数据里混着大量「合成占位几何」：当某个政体没有真实边界数据时，
数据源常以**首都为圆心生成一个圆**来占位。这类图形在视觉上与真实疆界无异，
一旦交付给客户（尤其是历史区观众）会立刻穿帮。

实测案例（AtlasPI 1941）：
    德意志国   33 顶点  形态变异系数 CV=0.171   → 实为圆饼，官方置信度却是 0.65
    意大利王国 33 顶点  CV=0.105                → 圆饼，置信度 0.8
    大日本帝国 15 顶点  CV=0.550                → 极粗块，置信度 0.9

**结论：数据源自带的 confidence_score 不能反映几何质量，必须独立校验。**

判据
----
先算每个多边形的「形态变异系数」CV = std(顶点到质心距离) / mean(...)：
    CV ≈ 0      → 完美圆（合成占位）
    CV 较大     → 不规则，倾向真实边界
再结合顶点数与环数：
    ring=1 且 顶点≤50 且 CV<0.35  → synthetic（合成占位）
    CV < 0.10                     → synthetic
    顶点 < 120 或 CV < 0.30       → coarse（粗略，可用但需标注）
    否则                          → real
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

REAL = "real"
COARSE = "coarse"
SYNTHETIC = "synthetic"
INVALID = "invalid"

LEVEL_LABEL = {
    REAL: "真实边界",
    COARSE: "粗略",
    SYNTHETIC: "合成占位",
    INVALID: "无效几何",
}

# 阈值（可被 style / scene 覆盖）
SYNTHETIC_CV = 0.10          # 低于此值直接判为圆
SYNTHETIC_CV_SINGLE = 0.35   # 单环小顶点数的放宽阈值
SYNTHETIC_MAX_VERTS = 50     # 单环且顶点数不超过此值 → 疑似圆
COARSE_CV = 0.30
COARSE_VERTS = 120


@dataclass
class GeometryQuality:
    level: str = INVALID
    vertex_count: int = 0
    ring_count: int = 0
    cv: float = 0.0            # 主要环的形态变异系数
    area_deg2: float = 0.0     # 经纬度平方度（粗略面积）
    score: float = 0.0         # 0~1，越高越可信
    reasons: list[str] = field(default_factory=list)

    @property
    def label(self):
        return LEVEL_LABEL.get(self.level, self.level)

    @property
    def is_usable(self):
        return self.level in (REAL, COARSE)

    def to_dict(self):
        return {
            "level": self.level, "label": self.label,
            "vertex_count": self.vertex_count, "ring_count": self.ring_count,
            "cv": round(self.cv, 4), "area_deg2": round(self.area_deg2, 6),
            "score": round(self.score, 3), "reasons": self.reasons,
        }


def _ring_cv(ring):
    n = len(ring)
    if n < 3:
        return None
    cx = sum(p[0] for p in ring) / n
    cy = sum(p[1] for p in ring) / n
    ds = [math.hypot(p[0] - cx, p[1] - cy) for p in ring]
    m = sum(ds) / n
    if m <= 1e-12:
        return None
    var = sum((d - m) ** 2 for d in ds) / n
    return math.sqrt(var) / m


def _ring_area(ring):
    n = len(ring)
    if n < 3:
        return 0.0
    s = 0.0
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def assess_region(region) -> GeometryQuality:
    """评估单个 Region 的几何质量。"""
    q = GeometryQuality()
    rings = [r for r in (region.rings or []) if r and len(r) >= 3]
    q.ring_count = len(rings)
    if not rings:
        q.level = INVALID
        q.reasons.append("无有效环（顶点<3）")
        return q

    # 主环 = 顶点最多的环
    main = max(rings, key=len)
    q.vertex_count = len(main)
    cv = _ring_cv(main)
    q.cv = cv if cv is not None else 0.0
    q.area_deg2 = sum(_ring_area(r) for r in rings)

    if q.vertex_count < 4 or q.area_deg2 <= 1e-9:
        q.level = INVALID
        q.reasons.append(f"退化几何：{q.vertex_count} 顶点 / 面积 {q.area_deg2:.2e}")
        return q

    # 单环 + 顶点少 + 接近圆 → 合成占位
    if (q.ring_count == 1 and q.vertex_count <= SYNTHETIC_MAX_VERTS
            and q.cv < SYNTHETIC_CV_SINGLE):
        q.level = SYNTHETIC
        q.reasons.append(
            f"单环 {q.vertex_count} 顶点、形态变异系数 {q.cv:.3f} → 高度疑似以质心为圆心的合成圆")
        return q

    if q.cv < SYNTHETIC_CV:
        q.level = SYNTHETIC
        q.reasons.append(f"形态变异系数 {q.cv:.3f} < {SYNTHETIC_CV} → 接近完美圆")
        return q

    if q.vertex_count < COARSE_VERTS or q.cv < COARSE_CV:
        q.level = COARSE
        q.reasons.append(
            f"顶点 {q.vertex_count} / CV {q.cv:.3f} → 粗略，可用于示意但需标注")
    else:
        q.level = REAL

    # 综合分：顶点数与形态各占一半
    v_score = min(1.0, math.log10(max(q.vertex_count, 1)) / math.log10(2000))
    c_score = min(1.0, q.cv / 0.5)
    q.score = round(0.5 * v_score + 0.5 * c_score, 3)
    return q


def assess_series(series, attach: bool = True) -> dict:
    """评估整个 Series，可选把结果写回 region.props['geometry_quality']。"""
    from collections import Counter
    dist = Counter()
    suspects = []
    for fr in series.frames:
        for reg in fr.regions:
            q = assess_region(reg)
            dist[q.level] += 1
            if attach:
                reg.props = dict(reg.props or {})
                reg.props["geometry_quality"] = q.to_dict()
            if q.level in (SYNTHETIC, INVALID):
                suspects.append({
                    "year": fr.year, "name": reg.name, "level": q.level,
                    "label": q.label, "cv": round(q.cv, 3),
                    "verts": q.vertex_count, "rings": q.ring_count,
                    "reasons": q.reasons,
                })
    total = sum(dist.values()) or 1
    return {
        "total": sum(dist.values()),
        "distribution": dict(dist),
        "usable_ratio": round(
            (dist[REAL] + dist[COARSE]) / total, 3),
        "real_ratio": round(dist[REAL] / total, 3),
        "suspects": suspects,
        "verdict": _verdict(dist),
    }


def _verdict(dist) -> str:
    total = sum(dist.values()) or 1
    syn = dist[SYNTHETIC] + dist[INVALID]
    if syn / total > 0.30:
        return (f"⚠️ 有 {syn}/{total} 个实体为合成/无效几何，"
                f"**不可直接用于交付**；需换数据源或人工补边界")
    if syn > 0:
        return (f"⚠️ 有 {syn}/{total} 个实体为合成/无效几何，"
                f"交付前必须逐个确认并在图中标注")
    return "✅ 未检出合成几何"


def filter_regions(series, min_level=(REAL, COARSE)):
    """按质量层级过滤（会原地修改 series.frames[].regions）。"""
    kept, dropped = 0, 0
    for fr in series.frames:
        new = []
        for reg in fr.regions:
            q = assess_region(reg)
            if q.level in min_level:
                new.append(reg)
                kept += 1
            else:
                dropped += 1
        fr.regions = new
    return {"kept": kept, "dropped": dropped}
