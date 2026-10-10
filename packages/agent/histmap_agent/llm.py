"""LLM 客户端 —— 全项目**唯一**一处分派模型调用的地方。

为什么单独抽出来：这套系统里跟模型有关的行为只有三种（起草题材、对话改图、
读参考图写风格描述），但它们都必须遵守同一组约定：

  · **鉴权失败要能被精确识别**。服务端要靠这个决定「用户填的 Key 被拒了，
    回退到 .env 里那个」；靠错误文本里有没有 "Key" 这个子串判断是不行的，
    早先 401 就因此落进普通错误分支，界面上只留一句英文原文。
  · **服务商的原文要带出来**。401 也分「key 不存在」和「没余额」，
    只有它的响应体能说清。
  · **接口地址由调用方给**。界面上选了火山方舟/百炼，它们的 key 只能打
    自己的域名；这个参数一度不存在，别家的 key 被发到硅基流动，必然 401。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field


class ModelAuthError(SystemExit, Exception):
    """模型鉴权/余额失败（401/403/402）。

    单独一个异常类型，是因为调用方需要精确区分「key 不对」和「别的错」。

    **为什么同时继承 SystemExit 和 Exception**：只继承 SystemExit 会踩一个很隐蔽的坑 ——
    SystemExit 属于 BaseException，**不是 Exception**，所以写 `except Exception`
    的兜底分支根本接不住它。实测就是这么炸的：风格图里 `vlm_describe` 明明写了
    「视觉模型不可用就退回纯代码」的 except Exception，余额不足时却一路冒到
    FastAPI，界面上是 500，日志里才看得出是 402。
    两个都继承之后，`except SystemExit`（命令行那套）和 `except Exception`
    （兜底那套）都能接住，不会再有地方悄悄漏掉它。
    """


# 余额不足单独一个类型：Key 和模型名都对，只是账户没钱了。
# 界面上要能把这件事说清楚，不能混进「Key 不对」里让人白折腾。
class ModelBalanceError(ModelAuthError):
    """HTTP 402 / 余额不足。"""


@dataclass
class LLMConfig:
    """一次模型调用的全部参数。key 为空表示没配。

    **base 和 model 都不给厂商默认值**（原来默认写死硅基流动）。
    理由：这个项目不替用户选厂商。以前默认值是硅基流动，于是「用户在界面选了
    火山方舟（豆包）」时，任何一处没拿到调用方参数的代码路径都会悄悄打回硅基 ——
    表现就是「我让你用豆包，你怎么老去找硅基」。现在默认是空：
    没配就是没配，会明确报「没配 Key / 没指定服务商」，而不是偷偷换一家。
    """
    key: str = ""
    model: str = ""
    base: str = ""
    temperature: float = 0.2
    timeout: int = 180
    # 记下这次调用用的是哪来的 key，出错时要能说清（"你填的" / "服务端 .env 的"）
    label: str = ""
    provider: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.key and self.model and self.base)


# ── 服务商登记表：**全项目唯一一处**「哪家叫什么、地址是什么」──────────
# 加一家只改这里，不在别处写死任何厂商。env 是「去哪几个环境变量里找 key」，
# 按顺序取第一个非空的。
PROVIDERS: dict = {
    "火山方舟": {
        "base": "https://ark.cn-beijing.volces.com/api/v3",
        "env": ("ARK_API_KEY", "VOLC_API_KEY", "VOLCENGINE_API_KEY",
                "DOUBAO_API_KEY"),
        "model": "doubao-seed-1-6-250615",
    },
    "DeepSeek 官方": {
        "base": "https://api.deepseek.com",
        "env": ("DEEPSEEK_API_KEY",),
        "model": "deepseek-flash",
    },
    "硅基流动": {
        "base": "https://api.siliconflow.cn/v1",
        "env": ("SILICONFLOW_API_KEY", "SF_API_KEY"),
        "model": "Qwen/Qwen2.5-72B-Instruct",
    },
    "阿里百炼": {
        "base": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env": ("DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "QWEN_API_KEY"),
        "model": "qwen-plus",
    },
    "智谱": {
        "base": "https://open.bigmodel.cn/api/paas/v4",
        "env": ("ZHIPU_API_KEY", "GLM_API_KEY"),
        "model": "glm-4-plus",
    },
    "魔搭": {
        "base": "https://api-inference.modelscope.cn/v1",
        "env": ("MODELSCOPE_TOKEN", "MODELSCOPE_API_KEY"),
        "model": "Qwen/Qwen2.5-72B-Instruct",
    },
}


def _env_file() -> dict:
    """读仓库根目录的 .env（只在需要时读，结果不缓存 —— 用户可以随时改）。"""
    import os as _os
    here = _os.path.dirname(_os.path.abspath(__file__))
    root = _os.path.dirname(_os.path.dirname(_os.path.dirname(here)))
    p = _os.path.join(root, ".env")
    out: dict = {}
    if _os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def server_default(provider: str = "", model: str = "") -> LLMConfig:
    """服务端默认用哪家、哪个模型。**不偏向任何厂商。**

    优先级：
      1. 显式传入的 provider / model
      2. `.env` 里的 HISTMAP_PROVIDER（想固定用哪家就写它）
      3. `.env` / 环境变量里**唯一**配了 key 的那一家
      4. 配了多家又没指定 → 报错，让人明确选一家
             （以前这里会默默挑硅基流动，于是「我让你用豆包」变成「你怎么老去找硅基」）

    `.env` 里认的通用名（优先级最高，用它就不必记各家的变量名）：
        HISTMAP_API_KEY / HISTMAP_BASE / HISTMAP_MODEL / HISTMAP_PROVIDER
    """
    import os
    env = dict(_env_file())
    env.update({k: v for k, v in os.environ.items() if k not in env})

    gen_key = env.get("HISTMAP_API_KEY") or ""
    gen_base = (env.get("HISTMAP_BASE") or "").rstrip("/")
    gen_model = env.get("HISTMAP_MODEL") or ""
    if gen_key:
        return LLMConfig(key=gen_key, base=gen_base or "",
                         model=model or gen_model,
                         provider=provider or env.get("HISTMAP_PROVIDER") or "",
                         label=".env 的 HISTMAP_API_KEY")

    want = provider or env.get("HISTMAP_PROVIDER") or ""
    if want:
        p = PROVIDERS.get(want)
        if not p:
            raise SystemExit(
                f".env 里指定的 HISTMAP_PROVIDER=「{want}」不认识。"
                f"可选：{'、'.join(PROVIDERS)}")
        for e in p["env"]:
            if env.get(e):
                return LLMConfig(key=env[e], base=p["base"],
                                 model=model or gen_model or p["model"],
                                 provider=want, label=f".env 的 {e}")
        raise SystemExit(
            f"指定了用「{want}」，但 .env 里没有它的 Key。"
            f"请填 {' 或 '.join(p['env'])} 之一。")

    have = []
    for name, p in PROVIDERS.items():
        for e in p["env"]:
            if env.get(e):
                have.append((name, e, env[e], p))
                break
    if not have:
        raise SystemExit(
            "服务端没有配任何模型的 Key。任选一家填进 .env（推荐用通用名）：\n"
            "    HISTMAP_API_KEY=你的key\n"
            "    HISTMAP_BASE=https://ark.cn-beijing.volces.com/api/v3\n"
            "    HISTMAP_MODEL=doubao-seed-1-6-250615\n"
            f"或按厂商名填：{'、'.join(PROVIDERS)}")
    if len(have) > 1:
        raise SystemExit(
            "服务端配了多家的 Key，但没说要默认用哪家 —— 我不替你挑"
            f"（以前会默默挑硅基流动，那正是「选了豆包却打到硅基」的原因）。\n"
            f"请在 .env 里加一行指定：\n"
            f"    HISTMAP_PROVIDER={' 或 '.join(n for n, _, _, _ in have)}\n"
            f"目前配置了的：{'、'.join(f'{n}({e})' for n, e, _, _ in have)}")
    name, e, k, p = have[0]
    return LLMConfig(key=k, base=p["base"], model=model or gen_model or p["model"],
                     provider=name, label=f".env 的 {e}")



def _strip_fence(txt: str) -> str:
    """模型爱把 JSON 包在 ``` 里，即使说了不要围栏。"""
    t = (txt or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        if t.rstrip().endswith("```"):
            t = t.rstrip()[:-3]
    return t.strip()


