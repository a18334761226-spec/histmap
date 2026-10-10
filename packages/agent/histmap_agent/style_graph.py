"""风格与提示词抽取图（LangGraph）—— 对应「抽取用户上传的风格和提示词」
和「通过提示词+数据+风格让大模型生成」这两步。

分两条腿，各管一段
------------------
**抽取**（读参考图，出两样东西）
  · style_desc    —— 中文的风格描述，给人看，也解释"这套风格是什么"
  · image_prompt  —— 给图生图模型用的英文提示词，只描述材质与色彩
  · palette/tags  —— 代表色与标签
  还要**同时**保留原有的纯代码提取（调色板/颗粒/暗角）——
  模型给的是描述，代码给的是数值，两者互补而不是替代。

**生成**（用提示词 + 数据 + 风格让模型出图）
  这里必须说清楚一件事：**模型不画地图**。地图的国界、控制区、中文标注
  全部由确定性代码绘制；模型只负责「质感」这一层。原因是实测过的 ——
  让扩散模型整张重画，结构相似度 NCC 是 **−0.075**，中文标注会变成乱码笔画，
  而且换更强的模型只会把错的图渲染得更漂亮。

  所以生成这一步是：拿一张**已经画好的地图**当底图 → 图生图只换质感 →
  过保真度闸门（NCC ≥ 0.90 且色块保真 ≥ 80%）→ 过了才用来抽材质层。

**为什么这道闸门是关键**：它让"用图像模型"这件事变得安全。模型一旦动了
地图内容，闸门就判失败，我们收紧提示词重试；重试还不过就**退回纯代码的
材质层**。也就是说最坏情况下画面只是"没变成参考图那种质感"，
绝不会出现一张被改坏的地图。

图长这样：

    START → cv_extract → vlm_describe ─┬(拿到提示词)→ generate_sample
                                       └(视觉模型不可用)→ finalize(纯代码)
    generate_sample → fidelity_gate ─┬(过)→ extract_material → finalize
                                     ├(不过 & 还有次数)→ tighten_prompt ─┐
                                     └(不过 & 没次数)→ finalize(纯代码)  │
                                                                        │
                                            回到 generate_sample ◄───────┘
"""
from __future__ import annotations

import operator
import os
import sys
from typing import Annotated, Any, TypedDict

