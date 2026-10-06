"""走一遍真实产品路径：上传参考图 → 提取风格 → 按不同强度渲染 → 量效果。

跑法（服务已在 8810 跑着）：python src/check_style_flow.py

这是用户要的主线，所以要能一键验证：
  · 上传后拿到的 profile 是否完整（前端要靠它回传）
  · 套上风格后与没套的差多少（风格是否真的生效）
  · 各政权的颜色是否仍然分得开（图还能不能读）
  · 强度 0 → 应该等于原图；强度越大差异越大（滑杆是否单调有效）
"""
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np                    # noqa: E402
import style_from_image as SFI        # noqa: E402

B = "http://127.0.0.1:8810"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFS = os.path.join(ROOT, "output", "style_refs")
OUT = os.path.join(ROOT, "output", "style_flow")


def post_json(p, body, t=300):
    r = urllib.request.Request(B + p, data=json.dumps(body).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=t).read())


def post_file(p, path, t=300):
    """故意不设 Content-Type —— 前端 FormData 就是这样，后端不该依赖它。"""
    blob = open(path, "rb").read()
    bd = "----flow"
    body = (f"--{bd}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: image/png\r\n\r\n").encode() + blob + f"\r\n--{bd}--\r\n".encode()
    r = urllib.request.Request(B + p, data=body,
                               headers={"Content-Type": f"multipart/form-data; boundary={bd}"},
                               method="POST")
    return json.loads(urllib.request.urlopen(r, timeout=t).read())


def get_bytes(url, t=120):
    return urllib.request.urlopen(B + url, timeout=t).read()


def diff(a: bytes, b: bytes):
    from PIL import Image
    x = np.asarray(Image.open(io.BytesIO(a)).convert("RGB"), dtype=np.float32)
    y = np.asarray(Image.open(io.BytesIO(b)).convert("RGB"), dtype=np.float32)
    return float(np.abs(x - y).mean())


def main():
    from PIL import Image
    os.makedirs(OUT, exist_ok=True)
    if not os.path.isdir(REFS):
        raise SystemExit("先跑 python src/probe_style_strength.py 生成参考图")

    # 基准：不套风格
    r0 = post_json("/api/render", {"scene": "song", "date": "1140-01-01",
                                   "theme": "light", "size": "16x9", "style": "none"})
    base = get_bytes(r0["image"])
    Image.open(io.BytesIO(base)).save(os.path.join(OUT, "base.png"))
    print("基准图:", r0["image"])

    fails = []
    for fn in sorted(os.listdir(REFS)):
        if not fn.endswith(".png"):
            continue
        path = os.path.join(REFS, fn)
        print(f"\n== {fn} ==")
        got = post_file("/api/style/extract", path)
        prof = got.get("profile") or {}
        need = ["palette", "lum_quantiles", "saturation", "grain", "vignette",
                "white_balance_gain"]
        miss = [k for k in need if k not in prof]
        ok1 = not miss
        print(f"  提取 profile: {'完整' if ok1 else '缺 ' + str(miss)}")
        if not ok1:
            fails.append(f"{fn} profile 缺字段")
        print(f"  主色 {', '.join(c['hex'] for c in prof.get('palette', [])[:4])}")

        last = None
        for s in (0.0, 0.5, 1.0):
            r = post_json("/api/render", {"scene": "song", "date": "1140-01-01",
                                          "theme": "light", "size": "16x9",
                                          "style": "none", "strength": s,
                                          "style_profile": prof})
            img = get_bytes(r["image"])
            d = diff(base, img)
            Image.open(io.BytesIO(img)).save(
                os.path.join(OUT, f"{fn[:-4]}_s{int(s*100)}.png"))
            note = ""
            if s == 0.0:
                note = "（应当≈0，即等于原图）"
                if d > 1.5:
                    fails.append(f"{fn} 强度 0 却与原图差了 {d:.1f}")
            print(f"  强度 {s:.1f}: 与原图差 {d:6.2f}/255 {note}")
            if last is not None and d < last - 1.0:
                fails.append(f"{fn} 强度 {s} 反而比上一档更像原图（滑杆不单调）")
            last = d
        if last is not None and last < 8:
            fails.append(f"{fn} 满强度只差 {last:.1f}，风格几乎没套上")

        # 区域是否还分得开：用映射后的区域色算
        mapd = SFI.derive_palette(prof, ["#a89a5c", "#b0756a", "#8a9ab0"], "#efe7d6",
                                  "light", 1.0)["map"]
        rc = SFI.region_contrast([c for k, c in mapd.items() if k != "#efe7d6"])
        print(f"  区域可区分度 {rc:.3f} {'OK' if rc >= 0.10 else '!! 太挤'}")
        if rc < 0.10:
            fails.append(f"{fn} 区域可区分度 {rc:.3f} < 0.10，图会糊")

    print("\n并排对照（左=原图，中=强度0.5，右=满强度）:")
    tiles = []
    for fn in sorted(os.listdir(REFS)):
        if fn.endswith(".png"):
            tiles.append([os.path.join(OUT, "base.png"),
                          os.path.join(OUT, f"{fn[:-4]}_s50.png"),
                          os.path.join(OUT, f"{fn[:-4]}_s100.png")])
    tw, th = 460, 259
    sheet = Image.new("RGB", (tw * 3, th * len(tiles)), (16, 16, 18))
    for r, row in enumerate(tiles):
        for c, p in enumerate(row):
            if os.path.exists(p):
                sheet.paste(Image.open(p).convert("RGB").resize((tw, th)), (c * tw, r * th))
    sheet.save(os.path.join(OUT, "flow_compare.png"))
    print(" ", os.path.join(OUT, "flow_compare.png"))

    print("\n== 结果 ==")
    if fails:
        for f in fails:
            print("  FAIL", f)
        return 1
    print("  全部通过：风格生效、强度单调、区域仍可分辨")
    return 0


if __name__ == "__main__":
    sys.exit(main())
