#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风格提取层 · 从一张参考图提取风格并套用到地图
================================================
这就是「有输入图片的地方根据图片提取风格」。

**为什么用代码而不是调模型**
   实测过：让图像模型直接美化地图，它会连同数据一起美化掉
   （德国红/苏联紫被抹成一片褐色，中文标注变乱码，NCC −0.075）。
   但「提取风格」这件事本身是**可测量的**——色调分布、纸纹频率、
   颗粒强度、暗角曲线、白平衡，全都是统计量。既然是统计量，代码就能算，
   而且算出来是**确定性**的：同一张参考图 + 同一张底图 = 永远同一个结果。

**关键约束：保住信息**
   风格化最容易毁掉的是「哪些区域属于谁」这个信息。
   所以本模块分两条路：
     · tone/texture 模式（默认）：只取参考图的明暗、质感、白平衡，
       底图的**色相关系原样保留** → 德国红和苏联紫依然可辨
     · palette 模式：连配色一起换成参考图的色系，但会**保持颜色的相对区分度**
       （把底图调色板按明度序重锚到参考图的主色上）

用法
    python src/style_from_image.py extract 参考图.png --out style.json
    python src/style_from_image.py apply 底图.png --style style.json [--palette]
    python src/style_from_image.py demo 参考图.png 底图.png      # 一步到位
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PIL import Image, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output", "styled")


# ── 色彩工具 ────────────────────────────────────────────────
def _srgb_to_lin(c):
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(c):
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(np.clip(c, 0, None), 1 / 2.4) - 0.055)


def luminance(rgb):
    """感知亮度（Rec.709 权重），在**线性**空间算才符合物理。"""
    lin = _srgb_to_lin(np.clip(rgb, 0, 1))
    return 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]


