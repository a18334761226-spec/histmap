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
import sys
import time
import urllib.request

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
for p in (os.path.join(ROOT, "packages", "core"), os.path.join(ROOT, "src"), HERE):
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
    "chat": [
        {"id": "Qwen/Qwen2.5-7B-Instruct", "label": "Qwen2.5-7B（快、便宜）"},
        {"id": "Qwen/Qwen2.5-32B-Instruct", "label": "Qwen2.5-32B（更会听懂话）"},
        {"id": "Qwen/Qwen2.5-72B-Instruct", "label": "Qwen2.5-72B（最强，最贵）"},
        {"id": "deepseek-ai/DeepSeek-V3", "label": "DeepSeek-V3"},
    ],
    "image_edit": [
        {"id": "Qwen/Qwen-Image-Edit-2509", "label": "Qwen-Image-Edit-2509"},
        {"id": "Qwen/Qwen-Image-Edit", "label": "Qwen-Image-Edit"},
    ],
}

STYLE_PRESETS = [
    {"id": "none", "label": "不加质感（原始渲染）"},
    {"id": "atlas", "label": "古典雕版（做旧纸张）"},
    {"id": "vintage", "label": "复古羊皮纸"},
    {"id": "ink", "label": "宣纸水墨（需浅色主题）"},
    {"id": "modern", "label": "现代信息图（微质感）"},
]


# ════════════════════════════════════════════════════════════
# 基础
# ════════════════════════════════════════════════════════════
@app.get("/api/health")
def health():
    return {"ok": True, "topics": list(topics.load_topics()), "version": app.version}


@app.get("/api/scenes")
def api_scenes():
    """题材列表。题材是**数据**（data/topics/topics.json），不是代码。"""
    return {"scenes": topics.list_topics()}


@app.get("/api/styles")
def api_styles():
    return {"presets": STYLE_PRESETS}


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
    return {"profile": prof, "preview": f"/media/{os.path.basename(tmp)}"}


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


def _apply_quality(img, req: RenderReq):
    """把预设质感 / 参考图风格套上去。"""
    if req.style_profile:
        import style_from_image as SFI
        prof = dict(req.style_profile)
        if "_texture_grid" not in prof:        # 前端回传时丢了网格，重新生成一张
            import numpy as np
            g = np.random.default_rng(7).random((12, 12))
            prof["_texture_grid"] = g.tolist()
        variety = SFI.palette_variety(prof.get("palette") or [])
        return SFI.apply_style(img, prof, adopt_palette=variety >= 0.18,
                               strength=req.strength)
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
    try:
        img = topics.render(req.scene, req.date, req.theme, req.size)
    except FileNotFoundError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"渲染失败: {type(e).__name__}: {e}")
    img = _apply_quality(img, req)

    name = f"render_{req.scene}_{req.date}_{req.theme}_{req.style}_{int(time.time()*1000)}.png"
    path = os.path.join(MEDIA, name)
    img.save(path)
    return {
        "image": f"/media/{name}",
        "width": img.size[0], "height": img.size[1],
        "elapsed": round(time.time() - t0, 2),
        "params": req.model_dump(exclude={"style_profile"}),
    }


# ════════════════════════════════════════════════════════════
# 3) 视频
# ════════════════════════════════════════════════════════════
class VideoReq(BaseModel):
    scene: str
    date_from: str
    date_to: str
    theme: str = "dark"
    size: str = "16x9"
    style: str = "none"
    fps: int = 24
    hold: float = 0.30                        # 每帧停留秒数
    max_frames: int = 60                      # 上限，防手滑出几千帧