def _extract_json(txt: str) -> dict:
    """从模型的回复里抠出一个 JSON 对象。

    先按整体解析；不成再退化成「第一个 { 到最后一个 }」——
    模型经常在 JSON 前后加一句"好的，以下是…"，那不该算失败。
    """
    t = _strip_fence(txt)
    try:
        return json.loads(t)
    except Exception:
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except Exception:
            pass
    raise SystemExit(f"模型没有返回可解析的 JSON。原文前 300 字：\n{t[:300]}")


def _sanitize_proxy_env() -> str:
    """把 NO_PROXY 改写成 httpx 能解析的形式。返回改写前的值（没改就返回空）。

    踩过的坑：这台机器的 NO_PROXY 是
        localhost,127.0.0.1,::1,[::1]
    httpx 0.28 解析不了带方括号的 IPv6 写法，在**创建客户端的第一行**就
    `InvalidURL: Invalid port: ':1]'` 崩掉 —— 而 urllib 是容忍的，
    所以只有走 SDK 的那条路会炸，报错信息里完全看不出跟代理有关。
    更麻烦的是这个客户端是 SDK **内部**建的：给它传 http_client 也没用，
    它还会另建一个（实测）。所以只能从环境变量这一层修。

    改写是安全的：这些条目的作用只是「访问本机地址时绕过代理」，
    而模型接口在公网，本来就不该绕过；去掉方括号那种写法不影响任何实际行为。
    """
    old = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if "[" not in old:
        return ""
    keep = []
    for item in old.split(","):
        it = item.strip()
        if not it or "[" in it or "]" in it:
            # 带方括号的 IPv6（httpx 解析不了）直接丢；它在 no_proxy 里
            # 只对「访问 ::1」有意义，而我们的接口地址不在本机
            continue
        keep.append(it)
    new = ",".join(keep) or "localhost,127.0.0.1"
    os.environ["NO_PROXY"] = new
    os.environ["no_proxy"] = new
    return old