from .llm import LLMConfig, chat_json_vision
from .prompts import STYLE_NEGATIVE_PROMPT, STYLE_VLM_PROMPT

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
for _p in (os.path.join(ROOT, "src"), os.path.join(ROOT, "packages", "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 保真度闸门的阈值跟 stylize.check_fidelity 保持一致（那边是权威实现）
# 最多生成几次。同一提示词换种子，纸纹的"干净程度"能从 12.5 跳到 7.8，
# 所以多试几次的收益很明显；每次约 20 秒，3 次是可接受的等待。
MAX_GEN_ATTEMPTS = 3


TEXTURE_PROMPT_TAIL = (
    " A seamless full-bleed flat texture that covers the ENTIRE frame edge to edge, "
    "corner to corner, with NO paper edge, NO torn or curled border, NO drop shadow, "
    "NO black margin, NO rolled corner. Perfectly flat even lighting across the whole "
    "surface. Uniform subtle texture only — nothing drawn on it: "
    "ABSOLUTELY NO text, no letters, no numbers, no map, no coastlines, no borders, "
    "no grid, no decorative linework, no ornaments, no pattern, no signature, "
    "no watermark, no objects, no stains shaped like anything.")

# 合成纸纹时用的容差。底图背景是纯色，跟它的曼哈顿距离小于这个值就当作"背景"，
# 只在背景像素上贴纸纹 —— 这样**结构上不可能**碰到任何地图内容。
BG_TOLERANCE = 26


class StyleState(TypedDict, total=False):
    ref_path: str                  # 用户上传的参考图
    base_path: str                 # 要套质感的底图（代码渲染出来的精确地图）
    out_path: str                  # 生成的样张落盘位置
    vlm_cfg: LLMConfig             # 视觉模型（读参考图）
    img_key: str                   # 图像模型的 key
    img_model: str
    # 图像模型的接口地址。**必须能传进来**：豆包 Seedream 在火山方舟、
    # Qwen-Image 在硅基流动，两家的模型名互不相认。早先 base 写死在
    # stylize.SiliconFlow 里，模型清单里那个 base 字段从来没被用过。
    img_base: str
    # 调用方已经给了提示词时，就不用再问一次视觉模型。
    # 省一次模型调用（更快更省），也避免「调用方选了火山方舟、
    # 我们却拿一个 Qwen 的视觉模型名去打火山」这种错配。
    prompt_override: str
    mode: str                      # "texture"（默认，安全）| "edit"（图生图，实测会毁标注）
    log: Annotated[list[str], operator.add]

    cv_profile: dict[str, Any]     # 纯代码提取（调色板/颗粒/暗角）
    vlm: dict[str, Any]            # 视觉模型给的描述与提示词
    prompt: str                    # 本轮实际使用的生图提示词
    styled_path: str
    texture_path: str
    blank: dict[str, Any]          # 纸纹空白度（模型有没有在纸上画东西）
    fidelity: dict[str, Any]
    attempts: int
    tighten: int                   # 收紧了几次
    verdict: str                   # textured / generated / cv_only
    material: dict[str, Any]


# ══════════════════════════════════════════════════════════════
#  节点：抽取
# ══════════════════════════════════════════════════════════════
def cv_extract(state: StyleState) -> dict:
    """纯代码提取：调色板、颗粒、暗角、纹理网格。

    这一路**不调模型**，所以它永远可用 —— 视觉模型挂了也有东西兜底。
    """
    import style_from_image as SFI
    prof = SFI.extract_style(state["ref_path"], verbose=False)
    variety = SFI.palette_variety(prof.get("palette") or [])
    return {
        "cv_profile": prof,
        "log": [f"代码提取：{len(prof.get('palette') or [])} 个主色，"
                f"色相多样性 {variety:.2f}，颗粒 {prof.get('grain', 0):.4f}，"
                f"暗角 {prof.get('vignette', 0):.3f}"],
    }


def vlm_describe(state: StyleState) -> dict:
    """让视觉模型读参考图，产出**风格描述 + 生图提示词**。

    这就是「抽取风格和提示词」里的提示词那一半 —— 纯代码提取不出"这套风格
    该怎么用文字描述"，而写提示词恰好是模型擅长的事。

    调用方已经给了提示词（prompt_override）时直接跳过：那说明它在更早的
    一步已经抽过了（比如 /api/style/extract?vlm=true 抽完再把提示词传进来）。
    再问一次模型既慢又费，还会引入错配 —— 调用方选了火山方舟，
    我们却拿一个 Qwen 的视觉模型名去火山打。
    """
    if (state.get("prompt_override") or "").strip():
        return {"prompt": state["prompt_override"].strip(),
                "log": ["用调用方已抽好的提示词，不再问一次视觉模型"]}
    try:
        v = chat_json_vision(state["vlm_cfg"], STYLE_VLM_PROMPT,
                             state["ref_path"])
    except (Exception, SystemExit) as e:
        # 视觉模型不可用（没配、模型不支持读图、余额不足、超时）不该让整件事失败：
        # 退回纯代码那条路，只是没有文字描述和生图提示词。
        # **这里必须连 SystemExit 一起接**：ModelAuthError 是 SystemExit 的子类，
        # 而 SystemExit 属于 BaseException，`except Exception` 接不住它 ——
        # 实测就因为这一条，余额不足时本该"优雅退回纯代码"的分支没生效，
        # 错误一路冒到 FastAPI 变成了 500。
        return {"vlm": {}, "prompt": "",
                "log": [f"视觉模型不可用，回退纯代码提取：{type(e).__name__}: "
                        f"{str(e)[:200]}"]}
    desc = str(v.get("style_desc") or "").strip()
    prompt = str(v.get("image_prompt") or "").strip()
    return {
        "vlm": v,
        "prompt": prompt,
        "log": [f"视觉模型：风格描述「{desc[:60]}…」" if len(desc) > 60
                else f"视觉模型：风格描述「{desc}」",
                f"生图提示词 {len(prompt)} 字符，标签 {v.get('tags') or []}",
                f"底色判定：{'浅色（纸）' if v.get('is_light') else '深色（墨底）'}"],
    }


# ══════════════════════════════════════════════════════════════
#  节点：生成
# ══════════════════════════════════════════════════════════════
def generate_sample(state: StyleState) -> dict:
    """图生图：把**代码画好的地图**变成参考图那种质感。

    注意输入是 base_path（精确地图），不是参考图 —— 参考图只提供提示词，
    内容永远来自代码渲染。这样"地图是对的"这件事从头到尾没被交给模型。
    """
    import stylize as Z
    key = state.get("img_key") or ""
    if not key:
        return {"styled_path": "", "log": ["没有图像模型的 Key，跳过生成"]}
    if not state.get("base_path") or not os.path.exists(state["base_path"]):
        return {"styled_path": "", "log": ["没有底图，跳过生成"]}

    prov = Z.SiliconFlow(key, model=state.get("img_model") or None,
                         base=state.get("img_base") or None)
    prompt = state.get("prompt") or ""
    # 负向提示词单独给：在正向里写「不要文字」远不如放进 negative 有效
    full = (prompt + "\n\nKeep every border, colour block and label exactly "
                     "as in the input image. Do not redraw anything. "
                     "Change only paper texture, grain and overall tone.")
    out = state.get("out_path") or os.path.join(
        ROOT, "output", "api", "style_sample.png")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        url, secs, seed = prov.run(state["base_path"], full, steps=30)
        Z._download(url, out)
    except Exception as e:
        return {"styled_path": "",
                "log": [f"生成失败：{type(e).__name__}: {str(e)[:200]}"]}

    # **把样张拉回底图的尺寸**再往下走。
    # 这不是"美化结果"，而是去掉一个与内容无关的干扰项：实测
    # Qwen-Image-Edit-2509 会把 1920×1080 的输入吐成 1360×768，
    # 光这一次重采样就把细边界和中文标注糊掉了 —— 于是保真度闸门
    # 判 NCC 0.74 不通过，可失败的其实是"分辨率"而不是"模型改图"。
    # 尺寸是运输细节，不是内容决定；拉回来之后再比才是公平的比较。
    norm = ""
    try:
        from PIL import Image
        b = Image.open(state["base_path"])
        s = Image.open(out)
        if s.size != b.size:
            s.convert("RGB").resize(b.size, Image.LANCZOS).save(out)
            norm = f"（{s.size[0]}×{s.size[1]} 已拉回 {b.size[0]}×{b.size[1]}）"
        b.close()
        s.close()
    except Exception as e:
        norm = f"（尺寸归一失败：{type(e).__name__}）"
    return {"styled_path": out,
            "attempts": (state.get("attempts") or 0) + 1,
            "log": [f"第 {(state.get('attempts') or 0) + 1} 次生成：{secs:.0f}s，"
                    f"seed={seed} -> {os.path.basename(out)}{norm}"]}


def generate_texture(state: StyleState) -> dict:
    """**纯文生图**：让模型画一张空白纸纹（不带输入图）。

    为什么不用图生图：实测把地图交给 Qwen-Image-Edit-2509 之后，纸纹确实
    做得很好，但**中文标注全被改成乱码笔画**（`所属政权`→`所鹰政权`，
    一堆地名变了字），尺寸归一之后结构相似度也只有 0.84 < 0.90。
    原因是"编辑这张图"这个任务本身就允许模型重画内容 —— 提示词再强调
    「不要改」也只是请求，不是约束。

    换成"从零画一张空白纸"之后，模型**没有可以改坏的对象**：
    纸纹本来就该只有材质、没有内容。然后我们把代码画的地图贴上去。
    """
    import stylize as Z
    key = state.get("img_key") or ""
    if not key:
        return {"texture_path": "", "log": ["没有图像模型的 Key，跳过纸纹生成"]}
    prompt = (state.get("prompt") or "").strip()
    if not prompt:
        return {"texture_path": "", "log": ["视觉模型没给出提示词，跳过纸纹生成"]}
    out = state.get("out_path") or os.path.join(
        ROOT, "output", "api", "style_texture.png")
    out = os.path.splitext(out)[0] + "_texture.png"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    prov = Z.SiliconFlow(key, model=state.get("img_model") or None,
                         base=state.get("img_base") or None)
    # 纸纹按底图的尺寸生成，省一次重采样
    size = "1024x1024"
    try:
        from PIL import Image
        if state.get("base_path") and os.path.exists(state["base_path"]):
            w, h = Image.open(state["base_path"]).size
            scale = min(1.0, 1440 / max(w, h))
            size = f"{int(w*scale)//8*8}x{int(h*scale)//8*8}"
    except Exception:
        pass
    try:
        # model 传空：用 prov 上那个（= 调用方选的模型）。
        # 原来这里写死 `model="Qwen/Qwen-Image"`，于是就算调用方选了
        # 豆包 Seedream，发出去的还是 Qwen 的名字 —— 跟 base 写死是同一个
        # 问题的另一半。prov.model 已经在 __init__ 里兜了默认值。
        url, secs, seed = prov.text2img(prompt + TEXTURE_PROMPT_TAIL,
                                        size=size,
                                        negative_prompt=STYLE_NEGATIVE_PROMPT)
        Z._download(url, out)
    except Exception as e:
        return {"texture_path": "",
                "log": [f"纸纹生成失败：{type(e).__name__}: {str(e)[:200]}"]}

    # **把中间裁出来铺满**。提示词里已经写了「满幅、无毛边、无黑边、不卷角」
    # 两遍，模型还是画了一张"带毛边和卷角的纸"、四周留黑 —— 这是它从
    # "old paper" 这个词带出来的强先验，靠提示词拦不住（实测两次都一样）。
    # 纸纹没有语义，裁掉边不影响任何东西，所以这一步交给代码而不是继续调提示词。
    crop_note = ""
    try:
        from PIL import Image
        im = Image.open(out).convert("RGB")
        w, h = im.size
        c = im.crop((int(w * 0.11), int(h * 0.11), int(w * 0.89), int(h * 0.89)))
        c.resize((w, h), Image.LANCZOS).save(out)
        crop_note = "，已裁掉模型画的毛边（取中间 78% 铺满）"
        im.close()
    except Exception as e:
        crop_note = f"（裁边失败：{type(e).__name__}）"
    return {"texture_path": out,
            "attempts": (state.get("attempts") or 0) + 1,
            "log": [f"纸纹生成：{secs:.0f}s，{size}，seed={seed} -> "
                    f"{os.path.basename(out)}{crop_note}"]}


# 判定「模型画出来的是不是一张干净的纸」的阈值。
# 取的是**中尺度亮度标准差**（缩到 64×36 再看起伏）：纸纹是高频低幅，
# 起伏很小；而模型画进去的东西（狐狸、地形线画、卷角、黑边）是中尺度的大块
# 明暗。实测标定：
#     代码画的纯背景       0.00
#     干净纸纹（可用）     7.10 / 7.82
#     有内容的纸纹       11.41 / 12.46 / 12.63 / 13.28
#     带毛边黑框的纸纹   65.93 / 66.90
# 11.0 落在「干净」和「明显有内容」之间。这个数是**启发式**，样本只有 8 张，
# 而且同一提示词换个种子就能从 12.5 跳到 7.8 —— 所以它只用来决定
# "这一张要不要用、要不要换个种子重来"，不用来决定成败：
# 真正保证安全的是后面 content_untouched 那道「内容改动必须为 0」。
BLANK_MAX_STD = 11.0


def texture_blank(state: StyleState) -> dict:
    """纸纹的「空白度」闸门。

    为什么必须查：文生图模型**没法可靠地画出一张空白纸**。提示词里写满了
    「满幅、无物体、无图案、无边框」，它还是会在角落里画一只狐狸、
    在右边画一片地形线画、或者干脆画一张带毛边和黑框的纸（实测四种结果都有）。
    这不是提示词没写好，是模型的先验 —— 给它 "old paper" 它就想配点装饰。

    高通能滤掉大块的东西，但**线画和纸纹一样是高频的**，单靠滤波切不干净。
    所以这里独立量一次：起伏过大就说明纸上有内容，不用它。
    """
    import numpy as np
    from PIL import Image
    p = state.get("texture_path") or ""
    if not p or not os.path.exists(p):
        return {"fidelity": {"ok": False, "issues": ["没有纸纹"]},
                "log": ["纸纹空白度：没有纸纹"]}
    im = Image.open(p).convert("L")
    a = np.asarray(im).astype(np.float32)
    gy, gx = np.gradient(a)
    edge = float((np.hypot(gx, gy) > 40).mean())
    small = np.asarray(im.resize((64, 36), Image.LANCZOS)).astype(np.float32)
    std = float(small.std())
    ok = std <= BLANK_MAX_STD
    rep = {"ok": ok, "mid_std": round(std, 2), "edge_ratio": round(edge, 5),
           "threshold": BLANK_MAX_STD,
           "issues": [] if ok else
           [f"纸纹起伏 {std:.2f} > {BLANK_MAX_STD} —— 模型在纸上画了东西"]}
    return {"blank": rep,
            "log": [f"纸纹空白度：中尺度起伏 {std:.2f}"
                    f"（阈值 {BLANK_MAX_STD}）-> {'是干净的纸' if ok else '纸上有内容'}"]}


def after_blank(state: StyleState) -> str:
    """空白度没过怎么办。

    注意「没法生成」（没有 Key / 没有提示词 / 生成失败）必须**直接收尾**，
    不能走重试边：那几种情况下 generate_texture 什么都没干，
    重试还是同样的结果，于是会在 generate_texture ↔ texture_blank 之间
    死转直到撞上 LangGraph 的递归上限（实测就是这么炸的，
    报错还是一句 GraphRecursionError，完全看不出是"没有 Key"）。
    """
    b = state.get("blank") or {}
    if b.get("ok"):
        return "composite"
    if not (state.get("texture_path") or ""):
        return "give_up"
    if (state.get("attempts") or 0) < MAX_GEN_ATTEMPTS:
        return "retry"
    return "give_up"


def composite_texture(state: StyleState) -> dict:
    """把代码画的地图**贴到**模型生成的纸纹上。

    两步，各解决一个实测出来的问题：

    **第一步：高通，把模型"画进去的东西"滤掉。**
    文生图模型没法可靠地画"空白纸"—— 提示词里写满了「满幅、无物体、无图案」，
    它还是会在角落里画一只狐狸、在右边画一片地形线画（实测）。这不是提示词
    没写好，是模型的先验：给它"old paper"它就想配点装饰。
    所以不跟它较劲：把纹理除以自己的大半径模糊，**只留高频颗粒**，
    比模糊半径大的结构（狐狸、线画、毛边、黑框）全部被除掉。
    纸纹本来就是高频的，狐狸本来就是低频的 —— 这一刀正好切在两者之间。

    **第二步：底色用代码的，不用模型的。**
    乘上去的底是底图自己的背景色，所以画布色调永远跟地图配色一致；
    模型只贡献"颗粒这个乘数"，它没有机会改变整体色调。

    最后只在**背景像素**上合成（跟背景色曼哈顿距离 ≤ BG_TOLERANCE），
    结构上碰不到任何国界、色块、标注。贴完由 content_untouched 独立复核。
    """
    import numpy as np
    from PIL import Image, ImageFilter

    tex_p = state.get("texture_path") or ""
    base_p = state.get("base_path") or ""
    out = state.get("out_path") or os.path.join(
        ROOT, "output", "api", "style_sample.png")
    if not tex_p or not os.path.exists(tex_p):
        return {"styled_path": "", "log": ["没有纸纹，跳过合成"]}
    base = Image.open(base_p).convert("RGB")
    W, H = base.size
    a = np.asarray(base).astype(np.float32)

    # 底图背景色（四角中位）
    bg = np.median(np.concatenate([a[:8, :8].reshape(-1, 3),
                                   a[:8, -8:].reshape(-1, 3),
                                   a[-8:, :8].reshape(-1, 3),
                                   a[-8:, -8:].reshape(-1, 3)]), axis=0)

    # ── 第一步：高通 ──
    tex = Image.open(tex_p).convert("L").resize((W, H), Image.LANCZOS)
    tg = np.asarray(tex).astype(np.float32)
    # 半径要**大于模型可能画进去的任何东西**。0.05 倍时狐狸还留着一层淡影
    # （它的边缘有比半径更细的结构），0.12 倍才把它压到看不出。
    # 代价是纸的斑驳感也一起变淡 —— 但那本来就该由"底色 + 颗粒"表现，
    # 而不是由模型画一张有内容的图来表现。
    radius = max(8.0, max(W, H) * 0.12)
    low = np.asarray(tex.filter(ImageFilter.GaussianBlur(radius))).astype(np.float32)
    low = np.maximum(low, 8.0)
    grain = tg / low                             # 1.0 附近的高频乘数
    # 再把低频本身的起伏压掉：狐狸、线画这类"整块偏暗/偏亮"的区域
    # 主要就藏在这一层里，压掉它比继续加大半径更直接。
    dev = low - float(np.median(low))
    limit = 0.16 * 255.0
    low_flat = float(np.median(low)) + np.clip(dev, -limit, limit)
    grain = tg / np.maximum(low_flat, 8.0)
    amp = 0.10                                   # 颗粒幅度上限，别把画面压花
    grain = np.clip(grain, 1 - amp, 1 + amp)
    # 高频里也混进了模型的噪点，轻度平滑一下，避免出现椒盐
    grain = np.asarray(Image.fromarray(
        np.clip(grain * 128, 0, 255).astype(np.uint8), "L")
        .filter(ImageFilter.GaussianBlur(0.6)), dtype=np.float32) / 128.0
    kept = float(np.abs(grain - 1).mean())
    removed = float(np.abs(tg - low).mean())

    # ── 第二步：底色用代码的 ──
    textured_bg = np.clip(bg[None, None, :] * grain[..., None], 0, 255)

    # ── 只贴背景像素（不羽化：见下面注释）──
    dist = np.abs(a - bg[None, None, :]).sum(axis=2)
    mask = (dist <= BG_TOLERANCE)
    # **不做羽化**。一开始我加了 1.2px 高斯模糊让边缘柔和，结果闸门判
    # 「42226 个内容像素被改动」—— 羽化让 mask 在 0~1 之间取值，
    # 恰好把纸纹按比例混进了紧邻背景的那圈内容像素（也正是国界和标注的边）。
    # 硬边反而没问题：抗锯齿的边缘像素 dist 本来就大于容差，不会被碰。
    # 这条闸门抓的是我自己代码里的泄漏，不是"我确信我只贴了背景"。
    comp = np.where(mask[..., None], textured_bg, a)
    comp = np.clip(np.rint(comp), 0, 255).astype(np.uint8)
    Image.fromarray(comp).save(out)
    cov = float(mask.mean())
    return {"styled_path": out,
            "log": [f"合成：高通滤掉比 {radius:.0f}px 粗的结构"
                    f"（模型画进去的东西幅度 {removed:.1f}，留下的颗粒 {kept:.4f}）",
                    f"      底色改用代码的 {tuple(int(x) for x in bg)}，"
                    f"纸纹只作为乘数；贴在背景上覆盖 {cov*100:.0f}% 画面"]}


def content_untouched(state: StyleState) -> dict:
    """这一路专用的闸门：**数一下非背景像素被改了多少**。

    比 NCC 更贴题：这一路的风险不是"整体像不像"，而是"有没有碰到内容"。
    非背景像素 = 国界、色块、标注、图例、页脚，它们必须**一个都没变**。
    """
    import numpy as np
    from PIL import Image
    b = np.asarray(Image.open(state["base_path"]).convert("RGB")).astype(np.int16)
    s = np.asarray(Image.open(state["styled_path"]).convert("RGB")).astype(np.int16)
    if b.shape != s.shape:
        return {"fidelity": {"ok": False,
                             "issues": [f"尺寸不一致 {b.shape} vs {s.shape}"]},
                "log": ["闸门：尺寸不一致"]}
    bg = np.median(np.concatenate([b[:8, :8].reshape(-1, 3),
                                   b[:8, -8:].reshape(-1, 3),
                                   b[-8:, :8].reshape(-1, 3),
                                   b[-8:, -8:].reshape(-1, 3)]), axis=0)
    content = np.abs(b - bg).sum(axis=2) > BG_TOLERANCE
    changed = (np.abs(b - s).sum(axis=2) > 24) & content
    n = int(content.sum())
    ratio = float(changed.sum()) / max(1, n)
    ok = ratio < 1e-4
    rep = {"ok": ok, "content_pixels": n, "changed_pixels": int(changed.sum()),
           "content_changed_ratio": round(ratio, 8),
           "issues": [] if ok else
           [f"有 {int(changed.sum())} 个内容像素被改动了（应严格为 0）"]}
    return {"fidelity": rep,
            "log": [f"闸门：内容像素 {n} 个，被改动 {int(changed.sum())} 个 "
                    f"-> {'通过' if ok else '不通过'}"]}


def fidelity_gate(state: StyleState) -> dict:
    """保真度闸门：模型有没有偷偷改地图内容。

    这是整条链上**最要紧的一步**。判定不通过就不采用模型的结果，
    而不是"看起来还行就用"—— 实测模型改坏地图时画面往往还挺好看。
    """
    import stylize as Z
    styled = state.get("styled_path") or ""
    base = state.get("base_path") or ""
    if not styled or not os.path.exists(styled) or not os.path.exists(base):
        return {"fidelity": {"ok": False, "issues": ["没有可比较的样张"]},
                "log": ["闸门：没有样张，跳过"]}
    rep = Z.check_fidelity(base, styled)
    msg = (f"闸门：NCC {rep.get('ncc')}（≥0.90 才过）"
           f" 色块保真 {rep.get('color_keep')}（≥0.80 才过）"
           f" -> {'通过' if rep.get('ok') else '不通过'}")
    log = [msg] + [f"    {i}" for i in (rep.get("issues") or [])]
    return {"fidelity": rep, "log": log}


def tighten_prompt(state: StyleState) -> dict:
    """闸门没过：收紧提示词再试。

    收紧的办法不是"更努力地描述风格"，而是**把风格的描述再削一层**，
    只留材质 —— 模型改图的冲动主要来自提示词里那些可以被它"发挥"的词。
    """
    base = state.get("prompt") or ""
    n = (state.get("tighten") or 0) + 1
    extra = ("\n\nIMPORTANT: do not alter geometry, borders, fills, labels or "
             "colours in any way. Apply ONLY a subtle paper texture and a slight "
             "tone shift. If unsure, change less.")
    return {
        "prompt": base + extra,
        "tighten": n,
        "log": [f"收紧提示词（第 {n} 次）：只保留材质，禁止任何几何改动，重新生成"],
    }


def extract_material(state: StyleState) -> dict:
    """从**通过闸门**的样张里抽出确定性的材质层。

    这是「让模型只跑一两次」的关键：模型出的样张只用来定"质感长什么样"，
    一旦抽成参数（调色映射 + 纹理网格 + 颗粒 + 暗角），后面成百上千帧
    就全部由代码套用，模型不再参与 —— 可复现、不按帧计费、不会漂移。
    """
    import style_from_image as SFI
    try:
        prof = SFI.extract_style(state["styled_path"], verbose=False)
    except Exception as e:
        return {"material": {}, "verdict": "cv_only",
                "log": [f"从样张抽材质失败：{type(e).__name__}: {e}"]}
    prof.pop("_texture_grid", None)
    mode = state.get("mode") or "texture"
    return {
        "material": prof,
        "verdict": "textured" if mode != "edit" else "generated",
        "log": [f"从合成结果抽出材质层：{len(prof.get('palette') or [])} 个主色，"
                f"颗粒 {prof.get('grain', 0):.4f}，暗角 {prof.get('vignette', 0):.3f}"],
    }


def finalize(state: StyleState) -> dict:
    """收尾。把代码提取与模型提取合成一份可用的结果。"""
    verdict = state.get("verdict") or "cv_only"
    v = state.get("vlm") or {}
    cv = state.get("cv_profile") or {}
    msg = {
        "textured": "已用模型生成纸纹并把代码绘制的地图贴上去（内容零改动）",
        "generated": "已用模型生成质感样张并通过保真度闸门",
        "cv_only": "未采用模型生成（不可用或没通过闸门），改用纯代码提取的材质",
    }.get(verdict, verdict)
    return {"log": [f"完成：{msg}"]}


# ══════════════════════════════════════════════════════════════
#  条件边
# ══════════════════════════════════════════════════════════════
def after_vlm(state: StyleState) -> str:
    """有提示词且有底图才去生成，否则直接收尾。"""
    if not (state.get("prompt") or "").strip():
        return "skip"
    if (state.get("mode") or "texture") == "edit":
        return "edit" if state.get("base_path") else "skip"
    return "texture"


def after_gate(state: StyleState) -> str:
    if (state.get("fidelity") or {}).get("ok"):
        return "accept"
    if (state.get("attempts") or 0) < MAX_GEN_ATTEMPTS:
        return "retry"
    return "give_up"


def build_style_graph():
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(StyleState)
    g.add_node("cv_extract", cv_extract)
    g.add_node("vlm_describe", vlm_describe)
    # 安全那一路：模型只画纸纹，地图由代码贴上去
    g.add_node("generate_texture", generate_texture)
    g.add_node("texture_blank", texture_blank)
    g.add_node("composite_texture", composite_texture)
    g.add_node("content_untouched", content_untouched)
    # 图生图那一路（实测会毁中文标注，保留是为了能复现和对照）
    g.add_node("generate_sample", generate_sample)
    g.add_node("fidelity_gate", fidelity_gate)
    g.add_node("tighten_prompt", tighten_prompt)
    g.add_node("extract_material", extract_material)
    g.add_node("finalize", finalize)

    g.add_edge(START, "cv_extract")
    g.add_edge("cv_extract", "vlm_describe")
    g.add_conditional_edges("vlm_describe", after_vlm,
                            {"texture": "generate_texture",
                             "edit": "generate_sample",
                             "skip": "finalize"})
    # 纸纹这一路：生成 → 查空白度 →（不过就换个种子重来）→ 合成 → 查内容零改动
    g.add_edge("generate_texture", "texture_blank")
    g.add_conditional_edges("texture_blank", after_blank,
                            {"composite": "composite_texture",
                             "retry": "generate_texture",
                             "give_up": "finalize"})
    g.add_edge("composite_texture", "content_untouched")
    g.add_conditional_edges("content_untouched", after_gate,
                            {"accept": "extract_material",
                             "retry": "generate_texture",
                             "give_up": "finalize"})
    # 图生图这一路
    g.add_edge("generate_sample", "fidelity_gate")
    g.add_conditional_edges("fidelity_gate", after_gate,
                            {"accept": "extract_material",
                             "retry": "tighten_prompt",
                             "give_up": "finalize"})
    g.add_edge("tighten_prompt", "generate_sample")   # 重试环
    g.add_edge("extract_material", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = None


def run_style_graph(ref_path: str, base_path: str = "", vlm_cfg: LLMConfig | None = None,
                    img_key: str = "", img_model: str = "", out_path: str = "",
                    mode: str = "texture", verbose: bool = True,
                    img_base: str = "",
                    prompt_override: str = "") -> tuple[dict, list[str]]:
    """跑一遍风格图。返回 (结果, 过程记录)。

    mode:
      "texture"（默认）—— 模型只画纸纹，地图由代码贴上去，内容零改动。
      "edit"           —— 图生图直接编辑地图。**实测会毁掉中文标注**
                          （NCC 0.84、`所属政权` 变 `所鹰政权`），
                          保留它只是为了能复现和对照，不要用于出片。

    img_base：图像模型的接口地址。豆包 Seedream 在火山方舟、
    Qwen-Image 在硅基流动，**两家的模型名互不相认**，所以必须由调用方给。
    """
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_style_graph()
    out = _GRAPH.invoke({
        "ref_path": ref_path, "base_path": base_path or "",
        "vlm_cfg": vlm_cfg or LLMConfig(), "img_key": img_key,
        "img_model": img_model, "img_base": img_base,
        "prompt_override": prompt_override,
        "out_path": out_path, "mode": mode,
        "attempts": 0, "tighten": 0, "log": [],
    }, {"recursion_limit": 60})
    log = list(out.get("log") or [])
    if verbose:
        for line in log:
            print("  " + line)
    v = out.get("vlm") or {}
    cv = out.get("cv_profile") or {}
    res = {
        "mode": mode,
        "style_desc": v.get("style_desc") or "",
        "image_prompt": (v.get("image_prompt") or ""),
        "tags": v.get("tags") or [],
        "is_light": v.get("is_light"),
        "vlm_palette": (v.get("palette") or [])[:8],
        "palette": ((v.get("palette") or [])[:8]) or (cv.get("palette") or []),
        "cv_profile": cv,
        "material": out.get("material") or {},
        "blank": out.get("blank") or {},
        "fidelity": out.get("fidelity") or {},
        "verdict": out.get("verdict") or "cv_only",
        "styled_path": out.get("styled_path") or "",
        "texture_path": out.get("texture_path") or "",
        "negative_prompt": STYLE_NEGATIVE_PROMPT,
        "log": log,
    }
    return res, log
