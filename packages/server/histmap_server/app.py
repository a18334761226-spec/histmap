#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
histmap-server · 把引擎包成一个可用的应用
==============================================
五件事：
  1. 上传参考图 → 提取风格（纯代码，零成本，确定性）
  2. 对话指定/修改要生成的内容（LLM 只做「把话翻译成结构化参数」）
  3. 出图 + 出片，产物可下载
  4. 模型与 API Key 由用户在前端填（服务端只透传，不落盘）
  5. 开源：无数据库、无外部服务依赖、单进程即可跑

**Key 的处理原则**：前端存 localStorage，随请求用 header 传。
服务端**不写盘、不记日志**。这样开源出去也不会有人因为 clone 而泄露密钥。

启动
    python -m histmap_server.app            # 默认 127.0.0.1:8810
    或  uvicorn histmap_server.app:app --port 8810
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import time
import urllib.request
import zipfile

from fastapi import (FastAPI, File, Header, HTTPException, Request,
                     UploadFile)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               Response)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PKG_PARENT = os.path.dirname(HERE)              # packages/server
# 这四行是「零配置启动」的关键：把 packages/server 也放进来，
# `python packages\server\histmap_server\app.py` 才能 import histmap_server。
# 少了它就必须先 set PYTHONPATH，而那正是新用户第一次跑必踩的一脚。
for p in (os.path.join(ROOT, "packages", "core"), os.path.join(ROOT, "src"),
          PKG_PARENT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from histmap_server import jobs, topics          # noqa: E402

MEDIA = os.path.join(ROOT, "output", "api")
os.makedirs(MEDIA, exist_ok=True)

app = FastAPI(title="histmap", version="0.2")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── 可选模型清单（前端下拉用；实际能用哪些取决于用户的 key） ──
MODELS = {
    # 这些都是 OpenAI 兼容协议，换一家只要改 Base URL + 模型名 + Key。
    # 挑模型看的是**出控制表的质量**（年份齐不齐、单元名对不对），
    # 不是「谁画得好看」—— 图是代码画的，模型不碰像素。
    "chat": [
        # svc 是服务商名，界面按它分组。不能拿模型标签当服务名 ——
        # 那样下拉框的分组会显示成「Qwen2.5-7B」而不是「硅基流动」（踩过）。
        # ── 硅基流动（国内直连，一个 key 用多家模型）──
        {"id": "deepseek-ai/DeepSeek-V4-Flash", "label": "DeepSeek V4 Flash（快）",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动"},
        {"id": "deepseek-ai/DeepSeek-V4-Pro", "label": "DeepSeek V4 Pro（强）",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动"},
        {"id": "deepseek-ai/DeepSeek-V3.2", "label": "DeepSeek V3.2",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动"},
        {"id": "deepseek-ai/DeepSeek-R1", "label": "DeepSeek R1（推理）",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动"},
        {"id": "Qwen/Qwen2.5-72B-Instruct", "label": "Qwen2.5-72B（实测可用）",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动"},
        {"id": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5-7B（快、便宜）",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动"},
        # 注：DeepSeek 命名里没有「4.1」，只有 V4-Flash / V4-Pro / V3.2 / R1。
        # 上面这些 id 是本机从 /v1/models 实际查出来的，不是猜的。
        # ── 火山方舟（豆包）──
        # 模型名要填控制台里的**接入点 ID**（ep-…）或模型 ID。
        {"id": "doubao-seed-1-6-250615", "label": "豆包 Seed 1.6",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        {"id": "doubao-1-5-pro-32k-250115", "label": "豆包 1.5 Pro 32k",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        # ── 阿里百炼（通义千问）──
        {"id": "qwen-plus", "label": "通义千问 plus",
         "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "svc": "阿里百炼"},
        {"id": "qwen-max", "label": "通义千问 max",
         "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "svc": "阿里百炼"},
        # ── 智谱 ──
        {"id": "glm-4-plus", "label": "GLM-4-Plus",
         "base": "https://open.bigmodel.cn/api/paas/v4", "svc": "智谱"},
        # ── 魔搭 ──
        {"id": "Qwen/Qwen2.5-72B-Instruct", "label": "Qwen2.5-72B",
         "base": "https://api-inference.modelscope.cn/v1", "svc": "魔搭"},
    ],
    "image_edit": [
        {"id": "Qwen/Qwen-Image-Edit-2509", "label": "Qwen-Image-Edit-2509",
         "base": "https://api.siliconflow.cn/v1"},
        {"id": "Qwen/Qwen-Image-Edit", "label": "Qwen-Image-Edit",
         "base": "https://api.siliconflow.cn/v1"},
        # 注意：图像模型在这个项目里**不用来画地图**。用它的地方是
        # 「生成没有语义的材质」（纸纹/做旧/边框），生成完由代码把地图合成上去。
        # 拿它整张重画地图，实测结构相似度会掉到 NCC −0.075。
        {"id": "doubao-seedream-3-0-t2i-250415", "label": "豆包 Seedream 3.0（方舟·画材质）",
         "base": "https://ark.cn-beijing.volces.com/api/v3"},
    ],
}

STYLE_PRESETS = [
    {"id": "none", "label": "不加质感", "desc": "引擎原始输出：干净色块 + 实线边界"},
    {"id": "atlas", "label": "古典雕版", "desc": "做旧纸张、网点颗粒，像旧地图集的插图"},
    {"id": "vintage", "label": "复古羊皮纸", "desc": "更黄的底色、边缘压暗，适合讲古代"},
    {"id": "ink", "label": "宣纸水墨", "desc": "墨色渗化、纸纹明显，配浅色主题"},
    {"id": "modern", "label": "现代信息图", "desc": "微质感、高对比，适合短视频信息流"},
]

STYLE_PREVIEW_DIR = os.path.join(ROOT, "output", "style_previews")
# 预览用哪个题材出样：取一个「任何机器上都能渲染」的组合
PREVIEW_SCENE = "ww2-europe"
PREVIEW_DATE = "1941-06-22"


def style_preview(style_id: str) -> str | None:
    """给每个质感预设生成一张真实的小样（首次访问时生成，之后走磁盘缓存）。

    为什么不手写一张示意图：那样迟早和真实效果脱节。这里就是真渲染 + 真后期，
    所见即所得。
    """
    if style_id == "none":
        f = os.path.join(STYLE_PREVIEW_DIR, "none.jpg")
    else:
        f = os.path.join(STYLE_PREVIEW_DIR, f"{style_id}.jpg")
    if os.path.exists(f) and os.path.getsize(f) > 2000:
        return f"/media/style_previews/{os.path.basename(f)}"
    if not topics.get(PREVIEW_SCENE):
        return None
    try:
        from PIL import Image
        os.makedirs(STYLE_PREVIEW_DIR, exist_ok=True)
        img = topics.render(PREVIEW_SCENE, PREVIEW_DATE, "light", "16x9")
        if style_id != "none":
            import postfx
            img = postfx.stylize(img, style_id, seed=7)
        # 缩到 560 宽足够看清质感，体积压在几十 KB
        w = 560
        img = img.convert("RGB").resize((w, round(img.height * w / img.width)),
                                        Image.LANCZOS)
        img.save(f, quality=84, optimize=True)
    except Exception as e:
        print(f"[style] 生成 {style_id} 小样失败：{type(e).__name__}: {e}")
        return None
    return f"/media/style_previews/{os.path.basename(f)}"


@app.get("/api/styles")
def api_styles(with_preview: int = 1):
    # 默认带上真实小样；with_preview=0 时只回清单（冒烟自检/脚本用，快）
    if not with_preview:
        return {"presets": STYLE_PRESETS}
    out = []
    for p in STYLE_PRESETS:
        q = dict(p)
        q["preview"] = style_preview(p["id"])
        out.append(q)
    return {"presets": out}


# ════════════════════════════════════════════════════════════
# 基础
# ════════════════════════════════════════════════════════════
@app.get("/api/health")
def health():
    return {"ok": True, "topics": list(topics.load_topics()), "version": app.version,
            # 几何过期提示：改了控制表却没重跑构建时，这里点名是哪几年
            "stale": topics.stale_report()}


@app.get("/api/scenes")
def api_scenes():
    """题材列表。题材是**数据**（data/topics/topics.json），不是代码。"""
    return {"scenes": topics.list_topics()}


@app.get("/api/models")
def api_models():
    return MODELS


# ════════════════════════════════════════════════════════════
# 1) 风格提取：上传参考图 → 风格 profile
# ════════════════════════════════════════════════════════════
@app.post("/api/style/extract")
async def style_extract(file: UploadFile = File(...)):
    """从上传的参考图提取可复用的风格参数（纯代码，不调任何模型）。"""
    import style_from_image as SFI
    from PIL import Image

    raw = await file.read()
    if len(raw) > 20 * 1024 * 1024:
        raise HTTPException(413, "图片超过 20 MB")
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, "无法解析这张图片")

    tmp = os.path.join(MEDIA, f"ref_{int(time.time()*1000)}.png")
    im.save(tmp)
    try:
        prof = SFI.extract_style(tmp, verbose=False)
        variety = SFI.palette_variety(prof.get("palette") or [])
    except Exception as e:
        raise HTTPException(500, f"提取失败: {e}")

    prof.pop("_texture_grid", None)          # 网格太大，不往回传
    prof["_variety"] = round(variety, 4)
    prof["_advice"] = (
        "参考图是单一色系，配色替换已自动关闭，只取质感"
        if variety < 0.18 else
        "参考图色相丰富，可以连配色一起替换" if variety > 0.35 else
        "参考图色相偏单调，建议只取质感")
    return {"profile": prof, "preview": f"/media/api/{os.path.basename(tmp)}"}


# ════════════════════════════════════════════════════════════
# 2) 渲染
# ════════════════════════════════════════════════════════════
class RenderReq(BaseModel):
    scene: str
    date: str
    theme: str = "dark"
    size: str = "16x9"
    style: str = "none"                       # 预设质感
    style_profile: dict | None = None         # 从参考图提取的 profile
    strength: float = 1.0
    # 文案覆盖：None = 用题材默认（事件副标题/口径声明），"" = 明确留白。
    title: str | None = None
    subtitle: str | None = None
    footer: str | None = None


def _norm_profile(prof: dict | None) -> dict | None:
    """补回前端回传时丢掉的纹理网格。

    前端只存 palette / 亮度分位这些可读字段，`_texture_grid` 是 12x12 的
    低频图，回传体积不值当，这里按固定种子重新生成一张 —— 同参数同结果。
    """
    if not prof:
        return None
    p = dict(prof)
    if "_texture_grid" not in p:
        import numpy as np
        p["_texture_grid"] = np.random.default_rng(7).random((12, 12)).tolist()
    return p


def _apply_quality(img, req: RenderReq):
    """预设质感（postfx）。

    注意：**参考图风格不在这里套**。参考图风格必须先于渲染生效 ——
    它要换的是区域色/画布/文字这些分类色，渲染完再改就晚了，
    而且对像素做整体调色会把不同政权的颜色推到一起（实测直接毁图）。
    所以 style_profile 由 topics.render 内部走分类色重映射。
    """
    if req.style and req.style != "none":
        import postfx
        return postfx.stylize(img, req.style, seed=7)
    return img


@app.post("/api/render")
def api_render(req: RenderReq):
    if not topics.get(req.scene):
        raise HTTPException(404, f"没有这个题材: {req.scene}")
    t = topics.get(req.scene)
    if req.theme not in t.themes:
        req.theme = t.themes[0]
    t0 = time.time()
    prof = _norm_profile(req.style_profile)
    try:
        img = topics.render(req.scene, req.date, req.theme, req.size,
                            title=req.title, subtitle=req.subtitle, footer=req.footer,
                            style_profile=prof, strength=req.strength)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"渲染失败: {type(e).__name__}: {e}")
    img = _apply_quality(img, req)

    name = f"render_{req.scene}_{req.date}_{req.theme}_{req.style}_{int(time.time()*1000)}.png"
    path = os.path.join(MEDIA, name)
    img.save(path)
    return {
        # 注意：MEDIA 是 output/api，而 StaticFiles 挂在 output/ 上，
        # 所以 URL 必须带 api/ 这一段。少了它就 404 破图（踩过）。
        "image": f"/media/api/{name}",
        "width": img.size[0], "height": img.size[1],
        "elapsed": round(time.time() - t0, 2),
        "params": req.model_dump(exclude={"style_profile"}),
    }


# ════════════════════════════════════════════════════════════
# 3) 视频
# ════════════════════════════════════════════════════════════
class FrameReq(BaseModel):
    """分镜里的一帧。文案留 None 表示「用题材默认」。"""
    date: str
    hold: float | None = None                 # 本帧停留秒数，缺省用 hold
    title: str | None = None
    subtitle: str | None = None
    footer: str | None = None


class CardReq(BaseModel):
    """片头/片尾标题卡。text 为空则不出这张卡。"""
    text: str = ""
    sub: str = ""
    seconds: float = 2.0


class VideoReq(BaseModel):
    scene: str
    # 两种给帧的方式：
    #   1) date_from/date_to —— 按区间自动挑（快捷）
    #   2) frames —— 显式分镜，顺序、每帧文案、每帧停留都由调用方定（编辑器用）
    date_from: str = ""
    date_to: str = ""
    frames: list[FrameReq] | None = None
    theme: str = "dark"
    size: str = "16x9"
    style: str = "none"
    style_profile: dict | None = None
    # 风格强度：0 = 用题材自带配色，1 = 完全换成参考图的色彩世界。
    # 这个字段一度漏了 —— 渲染路径里写了 strength=req.strength，
    # 而 VideoReq 上没有它，于是**所有出片任务**都 AttributeError 挂掉。
    # 单张出图那条路是好的，所以只在「出片」时才暴露。
    strength: float = 1.0
    fps: int = 24
    hold: float = 0.30                        # 每帧默认停留秒数
    # 转场：0 = 硬切（快，走 concat）；>0 = 交叉溶解秒数。
    # 这个字段在上一版就声明了却从没被用过 —— 也就是说转场一直是硬切，
    # 界面上写着的「交叉溶解」是假的。现在真的实现了。
    fade: float = 0.0
    # 镜头运动：none / in（缓推）/ out（缓拉）。让静态地图有点呼吸感。
    motion: str = "none"
    intro: CardReq | None = None
    outro: CardReq | None = None
    watermark: str = ""                       # 水印文字，烧在右下角
    # 上限只用来兜住手滑（比如拿月份当区间滑到几万帧），不该成为常见区间的暗坑：
    # 二战 1939–1945 有 146 个时间点，卡在 60 就是「界面说 146 张、实际只出 60 张」。
    max_frames: int = 240
    # "video" = 出完全部图再合成片子；"frames" = 只出图，不调 ffmpeg。
    # 后者是给分镜预演用的：先在浏览器里照真实节奏放一遍，满意了再出片，
    # 不用为了看一眼效果先等一次视频合成。
    mode: str = "video"


def plan_frames(req: VideoReq) -> list[FrameReq]:
    """把两种给帧方式统一成一份分镜。

    显式 frames 时按用户给的顺序原样执行（顺序就是片子的叙事），
    只做两项校验：日期必须该题材真能渲染、帧数不超上限。
    """
    t = topics.get(req.scene)
    if not t:
        return []
    supported = t.dates() or []
    if req.frames:
        out = []
        for f in req.frames[:req.max_frames]:
            d = f.date
            if d not in supported:
                if not supported:
                    continue
                try:
                    d = min(supported, key=lambda x: abs(_days(x) - _days(d)))
                except Exception:
                    continue
            out.append(f.model_copy(update={"date": d}))
        return out
    return [FrameReq(date=d) for d in
            pick_dates(req.scene, req.date_from, req.date_to, req.max_frames)]


def pick_dates(scene_id: str, date_from: str, date_to: str, max_frames: int):
    """在 [from, to] 之间挑出题材**真正支持**的日期点。

    不是按天数均分 —— 每个题材能渲染的日期是离散的（唐只有 5 个年份，
    二战有 146 个），按天数切会切出一堆渲染不了的日期。
    """
    ds = topics.get(scene_id).dates()
    # 日期一律换算成序数再比。直接拿字符串比大小是错的：
    # '980-01-01' > '1040-01-01'（逐字符 '9' > '1'），
    # 会把 980 年这种三位数年份整个排到后面去，区间筛选全乱。
    a, b = _days(date_from), _days(date_to)
    lo, hi = min(a, b), max(a, b)
    sel = [d for d in ds if lo <= _days(d) <= hi]
    if not sel:
        # 区间内一个都没有 → 退化成「离区间端点最近的那个」
        sel = [min(ds, key=lambda d: min(abs(_days(d) - lo), abs(_days(d) - hi)))] if ds else []
    if len(sel) > max_frames:                 # 均匀抽稀，保留首尾
        step = (len(sel) - 1) / (max_frames - 1)
        sel = [sel[round(i * step)] for i in range(max_frames)]
    return sel


def _days(d: str) -> int:
    """日期 → 序数。容忍 '807' / '807-1-1' / '0807-01-01' 各种写法。"""
    from datetime import date as _d
    parts = [p for p in str(d)[:10].split("-") if p != ""]
    y = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 1
    dd = int(parts[2]) if len(parts) > 2 else 1
    try:
        return _d(y, m, dd).toordinal()
    except ValueError:                     # 月份/日越界（脏数据）→ 夹到合法范围
        return _d(y, max(1, min(12, m)), max(1, min(28, dd))).toordinal()


def _animate_job(jid: str, req: VideoReq):
    """先出**全部**图片（逐张落盘、逐张上报），最后才合成视频。

    用户要的就是这个顺序：先看到每一帧，最后才是片子。
    所以帧是写到 jobs/<id>/frames/ 的，前端可以边渲染边显示缩略图。
    """
    import subprocess
    import make_ww2_video as M

    sc = topics.get(req.scene)
    out_dir = jobs.job_dir(jid)
    fdir = os.path.join(out_dir, "frames")
    os.makedirs(fdir, exist_ok=True)
    mp4 = os.path.join(out_dir, "video.mp4")

    frames = plan_frames(req)
    if not frames:
        jobs.update(jid, status="failed", error="这个区间里没有可渲染的日期")
        return
    n = len(frames)
    jobs.update(jid, status="running", total=n, frames=[],
                message=f"准备渲染 {n} 张")

    # ── 1) 逐张出图 ──
    vprof = _norm_profile(req.style_profile)
    urls, holds, fpath = [], [], []
    for i, f in enumerate(frames):
        holds.append(max(0.05, f.hold if f.hold else req.hold))
        img = topics.render(req.scene, f.date, req.theme, req.size,
                            title=f.title, subtitle=f.subtitle, footer=f.footer,
                            style_profile=vprof, strength=req.strength)
        img = _apply_quality(img, RenderReq(scene=req.scene, date=f.date, theme=req.theme,
                                            size=req.size, style=req.style))
        p = os.path.join(fdir, f"{i:04d}.png")
        img.save(p)
        fpath.append(p)
        urls.append(f"/media/jobs/{jid}/frames/{i:04d}.png")
        jobs.update(jid, frames=list(urls), holds=holds,
                    progress=round((i + 1) / n * 0.85, 3),
                    message=f"出图 {i+1}/{n} · {str(f.date)[:10]}")

    # ── 2) 合成视频 ──
    cards = {}
    if req.mode == "frames":
        # 只要图：把每帧的停留时长一并带上，前端可以照真实节奏预演
        jobs.update(jid, status="done", progress=1.0, message="全部图片已出",
                    result={"video": None, "frames": urls, "n_frames": len(urls),
                            "holds": [round(h, 2) for h in holds],
                            "seconds": round(sum(holds), 1), "size": req.size,
                            "verify": "只出图模式", "zip": f"/media/jobs/{jid}/frames.zip"})
        return

    jobs.update(jid, message="合成视频…", progress=0.9)
    ff = M.find_ffmpeg()

    # 片头/片尾标题卡：跟正片同一套配色与字体，不是贴上去的外来图
    imgs = list(fpath)
    seg_holds = list(holds)
    for key, card in (("intro", req.intro), ("outro", req.outro)):
        if not card or not (card.text or "").strip():
            continue
        cimg = topics.title_card(sc, req.theme, req.size, card.text.strip(),
                                 (card.sub or "").strip())
        cp = os.path.join(fdir, f"card_{key}.png")
        cimg.save(cp)
        cards[key] = f"/media/jobs/{jid}/frames/card_{key}.png"
        if key == "intro":
            imgs.insert(0, cp)
            seg_holds.insert(0, max(0.3, card.seconds))
        else:
            imgs.append(cp)
            seg_holds.append(max(0.3, card.seconds))

    # 水印在写片段这一步烧进去：用 PIL 画比让 ffmpeg 叠字可控（字体、描边都好办）
    if req.watermark.strip():
        from PIL import Image as _Im
        for k, p in enumerate(imgs):
            try:
                im = _Im.open(p)
                topics.add_watermark(im, req.watermark.strip(), req.theme).save(p)
            except Exception as e:
                print(f"[水印] 第 {k} 张失败：{type(e).__name__}: {e}")
        if imgs:                      # 帧文件被改写过了，前端要强制刷新缩略图
            jobs.update(jid, frames=[u + f"?w={int(time.time())}" for u in urls])

    use_clips = req.motion != "none" or req.fade > 0.01 or bool(cards)
    errf = open(os.path.join(out_dir, "ffmpeg.log"), "w",
                encoding="utf-8", errors="replace")
    try:
        if use_clips:
            okc, whyc, want, extra = build_clip_video(
                ff, out_dir, imgs, seg_holds, req, mp4,
                lambda m: jobs.update(jid, message=m))
        else:
            cmd = build_ffmpeg_cmd(ff, fdir, mp4, holds, req.fps, out_dir)
            rc = subprocess.call(cmd, stderr=errf)
            want = sum(holds)
            okc = rc == 0
            whyc = f"ffmpeg rc={rc}"
            extra = {}
            if not okc:
                errf.flush()
                whyc = open(os.path.join(out_dir, "ffmpeg.log"),
                            encoding="utf-8", errors="replace").read()[-300:]
    finally:
        errf.close()

    ok, why = M.verify_mp4(ff, mp4)
    if not okc or not ok:
        jobs.update(jid, status="failed", error=f"{whyc}；{why}")
        return
    # 时长核对：说了几秒就该是几秒。不核的话，每帧停留这种新功能
    # 会「看起来跑通了」但片子长度不对（concat 的 duration 语义就栽在这）。
    got = M.probe_duration(ff, mp4)
    if got is not None and abs(got - want) > max(0.25, 2.5 / max(1, req.fps)):
        jobs.update(jid, status="failed",
                    error=f"片子时长对不上：按分镜算应为 {want:.2f}s，"
                          f"实际 {got:.2f}s。")
        return
    secs = round(got, 2) if got is not None else round(want, 1)
    res = {"video": f"/media/jobs/{jid}/video.mp4",
           "frames": urls, "n_frames": len(urls),
           "holds": [round(h, 2) for h in holds],
           "seconds": secs, "want_seconds": round(want, 2),
           "size": req.size, "verify": why,
           "zip": f"/media/jobs/{jid}/frames.zip",
           "fade": round(req.fade, 2) if req.fade > 0.01 else 0,
           "motion": req.motion,
           "watermark": req.watermark.strip()}
    res.update(extra)
    if cards:
        res["cards"] = cards
    jobs.update(jid, status="done", progress=1.0, message="完成", result=res)


def _zoompan_vf(motion: str, n_frames: int, fps: int, size: str) -> str:
    """镜头运动的滤镜串。

    放大倍率按「这段总共推多少」算，不写死每帧增量 —— 写死的话
    1 秒的帧和 3 秒的帧推的速度会不一样，节奏就乱了。
    参数是 src/probe_motion.py 量出来的：zoompan 配 d=1 时 on 随输出帧递增。
    """
    zmax = 1.12
    step = (zmax - 1.0) / max(1, n_frames)
    xy = "x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
    if motion == "in":
        z = f"min(1+{step:.6f}*on,{zmax})"
    else:                                     # out
        z = f"max({zmax}-{step:.6f}*on,1.0)"
    return f"zoompan=z='{z}':{xy}:d=1:s=1920x1080:fps={fps},format=yuv420p"


def build_clip_video(ff: str, out_dir: str, imgs: list[str], holds: list[float],
                     req: VideoReq, mp4: str, progress=None):
    """先给每张图编一个片段（可带镜头运动），再拼起来（可带交叉溶解）。

    为什么不直接对整条图片序列上滤镜：zoompan 是「对一段连续画面缓慢推近」，
    套在整条序列上会从头到尾一直推，而不是每张推一次。
    所以必须先切段。

    返回 (ok, why, 期望时长, 额外信息)
    """
    import subprocess

    fps = max(1, int(req.fps))
    n = len(imgs)
    cdir = os.path.join(out_dir, "clips")
    os.makedirs(cdir, exist_ok=True)
    segs, durs = [], []

    for i, p in enumerate(imgs):
        nf = max(1, round(max(0.05, holds[i]) * fps))
        seg = os.path.join(cdir, f"{i:04d}.mp4")
        # 片头/片尾卡不做推拉：它是静态标题，动了反而晃
        is_card = os.path.basename(p).startswith("card_")
        motion = "none" if is_card else req.motion
        if motion == "none":
            vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"
        else:
            vf = _zoompan_vf(motion, nf, fps, req.size)
        cmd = [ff, "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(fps),
               "-i", p, "-frames:v", str(nf), "-vf", vf,
               "-c:v", "libx264", "-preset", "medium", "-crf", "18",
               "-pix_fmt", "yuv420p", seg]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            return False, f"片段 {i} 编码失败：{(r.stderr or '').strip()[:200]}", 0, {}
        segs.append(seg)
        durs.append(nf / fps)
        if progress:
            progress(f"编码片段 {i+1}/{n}")

    total = sum(durs)
    D = float(req.fade or 0.0)
    if D > 0.01:
        # 转场不能长到把短帧整个吃掉，否则两帧完全重叠、画面会闪
        D = min(D, 0.45 * min(durs))

    if D <= 0.01:
        lst = os.path.join(out_dir, "clips.txt")
        with open(lst, "w", encoding="utf-8") as fh:
            for s in segs:
                fh.write(f"file '{s.replace(chr(92), '/')}'\n")
        cmd = [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
               "-i", lst, "-c", "copy", "-movflags", "+faststart", mp4]
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            # -c copy 偶发因参数不一致失败，退回重编码
            cmd[cmd.index("-c") + 1] = "libx264"
            cmd.insert(cmd.index("libx264") + 1, "-crf")
            cmd.insert(cmd.index("-crf") + 1, "18")
            r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            return False, f"拼接失败：{(r.stderr or '').strip()[:200]}", 0, {}
        return True, "ok", total, {"transition": "hard",
                                   "segments": n, "fade": 0}

    # 交叉溶解：offset = 前缀和 - (k+1)*D，实测总长 = 总时长 - D*(段数-1)
    args = []
    for s in segs:
        args += ["-i", s]
    parts, cur, acc = [], "[0:v]", 0.0
    for k in range(1, n):
        acc += durs[k - 1]
        off = max(0.0, acc - k * D)
        tag = f"[x{k}]"
        parts.append(f"{cur}[{k}:v]xfade=transition=fade:duration={D:.3f}"
                     f":offset={off:.3f}{tag}")
        cur = tag
    fc = ";".join(parts)
    # Windows 命令行有 32767 字符上限，超了就退回硬切并说清楚，别抛一个看不懂的错
    if len(fc) > 24000:
        return _fallback_hard_cut(ff, out_dir, segs, mp4, total,
                                  "帧数太多，交叉溶解的滤镜串超长，已退回硬切")
    cmd = [ff, "-y", "-loglevel", "error"] + args + [
        "-filter_complex", fc, "-map", cur,
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        return _fallback_hard_cut(ff, out_dir, segs, mp4, total,
                                  f"交叉溶解失败（{(r.stderr or '').strip()[:120]}），已退回硬切")
    return True, "ok", total - D * (n - 1), {"transition": "fade",
                                             "segments": n, "fade": round(D, 2)}


def _fallback_hard_cut(ff, out_dir, segs, mp4, total, note):
    import subprocess
    lst = os.path.join(out_dir, "clips.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for s in segs:
            fh.write(f"file '{s.replace(chr(92), '/')}'\n")
    r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-c:v", "libx264", "-preset", "medium",
                        "-crf", "18", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", mp4],
                       capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        return False, f"退回硬切也失败：{(r.stderr or '').strip()[:200]}", 0, {}
    return True, "ok", total, {"transition": "hard", "segments": len(segs),
                               "fade": 0, "note": note}


def plan_holds(holds: list[float], fps: int) -> list[int]:
    """把「每帧停留几秒」换算成「每帧占几个输出帧」。

    必须用累计边界相减，不能各帧单独 round：单独 round 的误差会一路累加，
    146 帧能漂出一两秒。这样算出来总帧数恰好是 round(总时长 × fps)，
    误差永远小于半帧。
    """
    out, prev = [], 0
    acc = 0.0
    for h in holds:
        acc += max(0.05, h)
        cur = round(acc * fps)
        out.append(max(1, cur - prev))
        prev = cur
    return out


def build_ffmpeg_cmd(ff: str, fdir: str, mp4: str, holds: list[float],
                     fps: int, out_dir: str) -> list[str]:
    """每帧**各自**的停留时长都要精确生效。

    走过的弯路：先是用 concat 的 `duration` 指令写每帧秒数 —— 看着最像对的写法，
    实测 3.5 秒的片子出来 4.0 秒（duration 管的是「下一帧从什么时间戳开始」，
    末帧还被多算一次）。换成「按帧数重复引用同一张图」之后又踩第二脚：
    concat 解复用器**不认 `-framerate`**，而且不给输入帧率时整条片子会塌成
    4 帧 / 0.17 秒，只有把 `-r fps` 放在 `-i` **之前**才真的按帧数计时。
    这两脚都是量出来的，不是猜出来的：src/probe_concat_duration.py 留了对照实验。
    """
    counts = plan_holds(holds, fps)
    # 判断依据是「停留时长是否一致」，不是「帧数是否一致」：
    # 0.6 秒在 24fps 下是 14.4 帧，各帧会摊成 14/15/14/15…，
    # 帧数看着不齐，但统一帧率那条路反而更准（3.600 vs 3.583）。
    if holds and len(set(round(h, 4) for h in holds)) <= 1:
        # 每帧一样长时走老路：一条 -framerate 就够，省掉几千行清单
        in_fps = max(0.05, 1.0 / max(0.05, holds[0]))
        return [ff, "-y", "-loglevel", "error", "-framerate", f"{in_fps:.4f}",
                "-i", os.path.join(fdir, "%04d.png"), "-r", str(fps),
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]

    listp = os.path.join(out_dir, "frames.txt")
    with open(listp, "w", encoding="utf-8") as fh:
        for i, n in enumerate(counts):
            p = os.path.join(fdir, f"{i:04d}.png").replace("\\", "/")
            for _ in range(n):
                fh.write(f"file '{p}'\n")
    # 输入侧那个 -r 不能省：它给 concat 解复用器定帧率，少了它按帧数计时不成立。
    return [ff, "-y", "-loglevel", "error", "-r", str(fps),
            "-f", "concat", "-safe", "0", "-i", listp, "-r", str(fps),
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]


@app.post("/api/video")
def api_video(req: VideoReq):
    if not topics.get(req.scene):
        raise HTTPException(404, "没有这个题材")
    planned = plan_frames(req)
    if not planned:
        raise HTTPException(400, "这个区间里没有可渲染的日期")
    jid = jobs.new_job("animate", req.model_dump(exclude={"style_profile"}))
    # jid 只走位置参数。早先写成 run_async(fn, jid, req, jid=jid)，
    # 同一个 jid 传了两次 → TypeError: got multiple values for argument 'jid'，
    # 视频这条路从第一版起就是坏的（当时没测到）。
    jobs.run_async(_animate_job, jid, req)
    # 截断了就明说，别让界面显示的数字和实际出的帧数对不上
    note = ""
    if not req.frames:
        lo, hi = sorted((_days(req.date_from), _days(req.date_to)))
        in_range = sum(1 for d in (topics.get(req.scene).dates() or [])
                       if lo <= _days(d) <= hi)
        if in_range > len(planned):
            note = (f"区间内 {in_range} 个时间点，超过上限 {req.max_frames}，"
                    f"已均匀抽稀为 {len(planned)} 帧")
    return {"job": jid, "n_dates": len(planned), "max_frames": req.max_frames,
            "dates": [f.date for f in planned], "note": note,
            "est_seconds": round(sum(
                max(0.05, f.hold if f.hold else req.hold) for f in planned), 1)}


@app.post("/api/plan")
def api_plan(req: VideoReq):
    """只算分镜、不出图。编辑器用它把「区间」展开成可编辑的帧列表。"""
    if not topics.get(req.scene):
        raise HTTPException(404, "没有这个题材")
    planned = plan_frames(req)
    return {"frames": [{"date": f.date, "hold": f.hold} for f in planned],
            "n_dates": len(planned),
            "est_seconds": round(sum(
                max(0.05, f.hold if f.hold else req.hold) for f in planned), 1)}


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "没有这个任务")
    return j


@app.delete("/api/jobs/{jid}")
def api_job_delete(jid: str):
    """删任务。顺手把磁盘产物一起清掉，否则 output/jobs 会一直涨。"""
    if not jobs.get(jid):
        raise HTTPException(404, "没有这个任务")
    jobs.delete(jid)
    d = os.path.join(ROOT, "output", "jobs", jid)
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "deleted": jid}


@app.get("/api/jobs/{jid}/frames.zip")
def api_frames_zip(jid: str):
    """把这一单的全部帧打包。短视频博主真正要带走的就是这批素材。"""
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "没有这个任务")
    res = j.get("result") or {}
    urls = res.get("frames") or j.get("frames") or []
    if not urls:
        raise HTTPException(400, "这个任务还没有出图")
    fdir = os.path.join(ROOT, "output", "jobs", jid, "frames")
    if not os.path.isdir(fdir):
        raise HTTPException(404, "帧文件不在磁盘上")
    buf = io.BytesIO()
    scene = (j.get("params") or {}).get("scene") or "map"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 帧名带上年份和序号，拿到素材的人不用回来查表
        dates = (j.get("params") or {}).get("frames") or []
        for i, u in enumerate(urls):
            p = os.path.join(fdir, f"{i:04d}.png")
            if not os.path.exists(p):
                continue
            yr = ""
            if i < len(dates) and isinstance(dates[i], dict):
                yr = str(dates[i].get("date") or "")[:10]
            z.write(p, f"{scene}_{yr or f'{i:03d}'}_{i+1:03d}.png")
    buf.seek(0)
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":
                             f'attachment; filename="{scene}_{jid}_frames.zip"'})


@app.get("/api/jobs/{jid}/video.mp4")
def api_video_download(jid: str):
    """带文件名下载，别让浏览器直接内联播放。"""
    p = os.path.join(ROOT, "output", "jobs", jid, "video.mp4")
    if not os.path.exists(p):
        raise HTTPException(404, "还没有视频")
    scene = ((jobs.get(jid) or {}).get("params") or {}).get("scene") or "map"
    return FileResponse(p, media_type="video/mp4",
                        filename=f"{scene}_{jid}.mp4")


# ════════════════════════════════════════════════════════════
# 4) 对话：把自然语言翻译成结构化参数
# ════════════════════════════════════════════════════════════
SYS_PROMPT = """你是历史地图生成器的意图解析器。用户用中文说想要什么，你把它翻译成结构化参数。

可用场景：
{scenes}

可用主题(theme)：dark（深色，短视频向）、light（浅色，宣纸古籍向）
可用质感(style)：none / atlas（古典雕版）/ vintage（复古羊皮纸）/ ink（宣纸水墨）/ modern（现代信息图）
尺寸(size)：16x9 横屏 / 9x16 竖屏 / 4x3 方形（唐图推荐 4x3）

当前参数：
{state}

只输出一个 JSON 对象，不要任何其他文字：
{{"reply": "给用户看的中文回复，一两句，说明你改了什么",
  "state": {{"scene": "...", "date": "YYYY-MM-DD", "theme": "...", "style": "...", "size": "..."}},
  "action": "render" 或 "video" 或 null}}

规则：
- state 必须是**完整参数**（在用户旧参数基础上改），不是只返回改动的字段。
- 日期必须落在该场景支持的范围内，且用 YYYY-MM-DD 格式。
- 场景对象里给了 supported_dates 的，date **必须从里面原样挑一个最近的**，
  绝不允许自造年份。例：「唐宪宗二年」＝元和二年＝807 年，应选 807-01-01。
- 场景对象标了 any_date_in_range 的，date 可以自由给，但要落在 date_range 内。
- 用户只说「换成宋朝/唐朝」这类**只换场景**的要求时，date 用该场景的 default_date，
  不要顺手把日期也改成别的。
- 用户说「出个视频」「做成视频」→ action="video"；否则 action="render"。
- 用户只是问问题、没要求改参数 → action=null，state 原样返回。
- 拿不准的时候不要瞎编日期，用当前值。"""


class ChatReq(BaseModel):
    messages: list[dict]
    state: dict = {}


@app.post("/api/chat")
def api_chat(req: ChatReq, x_api_key: str | None = Header(None, alias="X-Api-Key"),
             x_model: str | None = Header(None, alias="X-Model"),
             x_base_url: str | None = Header(None, alias="X-Base-Url")):
    """对话改参数。需要用户提供 LLM 的 key（前端填，服务端不落盘）。"""
    if not x_api_key:
        raise HTTPException(401, "还没填 API Key —— 右上角设置里填一个（如硅基流动的 sk-…）")

    
    # 场景清单：把**真能渲染的日期**给模型，否则它会自己编。
    # 实测：问「唐宪宗二年」，不给清单时模型答 805-01-01（宪宗二年实为元和二年＝807），
    # 而 805 不在唐图的年份里，点渲染直接报「缺少几何」。
    slim = []
    for s in topics.list_topics():
        ds = s["dates"]
        t = topics.get(s["id"])
        item = {"id": s["id"], "title": s["title"],
                "desc": s.get("subtitle") or "",
                "themes": s["themes"],
                "date_range": [ds[0], ds[-1]] if ds else None,
                "default_date": s.get("default_date"),
                "n_dates": len(ds),
                # 推荐尺寸也来自题材数据，不在这里按题材名写死
                "recommended_size": s.get("default_size") or "16x9"}
        if t and t.kind == "dynasty":
            # 逐年离散（唐 5 个年份、宋 6 个），必须从这里面挑
            item["supported_dates"] = ds
            item["date_note"] = "只能从 supported_dates 里原样选一个，禁止自造年份"
        else:
            item["any_date_in_range"] = True
            item["date_note"] = ("区间内任意 YYYY-MM-DD 都可以。"
                                 "用户说年号/事件时取该事件当月或当日的日期")
        slim.append(item)
    prompt = SYS_PROMPT.format(scenes=json.dumps(slim, ensure_ascii=False),
                               state=json.dumps(req.state, ensure_ascii=False))

    base = (x_base_url or "https://api.siliconflow.cn/v1").rstrip("/")
    model = x_model or "Qwen/Qwen2.5-7B-Instruct"
    body = {
        "model": model,
        "messages": [{"role": "system", "content": prompt}] + req.messages[-12:],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    url = base + "/chat/completions"
    rq = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {x_api_key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(rq, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise HTTPException(e.code, f"模型调用失败 {e.code}: {detail}")
    except Exception as e:
        raise HTTPException(502, f"连不上模型服务: {e}")

    txt = ""
    try:
        txt = data["choices"][0]["message"]["content"]
        parsed = json.loads(strip_fence(txt))
    except Exception as e:
        # txt 必须先在循环外初始化：否则这里会 NameError，
        # 用户看到的是 500，而不是「模型没按 JSON 回」这句能自救的话。
        raise HTTPException(502, f"模型没按 JSON 回（{type(e).__name__}）：{txt[:300]}")
    parsed.setdefault("action", None)
    parsed.setdefault("state", req.state)
    return sanitize_chat(parsed, req.state)


def sanitize_chat(parsed: dict, old_state: dict) -> dict:
    """把模型吐出来的参数夹回真实可渲染的范围。

    模型给错日期是常态（「唐宪宗二年」答成 805，唐图只有 763/780/807/820/875）。
    只靠提示词约束不牢靠，所以这里再兜一层：题材不认识就退回旧值，
    日期不在可渲染集合里就吸附到最近的一个，并且**如实告诉用户改成了什么**。
    """
    st = dict(old_state or {})
    if not isinstance(parsed.get("state"), dict):
        parsed["state"] = st
        return parsed
    st.update(parsed["state"])

    fixes = []
    t = topics.get(str(st.get("scene") or ""))
    if not t:
        if old_state.get("scene"):
            fixes.append(f"没有「{st.get('scene')}」这个题材")
            st["scene"] = old_state["scene"]
            t = topics.get(str(st["scene"]))

    if t:
        ds = t.dates() or []
        want = str(st.get("date") or "")
        if ds:
            if want not in ds:
                try:
                    target = _days(want) if want else None
                except Exception:
                    target = None
                pick = (min(ds, key=lambda d: abs(_days(d) - target)) if target is not None
                        else (t.default_date() or ds[0]))
                fixes.append(f"{want or '（空日期）'} → {pick}")
                st["date"] = pick
        if st.get("theme") not in t.themes:
            st["theme"] = t.themes[0]
        if st.get("size") not in ("16x9", "9x16", "4x3", "1x1"):
            st["size"] = t.default_size or "16x9"

    parsed["state"] = st
    if fixes:
        note = "（已校正：" + "；".join(fixes) + "）"
        parsed["reply"] = (parsed.get("reply") or "").rstrip() + note
        parsed["corrected"] = fixes
    return parsed


def strip_fence(t: str) -> str:
    """有些模型会把 JSON 包在 ```json 围栏里，剥掉再解析。

    这跟 response_format=json_object 不冲突 —— 实测仍会偶发。
    """
    s = (t or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        if s.lstrip().startswith("json"):
            s = s.lstrip()[4:]
    return s.strip()


# ════════════════════════════════════════════════════════════
# 6) 新建题材：让用户自己加，而不是改代码
# ════════════════════════════════════════════════════════════
class TopicDraftReq(BaseModel):
    ask: str
    model: str | None = None


class TopicCreateReq(BaseModel):
    spec: dict
    install: bool = True


def _reload_topics():
    """新建题材后必须清缓存，否则界面要重启服务才看得到新题材。

    topics.load_topics() 是 lru_cache 的，不清就等于新建了个看不见的题材。
    """
    try:
        topics.load_topics.cache_clear()
        topics._ctrl_cache.clear()
    except Exception:
        pass


@app.post("/api/topic/draft")
def api_topic_draft(req: TopicDraftReq, request: Request,
                    x_api_key: str | None = Header(None, alias="X-Api-Key"),
                    x_base_url: str | None = Header(None, alias="X-Base-Url")):
    """把一句话变成题材规格草案。

    只出草案不落盘 —— 用户要先看一眼模型打算怎么画，再决定要不要建。
    """
    import new_topic as NT
    if not req.ask.strip():
        raise HTTPException(400, "说一句你想要什么题材")
    # 允许服务端用自己 .env 里的 key，**仅限本机访问**：
    #   · 你自己电脑上打开 → 本机请求 → 直接用 .env 的 key，功能开箱可用
    #   · 部署到公网 → 访客来自别的 IP → 必须带自己的 key，
    #     否则每个访客都在烧站长的额度
    # 想对公网也开放就显式设 HISTMAP_ALLOW_SERVER_KEY=1。
    host = (request.client.host if request.client else "") or ""
    is_local = host in ("127.0.0.1", "::1", "localhost", "testclient")
    allow_env = is_local or os.environ.get("HISTMAP_ALLOW_SERVER_KEY") == "1"
    try:
        spec = NT.draft(req.ask.strip(), key=x_api_key or "",
                        model=req.model or "Qwen/Qwen2.5-72B-Instruct",
                        verbose=False, allow_env_key=allow_env)
    except SystemExit as e:
        msg = str(e)
        if "Key" in msg:
            msg = ("起草要模型 Key。你可以：①右上角「设置」里填一个（本机使用）；"
                   "②或在本机的 .env 里写 SILICONFLOW_API_KEY=…（服务端会自动用）")
        raise HTTPException(401 if "Key" in str(e) else 400, msg)
    except Exception as e:
        raise HTTPException(500, f"起草失败：{type(e).__name__}: {e}")
    # 顺带把「这份草案能不能真的建出来」预判一下，省得用户点了才发现对不上
    warn = []
    if (spec.get("kind") or "partition") == "partition":
        geo = spec.get("geometry") or {}
        srcs = geo.get("sources") or ([geo] if geo.get("iso") else [])
        if not srcs:
            warn.append("模型没给出数据源，无法取几何")
    if not (spec.get("control") or {}):
        warn.append("模型没给出归属表")
    return {"spec": spec, "warnings": warn}


@app.post("/api/topic/create")
def api_topic_create(req: TopicCreateReq,
                     x_api_key: str | None = Header(None, alias="X-Api-Key")):
    """按规格真建一个题材：取几何 → 写控制表 → 注册 → 逐年构建。

    这是分钟级的活（要下行政区数据、逐年算几何），但比出片快，所以同步做。
    """
    import new_topic as NT
    spec = dict(req.spec or {})
    if not spec.get("id"):
        spec["id"] = NT._slug(spec.get("title") or "topic")
    try:
        rep = NT.build(spec, install=bool(req.install), quiet=True)
    except SystemExit as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"构建失败：{type(e).__name__}: {e}")
    _reload_topics()
    # 建完立刻回报这个题材在系统里长什么样，前端可以直接切过去
    t = topics.get(spec["id"])
    rep["ready"] = bool(t and topics.data_ready(t)[0])
    rep["dates"] = (t.dates() if t else []) or []
    return rep


# ════════════════════════════════════════════════════════════
# 7) 静态资源
# ════════════════════════════════════════════════════════════
app.mount("/media", StaticFiles(directory=os.path.join(ROOT, "output")), name="media")


@app.get("/", response_class=HTMLResponse)
def index():
    p = os.path.join(ROOT, "web", "app.html")
    if not os.path.exists(p):
        return HTMLResponse("<h1>缺少 web/app.html</h1>", status_code=500)
    return HTMLResponse(open(p, encoding="utf-8").read())


if __name__ == "__main__":
    import uvicorn

    # 监听地址：本地默认只绑回环（安全，不会被同网段的人扫到）；
    # 云端平台一定会注入 PORT，见到它就绑 0.0.0.0 —— 否则容器里跑得再好，
    # 外面也连不进来（这是上云最容易漏的一步）。想手动对外开放用 HOST=0.0.0.0。
    port = int(os.environ.get("PORT") or 8810)
    host = os.environ.get("HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    shown = "127.0.0.1" if host in ("127.0.0.1", "localhost") else host
    url = f"http://{shown}:{port}"

    # 启动自检：几何过期就在日志里点名，别让人对着旧图排查半天
    for s in topics.stale_report():
        if "error" in s:
            print(f"[自检] {s['topic']} 检查失败：{s['error']}")
        else:
            print(f"[自检] {s['topic']} 有 {len(s['stale_years'])} 年的几何是旧的"
                  f"（{s['stale_years']}）。重跑：{s['fix']}")
    # 缺数据就点名：云端最常见的问题就是数据集没下载，表现却是「点开图就 500」
    missing = topics.missing_data_report()
    for m in missing:
        print(f"[自检] {m['topic']} 缺数据：{m['need']}")
        print(f"        {m['fix']}")
    print(f"\n  工作台已启动：{url}（监听 {host}:{port}）\n  按 Ctrl+C 停止\n")

    # --open 才开浏览器：脚本里后台跑的时候不需要弹出窗口来打扰
    if "--open" in sys.argv and host in ("127.0.0.1", "localhost"):
        import threading
        import webbrowser

        def _open():
            # 等 uvicorn 真正开始监听再开，否则浏览器会先撞上一个打不开的地址
            for _ in range(40):
                try:
                    urllib.request.urlopen(url + "/api/health", timeout=1).read()
                    break
                except Exception:
                    time.sleep(0.25)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=host, port=port, log_level="info")
