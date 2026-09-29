"""
histmap-core · 地图投影

把 WGS84 经纬度投影到画布像素坐标。纯 Python + math，无额外依赖。

支持：
  equirectangular  等距圆柱（简单，适合全球）
  mercator         墨卡托（适合中低纬度，二战欧洲常用）
  lambert          兰伯特等角圆锥（适合中纬度大陆，如中国、欧洲）
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Viewport:
    """画布上用于绘制地图的矩形区域（像素）。"""

    x: float
    y: float
    width: float
    height: float


class Projection:
    """投影基类：经纬度 -> 像素。

    子类实现 _project(lon, lat) -> (x, y)，返回「投影平面坐标」；
    基类负责把它等比缩放并平移到 Viewport 内（保持长宽比，居中）。
    """

    name = "base"

    def __init__(self, bbox, viewport: Viewport, padding: float = 0.0):
        self.bbox = bbox
        self.vp = viewport
        self.padding = padding
        self._fit()

    # ── 子类实现 ─────────────────────────────────────────────
    def _project(self, lon: float, lat: float):
        raise NotImplementedError

    # ── 缩放计算 ─────────────────────────────────────────────
    def _fit(self):
        min_lon, min_lat, max_lon, max_lat = self.bbox
        # 采样边界求投影后范围
        xs, ys = [], []
        lat_steps = 24
        lon_steps = 24
        for i in range(lon_steps + 1):
            lon = min_lon + (max_lon - min_lon) * i / lon_steps
            for j in range(lat_steps + 1):
                lat = min_lat + (max_lat - min_lat) * j / lat_steps
                x, y = self._project(lon, lat)
                xs.append(x)
                ys.append(y)
        self.px_min, self.px_max = min(xs), max(xs)
        self.py_min, self.py_max = min(ys), max(ys)

        avail_w = self.vp.width - 2 * self.padding
        avail_h = self.vp.height - 2 * self.padding
        span_x = max(self.px_max - self.px_min, 1e-9)
        span_y = max(self.py_max - self.py_min, 1e-9)
        self.scale = min(avail_w / span_x, avail_h / span_y)

        # 居中偏移
        self.off_x = self.vp.x + self.padding + (avail_w - span_x * self.scale) / 2 - self.px_min * self.scale
        self.off_y = self.vp.y + self.padding + (avail_h - span_y * self.scale) / 2 - self.py_min * self.scale

    # ── 对外接口 ─────────────────────────────────────────────
    def __call__(self, lon: float, lat: float):
        x, y = self._project(lon, lat)
        return (x * self.scale + self.off_x, y * self.scale + self.off_y)

    def project_ring(self, ring):
        return [self(lon, lat) for lon, lat in ring]


class Equirectangular(Projection):
    name = "equirectangular"

    def _project(self, lon, lat):
        return (math.radians(lon), math.radians(lat))


class Mercator(Projection):
    name = "mercator"

    def _project(self, lon, lat):
        lat = max(min(lat, 85.0), -85.0)
        x = math.radians(lon)
        y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
        return (x, -y)          # 屏幕 y 向下


class LambertConformal(Projection):
    """兰伯特等角圆锥，双标准纬线。适合中纬度东西向延伸的区域。"""

    name = "lambert"

    def __init__(self, bbox, viewport, padding=0.0, std_parallels=None):
        min_lon, min_lat, max_lon, max_lat = bbox
        if std_parallels is None:
            std_parallels = (min_lat + (max_lat - min_lat) * 0.25,
                             min_lat + (max_lat - min_lat) * 0.75)
        self.lat1 = math.radians(std_parallels[0])
        self.lat2 = math.radians(std_parallels[1])
        self.lon0 = math.radians((min_lon + max_lon) / 2)
        self.lat0 = math.radians((min_lat + max_lat) / 2)

        if abs(self.lat1 - self.lat2) < 1e-9:
            n = math.sin(self.lat1)
        else:
            n = (math.log(math.cos(self.lat1) / math.cos(self.lat2)) /
                 math.log(math.tan(math.pi / 4 + self.lat2 / 2) /
                          math.tan(math.pi / 4 + self.lat1 / 2)))
        self.n = n
        self.F = (math.cos(self.lat1) * math.tan(math.pi / 4 + self.lat1 / 2) ** n) / n
        self.rho0 = self.F / math.tan(math.pi / 4 + self.lat0 / 2) ** n
        super().__init__(bbox, viewport, padding)

    def _project(self, lon, lat):
        lat_r = math.radians(max(min(lat, 89.0), -89.0))
        lon_r = math.radians(lon)
        rho = self.F / math.tan(math.pi / 4 + lat_r / 2) ** self.n
        theta = self.n * (lon_r - self.lon0)
        return (rho * math.sin(theta), -(self.rho0 - rho * math.cos(theta)))


REGISTRY = {
    Equirectangular.name: Equirectangular,
    Mercator.name: Mercator,
    LambertConformal.name: LambertConformal,
}


def get_projection(name: str):
    return REGISTRY.get(name, Equirectangular)
