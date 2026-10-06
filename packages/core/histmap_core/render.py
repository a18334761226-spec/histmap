"""
histmap-core · 渲染引擎

把 Series（统一契约）渲染成图片帧。题材无关 —— 只认契约。

特性
----
* 多投影（等距圆柱 / 墨卡托 / 兰伯特等角圆锥）
* 超采样抗锯齿
* 中文地名标注（带描边光晕，暗底可读）
* 自动图例、标题、副标题
* 输出 PNG 序列，供 ffmpeg 合成视频

用法
----
    r = Renderer(style, width=1080, height=1920)
    r.render_frame(frame, bbox).save("out.png")
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont

from .contract import Frame, Region, Series
from .projection import Viewport, get_projection


def _hex_to_rgb(c: str):
    c = (c or "#888888").lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    except Exception:
        return (136, 136, 136)


@dataclass
class Layout:
    """画布与各区块的排布（比例制，随分辨率自适应）。"""

    width: int = 1080
    height: int = 1920
    margin_ratio: float = 0.045
    title_ratio: float = 0.13        # 顶部标题带高度占比
    footer_ratio: float = 0.06       # 底部信息带高度占比

    @property
    def margin(self):
        return self.width * self.margin_ratio

    mode: str = "full"
    band_top_ratio: float = 0.22
    band_max_height_ratio: float = 0.52

    @property
    def map_area(self) -> Viewport:
        top = self.height * self.title_ratio
        bottom = self.height * (1 - self.footer_ratio)
        return Viewport(self.margin, top,
                        self.width - 2 * self.margin,
                        bottom - top)

    def map_area_for_ratio(self, data_ratio: float) -> Viewport:
        """按数据宽高比给出地图区域。

        full 模式：填满可用区（保比例，居中）
        band 模式：宽度占满，高度按比例推导，纵向定位在 band_top_ratio
        —— 9:16 竖版放 2:1 的世界地图时，硬塞必然大片留白，带状才对。
        """
        if self.mode != "band" or not data_ratio or data_ratio <= 0:
            return self.map_area
        m = self.margin
        avail_w = self.width - 2 * m
        h = avail_w / data_ratio
        max_h = self.height * self.band_max_height_ratio
        if h > max_h:
            h = max_h
            avail_w = h * data_ratio
        top = self.height * self.band_top_ratio
        left = (self.width - avail_w) / 2
        return Viewport(left, top, avail_w, h)


class Renderer:
    def __init__(self, style, layout: Layout | None = None,
                 projection: str = "mercator", supersample: int = 2):
        self.style = style
        self.layout = layout or Layout()
        self.projection_name = projection
        self.ss = max(1, int(supersample))
        self.debug_labels = False
        self._font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}

    # ── 字体 ─────────────────────────────────────────────────
    def font(self, size: int):
        size = int(size)
        if size in self._font_cache:
            return self._font_cache[size]
        path = self.style.label_font
        f = None
        for cand in (path, r"C:\Windows\Fonts\msyh.ttc",
                     r"C:\Windows\Fonts\simhei.ttf",
                     r"C:\Windows\Fonts\simsun.ttc"):
            if cand and os.path.exists(cand):
                try:
                    f = ImageFont.truetype(cand, size)
                    break
                except Exception:
                    continue
        if f is None:
            f = ImageFont.load_default()
        self._font_cache[size] = f
        return f

    # ── 文本工具 ─────────────────────────────────────────────
    def _text_size(self, draw, text, font):
        if not text:
            return (0, 0)
        box = draw.textbbox((0, 0), text, font=font)
        return (box[2] - box[0], box[3] - box[1])

    def _draw_text_halo(self, draw, xy, text, font, fill, halo, halo_w, anchor="mm"):
        x, y = xy
        if halo_w > 0 and halo:
            for dx in range(-halo_w, halo_w + 1):
                for dy in range(-halo_w, halo_w + 1):
                    if dx * dx + dy * dy <= halo_w * halo_w:
                        draw.text((x + dx, y + dy), text, font=font,
                                  fill=halo, anchor=anchor)
        draw.text((x, y), text, font=font, fill=fill, anchor=anchor)

    # ── 主渲染 ───────────────────────────────────────────────
    def render_frame(self, frame: Frame, bbox=None, series_meta=None,
                     legend_items=None, legend_title=None) -> Image.Image:
        st = self.style
        W = self.layout.width * self.ss
        H = self.layout.height * self.ss
        img = Image.new("RGB", (W, H), _hex_to_rgb(st.background))
        draw = ImageDraw.Draw(img, "RGBA")

        if bbox is None:
            bbox = self._frame_bbox(frame)
        # 按数据宽高比决定地图区域（band 模式下避免竖版大片留白）
        vp0 = self.layout.map_area_for_ratio(self._bbox_ratio(bbox))
        vp = Viewport(vp0.x * self.ss, vp0.y * self.ss,
                      vp0.width * self.ss, vp0.height * self.ss)
        proj = get_projection(self.projection_name)(bbox, vp, padding=8 * self.ss)

        # 1) 填充区域
        polys = []
        for i, reg in enumerate(frame.regions):
            if not reg.rings:
                continue
            color = _hex_to_rgb(st.color_for(reg, i))
            pts_all = [proj.project_ring(ring) for ring in reg.rings]
            if not pts_all or len(pts_all[0]) < 3:
                continue
            # 所有环都按「外环」填充。
            # 注意：契约里的 rings 是**拍平**的环列表，MultiPolygon 的每个部分
            # 都有自己的外环；若把 ring[1:] 当作「洞」用背景色覆盖，会把苏联、
            # 大英帝国这类多块实体的领土整片涂掉（已踩过）。
            # 真正的洞（如南非内的莱索托）暂不支持，属已知限制。
            for ring in pts_all:
                if len(ring) >= 3:
                    draw.polygon(ring, fill=color + (int(255 * st.region_alpha),))
            polys.append((reg, pts_all, color))

        # 2) 描边
        if st.border_width > 0:
            for reg, pts_all, color in polys:
                for ring in pts_all:
                    if len(ring) >= 3:
                        draw.line(list(ring) + [ring[0]],
                                  fill=_hex_to_rgb(st.border_color),
                                  width=max(1, int(st.border_width * self.ss)),
                                  joint="curve")

        # 3) 地名标注（面积过滤 + 碰撞避让）
        if st.label_size > 0:
            font = self.font(st.label_size * self.ss)
            # 必须累计**所有环**的面积：MultiPolygon 实体的首环可能只是个
            # 小岛（苏联、英国都会被这条坑到，表现为「面积太小，不标注」）。
            areas = [sum(abs(self._ring_area(pts)) for pts in p[1]) for p in polys]
            total_area = sum(areas) or 1.0
            placed = []
            placed_names = set()
            vx0, vy0 = vp.x, vp.y
            vx1, vy1 = vp.x + vp.width, vp.y + vp.height
            for i in sorted(range(len(polys)), key=lambda k: -areas[k]):
                reg, pts_all, color = polys[i]
                if areas[i] / total_area < st.label_min_area_ratio:
                    continue
                # 候选锚点：显式 label_pos > 最大环的质心（落在画布内）> 最大环包围盒∩视口。
                # 只看「最大环」是有意的：苏联有 136 个环，若遍历所有环找
                # 「质心在视口内的最大环」，一个卡累利阿小环会击败主环，
                # 把「苏联」标到瑞典头上（已踩过）。
                if reg.label_pos:
                    cand = proj(*reg.label_pos)
                else:
                    big = max(pts_all, key=lambda p: abs(self._ring_area(p)))
                    c = self._centroid(big)
                    cand = c if (vx0 <= c[0] <= vx1 and vy0 <= c[1] <= vy1) \
                        else self._visible_anchor(pts_all, vp)
                if cand is None:
                    if self.debug_labels:
                        print(f"    [label] {reg.name}: 无可用锚点，跳过")
                    continue
                # 多个实体可能映射到同一个中文名（1945 年 CShapes 有三个德国实体），
                # 同名只标一次，否则地图上会出现两个「德国」。
                if reg.name in placed_names:
                    continue
                tw, th = self._text_size(draw, reg.name, font)
                pad = 5 * self.ss
                dy, dx = th * 0.95, tw * 0.55
                # 碰撞后不直接放弃，先试几个偏移位（上下左右 + 斜向），
                # 这样斯堪的纳维亚、巴尔干这类拥挤区域也能尽量标全。
                tries = [(0, 0), (0, -dy), (0, dy), (-dx, 0), (dx, 0),
                         (-dx, -dy), (dx, -dy), (-dx, dy), (dx, dy),
                         (0, -2 * dy), (0, 2 * dy)]
                for ox, oy in tries:
                    x, y = cand[0] + ox, cand[1] + oy
                    if not (vx0 <= x <= vx1 and vy0 <= y <= vy1):
                        continue
                    box = (x - tw / 2 - pad, y - th / 2 - pad,
                           x + tw / 2 + pad, y + th / 2 + pad)
                    if self._collides(box, placed):
                        continue
                    placed.append(box)
                    placed_names.add(reg.name)
                    self._draw_text_halo(draw, (x, y), reg.name, font,
                                         _hex_to_rgb(st.label_color),
                                         _hex_to_rgb(st.label_halo),
                                         st.label_halo_width * self.ss)
                    if self.debug_labels:
                        print(f"    [label] {reg.name}: 锚点 "
                              f"({cand[0]:.0f},{cand[1]:.0f}) 偏移 ({ox:.0f},{oy:.0f})")
                    break
                else:
                    if self.debug_labels:
                        print(f"    [label] {reg.name}: 全部候选位冲突，丢弃 "
                              f"(面积占比 {areas[i]/total_area:.4f})")

        # 4) 标题
        if frame.title:
            f = self.font(st.title_size * self.ss)
            self._draw_text_halo(
                draw, (W / 2, H * self.layout.title_ratio * 0.42),
                frame.title, f, _hex_to_rgb(st.title_color),
                _hex_to_rgb(st.background), 3 * self.ss, anchor="mm")
        if frame.subtitle:
            f = self.font(st.subtitle_size * self.ss)
            self._draw_text_halo(
                draw, (W / 2, H * self.layout.title_ratio * 0.78),
                frame.subtitle, f, _hex_to_rgb(st.subtitle_color),
                _hex_to_rgb(st.background), 2 * self.ss, anchor="mm")

        # 5) 图例（可传入自定义项，如控制层图例）
        if st.legend_enabled:
            self._draw_legend(draw, img, frame, bbox, W, H,
                              items=legend_items, title=legend_title)

        if self.ss > 1:
            img = img.resize((self.layout.width, self.layout.height),
                             Image.LANCZOS)
        return img

    # ── 辅助 ─────────────────────────────────────────────────
    @staticmethod
    def _centroid_clipped(pts, vp):
        """只用落在视口内的顶点求质心；全部在外或不足 3 点则返回 None。

        用于跨洲实体（苏联、大英帝国）——它们的整体质心可能落在地图之外。
        """
        x0, y0 = vp.x, vp.y
        x1, y1 = vp.x + vp.width, vp.y + vp.height
        inside = [(p[0], p[1]) for p in pts
                  if x0 <= p[0] <= x1 and y0 <= p[1] <= y1]
        if len(inside) < 3:
            return None
        n = len(inside)
        return (sum(p[0] for p in inside) / n, sum(p[1] for p in inside) / n)

    @classmethod
    def _visible_anchor(cls, pts_all, vp):
        """跨洲实体（质心落到画布外）的兜底锚点。

        做法：取投影后面积最大的环，把它的包围盒与视口求交，取交集中点。
        早期版本用「视口内顶点的重心」，结果被北极海岸线上的密集顶点拉偏，
        把「苏联」标到了瑞典头上——包围盒法稳定得多。
        """
        if not pts_all:
            return None
        best = max(pts_all, key=lambda p: abs(cls._ring_area(p)))
        xs = [p[0] for p in best]
        ys = [p[1] for p in best]
        if not xs:
            return None
        x0 = max(min(xs), vp.x)
        x1 = min(max(xs), vp.x + vp.width)
        y0 = max(min(ys), vp.y)
        y1 = min(max(ys), vp.y + vp.height)
        if x1 <= x0 or y1 <= y0:
            return None
        return ((x0 + x1) / 2, (y0 + y1) / 2)

    @staticmethod
    def _centroid(ring):
        if not ring:
            return (0, 0)
        n = len(ring)
        return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)

    @staticmethod
    def _ring_area(ring):
        """鞋带公式求多边形面积（像素²）。"""
        if not ring or len(ring) < 3:
            return 0.0
        s = 0.0
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i]
            x2, y2 = ring[(i + 1) % n]
            s += x1 * y2 - x2 * y1
        return s / 2.0

    @staticmethod
    def _collides(box, placed):
        """标签包围盒是否与已放置的冲突。"""
        ax1, ay1, ax2, ay2 = box
        for bx1, by1, bx2, by2 in placed:
            if ax1 < bx2 and ax2 > bx1 and ay1 < by2 and ay2 > by1:
                return True
        return False

    @staticmethod
    def _bbox_ratio(bbox):
        """经纬度包围盒的宽高比，用于 band 布局判断。"""
        try:
            min_lon, min_lat, max_lon, max_lat = bbox
            dy = max(max_lat - min_lat, 1e-6)
            return (max_lon - min_lon) / dy
        except Exception:
            return 1.0

    @staticmethod
    def _frame_bbox(frame: Frame):
        xs, ys = [], []
        for r in frame.regions:
            b = r.bbox()
            if b:
                xs += [b[0], b[2]]
                ys += [b[1], b[3]]
        if not xs:
            return (-180.0, -90.0, 180.0, 90.0)
        # 留 6% 边距
        dx = (max(xs) - min(xs)) * 0.06 or 1.0
        dy = (max(ys) - min(ys)) * 0.06 or 1.0
        return (min(xs) - dx, min(ys) - dy, max(xs) + dx, max(ys) + dy)

    def _draw_legend(self, draw, img, frame, bbox, W, H, items=None, title=None):
        st = self.style
        if items is None:
            items = [(r.name, st.color_for(r, i))
                     for i, r in enumerate(frame.regions)]
        items = list(items)
        if not items:
            return
        f = self.font(st.legend_size * self.ss)
        pad = 14 * self.ss
        sw = 22 * self.ss
        line_h = (st.legend_size * 1.75) * self.ss
        head_h = line_h if title else 0
        text_w = max(self._text_size(draw, t, f)[0] for _, t in items)
        row_w = pad * 2 + sw + 8 * self.ss + text_w

        # 图例要**放得下，或者明说放不下**。早先是直接 [:max_items] 截断 ——
        # 唐 807 有 37 个藩镇、上限 12，于是 25 个颜色在图上没有解释：
        # 图例看着还在，其实已经在骗人。现在改成：先按可用高度算单栏容量，
        # 再按可用宽度算最多几栏，两者相乘就是**能画下的上限**；
        # 超出就留一格写「等 N 个」，绝不静默丢。
        avail_h = (H * (1 - self.layout.title_ratio - self.layout.footer_ratio)
                   - self.layout.margin * self.ss * 2)
        per_col = max(1, int((avail_h - pad * 2 - head_h) // line_h))
        max_cols = max(1, int((W * 0.40) // max(1, row_w)))
        capacity = per_col * max_cols
        # legend_max_items 现在只是**作者可选的硬上限**，而且一旦生效就必须
        # 画出「等 N 个」那一格。默认值(14)低于地理容量时不再生效 ——
        # 否则又回到了「默认配置静默截断」的老问题。
        cap = int(getattr(st, "legend_max_items", 0) or 0)
        if cap > 0:
            capacity = min(capacity, max(2, cap))
        dropped = 0
        if len(items) > capacity:
            keep = max(1, capacity - 1)
            dropped = len(items) - keep
            items = items[:keep] + [(f"等 {dropped} 个", None)]
        ncol = max(1, -(-len(items) // per_col))
        rows = -(-len(items) // ncol)
        box_w = row_w * ncol
        box_h = pad * 2 + line_h * rows + head_h

        pos = st.legend_position or "bottom-left"
        m = self.layout.margin * self.ss
        if "left" in pos:
            x0 = m
        else:
            x0 = W - m - box_w
        if "top" in pos:
            y0 = H * self.layout.title_ratio * 1.05
        else:
            y0 = H * (1 - self.layout.footer_ratio) - box_h - m

        draw.rectangle([x0, y0, x0 + box_w, y0 + box_h],
                       fill=_hex_to_rgb(st.panel_bg) + (228,),
                       outline=_hex_to_rgb(st.panel_border) + (255,),
                       width=max(1, self.ss))
        if title:
            tf = self.font(int(st.legend_size * 1.05) * self.ss)
            draw.text((x0 + pad, y0 + pad + line_h * 0.4), title, font=tf,
                      fill=_hex_to_rgb(st.ink if st.theme == "light" else "#e2e8f0"),
                      anchor="lm")
        for i, (name, color) in enumerate(items):
            col, row = divmod(i, rows)
            cx0 = x0 + pad + col * row_w
            cy = y0 + pad + head_h + line_h * row + line_h / 2
            if color:
                draw.rectangle([cx0, cy - sw / 3, cx0 + sw, cy + sw / 3],
                               fill=_hex_to_rgb(color))
            else:
                # 「等 N 个」那格不画色块，改画三条短横线示意「还有更多」
                for k in range(3):
                    yy = cy - sw / 4 + k * sw / 4
                    draw.line([cx0 + 2 * self.ss, yy, cx0 + sw - 2 * self.ss, yy],
                              fill=_hex_to_rgb(st.muted or st.ink), width=self.ss)
            draw.text((cx0 + sw + 8 * self.ss, cy), name, font=f,
                      fill=_hex_to_rgb(st.ink), anchor="lm")
        return dropped

    # ── 批量 ─────────────────────────────────────────────────
    def render_series(self, series: Series, out_dir: str,
                      prefix: str = "frame") -> list[str]:
        os.makedirs(out_dir, exist_ok=True)
        bbox = series.auto_bbox()
        paths = []
        for i, frame in enumerate(series.frames):
            im = self.render_frame(frame, bbox=bbox)
            p = os.path.join(out_dir, f"{prefix}_{i:05d}_{frame.year}.png")
            im.save(p)
            paths.append(p)
        return paths
