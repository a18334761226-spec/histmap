#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
风格化层 · 双后端（魔搭 ModelScope / 硅基流动 SiliconFlow）
================================================================
架构定位（见 docs/design-browser-render.md）：
    代码定几何 + 模型定质感

    底图（代码画：国界/控制区/中文标注 100% 正确）
        ↓  图生图，保持尺寸与内容
    风格化样张（模型只加质感：羊皮纸/雕版/水墨）
        ↓  拆成「调色映射 + 纹理层」
    131 帧全部成片（纯代码套用，模型不再参与）

**为什么不让模型逐帧生成**：逐帧生成会漂移、中文会错字、不可复现、按帧计费。
模型只需跑 1~7 次，把「质感」变成可复用的确定性映射。

两个后端的契约差异（2026-09 核实）
  * 魔搭 api-inference.modelscope.cn —— **异步**：提交拿 task_id，再轮询
    /v1/tasks/{id}。来源：官方 modelscope/ms-agent 的 ms_image_gen.py
  * 硅基流动 api.siliconflow.cn —— **同步**：响应里直接给图片 URL。
    来源：docs.siliconflow.cn/docs/api/images-generations-post
  * 两家都不支持 Qwen-Image-Edit 的尺寸参数 → 保持输入尺寸，正合我们要求

用法
    # 魔搭
    set MODELSCOPE_TOKEN=ms-xxxx
    python src/stylize.py --probe
    python src/stylize.py output/maps/ww2_1941_control_horizontal_16x9.png --style ink

    # 硅基流动
    set SILICONFLOW_API_KEY=sk-xxxx
    python src/stylize.py <底图> --provider siliconflow --style atlas

    # 只跑保真度闸门（不调 API）
    python src/stylize.py --fidelity 底图.png 风格化.png
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "output", "styled")

# 默认模型。Qwen-Image-Edit 迭代很快：
#   2025-08 Edit → 2025-09 -2509 → 2025-12 -2511 → 2026-02 Qwen-Image-2.0
# 用 --model 或 SF_MODEL / MS_MODEL 覆盖。
DEFAULT_MODEL = {
    "modelscope": "Qwen/Qwen-Image-Edit-2509",
    "siliconflow": "Qwen/Qwen-Image-Edit-2509",
}

# 风格预设。prompt 有两个硬约束：
#   1. 反复强调「保持所有边界、色块、文字完全不变」——否则模型会重画
#   2. 只描述材质/光照/纸张，不描述地理——否则模型会「脑补」地图内容
STYLES = {
    "atlas": {
        "label": "古典地图集（雕版线刻）",
        "prompt": ("将这张历史地图转换为 18 世纪欧洲古典地图集风格：米黄色手工纸底纹、"
                   "细密雕版刻线、轻微铜版印刷网点、边缘自然泛黄做旧。"
                   "严格要求：保持图中所有国家边界线的位置和形状完全不变，"
                   "保持所有色块的颜色与分布完全不变，保持所有中文文字的字形、"
                   "位置、大小完全不变。只改变纸张材质与印刷质感，不要重绘地图内容。"),
    },
    "vintage": {
        "label": "复古羊皮纸",
        "prompt": ("将这张历史地图转换为复古羊皮纸风格：暖褐色羊皮纸纤维纹理、"
                   "四角磨损做旧、轻微污渍与折痕、柔和暖光。"
                   "严格要求：所有国界、色块、中文标注保持原样不动，"
                   "不添加任何新的地理元素或文字。只加纸张质感。"),
    },
    "ink": {
        "label": "水墨国风（适合二十四史题材）",
        "prompt": ("将这张历史地图转换为中国水墨地图风格：宣纸底纹、淡墨晕染、"
                   "毛笔勾线的边界、朱砂点缀。"
                   "严格要求：保持所有区域划分的位置与形状不变，"
                   "保持所有中文文字清晰可辨且字形不变，不添加新的内容。"),
    },
    "modern": {
        "label": "现代信息图（高级感）",
        "prompt": ("将这张历史地图转换为现代高级信息图风格：深色磨砂背景、"
                   "极细发光边界线、微妙的噪点颗粒感、柔和内阴影与层次。"
                   "严格要求：所有区域的位置、颜色与中文文字保持不变，"
                   "只提升质感与光影，不改变任何数据内容。"),
    },
}