# ── HSL 工具（分类色重映射要用） ──────────────────────────────
def _hex_to_rgb01(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb01_to_hex(c) -> str:
    return "#%02x%02x%02x" % tuple(int(max(0.0, min(1.0, v)) * 255 + 0.5) for v in c)


def _rgb_to_hsl(c):
    r, g, b = c
    mx, mn = max(c), min(c)
    l = (mx + mn) / 2.0
    if mx == mn:
        return 0.0, 0.0, l
    d = mx - mn
    s = d / (2.0 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:
        h = ((g - b) / d) % 6
    elif mx == g:
        h = (b - r) / d + 2
    else:
        h = (r - g) / d + 4
    return h * 60.0, s, l


def _hsl_to_rgb(h, s, l):
    h = h % 360.0
    c = (1 - abs(2 * l - 1)) * s
    x = c * (1 - abs((h / 60.0) % 2 - 1))
    m = l - c / 2
    seg = int(h // 60) % 6
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][seg]
    return (r + m, g + m, b + m)


def _hue_dist(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


# ── 分类色重映射：这是「抽风格」真正的落点 ────────────────────
# ── OKLab：大色相跨度插值必须用它 ────────────────────────────
# 为什么不能用 HSL 直接插色相：纸色从暖米(h≈43°)换到深蓝(h≈210°)要绕 167°，
# HSL 会沿色相环走过去，中途正好穿过**绿色** —— 半强度的蓝图风格会得到
# 一整片亮绿的场地（实测就是这么翻车的）。OKLab 是感知均匀空间，
# 两点之间走的是最短的感知路径，不会绕道别的色相。
def _srgb_to_oklab(rgb):
    r, g, b = (_srgb_to_lin(np.asarray(c, dtype=np.float64)) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = np.cbrt(l), np.cbrt(m), np.cbrt(s)
    return (0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_,
            1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_,
            0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_)


def _oklab_to_srgb(lab):
    L, a, b = lab
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    lin = (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
           -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
           -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)
    return tuple(float(np.clip(_lin_to_srgb(np.asarray(v, dtype=np.float64)), 0, 1))
                 for v in lin)


def _blend_color(orig_hex: str, tgt_hex: str, t: float) -> str:
    """在 OKLab 里按 t 插值。t=0 原色，t=1 目标色。"""
    if t >= 1.0:
        return tgt_hex
    if t <= 0.0:
        return orig_hex
    a = _srgb_to_oklab(_hex_to_rgb01(orig_hex))
    b = _srgb_to_oklab(_hex_to_rgb01(tgt_hex))
    return _rgb01_to_hex(_oklab_to_srgb(tuple(x + (y - x) * t for x, y in zip(a, b))))


def derive_palette(prof: dict, region_colors: list[str], canvas_hex: str,
                   theme: str = "light", strength: float = 1.0) -> dict:
    """把参考图的色彩世界映射到地图的**分类色**上。

    为什么不能像旧版那样对像素乘白平衡增益：地图是**分类色图像**，
    它的颜色是有语义的（每个色 = 一个政权/一条路）。
    整体乘增益会把所有分类一起推向参考图的主色 —— 实测把一张
    「宋=黄褐 / 金=砖红 / 西夏=蓝灰」的图变成一整片青或一整片红，
    三个政权再也分不出来。那不是「套上风格」，那是把图毁了。

    正确做法是**逐类别**重映射，并且把「不同类别必须仍分得开」当成硬约束：
      · 参考图是浅色纸（羊皮纸/旧地图）→ 区域色铺在参考图的**中低明度段**，
        底色用参考图的高明度色
      · 参考图是深色场（蓝图/红黑）→ 区域色铺在**中高明度段**，底色用深色
      · 参考图只有一种色系时（色相多样性≈0，绝大多数真实参考图都是），
        靠**明度阶梯**拉开类别，而不是靠色相 —— 现实中的单色地图正是这么画的

    返回 {"canvas":hex, "map":{旧hex:新hex}, "text":hex, "text_dim":hex}
    """
    regs = list(dict.fromkeys(region_colors))            # 去重且保序
    if not regs:
        return {"canvas": canvas_hex, "map": {}, "text": None, "text_dim": None}

    # 参考图的明暗范围与主色相
    v = prof["lum_quantiles"]["v"]
    q = prof["lum_quantiles"]["q"]
    def at(p):
        return float(np.interp(p, q, v))
    lo, mid, hi = at(12), at(50), at(88)

    pal = prof.get("palette") or []
    # 主色相：取占比最大的那个色的色相
    pH, pS, pL = _rgb_to_hsl(_hex_to_rgb01(pal[0]["hex"])) if pal else (32.0, 0.30, 0.6)
    # 参考图里最亮与最暗的色（用来定纸色与场色）
    if pal:
        ls = [(_rgb_to_hsl(_hex_to_rgb01(c["hex"]))[2], c["hex"]) for c in pal]
        bright_hex = max(ls)[1]
        dark_hex = min(ls)[1]
    else:
        bright_hex, dark_hex = canvas_hex, canvas_hex

    ref_is_light = mid >= 0.45
    # 底色取自参考图：浅色参考图用它的亮色当纸，深色参考图用它的暗色当底
    new_canvas = bright_hex if ref_is_light else dark_hex

    # 画布与纸色的明度差必须够，否则区块会糊在背景里
    cL = _rgb_to_hsl(_hex_to_rgb01(new_canvas))[2]

    # 区域明度阶梯：按原图各区的明度排序后，铺到参考图的可用段上。
    # 段宽刻意压缩在可读区间内，避免出现纯黑或纯白的区块。
    if ref_is_light:
        lo_L, hi_L = max(0.22, lo * 0.72), min(0.78, hi * 0.86)
    else:
        lo_L, hi_L = max(0.34, mid * 0.86), min(0.90, hi * 1.02)
    if hi_L - lo_L < 0.22:                    # 参考图明暗太挤，强行撑开可读区间
        hi_L = min(0.92, lo_L + 0.22)

    order = sorted(range(len(regs)),
                   key=lambda i: _rgb_to_hsl(_hex_to_rgb01(regs[i]))[2])
    n = len(order)
    # 相邻两级至少差 0.075 —— 0.05 是能算出来但小图上看不清的下限，
    # 地图是要缩到手机屏上看的东西，留够余量。
    need = 0.075 * max(0, n - 1)
    if hi_L - lo_L < need:
        hi_L = min(0.94, lo_L + need)

    # 色相策略 —— 这里是整个函数最关键的一步：
    #   · 参考图本身有多种色相（spread 大）→ 把它的色相按区域原色相排序后分配过去，
    #     这样地图就继承了参考图的配色体系
    #   · 参考图只有一种色系（spread 小，羊皮纸/蓝图/红黑全是这种）→
    #     **完全**收拢到参考色相，区域之间的区分全靠明度阶梯。
    #     留一部分原色相是错的：原本三个政权色相能差 200°，留 28% 就是 56° 的乱跳，
    #     实测蓝图参考图会映射出绿和紫，根本不叫「蓝图风格」。
    spread = _palette_hue_spread(pal)
    multi = spread > 40.0
    # 多色参考图：按原色相排序，把参考图主色依次发下去
    ref_hues = []
    if multi:
        ref_hues = sorted({round(_rgb_to_hsl(_hex_to_rgb01(c["hex"]))[0], 1)
                           for c in pal[:6]})
    by_hue = sorted(range(len(regs)), key=lambda i: _rgb_to_hsl(_hex_to_rgb01(regs[i]))[0])
    hue_for_rank = {idx: k for k, idx in enumerate(by_hue)}

    mapping = {}
    for rank, idx in enumerate(order):
        t = rank / max(1, n - 1) if n > 1 else 0.5
        L = lo_L + (hi_L - lo_L) * t
        oh, os_, ol = _rgb_to_hsl(_hex_to_rgb01(regs[idx]))
        if multi and ref_hues:
            H = ref_hues[hue_for_rank[idx] % len(ref_hues)]
        else:
            # 单色参考图：全部收到参考色相，只给一点点抖动避免完全同色
            H = pH + (8.0 if rank % 2 else -8.0) * 0.5
        S = min(0.60, max(0.18, pS * 0.92 * strength + os_ * 0.25))
        if ref_is_light:
            S = min(S, 0.55)
        mapping[regs[idx]] = _rgb01_to_hex(_hsl_to_rgb(H, S, L))
    mapping[canvas_hex] = new_canvas

    # 强度：在原色与目标色之间插值。**这一步不能省** ——
    # 少了它 strength=0 也会把整张图换成满强度风格，滑杆就是个摆设（踩过）。
    if strength < 1.0:
        mapping = {k: _blend_color(k, v, strength) for k, v in mapping.items()}
        new_canvas = _blend_color(canvas_hex, new_canvas, strength)

    # 文字：底色最终是什么明度，文字就反着来 —— 这是**对比度要求**，不是风格选择。
    # 必须用混合之后的 new_canvas 算：用原底色的明度算的话，
    # 深色参考图 + 满强度会得到「深底 + 深字」，字直接看不见。
    cL = _rgb_to_hsl(_hex_to_rgb01(new_canvas))[2]
    if cL > 0.55:
        text = _rgb01_to_hex(_hsl_to_rgb(pH, min(0.45, pS), max(0.10, min(0.26, cL * 0.28))))
        dim = _rgb01_to_hex(_hsl_to_rgb(pH, min(0.32, pS), max(0.34, min(0.48, cL * 0.6))))
    else:
        text = _rgb01_to_hex(_hsl_to_rgb(pH, min(0.16, pS * 0.5), min(0.97, 0.86 + cL * 0.1)))
        dim = _rgb01_to_hex(_hsl_to_rgb(pH, min(0.20, pS * 0.6),
                                        max(0.60, min(0.80, 0.62 + cL * 0.2))))
    return {"canvas": new_canvas, "map": mapping, "text": text, "text_dim": dim,
            "ref_is_light": ref_is_light, "paper_hue": pH, "paper_sat": pS}


def _hue_shortest(from_h, to_h):
    """从 from 转到 to 的最短角差（带符号）。"""
    d = (to_h - from_h + 180.0) % 360.0 - 180.0
    return d


def _palette_hue_spread(pal):
    if len(pal) < 2:
        return 0.0
    hs = [_rgb_to_hsl(_hex_to_rgb01(c["hex"]))[0] for c in pal[:6]]
    return max(hs) - min(hs)


def region_contrast(colors: list[str]) -> float:
    """一组分类色两两之间的最小色差（0~1，越大越好分辨）。

    这是「套了风格之后图还能不能读」的量化判据：
    小于 0.10 基本就是糊成一片了。
    """
    if len(colors) < 2:
        return 1.0
    labs = [np.array(_rgb_to_hsl(_hex_to_rgb01(c))) for c in colors]
    worst = 1.0
    for i in range(len(labs)):
        for j in range(i + 1, len(labs)):
            a, b = labs[i], labs[j]
            dl = abs(a[2] - b[2])
            ds = abs(a[1] - b[1]) * 0.6
            dh = _hue_dist(a[0], b[0]) / 360.0
            # 明度差最可靠，色相差次之；任一够大就算分得开
            worst = min(worst, max(dl, dh * 1.4, ds))
    return float(worst)


# ── 提取 ────────────────────────────────────────────────────
def extract_style(path: str, n_colors: int = 8, verbose: bool = True) -> dict:
    """把一张参考图量成一组可复用的风格参数。"""
    im = Image.open(path).convert("RGB")
    W, H = im.size
    # 大图先降采样：统计量不需要全分辨率，而且快得多
    scale = min(1.0, 900.0 / max(W, H))
    if scale < 1.0:
        im = im.resize((int(W * scale), int(H * scale)), Image.LANCZOS)
    a = np.asarray(im, dtype=np.float32) / 255.0
    lum = luminance(a)

    # 1) 明暗分布：分位数比直方图更好存、更好插值
    qs = [0, 1, 5, 10, 25, 50, 75, 90, 95, 99, 100]
    levels = np.percentile(lum, qs).tolist()

    # 2) 白平衡 / 色偏：比较三通道均值。参考图偏黄，给出 (R>G>B) 的增益
    means = a.reshape(-1, 3).mean(axis=0)
    gray = float(means.mean())
    gains = (means / max(gray, 1e-6)).tolist()

    # 3) 平均饱和度：决定要不要把底图调淡
    mx = a.max(axis=2)
    mn = a.min(axis=2)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    sat_mean = float(sat.mean())

    # 4) 纸纹（低频）：重度模糊后的亮度起伏，反映纸张/光照的大尺度不均
    small = im.resize((max(8, im.width // 24), max(8, im.height // 24)), Image.LANCZOS)
    low = np.asarray(small.filter(ImageFilter.GaussianBlur(1.2)), dtype=np.float32) / 255.0
    low = luminance(low)
    low = (low - low.mean()) / max(low.std(), 1e-6)

    # 5) 颗粒（高频）：原图减模糊，标准差就是颗粒强度
    blur = np.asarray(im.filter(ImageFilter.GaussianBlur(1.6)), dtype=np.float32) / 255.0
    hi = (a - blur)
    grain = float(hi.std())

    # 6) 暗角：四角亮度 / 中心亮度
    g = lum
    h, w = g.shape
    cy0, cy1 = int(h * 0.35), int(h * 0.65)
    cx0, cx1 = int(w * 0.35), int(w * 0.65)
    center = float(g[cy0:cy1, cx0:cx1].mean())
    corners = float(np.mean([g[:h // 6, :w // 6].mean(), g[:h // 6, -w // 6:].mean(),
                             g[-h // 6:, :w // 6].mean(), g[-h // 6:, -w // 6:].mean()]))
    vignette = float(np.clip(1.0 - corners / max(center, 1e-6), -0.5, 0.9))

    # 7) 主色（供 palette 模式）：中位切分量化
    q = im.quantize(colors=n_colors, method=Image.MEDIANCUT).convert("RGB")
    cnt = np.asarray(q).reshape(-1, 3)
    uniq, counts = np.unique(cnt, axis=0, return_counts=True)
    order = np.argsort(-counts)[:n_colors]
    palette = [{"hex": "#%02x%02x%02x" % tuple(int(v) for v in uniq[i]),
                "share": round(float(counts[i]) / counts.sum(), 4)} for i in order]

    prof = {
        "name": os.path.splitext(os.path.basename(path))[0],
        "source_size": [W, H],
        "lum_quantiles": {"q": qs, "v": [round(x, 4) for x in levels]},
        "white_balance_gain": [round(x, 4) for x in gains],
        "saturation": round(sat_mean, 4),
        "texture_strength": round(float(low.std()), 4),
        "grain": round(grain, 5),
        "vignette": round(vignette, 4),
        "palette": palette,
        "_texture_grid": [[round(float(v), 3) for v in row] for row in low.tolist()],
    }
    if verbose:
        print(f"参考图 {os.path.basename(path)}  {W}x{H}")
        print(f"  亮度中位 {np.percentile(lum,50):.3f}   "
              f"白平衡增益 R{gains[0]:.2f} G{gains[1]:.2f} B{gains[2]:.2f}")
        print(f"  平均饱和 {sat_mean:.3f}   颗粒 {grain:.4f}   暗角 {vignette:.3f}")
        print(f"  主色 {', '.join(p['hex'] for p in palette[:5])}")
    return prof


# ── 套用 ────────────────────────────────────────────────────
def _hist_match(src_lum: np.ndarray, prof: dict) -> np.ndarray:
    """把底图亮度按参考图的分位数曲线重映射（保色相的关键一步）。

    只动**亮度**、不动各通道的相对比例，所以色相和饱和度关系原样保留——
    这正是模型做法（整体混向褐色）会毁掉而这里不会毁掉的东西。
    """
    q = np.array(prof["lum_quantiles"]["q"], dtype=np.float32) / 100.0
    v = np.array(prof["lum_quantiles"]["v"], dtype=np.float32)
    xp = np.percentile(src_lum, np.array(prof["lum_quantiles"]["q"], dtype=np.float32))
    # 分位点必须严格递增，否则 np.interp 行为未定义
    keep = np.concatenate([[True], np.diff(xp) > 1e-6])
    xp, v = xp[keep], v[keep]
    if len(xp) < 2:
        return src_lum
    return np.interp(src_lum, xp, v).astype(np.float32)


def apply_style(img: Image.Image, prof: dict, adopt_palette: bool = False,
                strength: float = 1.0, protect_text: bool = True,
                tone_match: bool = False, material_only: bool = False) -> Image.Image:
    """把**材质层**（纸纹 / 颗粒 / 暗角）套到已经上过色的地图上。

    历史教训（两版都栽在这，值得留着）：
      初版默认做直方图匹配，德国/苏联/英国全变成一样的米色，信息没了。
      第二版改成「只做白平衡 + 饱和度」，看着安全，实际**更糟**：
      白平衡是给整张图乘三通道增益，参考图偏蓝就整张变青、
      参考图偏红就整张变红 —— 实测三个政权的颜色全被推到一起，
      比直方图匹配还难读。

    根因是同一个：**对像素做整体运算**。地图是分类色图像，
    颜色即语义，只能**逐类别**重映射（见 derive_palette），
    不能用全局增益。所以颜色部分现在一律走 derive_palette 重渲染，
    这个函数只负责材质与光照（纹理/颗粒/暗角），
    material_only=True 时连饱和度都不碰。
    """
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    H, W = rgb.shape[:2]
    lum = luminance(rgb)
    orig_lum = lum.copy()

    # 0) 色调迁移：默认关闭，明确要求时才做（会损失分类对比，慎用）
    if tone_match:
        new_lum = _hist_match(lum, prof)
        new_lum = orig_lum + (new_lum - orig_lum) * strength
        rgb = np.clip(rgb * (new_lum / np.maximum(orig_lum, 1e-4))[..., None], 0, 1)

    # 1) 白平衡：**只在没走分类色重映射时**才允许，且强度压低。
    #    分类重映射已经决定了每个类别的颜色，再乘一层全局增益就会把类别推到一起。
    if not material_only:
        gain = np.array(prof["white_balance_gain"], dtype=np.float32)
        gain = gain / max(gain.mean(), 1e-6)          # 归一化，避免整体变亮变暗
        rgb = np.clip(rgb * (1.0 + (gain - 1.0) * 0.70 * strength), 0, 1)

    # 2) 饱和度向参考图靠拢，但**不归零**——归零就是把分区信息丢掉。
    #    下界锁在 0.55，保证各政权的颜色始终分得开。
    if not material_only:
        mx, mn = rgb.max(axis=2), rgb.min(axis=2)
        cur_sat = float(np.mean(np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0)))
        tgt = prof["saturation"]
        if cur_sat > 1e-6:
            k = 1.0 + ((tgt / cur_sat) - 1.0) * 0.45 * strength
            k = float(np.clip(k, 0.55, 1.5))
            gray = rgb.mean(axis=2, keepdims=True)
            rgb = np.clip(gray + (rgb - gray) * k, 0, 1)

    # 3) 文本保护掩码（越接近白色的像素越保护）
    if protect_text:
        keep = np.clip((orig_lum - 0.58) / 0.30, 0, 1)
    else:
        keep = np.zeros((H, W), np.float32)

    # 4) 纸纹（低频，乘性）：幅度压到 ±8%，只做「纸张不均匀」的暗示，
    #    不做大尺度明暗重组——重组就会破坏区域的可见性。
    grid = np.array(prof["_texture_grid"], dtype=np.float32)
    low = np.asarray(Image.fromarray(((grid * 0.25 + 0.5) * 255).astype(np.uint8), "L")
                     .resize((W, H), Image.BICUBIC), dtype=np.float32) / 255.0
    low = (low - 0.5) * 2.0
    tex = 1.0 + low * 0.08 * strength
    tex = tex * (1 - keep) + 1.0 * keep
    rgb = np.clip(rgb * tex[..., None], 0, 1)

    # 5) 颗粒
    if prof["grain"] > 0:
        rng = np.random.default_rng(7)
        g = rng.normal(0, min(prof["grain"], 0.06) * strength, (H, W, 1)).astype(np.float32)
        rgb = np.clip(rgb + g, 0, 1)

    # 6) 暗角：只处理「角比中心暗」的情形。参考图若四角更亮（带装饰边框的
    #    老地图很常见），不做反向提亮——那会把画面边缘洗白。
    v = float(prof["vignette"])
    if v > 1e-3:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2) / np.sqrt(2)
        mask = 1.0 - v * strength * np.clip(d, 0, 1) ** 2.0
        mask = mask * (1 - keep) + 1.0 * keep
        rgb = np.clip(rgb * mask[..., None], 0, 1)

    # 7) palette 模式：把配色重锚到参考图主色（分类重映射，不碰明暗结构）
    if adopt_palette:
        rgb = _remap_to_palette(rgb, prof["palette"], strength)

    return Image.fromarray((rgb * 255 + 0.5).astype(np.uint8), "RGB")


def palette_variety(palette: list) -> float:
    """参考图主色的「色相多样性」，0~1。太低说明它只有一种色系。

    羊皮纸类参考图的色相几乎全是暖黄 → 多样性接近 0 →
    若强行 adopt_palette，德国红和苏联紫会被压成同一片米色。此时应当拒绝。
    """
    if len(palette) < 2:
        return 0.0
    hs = []
    for p in palette:
        r, g, b = (int(p["hex"][i:i + 2], 16) / 255.0 for i in (1, 3, 5))
        mx, mn = max(r, g, b), min(r, g, b)
        if mx - mn < 1e-6:
            continue                       # 灰色不计入色相统计
        import colorsys
        hs.append(colorsys.rgb_to_hsv(r, g, b)[0])
    if len(hs) < 2:
        return 0.0
    hs = np.array(hs)
    # 圆周上的色相离散度
    ang = hs * 2 * np.pi
    R = np.sqrt(np.cos(ang).mean() ** 2 + np.sin(ang).mean() ** 2)
    return float(np.clip(1.0 - R, 0, 1))


def _remap_to_palette(rgb: np.ndarray, palette: list, strength: float) -> np.ndarray:
    """把底图颜色重锚到参考图主色。

    不是「每个像素换成最近主色」（那会变成色块拼贴、丢掉明暗层次），
    而是：**按明度序**把底图的色阶对应到参考图主色的明度序上，
    色相取自参考图、明暗变化保留底图的。这样换了色系，又保住区域区分度。

    前提是参考图本身有足够的色相多样性，否则所有区域会被压成同一色系
    ——调用方应先用 palette_variety() 判断。
    """
    pal = np.array([[int(p["hex"][i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
                    for p in palette], dtype=np.float32)
    if len(pal) < 2:
        return rgb
    pl = pal @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    pal = pal[np.argsort(pl)]                       # 按明度排序 → 一条色阶
    lin = np.linspace(0, 1, len(pal), dtype=np.float32)
    l = rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    lmin, lmax = float(l.min()), float(l.max())
    t = np.clip((l - lmin) / max(lmax - lmin, 1e-6), 0, 1)
    out = np.empty_like(rgb)
    for c in range(3):
        mapped = np.interp(t, lin, pal[:, c]).astype(np.float32)
        out[..., c] = rgb[..., c] * (1 - strength) + mapped * strength
    return np.clip(out, 0, 1)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="从参考图提取风格")
    e.add_argument("ref")
    e.add_argument("--out", default=None)
    e.add_argument("--colors", type=int, default=8)

    a = sub.add_parser("apply", help="把风格套到地图上")
    a.add_argument("map")
    a.add_argument("--style", required=True)
    a.add_argument("--palette", action="store_true", help="连配色一起换成参考图的色系")
    a.add_argument("--strength", type=float, default=1.0)
    a.add_argument("--out", default=None)

    d = sub.add_parser("demo", help="提取 + 套用一步完成")
    d.add_argument("ref")
    d.add_argument("map")
    d.add_argument("--palette", action="store_true")
    d.add_argument("--strength", type=float, default=1.0)

    args = ap.parse_args()

    if args.cmd == "extract":
        prof = extract_style(args.ref, args.colors)
        out = args.out or os.path.join(OUT, f"style_{prof['name']}.json")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(prof, f, ensure_ascii=False)
        print(f"  -> {out}")
        return

    if args.cmd == "apply":
        prof = json.load(open(args.style, encoding="utf-8"))
        img = Image.open(args.map)
        out = apply_style(img, prof, args.palette, args.strength)
        dst = args.out or os.path.splitext(args.map)[0] + f"__style_{prof['name']}.png"
        out.save(dst)
        print(f"{img.size} -> {out.size}   -> {dst}")
        return

    if args.cmd == "demo":
        prof = extract_style(args.ref)
        # 参考图只有一种色系时，强行 adopt_palette 会把所有区域压成同一色
        v = palette_variety(prof["palette"])
        print(f"  参考图色相多样性 {v:.3f}" +
              ("（单一色系，已自动关闭配色替换，只取质感）" if v < 0.18 else ""))
        pal = args.palette and v >= 0.18
        img = Image.open(args.map)
        out = apply_style(img, prof, pal, args.strength)
        os.makedirs(OUT, exist_ok=True)
        dst = os.path.join(OUT, f"{os.path.splitext(os.path.basename(args.map))[0]}"
                                f"__style_{prof['name']}.png")
        out.save(dst)
        print(f"{img.size} -> {out.size}   -> {dst}")


if __name__ == "__main__":
    main()
