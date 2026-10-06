#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验一下 .env 里的 key 到底还能不能用。

为什么要有它：界面上「让模型起草」失败时只给一句 HTTP 401，看不出是
key 失效、base URL 写错、还是模型名不存在。这个脚本把每一步拆开打状态码，
一次定位到底卡在哪 —— 而不是让人对着界面上的 401 猜。

用法：
    python src/check_key.py                 # 读 .env，测硅基流动
    python src/check_key.py --provider 火山方舟 --key sk-xxx
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def load_env():
    p = os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def hit(url, key, payload=None, timeout=40):
    """返回 (状态码, 响应体前 300 字)。状态码 -1 表示网络层就失败了。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method="POST" if data else "GET",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="硅基流动")
    ap.add_argument("--key", default="")
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
    from histmap_server.app import MODELS

    env = load_env()
    key = args.key or env.get("SILICONFLOW_API_KEY", "")
    if not key:
        print("✗ .env 里没有 SILICONFLOW_API_KEY，也没有 --key")
        return 1

    print(f"key: {key[:6]}…{key[-4:]}  长度 {len(key)}")
    # MODELS 的形状是 {"chat": [ {...}, ... ], ...}，不是 {模型名: {...}}
    entries = [m for m in MODELS.get("chat", []) if m.get("svc") == args.provider]
    if not entries:
        print(f"✗ MODELS 里没有服务商「{args.provider}」")
        print("  可选：" + "、".join(sorted({m.get("svc", "?")
                                            for m in MODELS.get("chat", [])})))
        return 1
    base = entries[0]["base"].rstrip("/")
    model = args.model or entries[0]["id"]
    print(f"服务商 {args.provider}   base {base}   模型 {model}\n")

    print("① 列模型（只验鉴权，不花 token）")
    code, body = hit(f"{base}/models", key)
    print(f"   HTTP {code}  {body[:200]}")
    if code == 401:
        print("   -> key 本身被拒。去服务商后台确认这个 key 还在、没被删/轮换。")
        return 1
    if code == -1:
        print("   -> 网络层就没通，先解决连通性（代理/DNS）。")
        return 1

    print("\n② 真的发一次对话（花一点点 token，但能验模型名）")
    code, body = hit(f"{base}/chat/completions", key, {
        "model": model,
        "messages": [{"role": "user", "content": "只回两个字：收到"}],
        "max_tokens": 16})
    print(f"   HTTP {code}  {body[:240]}")
    if code == 200:
        print("\n✓ key 和模型都可用。")
        return 0
    if code == 404:
        print(f"\n✗ key 没问题，但模型名 {model} 在这个服务商上不存在 —— 换模型名。")
    elif code == 401:
        print("\n✗ key 被拒。")
    elif code in (402, 403):
        print("\n✗ 鉴权过了但被拒（余额/权限/实名）。去后台看余额。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