# ── HTTP ────────────────────────────────────────────────────
def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get_json(url, headers, timeout):
    req = urllib.request.Request(url, method="GET", headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url, dst, timeout=180):
    with urllib.request.urlopen(url, timeout=timeout) as r, open(dst, "wb") as f:
        f.write(r.read())


def _b64_image(path: str) -> str:
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as f:
        return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


# ── 后端 ────────────────────────────────────────────────────
def _pick_image_url(body: dict) -> str:
    """从各家不同的响应里取出图片 URL。

    **同一个 OpenAI 兼容协议，两家回的字段名不一样：**
        硅基流动 / 魔搭： {"images": [{"url": ...}]}
        火山方舟（豆包）： {"data":   [{"url": ...}]}      ← OpenAI 原生写法
    踩过的坑：原来只读 `images`，于是选豆包时**图其实已经生成好了**，
    我们却因为读不到字段而抛「响应无图片」，把一张已经算完（已计费）的图扔掉。
    报错里还把整个响应打出来了 —— 现在能一眼看出 data 里明明有 url。
    所以两种都认，并且再兜一层：递归找任何像 url 的字符串。
    """
    for key in ("images", "data"):
        arr = body.get(key)
        if isinstance(arr, list):
            for it in arr:
                if isinstance(it, dict):
                    u = it.get("url") or it.get("image_url") or it.get("b64_json")
                    if u:
                        return u if str(u).startswith("http") else ""
                elif isinstance(it, str) and it.startswith("http"):
                    return it
    # 最后的兜底：响应里任何 http 开头的字符串
    for v in body.values():
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


class SiliconFlow:
    """OpenAI 兼容的**同步**图像接口：提交后响应里直接给图片 URL。

    名字叫 SiliconFlow 是历史原因（最早只接了这一家）。现在硅基流动和
    火山方舟（豆包 Seedream）都走这个协议，**所以 base 必须能传进来**。

    踩过的坑：`base` 原来是写死的类属性 `https://api.siliconflow.cn/v1`，
    而模型清单里明明有豆包 Seedream 那一条（base 指向火山方舟）——
    那个 base **从来没被用过**。于是选豆包时，请求带着**豆包的模型名和
    火山的 Key 打到了硅基流动的域名**，必然失败或者返回垃圾。
    跟之前那次 401 是同一类错：选了哪家，就得用哪家的地址。
    """

    name = "siliconflow"
    DEFAULT_BASE = "https://api.siliconflow.cn/v1"

    def __init__(self, key, model=None, base=None):
        self.key = key
        self.model = model or DEFAULT_MODEL["siliconflow"]
        # 去掉结尾斜杠：有些平台注入的地址带尾斜杠，拼出来会变成
        # //images/generations，少数网关会因此 404
        self.base = (base or self.DEFAULT_BASE).rstrip("/")

    def run(self, image_path, prompt, steps=30, seed=None, timeout=300):
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
             # 下游还要合成视频，关掉显式水印；最终输出由 add_ai_watermark 补
             "X-Enable-Watermark": "0"}
        payload = {"model": self.model, "prompt": prompt,
                   "image": _b64_image(image_path), "num_inference_steps": steps}
        if seed is not None:
            payload["seed"] = seed
        t0 = time.time()
        try:
            body = _post_json(f"{self.base}/images/generations", payload, h, timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:600]}") from None
        url = _pick_image_url(body)
        if not url:
            raise RuntimeError(f"响应无图片: {json.dumps(body, ensure_ascii=False)[:400]}")
        return url, time.time() - t0, body.get("seed")

    def text2img(self, prompt, model=None, size="1024x1024", steps=30,
                 seed=None, negative_prompt="", timeout=300):
        """纯文生图（**不带输入图**）。

        为什么要单独一个方法：图生图那条路实测会把中文标注改成乱码笔画
        （见 style_graph 的说明与实测数据），因为"编辑一张图"这个任务本身
        就允许模型重画内容。而**生成一张纸纹**不需要它保住任何内容 ——
        纸纹本来就该只有材质。把任务换成"从零画一张空白纸"，
        模型改坏东西的空间就没了，然后我们把地图**画在它上面**。
        """
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
             "X-Enable-Watermark": "0"}
        # **默认用 self.model，不要写死一家。**
        # 踩过的坑：这里原来写 `model or "Qwen/Qwen-Image"`，于是调用方
        # 明明选了豆包（base 是火山方舟），纯文生图却把 Qwen 的名字发过去，
        # 得到 `404 InvalidEndpointOrModel.NotFound: Qwen/Qwen-Image`。
        # self.model 已在 __init__ 里兜过默认值，直接用就行。
        payload = {"model": model or self.model, "prompt": prompt,
                   "image_size": size, "num_inference_steps": steps,
                   "batch_size": 1}
        # 火山方舟的图像接口用 size 而不是 image_size，多给一个不冲突
        if "volces.com" in self.base:
            payload["size"] = size
            payload.pop("image_size", None)
            payload.pop("num_inference_steps", None)
            payload["response_format"] = "url"
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt
        if seed is not None:
            payload["seed"] = seed
        t0 = time.time()
        try:
            body = _post_json(f"{self.base}/images/generations", payload, h, timeout)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:600]}") from None
        url = _pick_image_url(body)
        if not url:
            raise RuntimeError(f"响应无图片: {json.dumps(body, ensure_ascii=False)[:400]}")
        return url, time.time() - t0, body.get("seed")