def _http_client(cfg: "LLMConfig"):
    """自己建一个 httpx 客户端，显式带代理、不读环境。"""
    try:
        import httpx
    except ImportError:                            # pragma: no cover
        return None
    kw = {"trust_env": False, "timeout": float(cfg.timeout)}
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy"))
    if proxy:
        kw["proxy"] = proxy
    try:
        return httpx.Client(**kw)
    except Exception:
        kw.pop("proxy", None)                      # 代理串写坏了也不该整件事失败
        try:
            return httpx.Client(**kw)
        except Exception:
            return None


def _build_llm(cfg: LLMConfig):
    """建 ChatOpenAI。代理环境变量的坑见 _sanitize_proxy_env。"""
    if not cfg.key:
        raise ModelAuthError("没有可用的模型 Key")
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as e:                       # pragma: no cover
        raise SystemExit(
            "缺少 langchain-openai。装一下：python -m pip install langchain-openai"
        ) from e

    kwargs = dict(
        model=cfg.model,
        api_key=cfg.key,
        base_url=cfg.base,
        temperature=cfg.temperature,
        timeout=cfg.timeout,
        # 不在这里自动重试鉴权错误：401 重试一百次还是 401，只会让用户多等
        max_retries=1,
    )
    hc = _http_client(cfg)
    if hc is not None:
        kwargs["http_client"] = hc
    # 环境变量这一层也得修：SDK 内部还会自建客户端（给它传 http_client 也拦不住）
    fixed = _sanitize_proxy_env()
    try:
        return ChatOpenAI(**kwargs)
    finally:
        if fixed:
            os.environ["NO_PROXY"] = fixed
            os.environ["no_proxy"] = fixed


