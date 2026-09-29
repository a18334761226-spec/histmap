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
    return {"ok": True, "topics": list(topics.load_topics()), "version": app.version,
            # 几何过期提示：改了控制表却没重跑构建时，这里点名是哪几年
            "stale": topics.stale_report()}


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
class VideoReq(BaseModel):
    scene: str
    date_from: str
    date_to: str
    theme: str = "dark"
    size: str = "16x9"
    style: str = "none"
    style_profile: dict | None = None
    fps: int = 24
    hold: float = 0.30                        # 每个年份停留秒数
    # 上限只用来兜住手滑（比如拿月份当区间滑到几万帧），不该成为常见区间的暗坑：
    # 二战 1939–1945 有 146 个时间点，卡在 60 就是「界面说 146 张、实际只出 60 张」。
    max_frames: int = 240
    fade: float = 0.20                        # 交叉溶解时长


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

    dates = pick_dates(req.scene, req.date_from, req.date_to, req.max_frames)
    if not dates:
        jobs.update(jid, status="failed", error="这个区间里没有可渲染的日期")
        return
    jobs.update(jid, status="running", total=len(dates), frames=[],
                message=f"准备渲染 {len(dates)} 张")

    # ── 1) 逐张出图 ──
    urls, paths = [], []
    for i, d in enumerate(dates):
        img = topics.render(req.scene, d, req.theme, req.size)
        img = _apply_quality(img, RenderReq(scene=req.scene, date=d, theme=req.theme,
                                            size=req.size, style=req.style,
                                            style_profile=req.style_profile))
        p = os.path.join(fdir, f"{i:04d}.png")
        img.save(p)
        paths.append(p)
        urls.append(f"/media/jobs/{jid}/frames/{i:04d}.png")
        jobs.update(jid, frames=list(urls),
                    progress=round((i + 1) / len(dates) * 0.85, 3),
                    message=f"出图 {i+1}/{len(dates)} · {d[:10]}")

    # ── 2) 合成视频 ──
    jobs.update(jid, message="合成视频…", progress=0.9)
    ff = M.find_ffmpeg()
    errf = open(os.path.join(out_dir, "ffmpeg.log"), "w",
                encoding="utf-8", errors="replace")
    # 每张图在片子里停留 req.hold 秒：
    # 把**输入**帧率设成 1/hold，输出帧率设成 fps，ffmpeg 会自动补帧。
    # （直接把输入输出都设成 fps 的话，每张只闪 1/fps 秒，等于没有停留）
    in_fps = max(0.05, 1.0 / max(0.05, req.hold))
    cmd = [ff, "-y", "-loglevel", "error",
           "-framerate", f"{in_fps:.4f}",
           "-i", os.path.join(fdir, "%04d.png"),
           "-r", str(req.fps),
           "-c:v", "libx264", "-preset", "medium", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", mp4]
    rc = subprocess.call(cmd, stderr=errf)
    errf.close()
    ok, why = M.verify_mp4(ff, mp4)
    if rc != 0 or not ok:
        jobs.update(jid, status="failed", error=f"ffmpeg rc={rc}；{why}")
        return
    secs = round(len(urls) * req.hold, 1)
    jobs.update(jid, status="done", progress=1.0, message="完成",
                result={"video": f"/media/jobs/{jid}/video.mp4",
                        "frames": urls, "n_frames": len(urls),
                        "seconds": secs, "size": req.size, "verify": why})


@app.post("/api/video")
def api_video(req: VideoReq):
    if not topics.get(req.scene):
        raise HTTPException(404, "没有这个题材")
    jid = jobs.new_job("animate", req.model_dump(exclude={"style_profile"}))
    # jid 只走位置参数。早先写成 run_async(fn, jid, req, jid=jid)，
    # 同一个 jid 传了两次 → TypeError: got multiple values for argument 'jid'，
    # 视频这条路从第一版起就是坏的（当时没测到）。
    jobs.run_async(_animate_job, jid, req)
    sel = pick_dates(req.scene, req.date_from, req.date_to, req.max_frames)
    # 截断了就明说，别让界面显示的数字和实际出的帧数对不上
    lo, hi = sorted((_days(req.date_from), _days(req.date_to)))
    in_range = sum(1 for d in (topics.get(req.scene).dates() or []) if lo <= _days(d) <= hi)
    note = (f"区间内 {in_range} 个时间点，超过上限 {req.max_frames}，"
            f"已均匀抽稀为 {len(sel)} 帧" if in_range > len(sel) else "")
    return {"job": jid, "n_dates": len(sel), "in_range": in_range,
            "max_frames": req.max_frames, "note": note}


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
    # 启动自检：几何过期就在日志里点名，别让人对着旧图排查半天
    for s in topics.stale_report():
        if "error" in s:
            print(f"[自检] {s['topic']} 检查失败：{s['error']}")
        else:
            print(f"[自检] {s['topic']} 有 {len(s['stale_years'])} 年的几何是旧的"
                  f"（{s['stale_years']}）。重跑：{s['fix']}")
    port = int(os.environ.get("PORT") or 8810)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