def _video_job(jid: str, req: VideoReq):
    import subprocess
    import make_ww2_video as M
    from datetime import date as _d, timedelta

    sc = topics.get(req.scene)
    jobs.update(jid, status="running", message="准备中")
    out_dir = jobs.job_dir(jid)
    mp4 = os.path.join(out_dir, "video.mp4")

    d0, d1 = _d.fromisoformat(req.date_from), _d.fromisoformat(req.date_to)
    if d1 < d0:
        d0, d1 = d1, d0
    # 均匀取点，避免手滑传一个 20 年的区间
    total_days = max(1, (d1 - d0).days)
    n = min(req.max_frames, max(2, total_days // 30 + 1))
    dates = [d0 + timedelta(days=round(total_days * i / max(1, n - 1)))
             for i in range(n)]

    W, H = (1920, 1080) if req.size == "16x9" else (1080, 1920)
    ff = M.find_ffmpeg()
    cmd = [ff, "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
           "-r", str(req.fps), "-i", "-",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]
    errf = open(os.path.join(out_dir, "ffmpeg.log"), "w", encoding="utf-8",
                errors="replace")
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)

    hold_n = max(1, int(req.hold * req.fps))
    written = 0
    frames = []
    for i, d in enumerate(dates):
        img = topics.render(req.scene, d.isoformat(), req.theme, req.size)
        img = _apply_quality(img, RenderReq(scene=req.scene, date=d.isoformat(),
                                            theme=req.theme, size=req.size,
                                            style=req.style))
        frames.append(img)
        jobs.update(jid, progress=round((i + 1) / len(dates) * 0.7, 3),
                    message=f"渲染 {i+1}/{len(dates)}")
    # 补帧 + 交叉溶解（与脚本一致：动静跟着史料走，这里简化为定长停留）
    prev = None
    for i, img in enumerate(frames):
        if prev is not None:
            for k in range(1, 4):
                proc.stdin.write(Image.blend(prev, img, k / 4).tobytes())
                written += 1
        for _ in range(hold_n):
            proc.stdin.write(img.tobytes())
            written += 1
        prev = img
        jobs.update(jid, progress=0.7 + (i + 1) / len(frames) * 0.25,
                    message=f"编码 {i+1}/{len(frames)}")
    for _ in range(req.fps):                  # 结尾定格 1 秒
        proc.stdin.write(prev.tobytes())
        written += 1
    proc.stdin.close()
    rc = proc.wait()
    errf.close()

    ok, why = M.verify_mp4(ff, mp4)
    if rc != 0 or not ok:
        jobs.update(jid, status="failed",
                    error=f"ffmpeg rc={rc}；{why}")
        return
    jobs.update(jid, status="done", progress=1.0, message="完成",
                result={"video": f"/media/jobs/{jid}/video.mp4",
                        "frames": written, "seconds": round(written / req.fps, 1),
                        "size": req.size, "n_keyframes": len(frames),
                        "verify": why})


@app.post("/api/video")
def api_video(req: VideoReq):
    if not topics.get(req.scene):
        raise HTTPException(404, "没有这个场景")
    jid = jobs.new_job("video", req.model_dump())
    jobs.run_async(_video_job, jid, req, jid=jid)
    return {"job": jid}


@app.get("/api/jobs")
def api_jobs():
    return {"jobs": jobs.list_jobs()}


@app.get("/api/jobs/{jid}")
def api_job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404, "没有这个任务")
    return j


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

    
    # 场景清单里的 dates 太长，只留范围
    slim = []
    for s in topics.list_topics():
        ds = s["dates"]
        slim.append({"id": s["id"], "title": s["title"], "desc": s["desc"],
                     "themes": s["themes"],
                     "date_range": [ds[0], ds[-1]] if ds else None,
                     "n_dates": len(ds),
                     "recommended_size": "4x3" if s["id"] == "tang" else "16x9"})
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

    try:
        txt = data["choices"][0]["message"]["content"]
        parsed = json.loads(txt)
    except Exception:
        raise HTTPException(502, f"模型没按 JSON 回：{(txt or '')[:300]}")
    parsed.setdefault("action", None)
    parsed.setdefault("state", req.state)
    return parsed


# ════════════════════════════════════════════════════════════
# 5) 静态资源
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
    port = int(os.environ.get("PORT") or 8810)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
