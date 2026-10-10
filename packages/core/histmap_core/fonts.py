"""找一个能画中文的字体。**全项目只此一份**。

为什么必须收敛到一份
--------------------
这个 bug 已经发生过一次，而且是在云端才炸的：`topics.py` 的候选列表里有
Linux 路径，`render.py` 里只有 `C:\\Windows\\Fonts\\...` 四条。于是在 Windows 上
一切正常，一进容器两个模块走的是不同的回退链 —— `render.py` 找不到字体就
`ImageFont.load_default()`，那是 PIL 自带的**位图字体，画不出中文**，
结果整张图的地图色块完美、**所有中文标注全部消失**（不是方框，是没有）。

最坑的是它**不报错**：`/api/health` 200、渲染 200、图片 1920×1080、
独特颜色两万多种 —— 所有自动化检查都是绿的。只有人眼看图才发现标题没了。
所以这里除了把路径写全，还加了一个 `can_render_cjk()` 判定，
并把结果暴露给 `/api/health`，让这种"静默降级"至少能被脚本查出来。
"""
from __future__ import annotations

import glob
import os

# 各平台常见位置。Linux 各发行版/版本路径不一样，所以后面还有 glob 兜底，
# 不能只靠这张表。
EXPLICIT = (
    # Windows
    r"C:\Windows\Fonts\msyh.ttc",          # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",        # 黑体
    r"C:\Windows\Fonts\simsun.ttc",        # 宋体
    r"C:\Windows\Fonts\Deng.ttf",          # 等线
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux：Debian/Ubuntu 的 fonts-noto-cjk
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # Linux：其它常见中文字体包
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
)

# glob 兜底：发行版路径千奇百怪，列不完
PATTERNS = (
    "/usr/share/fonts/**/*CJK*.ttc",
    "/usr/share/fonts/**/*CJK*.otf",
    "/usr/share/fonts/**/*CJK*.ttf",
    "/usr/share/fonts/**/wqy*.ttc",
    "/usr/share/fonts/**/*Hei*.ttf",
    "/usr/share/fonts/**/*Ming*.ttf",
    "/usr/share/fonts/**/*Yuan*.ttf",
    # 最后才考虑无中文的字体：能画字总比画不出好
    "/usr/share/fonts/**/DejaVuSans.ttf",
    "/usr/share/fonts/**/LiberationSans-Regular.ttf",
)

# 用来验证「这个字体到底能不能画中文」。挑一个只有 CJK 字体才有的字。
_PROBE = "唐"
_warned = False
_resolved: dict = {}


def can_render_cjk(font) -> bool:
    """这个字体画得出中文吗。画不出就是 load_default 那种位图字体。"""
    try:
        box = font.getbbox(_PROBE)
        return bool(box) and (box[2] - box[0]) > 0
    except Exception:
        return False


def find_font(size: int, prefer: str = ""):
    """找一个 size 号、能画中文的字体。找不到也不会抛，但会**大声说一次**。"""
    global _warned
    from PIL import ImageFont

    size = max(1, int(size))
    key = (size, prefer or "")
    if key in _resolved:
        return _resolved[key]

    cands = []
    if prefer:
        cands.append(prefer)
    cands += list(EXPLICIT)
    for pat in PATTERNS:
        cands += sorted(glob.glob(pat, recursive=True))

    fallback = None
    for cand in cands:
        if not cand or not os.path.exists(cand):
            continue
        try:
            f = ImageFont.truetype(cand, size)
        except Exception:
            continue
        if can_render_cjk(f):
            _resolved[key] = f
            return f
        if fallback is None:
            fallback = f          # 留着，实在没有中文就是它

    if fallback is not None:
        _resolved[key] = fallback
        if not _warned:
            _warned = True
            print(f"[字体] 警告：只找到 {getattr(fallback, 'path', '?')}，"
                  f"它画不了中文 —— 图上的中文标注会消失。"
                  f"Linux 上装 fonts-noto-cjk 即可（Dockerfile 里已装）。")
        return fallback

    _resolved[key] = ImageFont.load_default()
    if not _warned:
        _warned = True
        print("[字体] 警告：一个中文字体都没找到，中文标注会消失。"
              "Linux 上装 fonts-noto-cjk（Dockerfile 里已装）。")
    return _resolved[key]


def font_report() -> dict:
    """给 /api/health 用：现在这套环境到底能不能画中文。

    这个函数存在的意义就是「让静默降级变得可观测」—— 那次云端事故里
    所有接口都是 200，只有人眼看图才知道中文全没了。
    """
    f = find_font(20)
    ok = can_render_cjk(f)
    return {"ok": ok, "path": getattr(f, "path", "(PIL 默认位图字体)"),
            "family": (f.getname()[0] if hasattr(f, "getname") else "?")}