class ModelScope:
    """异步：提交拿 task_id，再轮询 /v1/tasks/{id}。

    契约来源：modelscope/ms-agent 官方 ms_image_gen.py。
    """
    name = "modelscope"
    base = "https://api-inference.modelscope.cn"

    def __init__(self, key, model=None):
        self.key, self.model = key, model or DEFAULT_MODEL["modelscope"]

    def run(self, image_path, prompt, steps=30, seed=None, timeout=600):
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}
        payload = {"model": self.model, "prompt": prompt, "negative_prompt": "",
                   "image": _b64_image(image_path)}
        if seed is not None:
            payload["seed"] = seed
        t0 = time.time()
        try:
            body = _post_json(f"{self.base}/v1/images/generations", payload,
                              {**h, "X-ModelScope-Async-Mode": "true"}, 120)
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"提交失败 HTTP {e.code}: {e.read().decode('utf-8','replace')[:600]}") from None
        task_id = body.get("task_id")
        if not task_id:
            raise RuntimeError(f"提交未返回 task_id: {json.dumps(body, ensure_ascii=False)[:400]}")

        interval, waited, last = 2.0, 0.0, None
        while waited < timeout:
            time.sleep(interval)
            waited += interval
            try:
                data = _get_json(f"{self.base}/v1/tasks/{task_id}",
                                 {**h, "X-ModelScope-Task-Type": "image_generation"}, 60)
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"
                continue
            status = data.get("task_status")
            if status == "SUCCEED":
                imgs = data.get("output_images") or []
                if not imgs:
                    raise RuntimeError(f"SUCCEED 但无输出: {json.dumps(data, ensure_ascii=False)[:400]}")
                return imgs[0], time.time() - t0, data.get("seed")
            if status == "FAILED":
                raise RuntimeError(f"任务失败: {json.dumps(data, ensure_ascii=False)[:600]}")
            # PENDING / RUNNING：退避
            interval = min(interval * 1.5, 10.0)
        raise RuntimeError(f"轮询超时 {timeout}s（最后状态 {last}）")


PROVIDERS = {"siliconflow": SiliconFlow, "modelscope": ModelScope}


