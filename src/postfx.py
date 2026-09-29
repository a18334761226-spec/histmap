#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后期质感层 · 纯代码（确定性、可复现、零 API 成本）
======================================================
**为什么不让模型干这件事**

实测（2026-09，Qwen-Image-Edit-2509 @ 硅基流动）：
    底图 1920x1080 → 输出 1360x768，NCC −0.073，色块保真 34%
模型做出了非常漂亮的羊皮纸雕版质感，**但同时**：
  · 把所有控制区颜色抹成一片褐色 —— 地图再也读不出战局
  · 把所有中文标注变成乱码笔画
  · 重画了地理（凭空多出岛屿）

根因：让它「美化一张地图」，它会连同**数据**一起美化掉。
模型不知道哪些像素是「信息」、哪些是「材质」。

**正确分工**
    信息（区域颜色 / 边界 / 文字 / 图例）  ← 代码负责，一个像素都不能错
    材质（纸纹 / 做旧 / 暗角 / 光照）      ← 本模块负责，纯算法

关键设计：**用乘性调色而不是加性混色**。
    加性混合（alpha 混向褐色）会把红/紫/蓝全部拉平成一种颜色 —— 这正是模型犯的错。
    乘性调色只给每个通道一个增益（R>G>B 即变暖），
    保留颜色之间的**相对差异**，所以德国红和苏联紫依然一眼可辨。
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
from PIL import Image, ImageFilter, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 噪声 ────────────────────────────────────────────────────
def _value_noise(W, H, res, rng):
    """在低分辨率上取随机格点，再双三次放大 —— 便宜的 value noise。"""
    g = rng.random((max(2, res + 1), max(2, res + 1))).astype(np.float32)
    im = Image.fromarray((g * 255).astype(np.uint8), "L").resize((W, H), Image.BICUBIC)
    return np.asarray(im, dtype=np.float32) / 255.0


def fbm(W, H, octaves=5, base=3, seed=0):
    """分形布朗噪声（fBm）：叠加多个倍频，得到自然的纸张纤维感。"""
    rng = np.random.default_rng(seed)
    out = np.zeros((H, W), np.float32)
    amp, tot = 1.0, 0.0
    for o in range(octaves):
        out += amp * _value_noise(W, H, base * (2 ** o), rng)
        tot += amp
        amp *= 0.5
    out /= tot or 1.0
    return (out - out.min()) / max(1e-6, (out.max() - out.min()))


def vignette(W, H, strength=0.38, power=2.2):
    """径向暗角：四角压暗，把视线收进画面中心。"""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = W / 2.0, H / 2.0
    d = np.sqrt(((xx - cx) / cx) ** 2 + ((yy - cy) / cy) ** 2) / np.sqrt(2)
    return 1.0 - strength * np.clip(d, 0, 1) ** power


# ── 预设 ────────────────────────────────────────────────────
PRESETS = {
    "atlas": {
        "label": "古典雕版（做旧纸张）",
        "gain": (1.10, 1.00, 0.84),   # 乘性暖色：R>G>B
        "sat": 0.94,                  # 轻微降饱和，但绝不去色
        "contrast": 1.10,
        "texture": 0.30,              # 纸纹强度
        "tex_scale": 3,
        "octaves": 6,
        "vignette": 0.40,
        "grain": 5.0,                 # 颗粒噪点
        "edge_burn": 0.30,            # 四边做旧压暗
        "paper": True,                # 叠一层纸底色
    },
    "vintage": {
        "label": "复古羊皮纸",
        "gain": (1.14, 0.99, 0.78),
        "sat": 0.88,
        "contrast": 1.04,
        "texture": 0.42,
        "tex_scale": 2,
        "octaves": 6,
        "vignette": 0.52,
        "grain": 7.0,
        "edge_burn": 0.45,
        "paper": True,
    },
    "ink": {
        "label": "宣纸水墨",
        "gain": (1.04, 1.00, 0.94),
        "sat": 0.72,                  # 水墨要更淡
        "contrast": 0.96,
        "texture": 0.38,
        "tex_scale": 2,
        "octaves": 6,
        "vignette": 0.26,
        "grain": 4.0,
        "edge_burn": 0.18,
        "paper": True,
        "light": True,                # 宣纸是浅底，需要反相处理
    },
    "modern": {
        "label": "现代信息图（微质感）",
        "gain": (1.0, 1.0, 1.0),
        "sat": 1.02,
        "contrast": 1.08,
        "texture": 0.10,
        "tex_scale": 4,
        "octaves": 4,
        "vignette": 0.30,
        "grain": 2.5,
        "edge_burn": 0.0,
        "paper": False,
    },
}


