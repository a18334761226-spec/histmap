#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键（重）启动 7860 容器，并把宿主代码挂进去。

**为什么要有这个脚本**
本项目长期踩过一个大坑：我在 8810（直接跑 python）上改代码、验证，
而用户点的是 **7860（Docker 容器）**，容器里是几小时前的旧代码。
于是「我这边明明修好了」和「你那边还是报错」同时成立，
来回好几轮都在互相看不见的地方较劲。

解决：容器**挂载宿主机的代码目录**，而不是靠镜像里 `COPY . .` 的副本。
  · 改完代码 → 重启容器即可生效，不必重建镜像（重建要 1–2 分钟）
  · web/ 是每次请求现读的，改前端**连重启都不用**，刷页面就行
  · 只挂代码和 data，**不挂 packages/core/data** —— 那里有构建期下载的
    CShapes(25MB)，挂空目录会把它盖掉，一战/二战题材会变成缺数据

用法
    python src/run_container.py            # 重启
    python src/run_container.py --rebuild  # 先重建镜像再重启（依赖变了才需要）
    python src/run_container.py --logs     # 看日志
"""
import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "histmap-pub"
IMAGE = "histmap:local"

# 挂哪些目录。key 是容器内路径，value 是宿主机相对路径。
# **不要挂 packages/core**：构建期下载的 CShapes 在 packages/core/data/cache 里。
MOUNTS = {
    "/app/src": "src",
    "/app/packages/server": "packages/server",
    "/app/packages/agent": "packages/agent",
    "/app/packages/core/histmap_core": "packages/core/histmap_core",
    "/app/web": "web",
    "/app/data": "data",
}


def sh(args, **kw):
    return subprocess.run(args, text=True, capture_output=True,
                          errors="replace", **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true", help="先重建镜像")
    ap.add_argument("--logs", action="store_true", help="只看日志")
    ap.add_argument("--port", type=int, default=7860)
    a = ap.parse_args()

    if a.logs:
        print(sh(["docker", "logs", "--tail", "40", NAME]).stdout)
        return

    if a.rebuild:
        print("重建镜像（依赖没变的话 1–2 分钟）…")
        r = sh(["docker", "build", "-t", IMAGE, "."], cwd=ROOT)
        if r.returncode != 0:
            print(r.stdout[-2000:], r.stderr[-2000:])
            sys.exit(1)
        print("镜像重建完成")

    r = sh(["docker", "inspect", "-f", "{{.State.Running}}", NAME])
    if "true" in r.stdout:
        print("停掉旧容器…")
        sh(["docker", "rm", "-f", NAME])

    args = ["docker", "run", "-d", "--name", NAME,
            "-p", f"{a.port}:{a.port}",
            "-e", f"PORT={a.port}", "-e", "HOST=0.0.0.0",
            # **本地容器允许用 .env 里的 Key。**
            # 容器里看到的请求来源是 Docker 网关 172.17.0.1，不是 127.0.0.1，
            # 于是否则服务端会把你当**公网访客**：本机 .env 里明明配了 key，
            # 用模型的功能却全部 401「要一个 Key」。这是本地跑容器最容易卡住的点。
            # 部署到公网时**不要**加这个变量（README/DEPLOY 里已说明）。
            "-e", "HISTMAP_ALLOW_SERVER_KEY=1"]
    missing = []
    for inside, rel in MOUNTS.items():
        hostp = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.exists(hostp):
            missing.append(rel)
            continue
        args += ["-v", f"{hostp}:{inside}"]
    envf = os.path.join(ROOT, ".env")
    if os.path.exists(envf):
        args += ["-v", f"{envf}:/app/.env:ro"]
    else:
        print("！没有 .env —— 容器里没有任何模型的 Key（网页端自带 Key 仍可用）")
    if missing:
        print("！跳过不存在的目录：", ", ".join(missing))
    args.append(IMAGE)

    # **先把同名的旧容器清掉。**
    # Docker Desktop 重启之后，上次的容器会以 Exited 状态留着并继续占着名字，
    # 于是 `docker run --name histmap-pub` 直接报
    #   Conflict. The container name "/histmap-pub" is already in use
    # 而且报错信息停在 docker 的用法提示上，看起来像参数写错了，很难查。
    # 这台机器隔夜重启过，就是踩的这个。
    rm = sh(["docker", "rm", "-f", NAME])
    if rm.returncode == 0 and rm.stdout.strip():
        print("  已清掉上次留下的同名容器")
    elif rm.returncode != 0:
        # 容器不存在时 docker rm 也返回非 0，这里不当错误处理
        err = (rm.stderr or "").lower()
        if "no such container" not in err:
            print("  清理旧容器时有问题：", (rm.stderr or "")[:200])

    r = sh(args)
    if r.returncode != 0:
        print("启动失败：", r.stderr[-800:])
        sys.exit(1)
    cid = r.stdout.strip()[:12]
    print("已启动容器", cid, "，等它就绪…")
    for _ in range(40):
        time.sleep(1.5)
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{a.port}/api/health", timeout=3) as resp:
                import json
                h = json.loads(resp.read())
            print(f"\n  就绪：http://localhost:{a.port}/"
                  f"  题材 {len(h.get('topics') or [])} 个"
                  f"  字体 {'正常' if (h.get('font') or {}).get('ok') else '**有问题**'}")
            print("  按 Ctrl+C 停止的是我，不是容器；停容器用 docker stop "
                  + NAME)
            return
        except Exception:
            continue
    print("  等了 60 秒还没就绪，看日志：docker logs " + NAME)


if __name__ == "__main__":
    main()
