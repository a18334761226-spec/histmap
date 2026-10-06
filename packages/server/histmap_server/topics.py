#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
题材加载器 · 把「题材」变成数据
====================================
## 为什么重写

初版把每个题材写成一个 Python 类（`WW2Scene` / `TangScene`）。
后果是**题材 = 代码**：用户问「宋呢？清呢？一战呢？」，
每加一个都要写一套渲染逻辑。这不是系统，是几个 demo 拼在一起。

现在改成：**题材 = data/topics/*.json 里的一条记录**，渲染逻辑只有两种：

  · kind = "boundary"  用现成国界几何（CShapes）+ 控制时间线
                       —— 近现代战争、国际体系。换题材只换控制表。
  · kind = "dynasty"   史料构建：行政单元 → 今地名 → 合并现代县
                       —— 中国古代断代。换朝代只换单元表与控制表。

两种 kind 共用同一个渲染引擎，所以「统一风格」是天然成立的。

## 加一个新题材要做什么

  boundary 类：写一份控制时间线 JSON（事件式），加一条 topic 记录。**零 Python。**
  dynasty  类：写一份单元坐标表 + 一份控制表，加一条 topic 记录。**零 Python。**

坐标表可以用 `src/check_gazetteer.py` 做逐点几何校验，
控制表可以复用 `src/parse_tang_fanzhen.py` 的解析思路从史料生成。
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import date as _date, timedelta
from functools import lru_cache
from typing import Any

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
for p in (os.path.join(ROOT, "packages", "core"), os.path.join(ROOT, "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

TOPICS_FILE = os.path.join(ROOT, "data", "topics", "topics.json")
CONTROL_DIR = os.path.join(ROOT, "data", "control")
PROC = os.path.join(ROOT, "data", "processed")
CACHE = os.path.join(ROOT, "data", "cache")


# ════════════════════════════════════════════════════════════
# 题材定义
# ════════════════════════════════════════════════════════════
@dataclass
class Topic:
    id: str
    title: str
    kind: str
    bbox: list
    themes: list
    raw: dict

    @property
    def subtitle(self) -> str:
        return self.raw.get("subtitle", "")

    @property
    def source_note(self) -> str:
        return self.raw.get("source_note", "")

    @property
    def default_size(self) -> str:
        return self.raw.get("default_size", "16x9")

    def control_path(self) -> str | None:
        c = self.raw.get("control")
        if not c:
            return None
        for d in (CONTROL_DIR, PROC):
            p = os.path.join(d, c)
            if os.path.exists(p):
                return p
        return None

    def dates(self) -> list[str]:
        """该题材**当前真的能渲染**的日期点。

        dynasty 类只返回已构建几何的年份 —— 控制表里有 150 年不代表
        150 年都出得了图（县→单元的合并是预构建的）。返回没构建的年份，
        界面会给出一个点了就报错的滑块。
        """
        if self.kind == "dynasty":
            ys = []
            for y in (self.raw.get("years") or []):
                if os.path.exists(os.path.join(PROC, f"{self.id}_{y}_map.geojson")):
                    ys.append(int(y))
            if ys:
                return [f"{y}-01-01" for y in sorted(ys)]
        ctrl = self.control_path()
        if ctrl:
            data = json.load(open(ctrl, encoding="utf-8"))
            if "events" in data:
                return _boundary_dates(data)
            return _dynasty_dates(data)
        ys = self.raw.get("years") or []
        return [f"{y}-01-01" for y in ys]

    def default_date(self) -> str:
        d = self.raw.get("default_date")
        ds = self.dates()
        if d and d in ds:
            return d
        return ds[-1] if ds else (f"{self.raw['years'][0]}-01-01"
                                  if self.raw.get("years") else "1000-01-01")


def _boundary_dates(data: dict) -> list[str]:
    """boundary 类：月度帧 ∪ 事件帧（与既有脚本一致）。"""
    y0, y1 = None, None
    for e in data.get("events", []):
        s = str(e.get("date") or "")[:10]
        if not s:
            continue
        y = int(s[:4])
        y0 = y if y0 is None else min(y0, y)
        y1 = y if y1 is None else max(y1, y)
    if y0 is None:
        return []
    ds = set()
    for y in range(y0, y1 + 1):
        for m in range(1, 13):
            ds.add(f"{y}-{m:02d}-01")
    for e in data.get("events", []):
        s = str(e.get("date") or "")[:10]
        if s:
            ds.add(s)
    for mk in data.get("markers", []) or []:
        s = str(mk.get("date") or "")[:10]
        if s:
            ds.add(s)
    # 排序/筛选都按解析后的数字走，别按字符串：'1941-6-22' 这种
    # 单数字月份写成字符串会排到 '1941-12-01' 后面去。
    return sorted((d for d in ds if y0 <= _yr(d) <= y1), key=_date_key)


def _yr(d: str) -> int:
    return int(str(d).split("-")[0])


def _date_key(d: str):
    p = [p for p in str(d)[:10].split("-") if p != ""]
    return (int(p[0]), int(p[1]) if len(p) > 1 else 1, int(p[2]) if len(p) > 2 else 1)


def _dynasty_dates(data: dict) -> list[str]:
    """dynasty 类：控制表的顶层键就是年份。"""
    ys = []
    for k in data:
        if k.startswith("_"):
            continue
        try:
            ys.append(int(k))
        except ValueError:
            continue
    return [f"{y}-01-01" for y in sorted(ys)]


@lru_cache(maxsize=1)
def load_topics() -> dict[str, Topic]:
    if not os.path.exists(TOPICS_FILE):
        return {}
    data = json.load(open(TOPICS_FILE, encoding="utf-8"))
    out = {}
    for t in data.get("topics", []):
        out[t["id"]] = Topic(
            id=t["id"], title=t["title"], kind=t.get("kind", "boundary"),
            bbox=t.get("bbox") or [-180, -60, 180, 75],
            themes=t.get("themes") or ["dark", "light"], raw=t)
    return out


def get(topic_id: str) -> Topic | None:
    return load_topics().get(topic_id)


def list_topics() -> list[dict]:
    out = []
    for t in load_topics().values():
        try:
            ds = t.dates()
        except Exception:
            ds = []
        ready, why = data_ready(t)
        out.append({
            "id": t.id, "title": t.title, "subtitle": t.subtitle,
            "kind": t.kind, "themes": t.themes, "dates": ds,
            "n_dates": len(ds),
            "default_date": t.default_date() if ds else None,
            "default_size": t.default_size,
            "source_note": t.source_note,
            "license_note": t.raw.get("license_note", ""),
            # 数据齐不齐：前端据此把不可用的题材标出来并说清怎么补
            "ready": ready, "missing": why,
        })
    return out


# ════════════════════════════════════════════════════════════
# 两种渲染器
# ════════════════════════════════════════════════════════════
_SIZES = {"16x9": (1920, 1080, "full"), "9x16": (1080, 1920, "band"),
          "4x3": (1600, 1200, "full"), "1x1": (1400, 1400, "full")}


def _sizes(size: str):
    return _SIZES.get(size, _SIZES["16x9"])


# ── boundary 类 ─────────────────────────────────────────────
_geo_cache: dict = {}


def _boundary_geometry(topic: Topic, year: int):
    key = (topic.id, year)
    if key in _geo_cache:
        return _geo_cache[key]
    from histmap_core import Registry
    from histmap_core.datasets import cshapes
    ds = topic.raw.get("dataset", "cshapes")
    ad = Registry.get(ds)
    if not getattr(ad, "_cached_gj", None):
        ad._cached_gj = ad.load()
        ad.load = lambda use_cache=True: ad._cached_gj
    s = ad.build(years=[year])
    fr = s.frames[0]
    lon0, lat0, lon1, lat1 = topic.bbox
    fr.regions = [r for r in fr.regions
                  if r.bbox() and not (r.bbox()[2] < lon0 or r.bbox()[0] > lon1
                                       or r.bbox()[3] < lat0 or r.bbox()[1] > lat1)]
    _geo_cache[key] = fr
    return fr


def _apply_style_colors(style, region_colors: list, legend: list, palette_map: dict,
                        canvas_hex: str, text_hex: str | None,
                        text_dim: str | None) -> None:
    """把分类色重映射的结果刷到 Style 上（画布 / 文字 / 边框）。

    区域色本身在各自的地方刷（boundary 在 fr.regions，dynasty 在建 Region 时），
    图例要跟着一起换 —— 否则图例说「宋=黄褐」而图上宋是蓝的。
    """
    if palette_map:
        for it in (legend or []):
            if len(it) >= 2 and it[1] in palette_map:
                it[1] = palette_map[it[1]]
    style.background = canvas_hex
    if text_hex:
        style.title_color = text_hex
        style.ink = text_hex
    if text_dim:
        style.subtitle_color = text_dim
        style.muted = text_dim
        style.panel_border = text_dim


def _render_boundary(topic: Topic, date: str, theme: str, size: str,
                     title: str | None = None, subtitle: str | None = None,
                     footer: str | None = None,
                     style_profile: dict | None = None,
                     strength: float = 1.0) -> Image.Image:
    from histmap_core import Renderer, Layout, ControlTimeline, Style
    ctrl = topic.control_path()
    if not ctrl:
        raise FileNotFoundError(f"题材 {topic.id} 缺少控制表")
    tl = ControlTimeline.load(ctrl, theme=theme)
    d = _date.fromisoformat(date)
    fr = _boundary_geometry(topic, d.year)
    tl.layer_at(date).apply(fr)

    beats = tl.events_between(d.replace(day=1).isoformat(), date) if d.day > 1 else []
    fr.title = title or (f"{d.year} 年 {d.month} 月" + ("" if d.day == 1 else f" {d.day} 日"))
    if subtitle is not None:
        fr.subtitle = subtitle
    else:
        fr.subtitle = f"{beats[-1]['date'][5:]} · {beats[-1]['label']}" if beats else ""

    style = _topic_style(topic, theme)
    legend = _topic_legend(topic, theme)

    # 参考图风格：按**类别**重映射配色（不是对像素乘增益，那样会把政权颜色推到一起）
    prof = None
    if style_profile:
        prof, legend = _style_colors(style, fr, legend, topic, theme,
                                     style_profile, strength)

    W, H, mode = _sizes(size)
    lay = Layout(width=W, height=H, mode=mode,
                 band_top_ratio=0.20, band_max_height_ratio=0.52)
    r = Renderer(style, lay, projection="mercator", supersample=2)
    img = r.render_frame(fr, bbox=topic.bbox, legend_items=legend,
                         legend_title="实际控制")
    _draw_overlays(img, r, tl, d, style, W, H, topic, footer=footer)
    if prof is not None:
        img = _material_pass(img, prof, strength)
    return img


def _style_colors(style, fr, legend, topic: Topic, theme: str,
                  profile: dict, strength: float):
    """走一遍分类色重映射：区域色 + 图例色 + 画布 + 文字。

    返回 (用的还是原始 profile, 新的 legend)。**必须**把原始 profile 传下去，
    因为材质层要用它里面的 _texture_grid；derive_palette 的返回值里没有那张网格，
    早先就是拿它去跑材质层，直接 KeyError。
    注意 legend 的元素是**元组**（不可变），所以只能重建、不能就地改。
    """
    import style_from_image as SFI
    region_colors = [r.color for r in fr.regions]
    legend_colors = [c for _, c in (legend or [])]
    got = SFI.derive_palette(profile, region_colors + legend_colors,
                             style.background, theme=theme, strength=strength)
    for r in fr.regions:
        r.color = got["map"].get(r.color, r.color)
    new_legend = ([(n, got["map"].get(c, c)) for n, c in legend]
                  if legend else legend)
    style.background = got["canvas"]
    if got.get("text"):
        style.title_color = got["text"]
        style.ink = got["text"]
        # label_color 必须**显式**设：Style.from_dict 里 resolve_theme 早就跑完了，
        # 之后再改 ink 不会回写到 label_color/label_halo，于是蓝图风格下
        # 区域标签还是深色、贴在深蓝底上根本看不清（踩过）。
        style.label_color = got["text"]
        # 标签描边用新的底色：深底给深描边、浅底给浅描边，字才立得住
        style.label_halo = got["canvas"]
        style.panel_bg = got["canvas"]
    if got.get("text_dim"):
        style.subtitle_color = got["text_dim"]
        style.muted = got["text_dim"]
        style.panel_border = got["text_dim"]
    return profile, new_legend


def _material_pass(img, profile: dict, strength: float):
    """颜色换完之后，再压一层参考图的材质：纸纹 / 颗粒 / 暗角。

    只做材质，不做颜色 —— 颜色已经在分类色那一步定死了。
    """
    try:
        import style_from_image as SFI
        return SFI.apply_style(img, profile, strength=strength, material_only=True)
    except Exception as e:
        print(f"[风格] 材质层失败：{type(e).__name__}: {e}")
        return img


def _topic_style(topic: Topic, theme: str) -> "Style":
    from histmap_core import Style
    if theme == "light":
        return Style.from_dict({
            "id": f"{topic.id}_light", "theme": "light",
            "canvas": {"background": "#efe7d6"},
            "borders": {"color": "#6b5f4a", "width": 0.9},
            "labels": {"size": 19, "color": "#2b2620", "halo": "#f7f2e8",
                       "halo_width": 4, "min_area_ratio": 0.0011},
            "title_style": {"size": 50, "color": "#241f1a",
                            "subtitle_size": 24, "subtitle_color": "#6b5f50"},
            "legend": {"enabled": True, "position": "bottom-left",
                       "size": 16, "max_items": 16}})
    return Style.from_dict({
        "id": f"{topic.id}_dark",
        "canvas": {"background": "#0b1016"},
        "borders": {"color": "#161c24", "width": 0.8},
        "labels": {"size": 20, "color": "#ffffff", "halo": "#000000",
                   "halo_width": 4, "min_area_ratio": 0.0011},
        "title_style": {"size": 54, "color": "#ffffff",
                        "subtitle_size": 26, "subtitle_color": "#b8c2cc"},
        "legend": {"enabled": True, "position": "bottom-left",
                   "size": 17, "max_items": 16}})


def _topic_legend(topic: Topic, theme: str):
    """控制表自带 _legend 就用它，否则从调色板推导。"""
    ctrl = topic.control_path()
    if ctrl:
        data = json.load(open(ctrl, encoding="utf-8"))
        lg = data.get("_legend")
        if lg:
            from histmap_core.control import to_light
            if theme == "light":
                return [(n, to_light(c)) for n, c in lg]
            return [(n, c) for n, c in lg]
    return None


def _draw_overlays(img, renderer, tl, d, style, W, H, topic: Topic,
                   footer: str | None = None):
    """右下角大事记 + 底部口径声明。题材没提供就跳过。footer 可按帧覆盖。"""
    import make_ww2_video as M
    rows = []
    evs = getattr(tl, "events", []) or []
    mks = getattr(tl, "data", {}).get("markers") or []
    cand = []
    for e in list(evs) + list(mks):
        s = str(e.get("date") or "")[:10]
        lab = e.get("label") or ""
        if not (s and lab):
            continue
        try:                               # 脏日期直接跳过，别让整张图挂掉
            cand.append((_date.fromisoformat(s), lab))
        except ValueError:
            continue
    cand = [c for c in cand if c[0] <= d]
    cand.sort(key=lambda x: x[0])
    seen, picked = set(), []
    for dt, lab in reversed(cand):
        if lab in seen or any(lab in s or s in lab for s in seen):
            continue
        seen.add(lab)
        picked.append((dt, lab))
        if len(picked) >= 3:
            break
    picked.reverse()
    if picked:
        tb = M.ticker_box(W, H, renderer, [picked], style=style)
        M.draw_ticker(img, picked, tb)
    note = tl.data.get("_footer") or topic.source_note
    if note:
        M.draw_footer(img, note, renderer, style)


# ── dynasty 类 ──────────────────────────────────────────────
_dyn_cache: dict = {}
_ctrl_cache: dict = {}


def _topic_json(topic: Topic) -> dict:
    """题材的控制表原文（带缓存）。朝代名、显示名、口径声明都从这儿读。"""
    key = topic.id
    if key not in _ctrl_cache:
        p = topic.control_path()
        _ctrl_cache[key] = json.load(open(p, encoding="utf-8")) if p else {}
    return _ctrl_cache[key]


def _file_sha1(path: str) -> str:
    """JSON 文件的**语义**指纹：重排键序、改缩进、动换行都不算变化。

    直接哈希字节会让「只调了格式」被误报成过期，那样这个自检很快就没人信了。
    """
    import hashlib
    obj = json.load(open(path, encoding="utf-8"))
    canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                       separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(canon).hexdigest()[:16]


def _units_path(topic: Topic) -> str | None:
    """单元几何的来源文件：坐标表（units）或现成多边形表（units_file）。

    两种都算「有」—— 早先这个函数只认 units，于是用 units_file 的题材
    （美国内战）在自检里被误报成「缺数据」。
    """
    for key in ("units", "units_file"):
        p = topic.raw.get(key)
        if p:
            return p if os.path.isabs(p) else os.path.join(PROC, p)
    return None


def stale_years(topic: Topic) -> list[int]:
    """哪几年的几何是拿**旧**控制表/旧坐标表算出来的。

    改了 data/control/*.json 却忘了重跑 build_dynasty_map.py 时，
    界面照旧显示旧图，看图的人只会以为是别的地方坏了。
    这个函数就是用来戳破这件事的 —— 服务启动时会把结果打出来。
    """
    if topic.kind != "dynasty":
        return []
    ctrl, up = topic.control_path(), _units_path(topic)
    if not ctrl or not up or not (os.path.exists(ctrl) and os.path.exists(up)):
        return []
    cur_c, cur_u = _file_sha1(ctrl), _file_sha1(up)
    bad = []
    for y in topic.raw.get("years") or []:
        p = os.path.join(PROC, f"{topic.id}_{y}_map.geojson")
        if not os.path.exists(p):
            bad.append(int(y))
            continue
        try:
            m = (json.load(open(p, encoding="utf-8")).get("_meta") or {})
        except Exception:
            bad.append(int(y))
            continue
        if m.get("control_sha1") != cur_c or m.get("gazetteer_sha1") != cur_u:
            bad.append(int(y))
    return bad


def missing_data_report() -> list[dict]:
    """哪些题材缺运行期数据，以及怎么补。

    为什么要有这个：云端最常见的故障是数据集没下载（CShapes 25MB 不在仓库里，
    许可要求走下载器），但表现是「点开图就 500」，看日志才知道。启动时报出来，
    界面也据此把题材标成不可用 —— 比让人对着一堆 500 猜强。
    """
    out = []
    for t in load_topics().values():
        need = _needs(t)
        if not need:
            continue
        out.append({"topic": t.id, "need": need[0], "fix": need[1]})
    return out


def _needs(topic: "Topic") -> tuple[str, str] | None:
    """这个题材缺什么。返回 (说明, 补救命令) 或 None。"""
    if topic.kind == "dynasty":
        up = _units_path(topic)
        if not up or not os.path.exists(up):
            src = topic.raw.get("units") or topic.raw.get("units_file") or "（未声明）"
            return (f"单元几何表 {src}",
                    "该文件在 data/processed/ 下，属入库数据，缺失说明仓库不完整")
        ys = [y for y in (topic.raw.get("years") or [])
              if not os.path.exists(os.path.join(PROC, f"{topic.id}_{y}_map.geojson"))]
        if ys:
            return (f"{len(ys)} 个年份的几何（{ys[:6]}）",
                    f"python src/build_dynasty_map.py --topic {topic.id} --all-years --force")
        return None
    # boundary 类：几何来自现成数据集，没下载就跑不了
    ds = topic.raw.get("dataset", "cshapes")
    try:
        from histmap_core import Registry
        ad = Registry.get(ds)
        fname = getattr(ad, "filename", None) or f"{ds}.geojson"
        if not ad.has_cache(fname):
            return (f"数据集 {ds}（{fname}）",
                    "python src/fetch_data.py --all"
                    "（CShapes 许可要求学术引用，故不进仓库，走下载器）")
    except Exception as e:
        return (f"数据集 {ds} 检查失败：{type(e).__name__}: {e}",
                "python src/fetch_data.py --all")
    return None


def data_ready(topic: "Topic") -> tuple[bool, str]:
    n = _needs(topic)
    return (True, "") if not n else (False, n[0])


def stale_report() -> list[dict]:
    """全部题材的过期情况，给启动日志和 /api/health 用。"""
    out = []
    for t in load_topics().values():
        try:
            ys = stale_years(t)
        except Exception as e:
            ys = []
            out.append({"topic": t.id, "error": f"{type(e).__name__}: {e}"})
            continue
        if ys:
            out.append({
                "topic": t.id, "stale_years": ys,
                "fix": f"python src/build_dynasty_map.py --topic {t.id} --all-years --force",
            })
    return out


def _dynasty_geometry(topic: Topic, year: int) -> dict:
    key = (topic.id, year)
    if key in _dyn_cache:
        return _dyn_cache[key]
    p = os.path.join(PROC, f"{topic.id}_{year}_map.geojson")
    if not os.path.exists(p):
        raise FileNotFoundError(
            f"缺少 {topic.id} {year} 年的几何。先跑：\n"
            f"  python src/build_dynasty_map.py --topic {topic.id} --year {year}")
    gj = json.load(open(p, encoding="utf-8"))
    _dyn_cache[key] = gj
    return gj


def rings_of(geom: dict) -> list:
    t = geom.get("type")
    c = geom.get("coordinates") or []
    out = []
    if t == "Polygon":
        for r in c:
            out.append([(float(p[0]), float(p[1])) for p in r])
    elif t == "MultiPolygon":
        for poly in c:
            for r in poly:
                out.append([(float(p[0]), float(p[1])) for p in r])
    return out


def _fit_frame_bbox(regions, base_bbox, floor: float = 0.62):
    """按**这一帧自己的数据**取景，而不是全题材共用一个框。

    为什么必须逐帧算：明清的并集范围包含新疆、西藏（要到 1700/1820 才有），
    而 1644 只有东部有内容 —— 用并集取景的话，那一帧的画面被压在右下角，
    左边大片空白、标注也小到看不清（实测就是这样）。

    逐帧取景的副作用是「每帧比例尺不同」，但那恰好是想要的：
    从 1400 到 1820 会形成镜头缓缓拉远的效果。

    下界 floor 防止某一帧只有一小块数据时把镜头怼得过近。
    """
    xs, ys = [], []
    for r in regions:
        b = r.bbox()
        if not b:
            continue
        xs += [b[0], b[2]]
        ys += [b[1], b[3]]
    if not xs:
        return base_bbox
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

    bw, bh = base_bbox[2] - base_bbox[0], base_bbox[3] - base_bbox[1]
    w, h = x1 - x0, y1 - y0
    # 内容太窄/太扁时（比如只剩一条），按底框的比例撑开，避免退化成一条线
    w = max(w, bw * 0.25)
    h = max(h, bh * 0.25)
    # 不小于底框的 floor 倍
    w, h = max(w, bw * floor), max(h, bh * floor)

    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    pad = 0.04
    w, h = w * (1 + pad), h * (1 + pad)
    # 别超出底框范围：超了就以底框为准
    nx0, nx1 = max(base_bbox[0], cx - w / 2), min(base_bbox[2], cx + w / 2)
    ny0, ny1 = max(base_bbox[1], cy - h / 2), min(base_bbox[3], cy + h / 2)
    if nx1 - nx0 < 1e-6 or ny1 - ny0 < 1e-6:
        return base_bbox
    return [nx0, ny0, nx1, ny1]


def _render_dynasty(topic: Topic, date: str, theme: str, size: str,
                    title: str | None = None, subtitle: str | None = None,
                    footer: str | None = None,
                    style_profile: dict | None = None,
                    strength: float = 1.0) -> Image.Image:
    from histmap_core import Renderer, Layout, Region, Frame, Style
    year = int(str(date).split("-")[0])          # 别用 [:4]，807 是三位数
    gj = _dynasty_geometry(topic, year)
    regions = []
    for f in gj["features"]:
        pr = f["properties"]
        rings = rings_of(f["geometry"])
        if not rings:
            continue
        regions.append(Region(
            id=pr["id"], name=pr["name"], rings=rings, color=pr["color"],
            label_pos=(pr["label_lon"], pr["label_lat"]) if pr.get("label_lon") else None,
            props={"units": pr.get("units") or pr.get("zhou") or [],
                   "owner": pr.get("owner") or pr["name"]}))
    regions.sort(key=lambda r: -len(r.rings))
    # 分朝代号（北宋/南宋）写在控制表里，标题按年显示 —— 不在这里硬编码朝代名
    era = _topic_json(topic).get("_era") or {}
    sub = f"{era.get(str(year))} · {topic.subtitle}" if era.get(str(year)) else topic.subtitle
    fr = Frame(year=year, regions=regions,
               title=title or f"{topic.title} · {year} 年",
               subtitle=sub if subtitle is None else subtitle)

    # 图例：标题写着「颜色为所属政权」却不给图例，观众没法对照。
    # 按面上首次出现的顺序列政权（顺序即控制表里写的顺序），不硬编码任何政权名。
    legend, seen_owner = [], set()
    for r in regions:
        own = (r.props or {}).get("owner") or r.name
        if own in seen_owner:
            continue
        seen_owner.add(own)
        legend.append((own, r.color))

    W, H, mode = _sizes(size)
    style = Style.from_dict({
        "id": f"{topic.id}_xuan", "theme": "light",
        "canvas": {"background": "#efe7d6"},
        "borders": {"color": "#6b5f4a", "width": 1.0},
        "labels": {"size": 19, "color": "#2b2620", "halo": "#f7f2e8",
                   "halo_width": 4, "min_area_ratio": 0.0006},
        "title_style": {"size": 50, "color": "#241f1a",
                        "subtitle_size": 24, "subtitle_color": "#6b5f50"},
        "legend": {"enabled": True, "position": "bottom-left",
                   "size": 16, "max_items": 12}})
    lay = Layout(width=W, height=H, mode=mode, title_ratio=0.11, footer_ratio=0.07)
    prof = None
    if style_profile:
        prof, legend = _style_colors(style, fr, legend, topic, theme,
                                     style_profile, strength)
    r = Renderer(style, lay, projection="mercator", supersample=2)
    img = r.render_frame(fr, bbox=_fit_frame_bbox(regions, topic.bbox),
                         legend_items=legend, legend_title="所属政权")
    note = footer if footer is not None else (
        _topic_json(topic).get("_footer")
        or gj.get("_meta", {}).get("method") or topic.source_note)
    if note:
        import make_ww2_video as M
        M.draw_footer(img, note, r, style)
    if prof is not None:
        img = _material_pass(img, prof, strength)
    return img


# ════════════════════════════════════════════════════════════
def _load_font(size: int, prefer: str = ""):
    """找一个能画中文的字体。跟 render.py 用同一套回退顺序。"""
    from PIL import ImageFont
    for cand in (prefer, r"C:\Windows\Fonts\msyh.ttc",
                 r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
                 "/System/Library/Fonts/PingFang.ttc",
                 "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if cand and os.path.exists(cand):
            try:
                return ImageFont.truetype(cand, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _fit_font(draw, text: str, want: int, limit_px: int, prefer: str = ""):
    """把字号往下调到能放进 limit_px 宽为止。标题太长就变小，而不是溢出画面。"""
    size = want
    while size > 12:
        f = _load_font(size, prefer)
        try:
            w = draw.textbbox((0, 0), text, font=f)[2]
        except Exception:
            break
        if w <= limit_px:
            return f
        size = int(size * 0.92)
    return _load_font(max(12, size), prefer)


def _vignette(img, strength: float = 0.16):
    """给卡片压一层很轻的暗角，免得整块底色太平、像没做完。"""
    from PIL import Image
    try:
        import numpy as np
    except Exception:
        return img
    W, H = img.size
    y, x = np.ogrid[:H, :W]
    cx, cy = W / 2, H / 2
    r = np.sqrt(((x - cx) / cx) ** 2 + ((y - cy) / cy) ** 2)
    m = np.clip(1.0 - strength * np.clip(r - 0.55, 0, None) ** 1.7, 0, 1)
    a = np.asarray(img).astype("float32")
    a *= m[..., None]
    return Image.fromarray(a.clip(0, 255).astype("uint8"))


def title_card(topic: Topic, theme: str, size: str, text: str, sub: str = ""):
    """片头/片尾标题卡。

    不另起一套视觉：底色、字色、字体全部取自该题材在当前主题下的样式，
    所以卡片和正片是一套东西，而不是「贴上去的一张图」。

    版式是**左对齐的编辑式**，不是居中大字：居中孤字看起来像占位图，
    左对齐 + 细边框 + 顶栏小字才像一张有设计过的标题卡。
    """
    from PIL import Image, ImageDraw
    W, H, _ = _sizes(size)
    st = _topic_style(topic, theme)
    img = Image.new("RGB", (W, H), st.background)
    img = _vignette(img)
    d = ImageDraw.Draw(img)
    ink = st.title_color or st.ink
    sub_c = st.subtitle_color or st.muted

    # 一圈内缩的细边框，像古籍地图的图廓
    m = int(min(W, H) * 0.055)
    d.rectangle([m, m, W - m, H - m], outline=sub_c, width=max(1, int(H * 0.0018)))
    m2 = m + max(3, int(H * 0.008))
    d.rectangle([m2, m2, W - m2, H - m2], outline=sub_c, width=1)

    # 顶栏：跟地图页脚一样的位置感，写清这是哪一套
    f_head = _load_font(max(13, int(H * 0.024)), st.label_font)
    d.text((m * 1.5, m * 1.5), f"histmap · {topic.title}", font=f_head, fill=sub_c)

    # 主标题块：左侧一根竖线 + 标题 + 副标题，整体垂直居中
    left = int(W * 0.13)
    limit = W - left - int(W * 0.1)
    f_main = _fit_font(d, text or "", int(H * 0.155), limit, st.label_font)
    f_sub = _fit_font(d, sub, int(H * 0.042), limit, st.label_font) if sub else None

    tb = d.textbbox((0, 0), text or "", font=f_main)
    th, tw = tb[3] - tb[1], tb[2] - tb[0]
    sb = d.textbbox((0, 0), sub, font=f_sub) if sub else (0, 0, 0, 0)
    sh = sb[3] - sb[1] if sub else 0
    gap = int(H * 0.05) if sub else 0
    block = th + gap + sh
    y = (H - block) // 2

    bar_w = max(3, int(W * 0.0045))
    d.rectangle([left - int(W * 0.028), y, left - int(W * 0.028) + bar_w, y + block],
                fill=sub_c)
    d.text((left - tb[0], y - tb[1]), text or "", font=f_main, fill=ink)
    if sub:
        d.text((left - sb[0], y + th + gap - sb[1]), sub, font=f_sub, fill=sub_c)

    # 右下角落款
    f_small = _load_font(max(12, int(H * 0.022)), st.label_font)
    d.text((W - m * 1.5, H - m * 1.5), "histmap", font=f_small,
           fill=sub_c, anchor="rs")
    return img


def add_watermark(img, text: str, theme: str = "dark"):
    """右下角烧一行水印。短视频发出去要能认出是谁做的。"""
    if not text:
        return img
    from PIL import ImageDraw
    d = ImageDraw.Draw(img, "RGBA")
    W, H = img.size
    fs = max(14, int(H * 0.026))
    f = _load_font(fs, _topic_style_default_font())
    pad = int(H * 0.03)
    tb = d.textbbox((0, 0), text, font=f)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    x, y = W - pad - tw, H - pad - th
    # 先描一层半透明底，浅色地图上也看得清
    d.rectangle([x - fs * 0.4, y - fs * 0.25, x + tw + fs * 0.4, y + th + fs * 0.35],
                fill=(0, 0, 0, 90) if theme == "dark" else (255, 255, 255, 120))
    d.text((x, y - tb[1]), text, font=f,
           fill=(255, 255, 255, 205) if theme == "dark" else (30, 30, 30, 205))
    return img


def _topic_style_default_font() -> str:
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            return p
    return ""


# ════════════════════════════════════════════════════════════
def render(topic_id: str, date: str, theme: str = "dark", size: str = "16x9",
           title: str | None = None, subtitle: str | None = None,
           footer: str | None = None, style_profile: dict | None = None,
           strength: float = 1.0):
    """出一张图。

    title / subtitle / footer 传 None 就用题材自己的默认文案（事件副标题、
    题材口径声明），传空字符串则是「明确要求留白」—— 两者不能混为一谈，
    否则用户想清掉一行标题都做不到。

    style_profile 是从参考图提取出来的风格参数。它走的是**分类色重映射**：
    逐类别换掉区域色/画布/文字，再压一层材质（纹理/颗粒/暗角）。
    绝不是对整张图乘颜色增益 —— 那样会把不同政权的颜色推到一起（实测毁图）。
    """
    t = get(topic_id)
    if not t:
        raise KeyError(f"没有这个题材: {topic_id}")
    if theme not in t.themes:
        theme = t.themes[0]
    kw = {"title": title, "subtitle": subtitle, "footer": footer,
          "style_profile": style_profile, "strength": strength}
    if t.kind == "dynasty":
        return _render_dynasty(t, date, theme, size, **kw)
    return _render_boundary(t, date, theme, size, **kw)
