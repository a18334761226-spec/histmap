"""量一下「参考图风格到底有没有被套上去」。

跑法：python src/probe_style_strength.py

为什么要量：用户说「我要的是抽取风格生成图片视频」，而 apply_style 的注释里
写着它**故意**关掉了色调迁移（因为直方图匹配会把分类色压平）。
保守是对的，但如果保守到「套了跟没套一样」，这个功能就等于不存在。
所以先拿数字说话：原图 vs 套风格后，平均像素差是多少。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np                      # noqa: E402
import style_from_image as SFI          # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFDIR = os.path.join(ROOT, "output", "style_refs")
OUT = os.path.join(ROOT, "output", "style_probe")


def make_refs():
    """造三张特征明确的参考图。用造图而不是找图，是为了每次跑结果可比。"""
    os.makedirs(REFDIR, exist_ok=True)
    W, H = 640, 400
    refs = {}

    # A) 暖褐羊皮纸：色相多样性≈0，正是最典型的「旧地图」参考
    im = Image.new("RGB", (W, H), (214, 190, 148))
    d = ImageDraw.Draw(im)
    rng = np.random.default_rng(3)
    for _ in range(5000):
        x, y = rng.integers(0, W), rng.integers(0, H)
        v = int(rng.integers(-26, 18))
        d.point((x, y), (214 + v, 190 + v, 148 + v))
    im = im.filter(ImageFilter.GaussianBlur(0.4))
    im.save(os.path.join(REFDIR, "ref_parchment.png"))
    refs["暖褐羊皮纸"] = "ref_parchment.png"

    # B) 冷青蓝图：白线蓝底，色相集中在 200-215
    im = Image.new("RGB", (W, H), (18, 42, 74))
    d = ImageDraw.Draw(im)
    for i in range(0, H, 22):
        d.line([(0, i), (W, i)], fill=(30, 62, 104), width=1)
    for i in range(0, W, 22):
        d.line([(i, 0), (i, H)], fill=(30, 62, 104), width=1)
    for _ in range(26):
        x0, y0 = rng.integers(0, W), rng.integers(0, H)
        d.rectangle([x0, y0, x0 + rng.integers(30, 90), y0 + rng.integers(20, 60)],
                    outline=(150, 210, 240), width=2)
    rng2 = np.random.default_rng(9)
    a = np.asarray(im).astype(np.float32)
    a += rng2.normal(0, 5, a.shape)
    Image.fromarray(a.clip(0, 255).astype("uint8")).save(os.path.join(REFDIR, "ref_blueprint.png"))
    refs["冷青蓝图"] = "ref_blueprint.png"

    # C) 高对比红黑：色相跨度大、明暗极端
    im = Image.new("RGB", (W, H), (16, 12, 14))
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x0, y0 = rng.integers(0, W), rng.integers(0, H)
        d.ellipse([x0, y0, x0 + rng.integers(20, 120), y0 + rng.integers(20, 120)],
                  fill=(int(rng.integers(140, 205)), int(rng.integers(20, 50)),
                        int(rng.integers(24, 56))))
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    im.save(os.path.join(REFDIR, "ref_ember.png"))
    refs["高对比红黑"] = "ref_ember.png"
    return refs


def diff(a: Image.Image, b: Image.Image):
    """平均通道差（0-255）。这是「套了跟没套差多少」的直接度量。"""
    x = np.asarray(a.convert("RGB"), dtype=np.float32)
    y = np.asarray(b.convert("RGB"), dtype=np.float32)
    return float(np.abs(x - y).mean())


def main():
    os.makedirs(OUT, exist_ok=True)
    sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
    sys.path.insert(0, os.path.join(ROOT, "packages", "core"))
    from histmap_server import topics

    refs = make_refs()
    base = topics.render("song", "1140-01-01", "light", "16x9")
    base.save(os.path.join(OUT, "base.png"))
    print("底图:", base.size, " 参考图目录:", REFDIR)

    tiles = [("原图", base)]
    print("\n判据两条：① 与参考图的色调距离（越小越像参考图）"
          "  ② 区域可区分度（<0.10 就是糊成一片）")

    for name, fn in refs.items():
        p = os.path.join(REFDIR, fn)
        prof = SFI.extract_style(p, verbose=False)
        print(f"\n== {name} ==")
        print(f"  参考主色 {', '.join(c['hex'] for c in prof['palette'][:4])}  "
              f"饱和 {prof['saturation']:.3f}  颗粒 {prof['grain']:.4f}")

        # 新链路：分类色重映射 → 再渲染 → 再压材质
        styled = topics.render("song", "1140-01-01", "light", "16x9",
                               style_profile=prof, strength=1.0)
        styled.save(os.path.join(OUT, f"{fn[:-4]}_styled.png"))
        tiles.append((name, styled))

        d = diff(base, styled)
        # 从**映射后的区域色**量可区分度。
        # 不要从渲染结果里数众数色：那张图上出现最多的永远是纸色的一堆近似色，
        # 量出来必然接近 0，是量错了地方（第一版就这么误判过）。
        got = SFI.derive_palette(prof, ["#a89a5c", "#b0756a", "#8a9ab0"], "#efe7d6",
                                 "light", 1.0)
        rc = SFI.region_contrast([c for k, c in got["map"].items() if k != "#efe7d6"])
        print(f"  新底色 {got['canvas']}  新文字 {got['text']}")
        print(f"  区域映射 {got['map']}")
        print(f"  ① 与原图差异 {d:5.2f}/255   ② 区域可区分度 {rc:.3f} "
              f"{'OK' if rc >= 0.10 else '!! 太挤，分不开'}")

    # 并排对照
    tw, th = 700, 394
    cols = 2
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (tw * cols, th * rows), (16, 16, 18))
    for i, (lab, im) in enumerate(tiles):
        sheet.paste(im.convert("RGB").resize((tw, th)), ((i % cols) * tw, (i // cols) * th))
    sheet.save(os.path.join(OUT, "compare.png"))
    print("\n并排对照（左上=原图）:", os.path.join(OUT, "compare.png"))


if __name__ == "__main__":
    main()
