"""测 concat 各参数组合下 MP4 的真实时长，定出「每帧独立停留」的正确写法。

跑法：python src/probe_concat_duration.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_ww2_video as M  # noqa: E402


def newest_frames():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "output", "jobs")
    dirs = [(os.path.getmtime(os.path.join(root, d)), d) for d in os.listdir(root)]
    for _, d in sorted(dirs, reverse=True):
        fd = os.path.join(root, d, "frames")
        if os.path.isdir(fd) and len(os.listdir(fd)) >= 4:
            return fd
    raise SystemExit("找不到有 4 帧以上的任务目录，先出一单")


def main():
    ff = M.find_ffmpeg()
    fdir = newest_frames()
    print("帧目录:", fdir)

    # 1.5 / 0.3 / 1.2 / 0.5 秒 @24fps -> 84 帧 -> 恰好 3.5 秒
    counts = [36, 7, 29, 12]
    want = sum(counts) / 24.0
    tmp = tempfile.mkdtemp()
    lst = os.path.join(tmp, "l.txt")
    with open(lst, "w", encoding="utf-8") as fh:
        for i, c in enumerate(counts):
            p = os.path.join(fdir, "%04d.png" % i).replace("\\", "/")
            for _ in range(c):
                fh.write("file '%s'\n" % p)

    def run(name, args):
        out = os.path.join(tmp, name + ".mp4")
        r = subprocess.run([ff, "-y", "-loglevel", "error"] + args +
                           ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                            "-pix_fmt", "yuv420p", out],
                           capture_output=True, text=True, errors="replace")
        if r.returncode != 0:
            print("  %-46s FAIL  %s" % (name, (r.stderr or "").strip()[:120]))
            return
        got = M.probe_duration(ff, out)
        flag = "OK" if got and abs(got - want) < 0.05 else "!! 偏了"
        print("  %-46s %ss  期望 %ss  %s" % (name, got, want, flag))

    print("\n各组合：")
    run("A 输出 -r 24", ["-f", "concat", "-safe", "0", "-i", lst, "-r", "24"])
    run("B -fps_mode cfr -r 24", ["-f", "concat", "-safe", "0", "-i", lst,
                                  "-fps_mode", "cfr", "-r", "24"])
    run("C 不给帧率", ["-f", "concat", "-safe", "0", "-i", lst])
    run("D 输入 -r 24 输出 -r 24", ["-r", "24", "-f", "concat", "-safe", "0",
                                    "-i", lst, "-r", "24"])

    # E：硬链接成序列，走 image2 老路（帧数精确，已被验证）
    seq = os.path.join(tmp, "seq")
    os.makedirs(seq, exist_ok=True)
    k = 0
    for i, c in enumerate(counts):
        src = os.path.join(fdir, "%04d.png" % i)
        for _ in range(c):
            dst = os.path.join(seq, "%04d.png" % k)
            try:
                os.link(src, dst)
            except Exception:
                import shutil
                shutil.copy(src, dst)
            k += 1
    run("E 硬链接序列 + -framerate 24",
        ["-framerate", "24", "-i", os.path.join(seq, "%04d.png"), "-r", "24"])
    print("\n共 %d 帧" % k)


if __name__ == "__main__":
    main()
