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
# 这几行是「零配置启动」的关键：把 packages/server 也放进来，
# `python packages\server\histmap_server\app.py` 才能 import histmap_server。
# 少了它就必须先 set PYTHONPATH，而那正是新用户第一次跑必踩的一脚。
# packages/agent 是模型编排层（起草图 / 风格图），new_topic 依赖它。
for p in (os.path.join(ROOT, "packages", "core"),
          os.path.join(ROOT, "packages", "agent"),
          os.path.join(ROOT, "src"), PKG_PARENT, HERE):
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
        # ── DeepSeek 官方 ──
        # **注意模型名和硅基流动完全不同**：官方是 `deepseek-flash` /
        # `deepseek-v4-pro`，而 `deepseek-ai/DeepSeek-V4-Flash` 是硅基流动的
        # 命名，官方不认。早先清单里只有硅基流动那套 id，用户选「自定义 +
        # api.deepseek.com」时下拉框里没有一个能用的名字，必然报「模型不存在」。
        # 下面这两个 id 与 base URL 来自官方文档（api-docs.deepseek.com），
        # 不是猜的：BASE URL https://api.deepseek.com，模型 deepseek-flash /
        # deepseek-v4-pro；旧名 deepseek-v4-flash 仍被接受但已退役。
        # 另外 deepseek-flash **支持读图**（Vision ✓），deepseek-v4-pro 不支持。
        {"id": "deepseek-flash", "label": "DeepSeek Flash（快，支持读图）",
         "base": "https://api.deepseek.com", "svc": "DeepSeek 官方", "vision": True},
        {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro（强，不支持读图）",
         "base": "https://api.deepseek.com", "svc": "DeepSeek 官方", "vision": False},
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
        {"id": "Qwen/Qwen3-VL-32B-Instruct", "label": "Qwen3-VL-32B（能读图）",
         "base": "https://api.siliconflow.cn/v1", "svc": "硅基流动", "vision": True},
        # 注：硅基流动的 DeepSeek 命名里没有「4.1」，只有 V4-Flash / V4-Pro / V3.2 / R1。
        # 上面这些 id 是本机从 /v1/models 实际查出来的，不是猜的。
        # ── 火山方舟（豆包）──
        # ⚠️ 这里的模型名**必须从方舟的 /v1/models 实际拉**，不能凭记忆写。
        # 实测教训：原来写的是 `doubao-seed-1-6-250615` 和
        # `doubao-1-5-pro-32k-250115`，两个**都已经下线**（status=Shutdown /
        # 直接不在清单里）。于是拿一个**完全正确的 key** 去调，得到的也是
        # `404 InvalidEndpointOrModel.NotFound` —— 用户只会看到「用豆包不行」，
        # 而真正的原因是名字过期了。火山会定期退役版本，所以这行要跟着更新。
        # 下面是 2026-10 从方舟 /v1/models 拉到的在售型号。
        # 也可以填控制台里的接入点 ID（ep-xxxx）。
        {"id": "doubao-seed-2-1-pro-260915", "label": "豆包 Seed 2.1 Pro（强）",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        {"id": "doubao-seed-2-1-turbo-260628", "label": "豆包 Seed 2.1 Turbo（快）",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        {"id": "doubao-seed-2-0-lite-260428", "label": "豆包 Seed 2.0 Lite（便宜）",
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
        # 注意：图像模型在这个项目里**不用来画地图**。用它的地方是
        # 「大模型生图」（/api/render/model）和「生成纸纹」。
        # 豆包这几条是 2026-10 从方舟 /v1/models 实际拉的 ——
        # 原来写的 `doubao-seedream-3-0-t2i-250415` 已不在清单里，调了就是 404。
        {"id": "doubao-seedream-5-0-pro-260628", "label": "豆包 Seedream 5.0 Pro",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        {"id": "doubao-seedream-5-0-flash-260915", "label": "豆包 Seedream 5.0 Flash（快）",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        {"id": "doubao-seedream-4-0-20260415", "label": "豆包 Seedream 4.0",
         "base": "https://ark.cn-beijing.volces.com/api/v3", "svc": "火山方舟"},
        {"id": "Qwen/Qwen-Image-Edit-2509", "label": "Qwen-Image-Edit-2509",
         "base": "https://api.siliconflow.cn/v1"},
        {"id": "Qwen/Qwen-Image-Edit", "label": "Qwen-Image-Edit",
         "base": "https://api.siliconflow.cn/v1"},
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
    # 字体也要报出来。为什么：云端那次「所有中文标注消失」的事故里，
    # 这个接口返回的是 ok=true、渲染也 200、图片尺寸颜色全正常 ——
    # 所有自动化检查都是绿的，只有人眼看图才发现标题没了。
    # 把字体状态放进健康检查，这种静默降级至少能被脚本查出来。
    try:
        from histmap_core.fonts import font_report
        font = font_report()
    except Exception as e:
        font = {"ok": False, "path": f"{type(e).__name__}: {e}"}
    return {"ok": True, "topics": list(topics.load_topics()), "version": app.version,
            # 几何过期提示：改了控制表却没重跑构建时，这里点名是哪几年
            "stale": topics.stale_report(),
            "font": font}


@app.get("/api/scenes")
def api_scenes():
    """题材列表。题材是**数据**（data/topics/topics.json），不是代码。"""
    return {"scenes": topics.list_topics()}


@app.get("/api/models")
def api_models():
    return MODELS


def _model_size(size: str) -> str:
    """把画布尺寸名换成图像模型的 size 参数（WxH）。

    **不传 size 会显著改变结果**：实测同一张底图、同一个 strength=0.8、
    同一段提示词，传 `1920x1080` 量到 NCC 0.935，不传时掉到 0.30 ——
    模型按自己的默认分辨率（方图）重新构图，参考图就被"重排"了。
    """
    w, h, _mode = topics._sizes(size)
    return f"{w}x{h}"


def vision_model_for(base: str, want: str = "") -> str:
    """给某个接口地址挑一个**能读图**的模型名。挑不出来就返回空串。

    为什么要按 base 挑：读参考图必须用视觉模型，而**每家的模型名不通用**。
    早先这里硬编码 `Qwen/Qwen3-VL-32B-Instruct`，于是用户选了
    DeepSeek 官方（api.deepseek.com）之后，这个 Qwen 的名字被发到官方接口 ——
    必然「模型不存在」。跟当初 401 那次是同一类错：
    选了哪家，就得用哪家的模型名。
    """
    chat = MODELS.get("chat") or []
    b = (base or "").rstrip("/")
    # 用户自己选的模型如果支持读图，就用它（他多半就是想用这个）
    if want:
        m = next((x for x in chat if x.get("id") == want), None)
        if m and m.get("vision"):
            return want
    same = [x for x in chat if (x.get("base") or "").rstrip("/") == b]
    vis = [x for x in same if x.get("vision")]
    if vis:
        return vis[0]["id"]
    return ""


# ════════════════════════════════════════════════════════════
# 1) 风格提取：上传参考图 → 风格 profile
# ════════════════════════════════════════════════════════════
@app.post("/api/style/extract")
async def style_extract(request: Request, file: UploadFile = File(...),
                        vlm: bool = False,
                        x_api_key: str | None = Header(None, alias="X-Api-Key"),
                        x_base_url: str | None = Header(None, alias="X-Base-Url"),
                        x_model: str | None = Header(None, alias="X-Model")):
    """从上传的参考图提取风格。

    默认**纯代码**（调色板/颗粒/暗角），不调任何模型 —— 所以永远可用。
    `vlm=true` 时再加一步：让视觉模型读这张图，产出**中文风格描述 + 给图生图
    模型的英文提示词**。这就是「抽取风格**和提示词**」里的提示词那一半：
    色调可以从像素里量出来，但"这套风格该怎么用文字描述、该怎么写成提示词"
    只能靠模型，纯代码量不出来。

    视觉模型失败不影响接口成功：退回纯代码结果，并在 `vlm_error` 里说明原因。
    """
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

    out = {"profile": prof, "preview": f"/media/api/{os.path.basename(tmp)}",
           "ref_path": tmp, "style_desc": "", "image_prompt": "",
           "tags": [], "is_light": None, "vlm": False}
    if vlm:
        host = (request.client.host if request.client else "") or ""
        is_local = host in ("127.0.0.1", "::1", "localhost", "testclient")
        key = (x_api_key or "").strip()
        if not key and is_local:
            try:
                import new_topic as NT
                key = (os.environ.get("SILICONFLOW_API_KEY")
                       or NT._key_from_env_file() or "")
            except Exception:
                key = os.environ.get("SILICONFLOW_API_KEY") or ""
        if not key:
            out["vlm_error"] = "没有可用的 Key（视觉模型要 Key；纯代码提取不受影响）"
        else:
            base = ((x_base_url or "").strip()
                    or os.environ.get("SILICONFLOW_BASE")
                    or "https://api.siliconflow.cn/v1")
            # 视觉模型必须**跟着接口地址走**：DeepSeek 官方是 deepseek-flash，
            # 硅基流动是 Qwen3-VL。发错了就是「模型不存在」。
            vmodel = vision_model_for(base, (x_model or "").strip())
            if not vmodel:
                out["vlm_error"] = (
                    f"这家服务商（{base}）在清单里没有已知的支持读图的模型，"
                    f"所以没法让它看参考图。纯代码提取不受影响；"
                    f"要读图请在设置里选一个标注了「能读图」的模型。")
            else:
                try:
                    from histmap_agent import LLMConfig
                    from histmap_agent.style_graph import run_style_graph
                    cfg = LLMConfig(key=key, base=base, model=vmodel,
                                    temperature=0.3)
                    # 只跑"读图出描述与提示词"这一段：不生成、不合成。
                    # 生成那一步是单独一个接口（/api/style/texture），
                    # 因为它要几十秒且要底图。
                    r, _ = run_style_graph(tmp, base_path="", vlm_cfg=cfg,
                                           verbose=False)
                    # vlm=True 的含义是「**读出来了**」，不是「试着读了」。
                    # 图内部会把视觉模型不可用接住并退回纯代码，这时 desc/prompt
                    # 是空的；若还报 vlm=True，调用方（和烟测）就会把
                    # "没读出来"当成功能正常。
                    got_desc = bool((r.get("style_desc") or "").strip())
                    out.update({
                        "vlm": got_desc,
                        "vlm_model": vmodel,
                        "style_desc": r.get("style_desc") or "",
                        "image_prompt": r.get("image_prompt") or "",
                        "tags": r.get("tags") or [],
                        "is_light": r.get("is_light"),
                        "vlm_palette": r.get("vlm_palette") or [],
                        "log": r.get("log") or [],
                        "negative_prompt": r.get("negative_prompt") or "",
                    })
                    if not got_desc:
                        out["vlm_error"] = next(
                            (l for l in (r.get("log") or [])
                             if "不可用" in l or "失败" in l),
                            "视觉模型没有给出风格描述")
                except Exception as e:
                    out["vlm_error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return out


class ModelImageReq(BaseModel):
    """**直接让大模型生成图片** —— 不做几何拼装。

    这个接口存在的理由：用户要的就是「大模型生图」。项目此前一直在优化
    「代码画图」那条路，把模型生图当成要避免的事；但对「图好不好看」这件事，
    决定权应该在用户手上，而不是我替他judge。所以把它做成一条正式的路：

      mode = "restyle"   给模型一张代码画的地图，让它整张重画成有质感的图
                         （地理是对的，但模型可能改动边界和标注）
      mode = "txt2img"   什么都不给，纯文生图。地理**不可能准**，
                         适合当海报/概念图，不适合当史实图

    两条路都会**如实回报**保真度（NCC / 色块保真），但**不做拦截** ——
    好不好用由用户看图和数字自己判断。
    """
    scene: str
    date: str = ""
    theme: str = "light"
    size: str = "16x9"
    mode: str = "restyle"
    prompt: str = ""              # 留空则按题材自动写一段
    negative_prompt: str = ""
    img_model: str = ""
    img_base: str = ""
    steps: int = 30
    seed: int | None = None
    # 图生图"参考强度"。**这个参数决定成败**：不给时豆包会从零重画，
    # 画出**现代**海岸线和省界（实测 NCC 0.24）；0.8 时结构保住了（NCC 0.935），
    # 质感仍然是模型给的。默认 0.8 是量出来的最佳点。
    strength: float = 0.8
    # 把代码渲染的文字层盖回去。**默认关**：strength=0.8 时模型会照着参考图
    # 把中文标注也基本复制对（实测地名可读）。开它则是"先抹字→模型→再盖字"，
    # 用于低 strength 或模型字画错时的兜底。
    overlay_text: bool = False


@app.post("/api/render/model")
def api_render_model(req: ModelImageReq, request: Request,
                     x_api_key: str | None = Header(None, alias="X-Api-Key"),
                     x_base_url: str | None = Header(None, alias="X-Base-Url")):
    """大模型生图（不做几何拼装）。"""
    import stylize as Z
    t = topics.get(req.scene)
    if not t:
        raise HTTPException(404, "没有这个题材")

    host = (request.client.host if request.client else "") or ""
    is_local = host in ("127.0.0.1", "::1", "localhost", "testclient")
    key = (x_api_key or "").strip()
    label = "你填的 Key"
    srv_base = ""
    if not key and is_local:
        try:
            from histmap_agent.llm import server_default
            d = server_default()
            key, label, srv_base = d.key, d.label, d.base
        except SystemExit as e:
            raise HTTPException(401, str(e))
    if not key:
        raise HTTPException(401, "生图要一个图像模型的 Key（设置里填，或本机 .env）")

    base_url = (req.img_base or x_base_url or "").strip() or srv_base
    if not base_url:
        raise HTTPException(400, "要指定图像模型的接口地址（设置里的「API 服务」）")

    # **图像模型不能拿对话模型来凑**。踩过的坑：这里原来回落到
    # `server_default().model`，而那是**对话**模型（Qwen2.5-72B），
    # 拿它去打 /images/generations 得到的是 `403 Illegal operation`。
    # 图像模型有自己的一份清单（MODELS["image_edit"]），按地址挑。
    model = (req.img_model or "").strip()
    if not model:
        b = base_url.rstrip("/")
        cand = [m for m in (MODELS.get("image_edit") or [])
                if (m.get("base") or "").rstrip("/") == b]
        if not cand:
            raise HTTPException(400, (
                f"没有为 {b} 配图像模型名。请在请求里给 img_model，"
                f"或在 .env 里配 HISTMAP_IMAGE_MODEL。"
                f"已知的图像模型：{[(m['id'], m['base']) for m in (MODELS.get('image_edit') or [])]}"))
        model = cand[0]["id"]

    stamp = int(time.time() * 1000)
    base_path = ""
    text_layer_path = ""
    ref_for_model = ""
    if req.mode == "restyle":
        date = req.date or (t.dates() or [""])[0]
        if not date:
            raise HTTPException(400, "这个题材没有可渲染的日期")
        try:
            img = topics.render(req.scene, date, req.theme, req.size)
        except Exception as e:
            raise HTTPException(500, f"底图渲染失败：{type(e).__name__}: {e}")
        base_path = os.path.join(MEDIA, f"modelbase_{req.scene}_{stamp}.png")
        img.save(base_path)
        ref_for_model = base_path

        if req.overlay_text:
            # **先把字抹掉再交给模型。**
            # 踩过的坑：直接把带字的底图给模型，它会照着画一遍 —— 但画出来是
            # 形近错字；然后我们又把自己准的字盖上去，两套字位置不重合，
            # 结果是「唐·唐豢藩鎮割據言807年7年」这种**重影**，比不盖还差。
            # 所以顺序必须是：抹字 → 交给模型 → 盖字。
            try:
                import numpy as np
                from PIL import Image as _I
                from PIL import ImageFilter
                layer = topics.render(req.scene, date, req.theme, req.size,
                                      text_only=True)
                text_layer_path = os.path.join(MEDIA,
                                               f"textlayer_{req.scene}_{stamp}.png")
                layer.save(text_layer_path)
                # 文字遮罩 = 文字层的 alpha。膨胀一点把描边/光晕也盖住，
                # 否则抹完会剩一圈残影；再轻微模糊让边界过渡自然。
                mimg = layer.split()[-1].filter(ImageFilter.MaxFilter(9))
                mimg = mimg.filter(ImageFilter.GaussianBlur(3))
                m = np.asarray(mimg).astype(np.float32) / 255.0
                a = np.asarray(img).astype(np.float32)
                # 用大半径模糊值填：文字底下多半是纯色块或纸底，
                # 模糊出来就是那块的颜色，填进去看不出补过
                blurred = np.asarray(
                    img.filter(ImageFilter.GaussianBlur(18))).astype(np.float32)
                filled = a * (1 - m[..., None]) + blurred * m[..., None]
                clean = os.path.join(MEDIA, f"modelclean_{req.scene}_{stamp}.png")
                _I.fromarray(np.clip(filled, 0, 255).astype("uint8")).save(clean)
                ref_for_model = clean
            except Exception as e:
                # 抹字失败就退回原图 —— 至少还能出图，只是会有重影风险
                text_layer_path = ""
                ref_for_model = base_path
                print(f"[生图] 抹字失败，用原图：{type(e).__name__}: {e}")

    # 默认提示词。措辞是**实测挑出来的**：同样 strength=0.8、同一张底图，
    # 这句（"把参考图当权威，保住形状/海岸线/边界/位置与整体构图"）
    # 量到 NCC 0.935；换成另一段意思相近但没点明"参考图是权威"的写法，
    # NCC 掉到 0.30。所以这段不要随手改，要改先量。
    prompt = (req.prompt or "").strip() or (
        "Use the provided map image as the authoritative reference. Preserve "
        "its shapes, coastlines, region boundaries, region positions and the "
        "overall composition exactly. Restyle it as an antique hand-drawn "
        "atlas plate: aged parchment paper, visible fibre grain, soft foxing "
        "stains, muted earthy low-saturation colours, fine engraved hatching, "
        "warm toned, subtle vignette. Keep every colour region in its place. "
        "Do not invent new landmasses or change any border.")
    # 注意：这里**故意不写**「不要画文字」。试过，NCC 从 0.935 掉到 0.845 ——
    # strength=0.8 时模型本来就会把参考图上的中文标注**照抄下来**（实测可读），
    # 而"别画文字"这句会让它去涂抹/改写字的位置，反而更偏离底图。
    # 需要绝对精确的文字时，用 overlay_text=True 那条路（先抹字、模型出图、
    # 再把代码的文字层盖回去），而不是靠提示词。
    out_path = os.path.join(MEDIA, f"modelgen_{req.scene}_{stamp}.png")
    prov = Z.SiliconFlow(key, model=model, base=base_url)
    try:
        if req.mode == "txt2img":
            url, secs, seed = prov.text2img(
                prompt, size="1024x1024", steps=req.steps, seed=req.seed,
                negative_prompt=req.negative_prompt or
                "text, letters, numbers, characters, watermark, signature, "
                "logo, blurry, low quality")
        else:
            url, secs, seed = prov.run(ref_for_model, prompt, steps=req.steps,
                                       seed=req.seed, strength=req.strength,
                                       size=_model_size(req.size))
        Z._download(url, out_path)
    except Exception as e:
        msg = f"{type(e).__name__}: {str(e)[:400]}"
        # 「远程主机强迫关闭了一个现有的连接」在生图这条路上几乎总是
        # **Key 不对**：图是 base64 塞进请求体上传的，服务商验鉴权失败后
        # 会直接掐断连接，而不是好好地回一个 401。原样抛出会让人以为是网络问题，
        # 于是去查代理、查防火墙，白花时间。
        if "10054" in msg or "ConnectionReset" in msg or "forcibly closed" in msg \
                or "远程主机强迫关闭" in msg:
            msg += (f"\n（这多半是 Key 不对：图像是随请求体上传的，"
                    f"服务商验不过就直接断连，而不是回 401。"
                    f"请先用「设置 → 测试连接」确认真实可用的 Key；"
                    f"当前用的地址是 {base_url}）")
        raise HTTPException(502, f"模型生图失败：{msg}")

    # ── 把代码渲染的文字层盖回去 ──
    # 实测：豆包 Seedream 把地图重画得很漂亮（纸纹、线画、网点都很好），
    # 但中文标注被它"画"成了形近错字：「盧龍」→「盧西」、「橫海」→「樓壺」、
    # 「所屬政權」→「所闰改划」。这不是提示词能修的 —— 它在按纹理画字。
    # 所以：质感用模型的，文字用代码的。文字层是带透明通道的，只盖字。
    overlay_note = ""
    model_path = out_path
    if req.overlay_text and req.mode == "restyle" and base_path:
        try:
            from PIL import Image as _I
            # 优先用前面已经渲染好的文字层（就是抹字时用的那一层），
            # 保证「抹掉的字」和「盖回的字」是同一套、位置完全一致。
            layer = _I.open(text_layer_path) if text_layer_path and \
                os.path.exists(text_layer_path) else None
            if layer is None:
                date = req.date or (t.dates() or [""])[0]
                layer = topics.render(req.scene, date, req.theme, req.size,
                                      text_only=True)
            bg = _I.open(out_path).convert("RGBA")
            if layer.size != bg.size:
                layer = layer.resize(bg.size, _I.LANCZOS)
            out_path = os.path.join(MEDIA, f"modelgen_{req.scene}_{stamp}_txt.png")
            _I.alpha_composite(bg, layer.convert("RGBA")).convert("RGB").save(out_path)
            overlay_note = ("已抹掉模型画错的字、再把代码渲染的文字层盖回去"
                            "（纸的质感是模型的，文字是代码的）")
        except NotImplementedError as e:
            overlay_note = f"没盖文字层：{e}"
        except Exception as e:
            overlay_note = f"盖文字层失败（用的是模型原图）：{type(e).__name__}: {e}"

    out = {"image": f"/media/api/{os.path.basename(out_path)}",
           # api_base 是**接口地址**，base 是**底图路径** —— 这两个原来都叫
           # "base"，后一次赋值把接口地址覆盖掉了，日志里就看到
           # 「模型 doubao-... @ /media/api/modelbase_....png」这种自相矛盾的行。
           "model": model, "api_base": base_url, "mode": req.mode,
           "prompt": prompt, "seconds": round(secs, 1), "seed": seed,
           "overlay_text": bool(req.overlay_text and req.mode == "restyle"),
           "key_source": label}
    if overlay_note:
        out["overlay_note"] = overlay_note
    if base_path:
        out["base"] = f"/media/api/{os.path.basename(base_path)}"
        if base_path != model_path:
            out["model_raw"] = f"/media/api/{os.path.basename(model_path)}"
        if ref_for_model and ref_for_model != base_path:
            out["model_input"] = f"/media/api/{os.path.basename(ref_for_model)}"
        # 如实回报保真度，但**不拦截** —— 用不用由用户看图决定
        try:
            from PIL import Image
            s = Image.open(out_path)
            b = Image.open(base_path)
            if s.size != b.size:
                s.convert("RGB").resize(b.size, Image.LANCZOS).save(out_path)
                out["resized_to"] = list(b.size)
            out["fidelity"] = Z.check_fidelity(base_path, out_path)
            out["fidelity"]["note"] = (
                "这是模型图和代码底图的差异。NCC 低说明模型改了内容"
                "（很可能重画了边界或把中文标注画成乱码笔画），"
                "但**是否可接受由你判断** —— 这个接口只回报，不拦截。")
        except Exception as e:
            out["fidelity_error"] = f"{type(e).__name__}: {e}"
    return out


class StyleTextureReq(BaseModel):
    """用模型生成纸纹，再把代码画好的地图贴上去。"""
    scene: str
    date: str = ""
    theme: str = "light"
    size: str = "16x9"
    ref_path: str = ""            # 参考图（/api/style/extract 返回的那个）
    style_desc: str = ""
    image_prompt: str = ""        # 前面抽出来的提示词；给了就用它，不再问一次视觉模型
    # 图像模型。**base 必须能由调用方给**：豆包 Seedream 在火山方舟、
    # Qwen-Image 在硅基流动，两家的模型名互不相认。早先 base 写死在
    # stylize.SiliconFlow 里、model 也写死成 Qwen/Qwen-Image，
    # 于是清单里那条「豆包 Seedream」根本不可能被用上 —— 典型的「配了不生效」。
    # 图像模型。留空就按「API 服务」自动挑这家的图像模型（豆包 Seedream /
    # Qwen-Image）。**默认值不能写死一家** —— 原来默认 `Qwen/Qwen-Image`，
    # 于是填了火山方舟也会把 Qwen 的名字发过去，得到 404/401。
    img_model: str = ""
    img_base: str = ""
    mode: str = "texture"         # texture（默认，安全）| edit（图生图，实测会毁标注）


@app.post("/api/style/texture")
def api_style_texture(req: StyleTextureReq, request: Request,
                      x_api_key: str | None = Header(None, alias="X-Api-Key"),
                      x_base_url: str | None = Header(None, alias="X-Base-Url"),
                      x_model: str | None = Header(None, alias="X-Model")):
    """「通过提示词 + 数据 + 风格让大模型生成」这一步。

    做法是**代码先画好精确的地图，模型只生成纸纹，再把地图贴到纸纹上**。
    为什么不让模型直接画地图：实测让它编辑一张地图，纸纹做得很好，
    但中文标注全被改成乱码笔画（`所属政权`→`所鹰政权`），
    尺寸归一之后结构相似度也只有 0.84。所以模型只做它做得好的那一层。

    返回 `fidelity.content_changed_ratio` —— **地图内容被改动的像素比例**，
    必须是 0。这是这一步唯一值得看的数字。
    """
    import new_topic as NT
    t = topics.get(req.scene)
    if not t:
        raise HTTPException(404, "没有这个题材")

    host = (request.client.host if request.client else "") or ""
    is_local = host in ("127.0.0.1", "::1", "localhost", "testclient")
    key = (x_api_key or "").strip()
    srv_base = ""
    if not key and is_local:
        # **不要只认 SILICONFLOW_API_KEY。** 这里原来就是只认它，于是
        # 「.env 里填了火山方舟的 key」时，这一步会拿**硅基的 key 去打火山**，
        # 得到 `401 AuthenticationError: The API key format is incorrect` ——
        # 看起来像 key 错了，其实是**拿错了那一家的 key**。
        # 统一走 server_default()：填了哪家就用哪家。
        try:
            from histmap_agent.llm import server_default
            d = server_default()
            key, srv_base = d.key, d.base
        except SystemExit as e:
            raise HTTPException(401, str(e))
        except Exception:
            key = key or NT._key_from_env_file() or ""
    if not key:
        raise HTTPException(401, "这一步要图像模型的 Key")

    date = req.date or (t.dates() or [""])[0]
    if not date:
        raise HTTPException(400, "这个题材没有可渲染的日期")
    try:
        img = topics.render(req.scene, date, req.theme, req.size)
    except Exception as e:
        raise HTTPException(500, f"底图渲染失败：{type(e).__name__}: {e}")
    base = os.path.join(MEDIA, f"base_{req.scene}_{int(time.time()*1000)}.png")
    img.save(base)

    from histmap_agent import LLMConfig
    from histmap_agent.style_graph import run_style_graph
    # 视觉模型也要按接口地址挑 —— 否则调用方选了火山方舟，我们却拿
    # 一个 Qwen 的视觉模型名去打火山（跟图像模型那次是同一个错）。
    # 调用方已经把提示词给全了的话，这一步根本用不到，prompt_override
    # 会让图直接跳过 vlm_describe，所以挑不出来也不影响。
    vlm_base = ((x_base_url or "").strip() or srv_base
                or os.environ.get("SILICONFLOW_BASE")
                or "https://api.siliconflow.cn/v1")
    cfg = LLMConfig(key=key, base=vlm_base,
                    model=vision_model_for(vlm_base, (x_model or "").strip())
                    or "Qwen/Qwen3-VL-32B-Instruct",
                    temperature=0.3)
    out_path = os.path.join(MEDIA, f"styled_{req.scene}_{int(time.time()*1000)}.png")
    ref = req.ref_path or base           # 没给参考图就拿底图当参考（只取质感）
    # 这一步要的是**纯纸**，不是地图。所以提示词里必须只提纸，
    # 一个"map / atlas / chart"之类的词都不能有 —— 提了就是在邀请它画地图。
    # 原来界面留空时会走视觉模型去"看图写提示词"，而它看的那张图是**地图**，
    # 写出来的提示词很容易带上"historical map"这种词，把模型引到画地图上去。
    # 所以这里给一个只讲纸的默认值；调用方明确给了 image_prompt 才用它的。
    paper_prompt = (req.image_prompt or "").strip() or (
        "A completely blank sheet of aged parchment paper, filling the whole "
        "frame edge to edge. An empty writing surface with nothing on it. "
        "Warm ivory to light tan tone, pronounced fibre grain, small dark "
        "specks, faint mottling, a few soft water stains, slightly darker and "
        "aged along the extreme outer edges. Even flat lighting across the "
        "whole sheet.")
    # 图像模型的地址：调用方给了就用；没给就从清单里按模型名反查
    # 图像模型的地址：调用方给了就用；没给就按模型名从清单反查；
    # 再不行就用服务端配置那一家的地址（**不再无脑兜底到硅基流动** ——
    # 那正是「填了火山方舟却拿硅基的 key 打火山」的来源）。
    img_base = (req.img_base or "").strip()
    if not img_base and req.img_model:
        hit = next((m for m in (MODELS.get("image_edit") or [])
                    if m.get("id") == req.img_model), None)
        img_base = (hit or {}).get("base") or ""
    img_base = img_base or ((x_base_url or "").strip() or srv_base)
    # 模型名也没给：按地址从清单里挑一个这家的图像模型
    img_model = (req.img_model or "").strip()
    if not img_model and img_base:
        cand = [m for m in (MODELS.get("image_edit") or [])
                if (m.get("base") or "").rstrip("/") == img_base.rstrip("/")]
        img_model = cand[0]["id"] if cand else ""
    if not img_base or not img_model:
        raise HTTPException(400, (
            f"不知道用哪家的图像模型。当前接口地址={img_base or '（未指定）'}，"
            f"模型={img_model or '（未指定）'}。请在设置里选「API 服务」，"
            f"或在 .env 里配 HISTMAP_BASE / HISTMAP_IMAGE_MODEL。"))
    try:
        r, _ = run_style_graph(ref, base_path=base, vlm_cfg=cfg, img_key=key,
                               img_model=img_model, img_base=img_base,
                               out_path=out_path,
                               mode=req.mode, verbose=False,
                               # 调用方给过提示词就用它，不再问一次视觉模型：
                               # 更省更快，也避免用错家的视觉模型名。
                               prompt_override=paper_prompt)
    except Exception as e:
        raise HTTPException(500, f"生成失败：{type(e).__name__}: {str(e)[:300]}")
    styled = r.get("styled_path") or ""
    return {
        "base": f"/media/api/{os.path.basename(base)}",
        "image": f"/media/api/{os.path.basename(styled)}" if styled else "",
        "mode": r.get("mode"), "verdict": r.get("verdict"),
        "img_model": img_model, "img_base": img_base,
        "style_desc": r.get("style_desc") or req.style_desc,
        "image_prompt": r.get("image_prompt") or req.image_prompt,
        "blank": r.get("blank") or {}, "fidelity": r.get("fidelity") or {},
        "log": r.get("log") or [],
    }


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
    # 「这一帧代表哪一段」，如 "1040–1080"。连续几年疆域没变时合并成一帧，
    # 标题就得写区间 —— 否则用户看到标题「1080 年」而画面是 1040 的样子。
    period: str | None = None


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


def merge_identical_frames(scene_id: str, frames: list["FrameReq"]):
    """把「控制状态相同」的相邻帧并成一帧，并给它一个年份区间。

    为什么要做：题材声明的年份未必年年在变。实测 6 个题材里 5 个有这个问题 ——
    宋的 1040 与 1080 控制表逐字相同，印巴分治的 1947/1950/1960/1970 四年同态。
    成片里就是同一张地图连播两遍，观众会以为卡住了。

    合并（而不是删年份）是对的：控制表里那一年确实就是这个状态，
    删掉等于篡改史实；合并成「1040–1080 年」才是历史地图集的通常做法。

    返回 (合并后的帧, 说明文字列表, 是否只有一个状态)。
    """
    if len(frames) < 2:
        return frames, [], False
    t = topics.get(scene_id)
    if not t or t.kind != "dynasty":
        return frames, [], False

    out: list[FrameReq] = []
    notes: list[str] = []
    for f in frames:
        try:
            y = int(str(f.date).split("-")[0])
        except Exception:
            out.append(f)
            continue
        sig = topics.control_state(t, y)
        prev = out[-1] if out else None
        prev_y = None
        if prev is not None:
            try:
                prev_y = int(str(prev.date).split("-")[0])
            except Exception:
                prev_y = None
        if (prev is not None and prev_y is not None and sig is not None
                and topics.control_state(t, prev_y) == sig):
            # 同态：不新增一帧，把上一帧代表的区间延长到这一年。
            # period 只在「跨度 > 1 年」时才有意义，且不覆盖用户自己写的 title。
            a, b = str(prev_y), str(y)
            prev_period = getattr(prev, "period", None)
            start = prev_period.split("\u2013")[0] if prev_period else a
            out[-1] = prev.model_copy(update={"period": f"{start}\u2013{b}"})
            continue
        out.append(f)

    merged = [f for f in out if getattr(f, "period", None)]
    if merged:
        ranges = [f.period for f in merged]
        notes.append(
            f"{len(frames)} 个年份里有 {len(frames) - len(out)} 个与上一年疆域完全相同，"
            f"已合并成一帧（{('、'.join(ranges))} 年）—— 硬放两帧会让人以为卡住了")
    return out, notes, len(out) == 1 and len(frames) > 1


def raw_dates(req: VideoReq) -> list[str]:
    """这次请求**原始**选中了哪些日期（合并同态帧之前）。

    「有几个年份没变化」「是不是只有一种状态」这类判断必须看原始日期；
    拿合并后的结果去算，年数已经被改小，结论就不对了。
    """
    if req.frames:
        return [f.date for f in req.frames[:req.max_frames]]
    return pick_dates(req.scene, req.date_from, req.date_to, req.max_frames)


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
    sel = pick_dates(req.scene, req.date_from, req.date_to, req.max_frames)
    merged, _, _ = merge_identical_frames(req.scene, [FrameReq(date=d) for d in sel])
    return merged


def pick_dates(scene_id: str, date_from: str, date_to: str, max_frames: int):
    """在 [from, to] 之间挑出题材**真正支持**的日期点。

    不是按天数均分 —— 每个题材能渲染的日期是离散的（唐只有 5 个年份，
    二战有 146 个），按天数切会切出一堆渲染不了的日期。

    起止为空或写坏时，退化成**该题材的全区间**，而不是报 500：
    界面上还没选日期就点一下是正常操作（编辑器用它展开分镜）。
    """
    ds = topics.get(scene_id).dates()
    if not ds:
        return []
    # 日期一律换算成序数再比。直接拿字符串比大小是错的：
    # '980-01-01' > '1040-01-01'（逐字符 '9' > '1'），
    # 会把 980 年这种三位数年份整个排到后面去，区间筛选全乱。
    try:
        lo = _days(date_from)
    except (ValueError, TypeError):
        lo = _days(ds[0])
    try:
        hi = _days(date_to)
    except (ValueError, TypeError):
        hi = _days(ds[-1])
    lo, hi = min(lo, hi), max(lo, hi)
    sel = [d for d in ds if lo <= _days(d) <= hi]
    if not sel:
        # 区间内一个都没有 → 退化成「离区间端点最近的那个」
        sel = [min(ds, key=lambda d: min(abs(_days(d) - lo), abs(_days(d) - hi)))]
    if len(sel) > max_frames:                 # 均匀抽稀，保留首尾
        step = (len(sel) - 1) / (max_frames - 1)
        sel = [sel[round(i * step)] for i in range(max_frames)]
    return sel


def _days(d: str) -> int:
    """日期 → 序数。容忍 '807' / '807-1-1' / '0807-01-01' 各种写法。

    空值或解析不出年份时抛 ValueError（**不是** IndexError）——
    调用方需要能用一个 except 分清「日期格式不对」和「程序有 bug」。
    早先 parts[0] 在空字符串上直接 IndexError，于是 /api/plan 传空日期
    就是 500，而界面上「还没选日期就点一下」是完全正常的操作。
    """
    from datetime import date as _d
    parts = [p for p in str(d)[:10].split("-") if p != ""]
    if not parts:
        raise ValueError(f"空日期：{d!r}")
    try:
        y = int(parts[0])
    except (TypeError, ValueError):
        raise ValueError(f"日期里没有年份：{d!r}")
    m = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 1
    dd = int(parts[2]) if len(parts) > 2 and str(parts[2]).isdigit() else 1
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
    # 合并掉同态帧之后，如果只剩一帧，那这个题材根本没有「演化」可看 ——
    # 早点说清楚，别让用户等完整套出图才发现片子是张静止图。
    # 判断必须基于**原始**日期（plan_frames 已经合并过一次了）。
    _, merge_notes, single = merge_identical_frames(
        req.scene, [FrameReq(date=d) for d in raw_dates(req)])
    if single:
        merge_notes.append("注意：这个题材声明的那几年疆域完全相同，"
                           "只有一种状态，出片会是一张静止的图 —— "
                           "要动画就得先在控制表里补上真实的疆域变化。")
    n = len(frames)
    jobs.update(jid, status="running", total=n, frames=[],
                message=f"准备渲染 {n} 张",
                notes=merge_notes)

    # ── 1) 逐张出图 ──
    vprof = _norm_profile(req.style_profile)
    urls, holds, fpath = [], [], []
    for i, f in enumerate(frames):
        holds.append(max(0.05, f.hold if f.hold else req.hold))
        img = topics.render(req.scene, f.date, req.theme, req.size,
                            title=f.title, subtitle=f.subtitle, footer=f.footer,
                            style_profile=vprof, strength=req.strength,
                            period=getattr(f, "period", None))
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
        r0 = {"video": None, "frames": urls, "n_frames": len(urls),
              "holds": [round(h, 2) for h in holds],
              "seconds": round(sum(holds), 1), "size": req.size,
              "verify": "只出图模式", "zip": f"/media/jobs/{jid}/frames.zip"}
        if merge_notes:
            r0["notes"] = merge_notes
        jobs.update(jid, status="done", progress=1.0, message="全部图片已出",
                    result=r0)
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
    if merge_notes:
        res["notes"] = merge_notes
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
    raw = raw_dates(req)
    if not req.frames:
        # 起止空/坏时 pick_dates 已经退化成全区间，这里也得跟着算，
        # 不能直接 _days('') 抛出去（那是 500，而这是正常操作）。
        ds_all = topics.get(req.scene).dates() or []
        try:
            lo, hi = sorted((_days(req.date_from), _days(req.date_to)))
        except (ValueError, TypeError):
            lo, hi = (_days(ds_all[0]), _days(ds_all[-1])) if ds_all else (0, 0)
        in_range = sum(1 for d in ds_all if lo <= _days(d) <= hi)
        if in_range > len(raw):
            note = (f"区间内 {in_range} 个时间点，超过上限 {req.max_frames}，"
                    f"已均匀抽稀为 {len(raw)} 帧")
    # 同态合并也从**原始日期**算，不能拿已合并的结果再算一遍 ——
    # 那样 len(frames) 已经不是原始年数，「只剩一个状态」这个判断会失灵。
    merged, merge_notes, single = merge_identical_frames(
        req.scene, [FrameReq(date=d) for d in raw])
    if merge_notes:
        note = (note + "；" if note else "") + "；".join(merge_notes)
    return {"job": jid, "n_dates": len(raw), "n_frames": len(planned),
            "max_frames": req.max_frames,
            "dates": [f.date for f in planned], "note": note,
            "merged": len(raw) - len(merged),
            "single_state": bool(single),
            "est_seconds": round(sum(
                max(0.05, f.hold if f.hold else req.hold) for f in planned), 1)}


@app.post("/api/plan")
def api_plan(req: VideoReq):
    """只算分镜、不出图。编辑器用它把「区间」展开成可编辑的帧列表。"""
    if not topics.get(req.scene):
        raise HTTPException(404, "没有这个题材")
    planned = plan_frames(req)
    raw = raw_dates(req)
    merged, merge_notes, single = merge_identical_frames(
        req.scene, [FrameReq(date=d) for d in raw])
    return {"frames": [{"date": f.date, "hold": f.hold,
                        "period": getattr(f, "period", None)} for f in planned],
            "n_dates": len(raw), "n_frames": len(planned),
            "merged": len(raw) - len(merged),
            "notes": merge_notes, "single_state": bool(single),
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
    """新建/改动题材后清各种缓存。

    现在这几个缓存都自己认文件 mtime（见 topics._cached_json / load_topics），
    所以正常路径不需要手动清；这里保留一次强制清空，是为了「刚写完文件、
    文件系统时间戳精度不够」的边界情况不会让新题材看不见。
    """
    try:
        topics._topics_cache.clear()
        topics._ctrl_cache.clear()
        topics._dyn_cache.clear()
    except Exception:
        pass


@app.post("/api/key/test")
def api_key_test(request: Request,
                 x_api_key: str | None = Header(None, alias="X-Api-Key"),
                 x_base_url: str | None = Header(None, alias="X-Base-Url"),
                 x_model: str | None = Header(None, alias="X-Model")):
    """验一次「设置」里的 Key / 接口地址 / 模型到底能不能用。

    为什么要专门做这个：key 填错时，用户是在**点了「让模型起草」之后**才看到
    一句 "HTTP Error 401: Unauthorized"，既不知道是自己填的 key 被拒、
    还是模型名不对、还是余额没了 —— 而且那一等就是一分多钟。
    把它拆成两步（先只验鉴权，再验模型名），点一下就能定位。
    """
    import urllib.error
    import urllib.request

    host = (request.client.host if request.client else "") or ""
    is_local = host in ("127.0.0.1", "::1", "localhost", "testclient")
    key = (x_api_key or "").strip()
    src = "你填的 Key"
    if not key:
        if not is_local:
            return {"ok": False, "step": "key",
                    "detail": "公网访问必须自带 Key（服务端不会用站长的额度替你调模型）"}
        try:
            import new_topic as NT
            key = (os.environ.get("SILICONFLOW_API_KEY")
                   or NT._key_from_env_file() or "")
        except Exception:
            key = os.environ.get("SILICONFLOW_API_KEY") or ""
        src = "服务端 .env 里的 Key"
    if not key:
        return {"ok": False, "step": "key",
                "detail": "没有可用的 Key：既没填，服务端 .env 里也没有"}

    base = (x_base_url or "").strip().rstrip("/") or "https://api.siliconflow.cn/v1"
    model = (x_model or "").strip() or "Qwen/Qwen2.5-72B-Instruct"
    out = {"ok": False, "key_source": src, "base": base, "model": model,
           "key_tail": f"{key[:6]}…{key[-4:]}"}

    def _call(url, payload=None, timeout=30):
        r = urllib.request.Request(
            url, data=json.dumps(payload).encode() if payload else None,
            method="POST" if payload else "GET",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:300]
        except Exception as e:
            return -1, f"{type(e).__name__}: {e}"

    code, body = _call(f"{base}/models")
    if code == -1:
        out.update(step="network", detail=f"接口地址连不上：{body}")
        return out
    if code in (401, 403):
        out.update(step="key", detail=f"{src}被拒绝（HTTP {code}）：{body}")
        return out
    if code != 200:
        out.update(step="base", detail=f"{base}/models 返回 HTTP {code}：{body}")
        return out

    code, body = _call(f"{base}/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": "只回两个字：收到"}],
        "max_tokens": 16}, timeout=60)
    if code == 200:
        out.update(ok=True, step="done",
                   detail=f"可用：{src}（{out['key_tail']}）+ 模型 {model}")
        return out
    if code == 404:
        out.update(step="model",
                   detail=f"Key 没问题，但这个接口上没有模型「{model}」——换一个模型名")
        return out
    # 402 / 余额不足要单独认出来：Key 是对的、模型名也是对的，
    # 只是账户没钱了。把它归到「模型」或「Key」都会让人白折腾。
    # 实测就是这么撞上的：列模型 200（不花 token），一发对话 402。
    if code in (402, 403) or "balance" in body.lower() or "insufficient" in body.lower():
        out.update(step="balance",
                   detail=f"Key 和模型都没问题，但**账户余额不足**：{body}")
        return out
    out.update(step="model",
               detail=f"发对话返回 HTTP {code}：{body}")
    return out


@app.post("/api/topic/draft")
def api_topic_draft(req: TopicDraftReq, request: Request,
                    x_api_key: str | None = Header(None, alias="X-Api-Key"),
                    x_base_url: str | None = Header(None, alias="X-Base-Url"),
                    x_model: str | None = Header(None, alias="X-Model")):
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

    # 界面上选的模型和接口地址必须真的生效。早先这两个值只从请求体里读，
    # 而前端是放在 X-Model / X-Base-Url 头里发的 —— 于是「设置」里
    # 选的模型和 base URL 在起草这条路上**完全没生效**，用户看到一个
    # 自己没选过的模型在跑，或者别家的 key 被发到硅基流动去撞 401。
    model = req.model or x_model or ""
    base = (x_base_url or "").strip()
    client_key = (x_api_key or "").strip()

    # **服务端默认用哪家，走 server_default()，不写死硅基流动。**
    # 原来这里是：
    #     model = ... or "Qwen/Qwen2.5-72B-Instruct"
    #     server_key = os.environ.get("SILICONFLOW_API_KEY") or ...
    # 于是 .env 里配的是火山方舟（豆包）、用户也想用豆包时，起草会拿
    # **硅基的 key + 硅基的模型名**去跑 —— 报了 200 也是走了另一家，
    # 而且响应里回报的模型名跟实际配置对不上，用户只会觉得"怎么老去找硅基"。
    srv_key, srv_base, srv_model, srv_label = "", "", "", ""
    if allow_env:
        try:
            from histmap_agent.llm import server_default
            _d = server_default()
            srv_key, srv_base = _d.key, _d.base
            srv_model, srv_label = _d.model, _d.label
        except SystemExit:
            srv_key = ""
        except Exception:
            srv_key = (os.environ.get("SILICONFLOW_API_KEY")
                       or NT._key_from_env_file() or "")
    if not model:
        model = srv_model
    if not base:
        base = srv_base
    server_key = srv_key

    # 「访问者没带 Key，服务端也不能用站长的」在公网是**常态**，不是异常。
    # 早先这种情况会一路走到 new_topic.draft 里抛出
    # 「设 SILICONFLOW_API_KEY，或用 --key 传」—— 那是给开发者看的命令行说明，
    # 网页访客既看不懂也做不到，而且状态码是 400（应该是 401 需要鉴权）。
    # 这里提前拦住，给一句网页用户能照做的话。
    if not client_key and not server_key:
        raise HTTPException(401, (
            "起草新题材需要一个模型 Key。点右上角「设置」填一个就行 —— "
            "它只存在你自己浏览器的 localStorage 里，不上传、不写盘、不进日志。"
            "（出图不需要 Key：地图是代码画的。）"))

    def _run(k: str, b: str):
        return NT.draft(req.ask.strip(), key=k, model=model, base=b,
                        verbose=False, allow_env_key=allow_env)

    used = "你填的 Key" if client_key else "本机 .env 里的 Key"
    note = ""
    # **必须把 server_key 显式传进去。**
    # 踩过的坑：这里原来写 `_run(client_key, base)`，没带 key 时 client_key 是空串，
    # 而 new_topic.draft 见到空 key 会**自己去读 .env 的 SILICONFLOW_API_KEY**
    # —— 于是服务端配的是火山方舟，发出去的却是硅基的 key，火山回
    # `401 AuthenticationError: The API key format is incorrect`。
    # 看着像 key 错了，其实是**拿错了那一家**。既然上面已经解析出 server_key
    # 和它对应的 base，就必须成对地传下去。
    eff_key = client_key or server_key
    try:
        spec = _run(eff_key, base)
    except NT.ModelAuthError as e:
        # 本机 + .env 里有能用的 key 时，别让一个填错的 key 把功能堵死 ——
        # 这是「自己电脑上打开就能用」的承诺。但必须**说清换了哪个 key**，
        # 否则用户会以为生效的是自己填的那个，之后换机器就莫名其妙失败。
        env_key = ""
        if allow_env and os.environ.get("HISTMAP_ALLOW_SERVER_KEY") != "0":
            # 同样不写死硅基：回退用的 key 要是**当前配置那一家**的。
            env_key = srv_key or (os.environ.get("SILICONFLOW_API_KEY")
                                  or NT._key_from_env_file() or "")
        if client_key and env_key and env_key != client_key:
            # 回退必须**连 base URL 一起回退**：把 .env 的 key 配到界面里
            # 残留的别家 base URL 上照样 401。这里传空 base，走服务端默认值。
            try:
                spec = _run(env_key, "")
                used = "本机 .env 里的 Key"
                note = ("你填的那个 Key 被服务商拒绝了（401/403），"
                        "这次改用了服务端 .env 里的 Key 和它对应的接口地址。"
                        "想一直用自己的，去「设置」里点『测试连接』看具体报错。")
            except Exception as e2:
                raise HTTPException(401, f"{e}\n（改用本机 .env 的 Key 也失败：{e2}）")
        else:
            raise HTTPException(401, str(e))
    except SystemExit as e:
        # 注：上面那个 except NT.ModelAuthError 一定先命中（它同时继承
        # SystemExit 和 Exception），所以走到这里的都是别的原因。
        msg = str(e)
        # 把面向命令行的措辞换成网页访客能照做的说法。
        # 这类消息本来是给 `python src/new_topic.py` 的用户看的，
        # 原样返回给网页用户只会让人以为坏了。
        if "SILICONFLOW_API_KEY" in msg or "--key" in msg:
            msg = ("起草新题材需要一个模型 Key，点右上角「设置」填一个即可。"
                   "（出图不需要 Key：地图是代码画的。）")
        raise HTTPException(400, msg)
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
    if note:
        warn.insert(0, note)
    return {"spec": spec, "warnings": warn, "model": model,
            # 回报**实际用的**接口地址，不要兜底成硅基 ——
            # 用户看这一行来判断"到底走了哪一家"，写错等于骗人。
            "base": base or srv_base,
            "used": used}


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
        # 同 api_topic_draft：这里抛出来的多半是面向命令行的措辞
        # （「先跑 python src/build_dynasty_map.py ...」之类），
        # 网页用户看不懂也做不到，换成能照做的说法。
        msg = str(e)
        if "python src/" in msg or "SILICONFLOW_API_KEY" in msg:
            msg = ("这个题材没能建出来：" + msg.split("\n")[0]
                   + "（出图不需要 Key；这一步要取行政区数据，可能要等一会儿）")
        raise HTTPException(400, msg)
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
    # 超采样倍数：按容器可用内存自动定。**必须在日志里打出来** ——
    # Render 免费档只有 512 MB，而 2 倍超采样出片峰值实测 848 MB，
    # 超了会被**静默杀掉**（日志里只有一句 Killed）。打出来才知道
    # 到底是"自动降到 1 倍了"还是"没降、被杀"。
    from histmap_core import auto_supersample, memory_limit_mb
    _lim = memory_limit_mb()
    _ss = auto_supersample(2)
    print(f"[自检] 内存上限 {_lim:.0f} MB（0=读不到）→ 超采样 {_ss} 倍"
          + ("（已为低内存自动降档）" if _ss == 1 else ""))

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