def _content_text(resp) -> str:
    txt = getattr(resp, "content", resp)
    if isinstance(txt, list):                      # 少数后端回 content 数组
        txt = "".join(str(x.get("text", x)) if isinstance(x, dict) else str(x)
                      for x in txt)
    return str(txt)


def chat_json(cfg: LLMConfig, prompt: str) -> dict:
    """发一次纯文字对话，要求回 JSON 对象。

    走 langchain-openai 的 ChatOpenAI（OpenAI 兼容协议），
    好处是超时/重试/错误类型都由它归一化，不用自己拼 HTTP。
    """
    llm = _build_llm(cfg)
    msgs = [{"role": "user", "content": prompt}]
    try:
        # response_format 有些服务商不认，认不认都不影响下面的兜底解析
        try:
            resp = llm.bind(response_format={"type": "json_object"}).invoke(msgs)
        except Exception:
            resp = llm.invoke(msgs)
    except Exception as e:
        raise _as_error(e, cfg)
    return _extract_json(_content_text(resp))


def _data_url(path: str) -> str:
    import base64
    ext = os.path.splitext(path)[1].lower().lstrip(".") or "png"
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png",
            "webp": "webp"}.get(ext, "png")
    with open(path, "rb") as f:
        return f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()


def chat_json_vision(cfg: LLMConfig, prompt: str, image_path: str) -> dict:
    """发一次**带图**的对话，要求回 JSON 对象。

    这就是「让模型读参考图」那一步。图片走 OpenAI 的 image_url + data URL 形式。
    """
    llm = _build_llm(cfg)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": _data_url(image_path)}},
    ]}]
    try:
        try:
            resp = llm.bind(response_format={"type": "json_object"}).invoke(msgs)
        except Exception:
            resp = llm.invoke(msgs)
    except Exception as e:
        raise _as_error(e, cfg)
    return _extract_json(_content_text(resp))


def _as_error(e: Exception, cfg: LLMConfig) -> Exception:
    """把各家 SDK 的异常归一成我们能分辨的两种。

    401/403 → ModelAuthError（调用方据此回退到别的 key）
    其它    → SystemExit，带上服务商原文，别让用户只看到一句英文。
    """
    name = type(e).__name__
    status = getattr(e, "status_code", None)
    body = ""
    resp = getattr(e, "response", None)
    if resp is not None:
        try:
            body = resp.text[:300]
        except Exception:
            body = ""
    if not body:
        body = str(e)[:300]
    where = f"（{cfg.label}，{cfg.model} @ {cfg.base}）" if cfg.label else \
            f"（{cfg.model} @ {cfg.base}）"
    if status in (401, 403) or "Authentication" in name or "PermissionDenied" in name:
        return ModelAuthError(f"模型鉴权失败：HTTP {status or '401/403'} {where} —— {body}")
    # 402 / 余额不足：**Key 和模型名都是对的**，只是账户没钱了。
    # 不单独认出来的话，界面上会说成「模型」或「Key」的问题，让人白改半天。
    if status == 402 or "insufficient" in body.lower() or "balance" in body.lower():
        return ModelBalanceError(
            f"模型账户余额不足{where} —— {body}。"
            f"列模型那类接口不花 token 所以还是 200，一发对话才是 402；"
            f"去服务商后台充值，不用改 Key 或模型名。")
    if status is None and "Timeout" in name:
        return SystemExit(f"模型调用超时{where} —— {body}")
    return SystemExit(f"模型调用失败：{name}"
                      + (f" HTTP {status}" if status else "") + f" {where} —— {body}")
