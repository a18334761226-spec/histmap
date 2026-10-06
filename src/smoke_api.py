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
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

OK, BAD = [], []


def call(base, path, method="GET", body=None, raw=None, ctype=None, timeout=600,
         headers=None):
    url = base.rstrip("/") + path
    data, hdrs = None, dict(headers or {})
    if raw is not None:
        data = raw
        hdrs.setdefault("Content-Type", ctype or "application/octet-stream")
    elif body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
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
    """报一条断言结果。

    detail 只在**失败**时打出来 —— 它写的是「为什么可能不对」，
    在 PASS 行后面跟着会读着自相矛盾（早先就出现过
    「PASS ... 参数被忽略了？」这种自打脸的输出）。
    想在通过时也看到数字，就把数字写进 name。
    """
    (OK if cond else BAD).append(name)
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"\n          {detail}" if (detail and not cond) else ""))
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

    # 质感小样：每个预设都该有一张真渲染出来的样图，而且是能下载的
    st, s2 = call(B, "/api/styles", timeout=900)
    presets2 = (s2 or {}).get("presets") or []
    missing = [p["id"] for p in presets2 if not p.get("preview")]
    check("每个质感预设都有真实小样", not missing, f"缺: {missing}")
    if presets2 and presets2[0].get("preview"):
        st3, blob3 = call(B, presets2[0]["preview"], timeout=120)
        check("质感小样可下载", st3 == 200 and len(blob3 or b"") > 3000,
              f"status={st3} bytes={len(blob3 or b'')}")

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

            # 文案覆盖必须真的改变画面。
            # 只检查「接口返回 200」是不够的 —— 参数被静默忽略也会返回 200。
            # 所以比像素：给了自定义标题的图必须和默认标题的图不一样。
            stt, rt = call(B, "/api/render", "POST",
                           {"scene": sid, "date": d, "title": "冒烟自检标题",
                            "theme": (s.get("themes") or ["dark"])[0],
                            "size": "16x9", "style": "none"}, timeout=900)
            if stt == 200 and rt.get("image"):
                _, blob_t = call(B, rt["image"], timeout=120)
                same = (blob_t == blob)
                check(f"[{sid}] 文案覆盖真的改变了画面", not same,
                      "自定义标题渲染出来的图和默认一模一样 —— 参数被忽略了？")
            else:
                check(f"[{sid}] 文案覆盖渲染", False, str(rt)[:120])

    # ── 3) 风格抽取 → 套用（用户要的主线，必须逐条断言）──
    # 造一张特征明确的参考图，保证每次跑结果可比、不依赖仓库里有没有素材
    refdir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "output", "style_refs")
    os.makedirs(refdir, exist_ok=True)
    ref = os.path.join(refdir, "smoke_ref.png")
    if not os.path.exists(ref):
        try:
            from PIL import Image, ImageDraw
            im = Image.new("RGB", (480, 300), (18, 42, 74))       # 冷青蓝图
            d = ImageDraw.Draw(im)
            for i in range(0, 300, 24):
                d.line([(0, i), (480, i)], fill=(30, 62, 104))
            for i in range(0, 480, 24):
                d.line([(i, 0), (i, 300)], fill=(30, 62, 104))
            d.rectangle([60, 60, 200, 140], outline=(150, 210, 240), width=3)
            im.save(ref)
        except Exception as e:
            print(f"  SKIP  风格主线（造参考图失败：{e}）")
            ref = None

    if ref and os.path.exists(ref):
        with open(ref, "rb") as f:
            blob = f.read()
        # 故意不设 Content-Type（前端 FormData 就是这样）—— 后端不该依赖它
        boundary = "----smoke"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{os.path.basename(ref)}\"\r\nContent-Type: image/png\r\n\r\n"
                ).encode() + blob + f"\r\n--{boundary}--\r\n".encode()
        st, r = call(B, "/api/style/extract", "POST", raw=body,
                     ctype=f"multipart/form-data; boundary={boundary}", timeout=600)
        prof = (r or {}).get("profile") or {}
        check("风格提取（含无 Content-Type 依赖）", st == 200 and prof,
              f"status={st} {str(r)[:120]}")
        miss = [k for k in ("palette", "lum_quantiles", "saturation", "grain",
                            "vignette", "white_balance_gain") if k not in prof]
        check("提取出的 profile 字段完整（前端要靠它回传）", not miss, f"缺 {miss}")

        s0 = next((x for x in (scenes or []) if x["id"] == "song"), None)
        ds0 = (s0 or {}).get("dates") or []
        if prof and ds0:
            d0 = ds0[len(ds0) // 2]
            th = (s0.get("themes") or ["light"])[0]
            imgs = {}
            for lab, extra in (("原图", {"style_profile": None}),
                               ("强度0", {"style_profile": prof, "strength": 0.0}),
                               ("强度1", {"style_profile": prof, "strength": 1.0})):
                st2, r2 = call(B, "/api/render", "POST",
                               dict({"scene": "song", "date": d0, "theme": th,
                                     "size": "16x9", "style": "none"}, **extra),
                               timeout=900)
                if st2 == 200 and r2.get("image"):
                    _, imgs[lab] = call(B, r2["image"], timeout=180)
                else:
                    imgs[lab] = None
            if all(imgs.values()):
                def png_diff(a, b):
                    import numpy as np
                    from PIL import Image
                    x = np.asarray(Image.open(io.BytesIO(a)).convert("RGB"), dtype=np.float32)
                    y = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"), dtype=np.float32)
                    return float(np.abs(x - y).mean())
                d0d = png_diff(imgs["原图"], imgs["强度0"])
                d1d = png_diff(imgs["原图"], imgs["强度1"])
                check("风格强度 0 等于原图", d0d < 2.0, f"实际差 {d0d:.2f}/255")
                check("风格强度 1 真的改变了画面", d1d > 25.0, f"实际差 {d1d:.2f}/255")
                check("强度滑杆单调有效", d1d > d0d + 10.0,
                      f"0 档 {d0d:.1f} → 1 档 {d1d:.1f}")
            else:
                check("风格套用渲染", False, "有一档没渲染出来")

        # 换了风格之后，各政权还必须分得开 —— 这是「图还能不能读」的底线
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import style_from_image as SFI
            got = SFI.derive_palette(prof, ["#a89a5c", "#b0756a", "#8a9ab0"],
                                     "#efe7d6", "light", 1.0)
            rc = SFI.region_contrast([c for k, c in got["map"].items() if k != "#efe7d6"])
            check("套风格后区域仍可分辨", rc >= 0.10, f"可区分度 {rc:.3f} < 0.10")
        except Exception as e:
            check("套风格后区域仍可分辨", False, f"{type(e).__name__}: {e}")
    else:
        print("  SKIP  风格主线（没找到测试参考图，用 --style-image 指定）")

    # ── 4) 分镜与出片 ──
    # 这一段是新增能力的主战场：显式分镜、每帧文案、每帧停留、导出。
    sid = "song"
    s = next((x for x in (scenes or []) if x["id"] == sid), None)
    ds = (s or {}).get("dates") or []
    theme0 = ((s or {}).get("themes") or ["dark"])[0]
    jid_done = None

    if not args.video:
        print("  SKIP  分镜/出片/导出（加 --video 开启）")
    elif len(ds) < 4:
        print("  SKIP  分镜测试（宋题材没有 4 个以上日期点）")
    else:
        # 4a) /api/plan 只算分镜不出图
        st, r = call(B, "/api/plan", "POST",
                     {"scene": sid, "date_from": ds[0], "date_to": ds[-1], "hold": 0.5},
                     timeout=120)
        check("/api/plan 展开区间", st == 200 and r.get("n_dates") == len(ds),
              f"n_dates={r.get('n_dates')} 期望={len(ds)}")

        # 4b) 不等停留时长 + 每帧文案 + 只出图模式
        frames = [
            {"date": ds[0], "hold": 1.5, "title": "宋初", "subtitle": "太平兴国"},
            {"date": ds[1], "hold": 0.3},
            {"date": ds[2], "hold": 1.2, "footer": "口径测试"},
            {"date": ds[3], "hold": 0.5},
        ]
        body = {"scene": sid, "theme": theme0, "size": "16x9", "style": "none",
                "hold": 0.6, "mode": "frames", "frames": frames}
        st, r = call(B, "/api/video", "POST", body, timeout=120)
        if check("只出图模式提交", st == 200 and r.get("job"), str(r)[:140]):
            jid_f = r["job"]
            j = {}
            t0 = time.time()
            while time.time() - t0 < 1200:
                time.sleep(2)
                _, j = call(B, f"/api/jobs/{jid_f}", timeout=60)
                if j.get("status") in ("done", "failed"):
                    break
            check("只出图模式跑完", j.get("status") == "done", str(j.get("error") or "")[:160])
            res = j.get("result") or {}
            check("只出图模式不产视频", not res.get("video"), str(res.get("video")))
            check("每帧停留按分镜生效",
                  res.get("holds") == [1.5, 0.3, 1.2, 0.5], str(res.get("holds")))
            check("帧数=分镜长度", res.get("n_frames") == 4, str(res.get("n_frames")))
            # 帧 ZIP
            st2, blob = call(B, f"/api/jobs/{jid_f}/frames.zip", timeout=300)
            import zipfile
            okz = st2 == 200 and (blob or b"")[:2] == b"PK"
            names = []
            if okz:
                try:
                    names = zipfile.ZipFile(io.BytesIO(blob)).namelist()
                except Exception as e:
                    okz = False
                    names = [f"解不开: {e}"]
            check("帧 ZIP 可下载且条目数对", okz and len(names) == 4, str(names))
            # 删除接口
            std, rd = call(B, f"/api/jobs/{jid_f}", "DELETE", timeout=60)
            check("删除任务", std == 200 and rd.get("ok"), str(rd)[:100])
            st3, _ = call(B, f"/api/jobs/{jid_f}", timeout=30)
            check("删除后查不到", st3 == 404, f"status={st3}")

        # 4c) 出片 + 时长核对（每帧停留不同 -> 走 concat 那条路）
        body = {"scene": sid, "theme": theme0, "size": "16x9", "style": "none",
                "hold": 0.6, "mode": "video", "frames": frames}
        st, r = call(B, "/api/video", "POST", body, timeout=120)
        if check("分镜出片提交", st == 200 and r.get("job"), str(r)[:140]):
            jid_done = r["job"]
            j = {}
            t0 = time.time()
            while time.time() - t0 < 1800:
                time.sleep(2)
                _, j = call(B, f"/api/jobs/{jid_done}", timeout=60)
                if j.get("status") in ("done", "failed"):
                    break
            check("分镜出片跑完", j.get("status") == "done", str(j.get("error") or "")[:200])
            res = j.get("result") or {}
            # 这是本轮新增里最容易错的一处：片子时长必须等于各帧停留之和
            want = res.get("want_seconds")
            got = res.get("seconds")
            check("片子时长=各帧停留之和", want is not None and got is not None
                  and abs(got - want) <= 0.25, f"期望 {want}s 实际 {got}s")
            check("MP4 有 moov 且可解码", "可完整解码" in str(res.get("verify")),
                  str(res.get("verify")))
            st2, blob = call(B, res.get("video") or "/nonexistent", timeout=300)
            check("MP4 走下载接口", st2 == 200 and (blob or b"")[4:8] == b"ftyp",
                  f"status={st2} bytes={len(blob or b'')}")

    # ── 4d) 区间出片（老路径，别被新分镜路径挤掉）──
    if args.video:
        for sid in ("tang", "ww2-europe"):
            s = next((x for x in (scenes or []) if x["id"] == sid), None)
            if not s:
                continue
            ds = s["dates"] or []
            body = {"scene": sid, "date_from": ds[0], "date_to": ds[-1],
                    "theme": (s.get("themes") or ["dark"])[0], "size": "16x9",
                    "style": "none", "hold": 0.5}
            st, r = call(B, "/api/video", "POST", body, timeout=120)
            if not check(f"[{sid}] 区间出片提交", st == 200 and r.get("job"), str(r)[:120]):
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
            check(f"[{sid}] 区间出片跑完", j.get("status") == "done",
                  str(j.get("error") or "")[:160])
            res = j.get("result") or {}
            check(f"[{sid}] 帧数齐全", res.get("n_frames") == want,
                  f"n_frames={res.get('n_frames')}")
            w2, g2 = res.get("want_seconds"), res.get("seconds")
            check(f"[{sid}] 区间片子时长对得上",
                  w2 is not None and g2 is not None and abs(g2 - w2) <= 0.4,
                  f"期望 {w2}s 实际 {g2}s")
            st2, blob = call(B, res.get("video") or "/nonexistent", timeout=300)
            check(f"[{sid}] MP4 可下载且完整", st2 == 200 and (blob or b"")[4:8] == b"ftyp",
                  f"status={st2} bytes={len(blob or b'')}")
    else:
        print("  SKIP  区间出片（加 --video 开启）")

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

    # ── 5b) Key / 接口地址 / 模型的预检 ──
    # 这一路踩过的坑：界面上「让模型起草」直接甩一句
    # "HTTP Error 401: Unauthorized"，用户不知道是 key、base URL 还是模型名不对，
    # 而且那份错 key 还会把服务端 .env 里能用的 key 顶掉。
    # 所以这三个分支必须常驻烟测：无 key、错 key、对 key。
    st, r = call(B, "/api/key/test", "POST", {})
    check("/api/key/test 有判断结果而不是 500",
          st == 200 and isinstance(r, dict) and "ok" in r and "step" in r,
          f"status={st} {str(r)[:140]}")

    st, r = call(B, "/api/key/test", "POST", {},
                 headers={"X-Api-Key": "sk-definitely-a-wrong-key-0000000000000000"})
    check("/api/key/test 错 key 时把问题指到 key 这一项",
          st == 200 and isinstance(r, dict) and r.get("ok") is False
          and r.get("step") == "key",
          f"status={st} {str(r)[:140]}")

    if key:
        st, r = call(B, "/api/key/test", "POST", {}, headers={"X-Api-Key": key})
        check("/api/key/test 对 key 时报可用", st == 200 and r.get("ok") is True,
              f"status={st} {str(r)[:160]}")

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