# ── 主效果 ──────────────────────────────────────────────────
def stylize(img: Image.Image, preset: str = "atlas", seed: int = 7,
            protect_text: bool = True) -> Image.Image:
    """对已渲染好的地图施加质感。

    `protect_text=True` 时，对高亮度近白像素（即文字与其描边）**减弱纸纹与暗角**，
    因为文字是信息，不该被材质吃掉。这是模型做不到而代码轻松能做到的事。
    """
    p = PRESETS[preset]
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    H, W = rgb.shape[:2]

    # 1) 纸底色 + 纤维纹理（乘性）
    if p.get("paper"):
        paper = np.array([0.96, 0.92, 0.83], np.float32) if not p.get("light") \
            else np.array([0.965, 0.955, 0.925], np.float32)
        rgb = rgb * (1 - 0.18) + (rgb * paper) * 0.18 if False else rgb
        rgb *= paper

    tex = fbm(W, H, octaves=p["octaves"], base=p["tex_scale"], seed=seed)
    tex = (tex - 0.5) * 2.0                      # → [-1,1]
    layer = 1.0 + tex * p["texture"]

    # 文字保护：亮像素处减弱纹理，避免笔画被纸纹啃掉
    if protect_text:
        lum = rgb.mean(axis=2)
        keep = np.clip((lum - 0.72) / 0.28, 0, 1)     # 越亮越接近 1
        layer = layer * (1 - keep) + 1.0 * keep

    rgb *= layer[..., None]

    # 2) 乘性调色（保住色相差异，这是与模型做法的分水岭）
    rgb *= np.array(p["gain"], np.float32)

    # 3) 饱和度
    lum = (rgb * np.array([0.299, 0.587, 0.114], np.float32)).sum(axis=2, keepdims=True)
    rgb = lum + (rgb - lum) * p["sat"]

    # 4) 对比度 S 曲线
    c = p["contrast"]
    rgb = np.clip((rgb - 0.5) * c + 0.5, 0, 1)

    # 5) 暗角
    v = vignette(W, H, p["vignette"])
    if protect_text:
        v = v * (1 - keep) + 1.0 * keep
    rgb *= v[..., None]

    # 6) 四边做旧压暗（让画面像一张被翻旧的纸）
    if p["edge_burn"] > 0:
        b = int(min(W, H) * 0.06)
        m = np.ones((H, W), np.float32)
        ramp = np.linspace(0, 1, b, dtype=np.float32) ** 1.5
        m[:b, :] *= ramp[:, None]
        m[-b:, :] *= ramp[::-1, None]
        m[:, :b] *= ramp[None, :]
        m[:, -b:] *= ramp[::-1][None, :]
        rgb *= (1 - p["edge_burn"] * (1 - m))[..., None]

    # 7) 颗粒
    if p["grain"] > 0:
        rng = np.random.default_rng(seed + 99)
        g = rng.normal(0, p["grain"] / 255.0, (H, W, 1)).astype(np.float32)
        rgb = rgb + g

    return Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8), "RGB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("--preset", default="atlas", choices=list(PRESETS))
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    img = Image.open(args.src)
    out = stylize(img, args.preset, args.seed)
    dst = args.out or os.path.splitext(args.src)[0] + f"__fx_{args.preset}.png"
    out.save(dst)
    print(f"{args.preset} · {PRESETS[args.preset]['label']}")
    print(f"  {img.size} -> {out.size}   -> {dst}")


if __name__ == "__main__":
    main()
