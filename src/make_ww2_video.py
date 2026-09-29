#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
二战欧洲 1939→1945 演变视频
================================
演示「数据适配层 + 统一契约 + 渲染引擎」在**动态**题材上的能力：

    真实几何（CShapes，7 个年份切片，加载一次复用）
  + 控制时间线（事件式，可求任意日期的状态）
  + 样式层（统一视觉规范）
  → 逐帧渲染 → 交叉溶解 → H.264 MP4

关键设计
--------
1. **几何按年缓存**：CShapes 的边界只按年变化，所以 84 个月份只需 7 次几何构建；
   控制状态是**属性叠加**，换月份只换一张状态表，不碰几何。
2. **事件驱动的时间轴**：帧不只在月初切，还在每个历史事件当天切，
   这样「巴黎陷落」这类节点会精确停在画面上。
3. **停留时长自适应**：有事件的帧停留久（0.55s），无事件的帧快速掠过（0.12s），
   动静节奏跟着史料走，而不是机械匀速。
"""
import json
import os
import subprocess
import sys
import time
from datetime import date, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))

from PIL import Image, ImageDraw                                        # noqa: E402

from histmap_core import (Renderer, Layout, Style, Registry,            # noqa: E402
                          assess_series, ControlTimeline)
from histmap_core.datasets import cshapes                               # noqa: E402
from histmap_core.render import _hex_to_rgb                             # noqa: E402

OUT_DIR = os.path.join(ROOT, "output")
MAP_DIR = os.path.join(OUT_DIR, "maps")
VID_DIR = os.path.join(OUT_DIR, "video")
FRM_DIR = os.path.join(OUT_DIR, "frames")
for d in (MAP_DIR, VID_DIR, FRM_DIR):
    os.makedirs(d, exist_ok=True)

TL_PATH = os.path.join(ROOT, "data", "control", "ww2_timeline.json")
BBOX = (-11.0, 33.0, 62.0, 62.0)
YEAR_FROM, YEAR_TO = 1939, 1945
FPS = 30
HOLD_STILL = 0.12      # 无事件帧停留秒数
HOLD_EVENT = 0.55      # 有事件帧停留秒数
FADE = 0.20            # 交叉溶解时长

LOG = os.path.join(ROOT, "ww2-video-report.txt")
lines = []


def rss_mb():
    """当前进程物理内存（MB）。诊断用；没有 psutil 时返回 -1。"""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except Exception:
        return -1.0


def log(s):
    lines.append(str(s))
    print(s)
    sys.stdout.flush()


# ── 图例（跨帧固定，避免颜色/条目抖动） ─────────────────────
LEGEND = [
    ("德国本土",   "#b03030"),
    ("德国吞并",   "#c8574f"),
    ("德国占领",   "#dd8a80"),
    ("被瓜分/分区占领", "#9a6b7a"),
    ("意大利",     "#c8722a"),
    ("轴心国盟邦", "#d9a06a"),
    ("苏联",       "#8e44ad"),
    ("英国及其属地", "#2f6fa8"),
    ("盟军控制",   "#4a90c8"),
    ("同盟国占领", "#6f8fb5"),
    ("其他独立国", "#46525e"),
    ("中立国",     "#5a6570"),
]

# 地图底部口径声明。历史地图不写清简化口径，观众会把示意当精确。
FOOTER = ("示意地图：分区/瓜分占领以单色表示（CShapes 视各国为单一几何体，未按军事分界线切开）；"
          "苏德战场内部、殖民体系与傀儡政权未细分")

# 浅色主题（二十四史/古籍向）的底色与墨色
LIGHT_BASE = "#efe7d6"      # 米白宣纸
LIGHT_INK = "#2b2620"       # 深墨

STYLE = Style.from_dict({
    "id": "ww2_timeline",
    "canvas": {"background": "#0b1016"},
    "borders": {"color": "#161c24", "width": 0.8},
    "labels": {"size": 20, "color": "#ffffff", "halo": "#000000",
               "halo_width": 4, "min_area_ratio": 0.0011},
    "title_style": {"size": 54, "color": "#ffffff",
                    "subtitle_size": 26, "subtitle_color": "#b8c2cc"},
    "legend": {"enabled": True, "position": "bottom-left",
               "size": 17, "max_items": 16},
})


def build_style(theme: str = "dark") -> Style:
    """浅色版不是「把背景刷白」就完事。

    标注描边、图例面板、区域配色三样都要跟着换：
      · 描边不换 → 黑描边在米白底上把字糊成一团
      · 面板不换 → 图例变成黑底白字贴在宣纸上
      · 配色不换 → 高饱和色在浅底上发飘（见 control.lighten_palette）
    """
    if theme != "light":
        return STYLE
    return Style.from_dict({
        "id": "ww2_light",
        "theme": "light",
        "canvas": {"background": LIGHT_BASE},
        # 淡彩配重墨勾边：区域色提亮降饱和后，靠加深的边界线把形状压住
        "borders": {"color": "#6b5f4a", "width": 1.1},
        "labels": {"size": 20, "color": LIGHT_INK, "halo": "#f7f2e8",
                   "halo_width": 4, "min_area_ratio": 0.0011},
        "title_style": {"size": 54, "color": LIGHT_INK,
                        "subtitle_size": 26, "subtitle_color": "#6b5f50"},
        "legend": {"enabled": True, "position": "bottom-left",
                   "size": 17, "max_items": 16},
    })


def build_legend(theme: str = "dark"):
    """图例配色跟随主题；浅色底上必须重新推导，否则色块在白底看不清。"""
    if theme != "light":
        return LEGEND
    from histmap_core.control import to_light
    return [(n, to_light(c)) for n, c in LEGEND]


# ── 日期序列 ────────────────────────────────────────────────
def month_starts():
    out = []
    for y in range(YEAR_FROM, YEAR_TO + 1):
        for m in range(1, 13):
            out.append(date(y, m, 1))
    return out


def build_dates(tl):
    ds = set(month_starts())
    for e in tl.events:
        try:
            ds.add(date.fromisoformat(str(e["date"])[:10]))
        except Exception:
            pass
    for mk in tl.data.get("markers") or []:
        try:
            ds.add(date.fromisoformat(str(mk["date"])[:10]))
        except Exception:
            pass
    ds = {d for d in ds if date(YEAR_FROM, 1, 1) <= d <= date(YEAR_TO, 12, 31)}
    return sorted(ds)


def beats_between(tl, prev: date | None, cur: date) -> list:
    """(prev, cur] 内的所有事件与标记，按日期排序。"""
    out = []
    for e in tl.events:
        try:
            d = date.fromisoformat(str(e["date"])[:10])
        except Exception:
            continue
        if (prev is None or d > prev) and d <= cur:
            out.append((d, e.get("label") or "", "event"))
    for mk in tl.data.get("markers") or []:
        try:
            d = date.fromisoformat(str(mk["date"])[:10])
        except Exception:
            continue
        if (prev is None or d > prev) and d <= cur:
            out.append((d, mk.get("label") or "", "marker"))
    out.sort(key=lambda t: (t[0], t[2]))
    return out


def recent_lines(tl, cur: date, n=3) -> list:
    """截至 cur 的最近 n 条史事，用于右下角大事记。

    取法是**按日期从新到旧**、同一天内保持时间线文件里的顺序。
    不能简单地「反转整个列表取前 n 条」——1945-05-08 那天有 13 条事件，
    那样会把最重要的「德国无条件投降」挤出面板。
    """
    items = []
    for e in tl.events:
        try:
            d = date.fromisoformat(str(e["date"])[:10])
        except Exception:
            continue
        if d <= cur:
            items.append((d, e.get("label") or ""))
    for mk in tl.data.get("markers") or []:
        try:
            d = date.fromisoformat(str(mk["date"])[:10])
        except Exception:
            continue
        if d <= cur:
            items.append((d, mk.get("label") or ""))
    items.sort(key=lambda t: t[0])

    by_date = {}
    for d, lab in items:
        by_date.setdefault(d, []).append(lab)

    picked, seen = [], set()
    for d in sorted(by_date, reverse=True):
        for lab in by_date[d]:
            if not lab or lab in seen:
                continue
            # 同一件事常在 events 和 markers 里各写一遍（如「巴巴罗萨行动」），
            # 用包含关系去重，避免大事记面板出现两行几乎一样的话。
            if any(lab in s or s in lab for s in seen):
                continue
            seen.add(lab)
            picked.append((d, lab))
        if len(picked) >= n:
            break
    picked = picked[:n]
    # 显示时按时间正序；同一天内保持文件顺序（最重要的写在最前）
    dates = sorted({d for d, _ in picked})
    out = []
    for d in dates:
        for dd, lab in picked:
            if dd == d:
                out.append((dd, lab))
    return out


def draw_footer(img, text, r, style=None):
    """底部口径声明。历史地图不写清简化口径，观众会把示意当精确。"""
    if not text:
        return
    st = style or STYLE
    W, H = img.size
    fs = max(12, int(H * 0.0148))
    font = r.font(fs)
    d = ImageDraw.Draw(img)
    while fs > 10 and d.textbbox((0, 0), text, font=font)[2] > W * 0.92:
        fs -= 1
        font = r.font(fs)
    x, y = W / 2, H * (1 - 0.028)
    halo = _hex_to_rgb(st.panel_bg)
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if dx * dx + dy * dy <= 4:
                d.text((x + dx, y + dy), text, font=font, fill=halo, anchor="mm")
    d.text((x, y), text, font=font, fill=_hex_to_rgb(st.muted), anchor="mm")


def ticker_box(W, H, r, all_rows, max_rows=3, style=None):
    """预先算出大事记面板的**固定**尺寸。

    为什么必须固定：每帧的面板宽度原来按当帧文字长度算，于是交叉溶解时
    A 帧面板外的地名标注会落在 B 帧面板内，50/50 混合出「幽灵文字」
    （印度/阿富汗透在面板上）。面板几何恒定后，溶解只发生在内容上。
    顺带让整片的面板位置稳定，观感更像成品。
    """
    st = style or STYLE
    fs = max(14, int(H * 0.0195))
    font = r.font(fs)
    dfont = r.font(max(12, int(fs * 0.8)))
    pad = int(H * 0.016)
    lh = int(fs * 1.55)
    d0 = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    w = 0
    for rows in all_rows:
        for d, lab in rows[:max_rows]:
            t = f"{d.strftime('%Y-%m-%d')}  {lab}"
            w = max(w, d0.textbbox((0, 0), t, font=font)[2])
    w = min(w, int(W * 0.44))
    bw = w + pad * 2
    bh = lh * max_rows + pad * 2
    x1 = W - int(W * 0.028)
    y1 = H - int(H * 0.055)
    return {"fs": fs, "font": font, "dfont": dfont, "pad": pad, "lh": lh,
            "w": w, "h": bh, "x0": x1 - bw, "y0": y1 - bh, "x1": x1, "y1": y1,
            # 配色随主题走，浅色主题下不再是黑底白字
            "bg": st.panel_bg, "border": st.panel_border,
            "ink": st.ink, "muted": st.muted}


def draw_ticker(img, rows, box):
    """右下角「大事记」面板（尺寸由 ticker_box 固定）。"""
    if not rows or not box:
        return
    W, H = img.size
    fs, pad, lh = box["fs"], box["pad"], box["lh"]
    x0, y0, x1, y1 = box["x0"], box["y0"], box["x1"], box["y1"]
    texts = [f"{d.strftime('%Y-%m-%d')}  {lab}" for d, lab in rows]
    # 按固定宽度截断，超长行不撑破面板
    d0 = ImageDraw.Draw(img)
    while texts and d0.textbbox((0, 0), max(texts, key=len), font=box["font"])[2] > box["w"]:
        texts = [t[:-2] + "…" if len(t) > 20 else t for t in texts]

    bg = _hex_to_rgb(box.get("bg") or "#090e14")
    bd = _hex_to_rgb(box.get("border") or "#566678")
    ink = _hex_to_rgb(box.get("ink") or "#e8eef4")
    muted = _hex_to_rgb(box.get("muted") or "#96a5b4")
    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    od.rounded_rectangle([x0, y0, x1, y1], radius=int(pad * 0.6),
                         fill=bg + (246,), outline=bd + (210,), width=1)
    od.text((x0 + pad, y0 + pad * 0.4), "近 期 战 局", font=box["dfont"],
            fill=muted + (255,))
    for i, t in enumerate(texts):
        od.text((x0 + pad, y0 + pad + i * lh), t, font=box["font"],
                fill=ink + (255,))
    img.paste(Image.alpha_composite(img.convert("RGBA"), ov).convert("RGB"), (0, 0))


# ── ffmpeg ──────────────────────────────────────────────────
def find_ffmpeg():
    try:
        import imageio_ffmpeg
        p = imageio_ffmpeg.get_ffmpeg_exe()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    cands = [
        os.path.join(os.environ.get("APPDATA", ""), "Python", "Python314",
                     "site-packages", "imageio_ffmpeg", "binaries",
                     "ffmpeg-win-x86_64-v7.1.exe"),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return "ffmpeg"


# ── 主流程 ──────────────────────────────────────────────────
def main(formats):
    log("=" * 74)
    log("  二战欧洲 1939→1945 演变视频")
    log("=" * 74)

    t0 = time.time()
    tl = ControlTimeline.load(TL_PATH)
    log(f"[1] 控制时间线: 事件 {len(tl.events)} 条 / 标记 {len(tl.data.get('markers') or [])} 条 "
        f"/ 基线实体 {len(tl.baseline)} 个")
    ds = build_dates(tl)
    log(f"    时间轴关键帧 {len(ds)} 个（{ds[0]} → {ds[-1]}），月度帧 + 事件帧合并")

    # 几何：按年构建一次，缓存复用
    ad = Registry.get("cshapes")
    gj = ad.load()
    ad.load = lambda use_cache=True: gj          # 25 MB JSON 只解析一次
    lon0, lat0, lon1, lat1 = BBOX
    geo, qrep = {}, None
    log(f"\n[2] 构建 {YEAR_FROM}–{YEAR_TO} 几何切片")
    for y in range(YEAR_FROM, YEAR_TO + 1):
        s = ad.build(years=[y])
        fr = s.frames[0]
        if y == 1941:
            rep = assess_series(s, attach=True)
            qrep = rep
            d = rep["distribution"]
            log(f"    几何质量闸门(1941): 真实 {d.get('real',0)} / 粗略 {d.get('coarse',0)} / "
                f"合成 {d.get('synthetic',0)}  真实率 {rep['real_ratio']*100:.1f}%")
        keep = []
        for rg in fr.regions:
            b = rg.bbox()
            if not b:
                continue
            if b[2] < lon0 or b[0] > lon1 or b[3] < lat0 or b[1] > lat1:
                continue
            keep.append(rg)
        fr.regions = keep
        geo[y] = fr
        log(f"    {y}: 实体 {len(keep)} 个")
    log(f"    几何就绪 {time.time()-t0:.1f}s")

    ff = find_ffmpeg()
    log(f"\n[3] ffmpeg: {ff}")

    for fname, cfg in formats.items():
        render(fname, cfg, tl, ds, geo, ff, qrep)

    with open(LOG, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"\n报告 -> {LOG}")
    log("完成")


def render(fname, cfg, tl, ds, geo, ff, qrep):
    W, H, mode = cfg["w"], cfg["h"], cfg["mode"]
    out_mp4 = os.path.join(VID_DIR, f"ww2_1939-1945_{fname}.mp4")
    err_log = os.path.join(VID_DIR, f"ffmpeg_{fname}.log")
    log("\n" + "-" * 74)
    log(f"[{fname}] {W}x{H} mode={mode}")
    log("-" * 74)

    lay = Layout(width=W, height=H, mode=mode,
                 band_top_ratio=0.20, band_max_height_ratio=0.52)
    r = Renderer(STYLE, lay, projection="mercator", supersample=2)

    cmd = [ff, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_mp4]
    errf = open(err_log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)

    def send(im):
        proc.stdin.write(im.tobytes())

    prev_img = None
    prev_date = None
    n_out = 0
    tf0 = time.time()
    poster_done = False
    lim = int(os.environ.get("WW2_LIMIT") or 0)
    if lim:
        ds = ds[:lim]
    # 面板尺寸按全片最宽的一行预先固定（避免交叉溶解出幽灵文字）
    tb = ticker_box(W, H, r, [recent_lines(tl, d, 3) for d in ds])
    log(f"    大事记面板固定尺寸 {tb['x1']-tb['x0']}x{tb['y1']-tb['y0']} @ "
        f"({tb['x0']},{tb['y0']})")

    for i, d in enumerate(ds):
        y = d.year
        fr = geo[y]
        layer = tl.layer_at(d.isoformat())
        st = layer.apply(fr)

        beats = beats_between(tl, prev_date, d)
        fr.title = (f"{y} 年 {d.month} 月" if d.day == 1
                    else f"{y} 年 {d.month} 月 {d.day} 日")
        if beats:
            bd, bl, _ = beats[-1]
            fr.subtitle = f"{bd.strftime('%m-%d')} · {bl}"
        elif prev_date is None:
            fr.subtitle = "战前的欧洲"
        else:
            fr.subtitle = ""

        img = r.render_frame(fr, bbox=BBOX, legend_items=LEGEND,
                             legend_title="实际控制")
        draw_ticker(img, recent_lines(tl, d, 3), tb)
        draw_footer(img, FOOTER, r)

        if not poster_done and y == 1942 and d.month == 11:
            p = os.path.join(MAP_DIR, f"ww2_poster_{fname}.png")
            img.save(p)
            log(f"    封面帧 -> {os.path.basename(p)}")
            poster_done = True

        # 交叉溶解
        if prev_img is not None:
            nf = max(1, int(FADE * FPS))
            for k in range(1, nf + 1):
                send(Image.blend(prev_img, img, k / (nf + 1)))
                n_out += 1
        hold = HOLD_EVENT if beats else HOLD_STILL
        for _ in range(max(1, int(hold * FPS))):
            send(img)
            n_out += 1

        prev_img, prev_date = img, d
        if (i + 1) % 5 == 0 or i == len(ds) - 1:
            log(f"    {i+1}/{len(ds)} 帧  当前 {d}  输出 {n_out} 帧 "
                f"({n_out/FPS:.1f}s)  用时 {time.time()-tf0:.0f}s  RSS {rss_mb():.0f}MB")

    # 结尾定格 2s
    for _ in range(FPS * 2):
        send(prev_img)
        n_out += 1

    proc.stdin.close()
    rc = proc.wait()
    errf.close()
    dur = n_out / FPS
    size = os.path.getsize(out_mp4) / 1024 / 1024 if os.path.exists(out_mp4) else 0
    log(f"    ffmpeg rc={rc}  {n_out} 帧 = {dur:.1f}s  {size:.1f} MB")

    # 输出校验：MP4 的 moov 索引块写在文件**末尾**，渲染中途被打断会留下一个
    # 体积看着正常、但播不了的截断文件（踩过：站点的演示视频 0:00 黑屏）。
    # 所以这里必须真解一遍，不能只看文件大小。
    ok, why = verify_mp4(ff, out_mp4)
    if ok:
        log(f"    ✓ 输出校验通过（{why}）")
    else:
        log(f"    ✗ 输出校验失败：{why}")
        log(f"      文件是截断的，不要当成成品使用。")
    log(f"    -> {out_mp4}")
    if rc != 0:
        log(open(err_log, encoding="utf-8", errors="replace").read()[-1500:])
    return ok and rc == 0


def verify_mp4(ff: str, path: str):
    """真解一遍 MP4，确认 moov 索引存在且能读完整。

    只看文件大小是不够的：被打断的 ffmpeg 会留下一个几 MB 的残file，
    `Get-Item` 看起来完全正常，浏览器却是 0:00 黑屏（踩过）。

    注意：只能用 ffmpeg 自己的参数，`-show_entries` 是 ffprobe 专用的，
    跑到 ffmpeg 上会报 "Unrecognized option"。
    """
    if not os.path.exists(path):
        return False, "文件不存在"
    import subprocess as sp
    try:
        p = sp.run([ff, "-v", "error", "-i", path, "-f", "null", "-"],
                   capture_output=True, text=True, timeout=600)
    except Exception as e:
        return False, f"解码异常 {e}"
    err = (p.stderr or "").strip()
    if "moov atom not found" in err:
        return False, "moov atom not found —— 渲染被中途打断，文件截断"
    if p.returncode != 0:
        return False, (err.splitlines() or ["未知错误"])[-1][:160]
    mb = os.path.getsize(path) / 1024 / 1024
    return True, f"可完整解码，{mb:.1f} MB"


if __name__ == "__main__":
    sel = sys.argv[1:] or ["16x9", "9x16"]
    ALL = {
        "16x9": {"w": 1920, "h": 1080, "mode": "full"},
        "9x16": {"w": 1080, "h": 1920, "mode": "band"},
    }
    main({k: ALL[k] for k in sel if k in ALL})
