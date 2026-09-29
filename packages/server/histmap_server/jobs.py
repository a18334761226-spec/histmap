#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
后台任务 · 视频渲染
========================
出片是分钟级的，不能卡住 HTTP 请求，所以走「提交 → 轮询」两段式。

设计上刻意做得很小：
  · 单进程内存队列，不引入 Redis/Celery（本地工具，不需要）
  · 任务保留最近 N 条，产物落在 output/jobs/<id>/
  · 进度按「已渲染帧数 / 总帧数」上报，前端可直接显示
"""
from __future__ import annotations

import os
import threading
import time
import traceback
import uuid
from collections import OrderedDict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
JOBS_DIR = os.path.join(ROOT, "output", "jobs")

MAX_JOBS = 20
_LOCK = threading.Lock()
_JOBS: "OrderedDict[str, dict]" = OrderedDict()


def new_job(kind: str, params: dict) -> str:
    jid = uuid.uuid4().hex[:12]
    with _LOCK:
        _JOBS[jid] = {
            "id": jid, "kind": kind, "params": params,
            "status": "queued", "progress": 0.0, "message": "排队中",
            "result": None, "error": None,
            "created": time.time(),
        }
        while len(_JOBS) > MAX_JOBS:
            _JOBS.popitem(last=False)
    os.makedirs(os.path.join(JOBS_DIR, jid), exist_ok=True)
    return jid


def update(jid: str, **kw):
    with _LOCK:
        if jid in _JOBS:
            _JOBS[jid].update(kw)


def get(jid: str):
    with _LOCK:
        j = _JOBS.get(jid)
        return dict(j) if j else None


def list_jobs():
    with _LOCK:
        return [dict(j) for j in _JOBS.values()]


def run_async(fn, *args, **kwargs):
    t = threading.Thread(target=_guard, args=(fn, args, kwargs), daemon=True)
    t.start()
    return t


def _guard(fn, args, kwargs):
    try:
        fn(*args, **kwargs)
    except Exception as e:
        traceback.print_exc()
        jid = kwargs.get("jid") or (args[0] if args else None)
        if isinstance(jid, str):
            update(jid, status="failed", error=f"{type(e).__name__}: {e}")


def job_dir(jid: str) -> str:
    d = os.path.join(JOBS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    return d
