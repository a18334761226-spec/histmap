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


class ModelAuthError(SystemExit):
    """模型鉴权失败（401/403）。

    单独一个异常类型，是因为调用方需要精确区分「key 不对」和「别的错」。
    继承 SystemExit 是为了兼容已有的 except SystemExit 分支。
    """


@dataclass
class LLMConfig:
    """一次模型调用的全部参数。key 为空表示没配。"""
    key: str = ""
    model: str = "Qwen/Qwen2.5-72B-Instruct"
    base: str = "https://api.siliconflow.cn/v1"
    temperature: float = 0.2
    timeout: int = 180
    # 记下这次调用用的是哪来的 key，出错时要能说清（"你填的" / "服务端 .env 的"）
    label: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return bool(self.key and self.model)


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


def chat_json(cfg: LLMConfig, prompt: str) -> dict:
    """发一次对话，要求回 JSON 对象。

    走 langchain-openai 的 ChatOpenAI（OpenAI 兼容协议），
    好处是超时/重试/错误类型都由它归一化，不用自己拼 HTTP。
    """
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
        llm = ChatOpenAI(**kwargs)
    finally:
        if fixed:
            os.environ["NO_PROXY"] = fixed
            os.environ["no_proxy"] = fixed
    msgs = [{"role": "user", "content": prompt}]
    try:
        # response_format 有些服务商不认，认不认都不影响下面的兜底解析
        try:
            resp = llm.bind(response_format={"type": "json_object"}).invoke(msgs)
        except Exception:
            resp = llm.invoke(msgs)
    except Exception as e:
        raise _as_error(e, cfg)

    txt = getattr(resp, "content", resp)
    if isinstance(txt, list):                      # 少数后端回 content 数组
        txt = "".join(str(x.get("text", x)) if isinstance(x, dict) else str(x)
                      for x in txt)
    return _extract_json(str(txt))


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
    if status is None and "Timeout" in name:
        return SystemExit(f"模型调用超时{where} —— {body}")
    return SystemExit(f"模型调用失败：{name}"
                      + (f" HTTP {status}" if status else "") + f" {where} —— {body}")