def load_dotenv():
    """从项目根的 .env 读密钥。

    这样用户不必把 token 贴进聊天记录或 shell 历史里。
    .env 必须在 .gitignore 中（开源前务必确认）。
    """
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return
    with open(p, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and v and not os.environ.get(k):
                os.environ[k] = v


def detect_provider() -> str:
    """按 key **前缀**判断后端，避免「拿 sk- 的 key 去调魔搭」这类低级错误。

        魔搭 ModelScope 令牌   ms- 开头
        硅基流动 SiliconFlow 密钥 sk- 开头

    只有前缀不可辨时才退回「哪个变量有值用哪个」。
    """
    ms = (os.environ.get("MODELSCOPE_TOKEN") or os.environ.get("MODELSCOPE_API_KEY")
          or os.environ.get("MS_TOKEN") or "")
    sf = os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("SF_API_KEY") or ""
    if sf.startswith("sk-") and not ms.startswith("ms-"):
        return "siliconflow"
    if ms.startswith("ms-"):
        return "modelscope"
    if sf:
        return "siliconflow"
    if ms:
        return "modelscope"
    return "siliconflow"


def make_provider(name: str):
    if name in ("", "auto"):
        name = detect_provider()
        print(f"[auto] 按已配置的密钥判定后端 = {name}")
    if name == "modelscope":
        key = (os.environ.get("MODELSCOPE_TOKEN") or os.environ.get("MODELSCOPE_API_KEY")
               or os.environ.get("MS_TOKEN"))
        model = os.environ.get("MS_MODEL")
    else:
        key = os.environ.get("SILICONFLOW_API_KEY") or os.environ.get("SF_API_KEY")
        model = os.environ.get("SF_MODEL")
    if not key:
        env = "MODELSCOPE_TOKEN" if name == "modelscope" else "SILICONFLOW_API_KEY"
        raise SystemExit(f"缺少 {env} 环境变量。")
    return PROVIDERS[name](key, model), key


# ── 保真度闸门 ──────────────────────────────────────────────
def check_fidelity(base_path: str, styled_path: str):
    """检查风格化是否破坏了底图的数据。

    模型最常见的失败不是「不好看」，而是**偷偷重画了文字或边界**——
    人眼扫一遍看不出来，但观众会。两个客观指标：
      1. 尺寸一致
      2. 结构相似度 NCC（降采样灰度互相关）
      3. 色块保真：底图为饱和色块的采样点，颜色是否还在同一色系
    """
    from PIL import Image
    import math
    a = Image.open(base_path).convert("RGB")
    b = Image.open(styled_path).convert("RGB")
    rep = {"size_base": list(a.size), "size_styled": list(b.size), "ok": True, "issues": []}
    if a.size != b.size:
        rep["issues"].append(f"尺寸不一致 {a.size} -> {b.size}（模型重采样了，底图细节已损）")
        b = b.resize(a.size)
        rep["ok"] = False

    def small(im):
        w = 256
        h = max(1, int(im.height * w / im.width))
        px = list(im.convert("L").resize((w, h)).tobytes())
        return px, sum(px) / len(px)

    pa, ma = small(a)
    pb, mb = small(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(pa, pb))
    da = math.sqrt(sum((x - ma) ** 2 for x in pa))
    db = math.sqrt(sum((y - mb) ** 2 for y in pb))
    ncc = num / (da * db) if da and db else 0.0
    rep["ncc"] = round(ncc, 4)
    if ncc < 0.90:
        rep["issues"].append(
            f"结构相似度 {ncc:.3f} < 0.90 —— 模型改动了图面内容（很可能重画了边界或文字）")
        rep["ok"] = False

    grid, same, tot = 24, 0, 0
    for gy in range(grid):
        for gx in range(grid):
            x = int((gx + 0.5) * a.width / grid)
            y = int((gy + 0.5) * a.height / grid)
            ca, cb = a.getpixel((x, y)), b.getpixel((x, y))
            mx, mn = max(ca), min(ca)
            if mx < 60 or (mx - mn) < 30:
                continue          # 只统计底图为饱和色块的点，跳过背景与文字
            tot += 1
            if abs(ca[0] - cb[0]) + abs(ca[1] - cb[1]) + abs(ca[2] - cb[2]) < 240:
                same += 1
    rep["color_keep"] = round(same / tot, 4) if tot else 1.0
    rep["color_samples"] = tot
    if tot >= 20 and rep["color_keep"] < 0.80:
        rep["issues"].append(f"色块保真度 {rep['color_keep']*100:.0f}% < 80% —— 分区颜色被改乱")
        rep["ok"] = False
    return rep


def add_ai_watermark(path: str, text: str = "AI 生成"):
    """按平台条款：关闭显式水印后，开发者须自行给最终输出加 AI 标识。"""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(path).convert("RGB")
    d = ImageDraw.Draw(im)
    fs = max(12, int(im.height * 0.016))
    try:
        f = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", fs)
    except Exception:
        f = None
    pad = int(fs * 0.5)
    x, y = im.width - pad, im.height - pad
    box = d.textbbox((0, 0), text, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.rectangle([x - w - pad * 2, y - h - pad * 2, x, y], fill=(20, 20, 20))
    d.text((x - w - pad, y - h - pad), text, font=f, fill=(200, 200, 200))
    im.save(path)


# ── 主流程 ──────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", help="底图 PNG（代码渲染出的精确地图）")
    ap.add_argument("--provider", default="auto",
                    choices=["auto"] + list(PROVIDERS),
                    help="auto = 按 key 前缀自动判定")
    ap.add_argument("--model", default=None, help="覆盖模型名")
    ap.add_argument("--style", default="atlas", choices=list(STYLES))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--check", action="store_true", help="只打印配置，不请求")
    ap.add_argument("--probe", action="store_true",
                    help="用最小代价真跑一次，验证 token 与图生图参数是否可用")
    ap.add_argument("--fidelity", nargs=2, metavar=("BASE", "STYLED"),
                    help="只跑保真度闸门，不调 API")
    args = ap.parse_args()
    load_dotenv()

    if args.fidelity:
        rep = check_fidelity(args.fidelity[0], args.fidelity[1])
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        sys.exit(0 if rep["ok"] else 1)

    prov, key = make_provider(args.provider)
    if args.model:
        prov.model = args.model
    print(f"后端 {prov.name}   模型 {prov.model}   Key {key[:6]}…{key[-4:]}")
    for k, v in STYLES.items():
        print(f"  风格 {k:9s} {v['label']}")
    if args.check:
        return

    if args.probe:
        base = args.base or os.path.join(
            ROOT, "output", "maps", "ww2_1941_control_horizontal_16x9.png")
        print(f"\n[probe] 用一张小图真跑一次，验证 token 与「图生图」参数名是否被接受")
        url, dt, seed = prov.run(base, "保持画面内容完全不变，只把纸张做成轻微做旧效果。",
                                 steps=8, timeout=300)
        print(f"  成功  {dt:.1f}s  seed={seed}\n  {url[:110]}")
        return

    if not args.base:
        ap.error("需要底图路径（或用 --probe / --check）")

    print(f"\n[1] 底图 {args.base}\n[2] 风格 {args.style} · {STYLES[args.style]['label']}")
    print(f"[3] 调用 {prov.model} …")
    url, dt, seed = prov.run(args.base, STYLES[args.style]["prompt"],
                             steps=args.steps, seed=args.seed)
    print(f"    {dt:.1f}s  seed={seed}")

    os.makedirs(OUT, exist_ok=True)
    name = os.path.splitext(os.path.basename(args.base))[0]
    dst = os.path.join(OUT, f"{name}__{args.style}.png")
    _download(url, dst)
    print(f"    -> {dst}")

    rep = check_fidelity(args.base, dst)
    print(f"[4] 保真度闸门: {'通过' if rep['ok'] else '未通过'}  "
          f"NCC={rep.get('ncc')}  色块保真={rep.get('color_keep')}")
    for i in rep["issues"]:
        print(f"    ! {i}")
    add_ai_watermark(dst)
    print("[5] 已补「AI 生成」标识（关闭平台显式水印后的开发者义务）")


if __name__ == "__main__":
    main()
