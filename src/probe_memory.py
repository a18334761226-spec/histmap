#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
内存泄漏定位：把渲染流水线拆成几个阶段，逐帧打印工作集内存。
用 Windows GetProcessMemoryInfo（ctypes）读当前进程的 WorkingSetSize。
"""
import gc
import os
import sys
import ctypes
import ctypes.wintypes as wt

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))
sys.path.insert(0, os.path.join(ROOT, "src"))


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def ws_mb():
    try:
        import psutil
        p = psutil.Process(os.getpid())
        return p.memory_info().rss / 1024 / 1024
    except Exception as e:
        return -1.0


import make_ww2_video as M          # noqa: E402
from histmap_core import Registry, ControlTimeline  # noqa: E402
from datetime import date           # noqa: E402

MODE = sys.argv[1] if len(sys.argv) > 1 else "full"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 12
SS = int(sys.argv[3]) if len(sys.argv) > 3 else 2

print(f"mode={MODE} frames={N} supersample={SS}  起始 {ws_mb():.0f} MB")

tl = ControlTimeline.load(M.TL_PATH)

ad = Registry.get("cshapes")
gj = ad.load()
ad.load = lambda use_cache=True: gj
s = ad.build(years=[1941])
fr = s.frames[0]
lon0, lat0, lon1, lat1 = M.BBOX
fr.regions = [r for r in fr.regions
              if r.bbox() and not (r.bbox()[2] < lon0 or r.bbox()[0] > lon1
                                   or r.bbox()[3] < lat0 or r.bbox()[1] > lat1)]
print(f"几何就绪 {ws_mb():.0f} MB  实体 {len(fr.regions)}")

r = M.Renderer(M.STYLE, M.Layout(width=1920, height=1080, mode="full"),
               projection="mercator", supersample=SS)

rows = []
proc = None
if MODE == "pipe":
    import subprocess
    ff = M.find_ffmpeg()
    out = os.path.join(M.VID_DIR, "_probe_pipe.mp4")
    proc = subprocess.Popen(
        [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", "1920x1080", "-r", "30", "-i", "-",
         "-c:v", "libx264", "-preset", "medium", "-crf", "18",
         "-pix_fmt", "yuv420p", out],
        stdin=subprocess.PIPE, stderr=open(os.path.join(M.VID_DIR, "_probe_err.log"), "w"))
    print(f"ffmpeg pid={proc.pid}")

prev_img = None
for i in range(N):
    d = date(1940, 1 + (i % 12), 1)
    fr.title = f"1940 年 {d.month} 月"
    fr.subtitle = "测试"
    img = r.render_frame(fr, bbox=M.BBOX, legend_items=M.LEGEND,
                         legend_title="实际控制")
    after_render = ws_mb()
    if MODE in ("full", "ticker", "pipe"):
        M.draw_ticker(img, M.recent_lines(tl, d, 3), r)
    if MODE == "gc":
        gc.collect()
    if MODE == "pipe" and proc is not None:
        nf = max(1, int(M.FADE * M.FPS))
        if prev_img is not None:
            for k in range(1, nf + 1):
                proc.stdin.write(Image.blend(prev_img, img, k / (nf + 1)).tobytes())
        for _ in range(max(1, int(M.HOLD_EVENT * M.FPS))):
            proc.stdin.write(img.tobytes())
    rows.append((i, after_render, ws_mb()))
    print(f"  帧 {i:2d}  render后 {after_render:7.0f} MB   结束 {ws_mb():7.0f} MB")

if proc is not None:
    proc.stdin.close()
    print(f"ffmpeg rc={proc.wait()}")

print(f"\n净增 {rows[-1][2] - rows[0][2]:.0f} MB / {N} 帧 "
      f"= 每帧 {(rows[-1][2]-rows[0][2])/N:.1f} MB")
