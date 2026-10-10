"""公开部署的验收测试：**没有 Key**、端口由平台注入时，应用还能不能用。

为什么这条最要紧：部署到公网之后，访问者不会带站长 .env 里的 key
（`api_topic_draft` 只在来源是 127.0.0.1 时才允许用服务端的 key）。
所以「没有 Key」是公网环境的**常态**，不是异常。要求是：

  · 出图/出片**必须完全可用** —— 几何是代码画的，本来就不需要模型
  · 需要模型的三处（起草题材 / 对话改图 / 读参考图）必须给出
    **说清原因的 401/提示**，而不是 500 或超时
  · 界面本身要能打开

这里连的是 /api/health 的 font.ok，也一样要查 —— 云端那次事故就是
接口全绿但图上没中文。
"""
import json
import os
import sys
import urllib.error
import urllib.request

B = os.environ.get("HISTMAP_BASE", "http://127.0.0.1:7860")
ok_all = True


def call(path, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(B + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"_err": f"{type(e).__name__}: {e}"}


def say(name, ok, detail=""):
    global ok_all
    print(("  OK   " if ok else "  FAIL ") + name + (f"  —— {detail}" if detail else ""))
    if not ok:
        ok_all = False


print(f"目标 {B}（模拟公网：无 Key、端口由平台注入）")
print()

st, h = call("/api/health")
say("健康检查可用", st == 200 and h.get("ok") is True, f"status={st}")
say("字体能画中文（否则图上没字）", (h.get("font") or {}).get("ok") is True,
    json.dumps(h.get("font"), ensure_ascii=False)[:120])

st, sc = call("/api/scenes")
scenes = (sc or {}).get("scenes") or []
notready = [s["id"] for s in scenes if not s.get("ready")]
say(f"题材列表可用（{len(scenes)} 个）", st == 200 and scenes, f"status={st}")
say("所有题材数据就绪", not notready, f"未就绪: {notready}")

# 不需要模型的：必须完全可用
st, j = call("/api/render", "POST",
             {"scene": "tang", "date": "807-01-01", "theme": "light", "size": "16x9"})
say("无需 Key 就能出图", st == 200 and j.get("image"), f"status={st} {str(j)[:120]}")
if j.get("image"):
    try:
        raw = urllib.request.urlopen(B + j["image"], timeout=120).read()
        say("图片可下载", raw[:4] == b"\x89PNG" and len(raw) > 50000,
            f"{len(raw)//1024} KB")
    except Exception as e:
        say("图片可下载", False, f"{type(e).__name__}: {e}")

st, j = call("/api/plan", "POST", {"scene": "song"}, timeout=60)
say("分镜可用（无需 Key）", st == 200 and j.get("n_frames"), f"status={st}")

# 需要模型的：必须是**说清原因的失败**，不能是 500
st, j = call("/api/topic/draft", "POST", {"ask": "北美独立战争"}, timeout=180)
say("无 Key 时起草给 401 且说明原因（不是 500）",
    st == 401 and isinstance(j, dict) and j.get("detail"),
    f"status={st} {str(j.get('detail'))[:150]}")

# 风格提取：纯代码那一路必须成功，视觉模型那一路失败要说清但**不影响整体**
try:
    import io
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (640, 360), (232, 220, 190))
    d = ImageDraw.Draw(im)
    for i in range(0, 640, 40):
        d.line([(i, 0), (i, 360)], fill=(120, 90, 60))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    blob = buf.getvalue()
except Exception as e:
    print("  SKIP  风格提取（造不出测试图: %s）" % e)
    blob = None

if blob:
    bd = "----pubcheck"
    body = (f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"ref.png\"\r\nContent-Type: image/png\r\n\r\n").encode() \
        + blob + f"\r\n--{bd}--\r\n".encode()
    req = urllib.request.Request(
        B + "/api/style/extract?vlm=true", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={bd}"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            st, j = r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        st, j = e.code, {}
    except Exception as e:
        st, j = 0, {"_err": str(e)}
    say("无 Key 时风格提取仍给出纯代码结果",
        st == 200 and (j.get("profile") or {}).get("palette"),
        f"status={st}")
    say("视觉模型那一步失败但说明了原因（不是静默失败）",
        bool(j.get("vlm_error")), str(j.get("vlm_error"))[:130])

print()
print("== 公开部署验收：%s ==" % ("全部通过" if ok_all else "**有失败项**"))
sys.exit(0 if ok_all else 1)
