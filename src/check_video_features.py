"""真跑一遍转场 / 镜头运动 / 片头卡 / 水印，量出时长对不对。

跑法（服务已在 8810 跑着）：python src/check_video_features.py
"""
import json
import sys
import time
import urllib.error
import urllib.request

B = "http://127.0.0.1:8810"


def post(p, b, t=120):
    r = urllib.request.Request(B + p, data=json.dumps(b).encode(),
                               headers={"Content-Type": "application/json"},
                               method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=t).read())


def run(name, body, want=None):
    r = post("/api/video", body)
    jid = r["job"]
    j = {}
    for _ in range(400):
        time.sleep(2)
        j = json.loads(urllib.request.urlopen(B + "/api/jobs/" + jid, timeout=30).read())
        if j.get("status") in ("done", "failed"):
            break
    res = j.get("result") or {}
    got = res.get("seconds")
    w = res.get("want_seconds")
    ok = j.get("status") == "done"
    flag = "OK " if ok else "FAIL"
    extra = ""
    if ok:
        extra = "期望 %.2fs 实际 %ss 转场=%s 段=%s %s" % (
            w or 0, got, res.get("transition"), res.get("segments"),
            res.get("note") or "")
        if want is not None and (w is None or abs(w - want) > 0.12):
            flag = "FAIL"
            extra += "  !! 期望时长算错（应为 %.2f）" % want
    else:
        extra = (j.get("error") or "")[:220]
    print("  %s  %-34s %s" % (flag, name, extra))
    return ok


def main():
    songs = json.loads(urllib.request.urlopen(B + "/api/scenes", timeout=60).read())
    ds = next(s for s in songs["scenes"] if s["id"] == "song")["dates"]
    base = {"scene": "song", "theme": "light", "size": "16x9", "hold": 0.6}
    frames = [{"date": d, "hold": 0.8} for d in ds[:4]]     # 4 帧 x 0.8s = 3.2s

    print("== 转场与镜头 ==")
    run("硬切（基线）", dict(base, frames=frames, mode="video"), want=3.2)
    run("交叉溶解 0.3s", dict(base, frames=frames, mode="video", fade=0.3),
        want=3.2 - 0.3 * 3)
    run("交叉溶解 + 缓推", dict(base, frames=frames, mode="video", fade=0.3,
                              motion="in"), want=3.2 - 0.3 * 3)
    run("镜头缓拉（无转场）", dict(base, frames=frames, mode="video", motion="out"),
        want=3.2)

    print("\n== 片头 / 片尾 / 水印 ==")
    run("片头 1.5s + 正片", dict(base, frames=frames, mode="video",
                               intro={"text": "宋 · 路制疆域", "sub": "980 → 1200",
                                      "seconds": 1.5}), want=1.5 + 3.2)
    run("片头 + 片尾 + 溶解", dict(base, frames=frames, mode="video", fade=0.25,
                               intro={"text": "宋", "seconds": 1.0},
                               outro={"text": "完", "sub": "histmap", "seconds": 1.0}),
        want=1.0 + 3.2 + 1.0 - 0.25 * 5)
    run("水印", dict(base, frames=frames, mode="video", watermark="@histmap"),
        want=3.2)

    print("\n== 长分镜（看交叉溶解在几十帧下会不会崩）==")
    many = [{"date": d, "hold": 0.5} for d in ds] * 5      # 30 帧
    run("30 帧 + 溶解", {"scene": "song", "theme": "light", "size": "16x9",
                      "hold": 0.5, "frames": many, "mode": "video", "fade": 0.2},
        want=sum(0.5 for _ in many) - 0.2 * (len(many) - 1))


if __name__ == "__main__":
    main()
