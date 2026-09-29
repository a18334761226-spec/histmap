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
                tone_match: bool = False) -> Image.Image:
    """把风格参数套到地图底图上。

    踩过的坑（很值得记）：初版默认做了**直方图匹配**（把底图亮度分位数映射到
    参考图的分位数），结果德国/苏联/英国全变成一样的米色、海洋和陆地一起变亮，
    信息彻底没了——**和图像模型犯的是同一个错**。

    根因：地图是**分类色图像**，它的明暗是有语义的
    （暗=海洋、中亮=陆地、白=文字）；而羊皮纸参考图的明暗只是材质。
    把前者的色调分布硬套到后者上，等于把「暗海洋」映射成「亮纸面」，
    语义被反转并压平。所以色调迁移默认**关闭**，
    只保留不改分类结构的操作：白平衡 / 饱和度 / 纹理 / 颗粒 / 暗角。

    `strength` 控制整体强度（0=原图，1=完全套用）。
    `protect_text` 对高亮度近白像素（文字与其描边）减弱纹理与暗角——
    文字是信息，不该被材质啃掉。
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

    # 1) 白平衡（乘性：只改通道增益，不改色相之间的相对关系）
    gain = np.array(prof["white_balance_gain"], dtype=np.float32)
    gain = gain / max(gain.mean(), 1e-6)          # 归一化，避免整体变亮变暗
    rgb = np.clip(rgb * (1.0 + (gain - 1.0) * 0.70 * strength), 0, 1)

    # 2) 饱和度向参考图靠拢，但**不归零**——归零就是把分区信息丢掉。
    #    下界锁在 0.55，保证德国红和苏联紫始终分得开。
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
