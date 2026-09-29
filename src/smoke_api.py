"""接口冒烟自检：把 web 端会用到的每个接口都真跑一遍。

用法（服务已在 8810 跑着）：
    python src/smoke_api.py
    python src/smoke_api.py --base http://127.0.0.1:8810 --video

为什么要有这个文件：这个项目的 bug 几乎全是「接口看着在、其实没被跑过」——
视频那条路从第一版起就是坏的（run_async 把 jid 传了两遍），
破图是 URL 少了 api/ 一段，风格上传是 Content-Type 写死成 json。
这些只要真跑一次就全暴露了，所以别再靠肉眼看代码。

--video 会真渲染区间视频（几十秒），默认跳过；其余接口默认全测。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

OK, BAD = [], []


def call(base, path, method="GET", body=None, raw=None, ctype=None, timeout=600):
    url = base.rstrip("/") + path
    data, headers = None, {}
    if raw is not None:
        data = raw
        headers["Content-Type"] = ctype or "application/octet-stream"
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            ct = r.headers.get("Content-Type") or ""
            if "json" in ct:
                return r.status, json.loads(payload.decode("utf-8"))
            return r.status, payload
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_err": f"{type(e).__name__}: {e}"}


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if detail else ""))
    return cond


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8810")
    ap.add_argument("--video", action="store_true", help="连区间视频一起测（慢）")
    ap.add_argument("--style-image", default=None, help="给一张参考图，测风格提取")
    args = ap.parse_args()
    B = args.base

    print(f"== 冒烟自检 {B} ==")

    # ── 1) 健康与元数据 ──
    st, h = call(B, "/api/health")
    check("/api/health 通", st == 200 and h.get("ok"), f"status={st}")
    check("几何没有过期", not h.get("stale"), f"stale={h.get('stale')}")

    st, sc = call(B, "/api/scenes")
    scenes = sc.get("scenes") if isinstance(sc, dict) else sc
    check("/api/scenes 有题材", st == 200 and scenes, f"n={len(scenes or [])}")

    st, stl = call(B, "/api/styles")
    presets = (stl or {}).get("presets") or []
    check("/api/styles 有预设", st == 200 and presets, f"n={len(presets)}")
    # 前端下拉框依赖这些 id，缺了就是空下拉
    check("/api/styles 含 none 与做旧预设",
          {"none"} <= {p.get("id") for p in presets} and len(presets) >= 2,
          str([p.get("id") for p in presets]))

    st, md = call(B, "/api/models")
    check("/api/models 能用", st == 200)

    st, page = call(B, "/")
    check("首页 / 返回 HTML", st == 200 and b"<html" in page.lower())

    # ── 2) 每个题材都渲染一张（这是核心路径，必须逐个过）──
    for s in (scenes or []):
        sid, ds = s["id"], s.get("dates") or []
        if not ds:
            check(f"[{sid}] 有可渲染日期", False, "dates() 为空 —— 几何文件缺失？")
            continue
        d = s.get("default_date") or ds[len(ds) // 2]
        st, r = call(B, "/api/render", "POST",
                     {"scene": sid, "date": d, "theme": (s.get("themes") or ["dark"])[0],
                      "size": "16x9", "style": "none"}, timeout=900)
        ok = st == 200 and r.get("image")
        check(f"[{sid}] 渲染 {d}", ok, f"{r.get('image') or r}")
        if ok:
            # 图片 URL 必须真能取回来 —— 破图就是这么漏过去的
            st2, blob = call(B, r["image"], timeout=120)
            check(f"[{sid}] 图片可下载", st2 == 200 and len(blob or b"") > 20000,
                  f"status={st2} bytes={len(blob or b'')}")
            # 取回来的必须是真 PNG，不是错误页
            check(f"[{sid}] 是 PNG", (blob or b"")[:8] == b"\x89PNG\r\n\x1a\n")

    # ── 3) 风格提取 ──
    img = args.style_image
    if not img:
        cand = os.path.join("site", "assets")
        if os.path.isdir(cand):
            for f in sorted(os.listdir(cand)):
                if f.lower().endswith((".png", ".jpg", ".jpeg")):
                    img = os.path.join(cand, f)
                    break
    if img and os.path.exists(img):
        with open(img, "rb") as f:
            blob = f.read()
        # 故意不设 Content-Type（前端 FormData 就是这样）—— 后端不该依赖它
        boundary = "----smoke"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{os.path.basename(img)}\"\r\nContent-Type: image/png\r\n\r\n"
                ).encode() + blob + f"\r\n--{boundary}--\r\n".encode()
        st, r = call(B, "/api/style/extract", "POST", raw=body,
                     ctype=f"multipart/form-data; boundary={boundary}", timeout=600)
        check("风格提取（含无 Content-Type 依赖）", st == 200 and r.get("profile"),
              f"status={st} {str(r)[:120]}")
    else:
        print("  SKIP  风格提取（没找到测试图，用 --style-image 指定）")

    # ── 4) 区间视频 ──
    if args.video:
        for sid in ("tang", "song", "ww2-europe"):
            s = next((x for x in (scenes or []) if x["id"] == sid), None)
            if not s:
                continue
            ds = s["dates"] or []
            body = {"scene": sid, "date_from": ds[0], "date_to": ds[-1],
                    "theme": (s.get("themes") or ["dark"])[0], "size": "16x9",
                    "style": "none", "hold": 0.5}
            st, r = call(B, "/api/video", "POST", body, timeout=120)
            if not check(f"[{sid}] 视频任务提交", st == 200 and r.get("job"), str(r)[:120]):
                continue
            jid, want = r["job"], r.get("n_dates")
            # 上限是防手滑的，不是暗坑：要么全出，要么在 note 里明说抽稀了
            expect = min(len(ds), r.get("max_frames") or len(ds))
            check(f"[{sid}] 帧数=min(区间点数, 上限) 且与声明一致",
                  want == expect, f"n_dates={want} 期望={expect}")
            if len(ds) > expect:
                check(f"[{sid}] 抽稀时说清了", bool(r.get("note")), str(r.get("note")))
            j = {}
            t0 = time.time()
            while time.time() - t0 < 1800:
                time.sleep(2)
                _, j = call(B, f"/api/jobs/{jid}", timeout=60)
                if j.get("status") in ("done", "failed"):
                    break
            check(f"[{sid}] 视频任务跑完", j.get("status") == "done", str(j.get("error") or "")[:160])
            res = j.get("result") or {}
            check(f"[{sid}] 帧数齐全", res.get("n_frames") == want,
                  f"n_frames={res.get('n_frames')}")
            st2, blob = call(B, res.get("video") or "/nonexistent", timeout=300)
            check(f"[{sid}] MP4 可下载且完整", st2 == 200 and (blob or b"")[4:8] == b"ftyp",
                  f"status={st2} bytes={len(blob or b'')}")
    else:
        print("  SKIP  区间视频（加 --video 开启）")

    # ── 5) 对话（需要真实 key，从 .env 读；没有就只测错误分支）──
    key = os.environ.get("SMOKE_KEY") or ""
    if not key:
        envp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
        if os.path.exists(envp):
            for line in open(envp, encoding="utf-8"):
                if line.strip().startswith("SILICONFLOW_API_KEY="):
                    key = line.split("=", 1)[1].strip()
    st0 = {"scene": "song", "date": "1080-01-01", "theme": "light",
           "size": "16x9", "style": "none"}

    st, r = call(B, "/api/chat", "POST", {"messages": [{"role": "user", "content": "hi"}], "state": st0})
    check("/api/chat 无 key 时给 401 而不是 500", st == 401, f"status={st} {str(r)[:100]}")

    if not key:
        print("  SKIP  对话真实调用（没找到 .env 里的 key）")
    else:
        cases = [("我要唐朝宪宗二年的藩镇图", "tang", "807-01-01"),
                 ("换成唐朝", None, None),
                 ("看一下北宋末年", "song", None),
                 ("给我出1943年的欧洲", "ww2-europe", None)]
        for q, want_scene, want_date in cases:
            body = json.dumps({"messages": [{"role": "user", "content": q}], "state": st0})
            req = urllib.request.Request(
                B.rstrip("/") + "/api/chat", data=body.encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Api-Key": key},
                method="POST")
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                ok = bool(out.get("state"))
            except Exception as e:
                out, ok = {}, False
                print(f"    （对话调用失败：{type(e).__name__}: {e}）")
            s = out.get("state") or {}
            cond = ok and (want_scene is None or s.get("scene") == want_scene) \
                 and (want_date is None or s.get("date") == want_date)
            check(f"对话「{q}」→ 可渲染参数", cond,
                  f"scene={s.get('scene')} date={s.get('date')}")
            # 关键：模型给的日期必须真能渲染，否则界面一点就报错
            if s.get("scene") and s.get("date"):
                st2, r2 = call(B, "/api/render", "POST",
                               {"scene": s["scene"], "date": s["date"],
                                "theme": s.get("theme") or "light",
                                "size": s.get("size") or "16x9", "style": "none"},
                               timeout=900)
                check(f"对话「{q}」给的日期真能渲染", st2 == 200 and r2.get("image"),
                      str(r2)[:120])

    print(f"\n== 结果：{len(OK)} 通过 / {len(BAD)} 失败 ==")
    for b in BAD:
        print(f"   x {b}")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
