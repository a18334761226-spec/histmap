"""测转场（xfade）与镜头运动（zoompan）的 ffmpeg 写法。

跑法：python src/probe_motion.py

为什么要有这个文件：上一版我把 VideoReq.fade 声明出来了却从没实现，
界面上写着「交叉溶解」而实际一直是硬切。ffmpeg 的滤镜参数语义很容易想当然，
所以这次先写对照实验：每种写法都量真实时长、真解一遍、看帧数对不对。
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_ww2_video as M  # noqa: E402

FPS = 24


def newest_frames(n=6):
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "output", "jobs")
    cands = []
    for d in os.listdir(root):
        fd = os.path.join(root, d, "frames")
        if os.path.isdir(fd):
            fs = sorted(f for f in os.listdir(fd) if f.endswith(".png"))
            if len(fs) >= n:
                cands.append((os.path.getmtime(fd), fd, fs))
    if not cands:
        raise SystemExit("找不到有足够帧的任务目录，先在界面上出一次「全部图」")
    _, fd, fs = max(cands)
    return fd, fs[:n]


def run(ff, args, out):
    r = subprocess.run([ff, "-y", "-loglevel", "error"] + args + [out],
                       capture_output=True, text=True, errors="replace")
    return r.returncode, (r.stderr or "").strip()[:200]


def frames_of(ff, path):
    """数一下真实输出帧数（读 stderr 的 frame= 汇总）。"""
    r = subprocess.run([ff, "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True, errors="replace")
    last = ""
    for line in (r.stderr or "").splitlines():
        if line.startswith("frame="):
            last = line
    return last.strip()[:80]


def main():
    ff = M.find_ffmpeg()
    fdir, files = newest_frames(6)
    print("帧目录:", fdir)
    print("用前 %d 张:" % len(files), files)
    tmp = tempfile.mkdtemp()
    holds = [1.0, 0.8, 1.2, 0.6, 1.0, 0.8]
    total = sum(holds)
    print("每帧停留:", holds, "合计 %.2fs" % total)

    # ── A) zoompan 单帧片段：能不能精确出 N 帧 ──
    print("\n[A] zoompan 单帧片段（镜头运动）")
    for motion in ("none", "in", "out"):
        out = os.path.join(tmp, "seg_%s.mp4" % motion)
        n = round(holds[0] * FPS)
        if motion == "none":
            vf = "scale=1920:1080,format=yuv420p"
        elif motion == "in":
            vf = ("zoompan=z='min(1+0.0016*on,1.14)':x='iw/2-(iw/zoom/2)':"
                  "y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=%d,format=yuv420p" % FPS)
        else:
            vf = ("zoompan=z='max(1.14-0.0016*on,1.0)':x='iw/2-(iw/zoom/2)':"
                  "y='ih/2-(ih/zoom/2)':d=1:s=1920x1080:fps=%d,format=yuv420p" % FPS)
        rc, err = run(ff, ["-loop", "1", "-framerate", str(FPS),
                           "-i", os.path.join(fdir, files[0]),
                           "-frames:v", str(n), "-vf", vf,
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           "-pix_fmt", "yuv420p"], out)
        if rc != 0:
            print("  %-6s FAIL  %s" % (motion, err))
            continue
        d = M.probe_duration(ff, out)
        want = n / FPS
        ok = d is not None and abs(d - want) < 0.06
        print("  %-6s 期望 %.3fs 实际 %ss %s   %s"
              % (motion, want, d, "OK" if ok else "!! 偏了", frames_of(ff, out)))

    # ── B) xfade 转场：链式拼接的 offset 算法对不对 ──
    print("\n[B] xfade 链式交叉溶解（offset = 前缀和 - (k+1)*D）")
    segs = []
    for i, h in enumerate(holds):
        out = os.path.join(tmp, "s%d.mp4" % i)
        n = round(h * FPS)
        rc, err = run(ff, ["-loop", "1", "-framerate", str(FPS),
                           "-i", os.path.join(fdir, files[i]),
                           "-frames:v", str(n),
                           "-vf", "scale=1920:1080,format=yuv420p",
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                           "-pix_fmt", "yuv420p"], out)
        if rc != 0:
            print("  片段 %d 失败: %s" % (i, err))
            return
        segs.append(out)

    for D in (0.0, 0.3, 0.6):
        out = os.path.join(tmp, "mix_%.1f.mp4" % D)
        if D <= 0:
            lst = os.path.join(tmp, "c.txt")
            with open(lst, "w", encoding="utf-8") as fh:
                for s in segs:
                    fh.write("file '%s'\n" % s.replace("\\", "/"))
            rc, err = run(ff, ["-f", "concat", "-safe", "0", "-i", lst,
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                               "-pix_fmt", "yuv420p"], out)
            want = total
        else:
            args = []
            for s in segs:
                args += ["-i", s]
            parts, cur = [], "[0:v]"
            s_sum = 0.0
            for k in range(1, len(segs)):
                s_sum += holds[k - 1]
                off = s_sum - k * D
                tag = "[x%d]" % k
                parts.append("%s[%d:v]xfade=transition=fade:duration=%.3f:offset=%.3f%s"
                             % (cur, k, D, max(0.0, off), tag))
                cur = tag
            fc = ";".join(parts)
            rc, err = run(ff, args + ["-filter_complex", fc, "-map", cur,
                                      "-c:v", "libx264", "-preset", "veryfast",
                                      "-crf", "20", "-pix_fmt", "yuv420p"], out)
            want = total - D * (len(segs) - 1)
        if rc != 0:
            print("  D=%.1f FAIL  %s" % (D, err))
            continue
        d = M.probe_duration(ff, out)
        ok = d is not None and abs(d - want) < 0.12
        print("  D=%.1f 期望 %.3fs 实际 %ss %s   %s"
              % (D, want, d, "OK" if ok else "!! 偏了", frames_of(ff, out)))


if __name__ == "__main__":
    main()
