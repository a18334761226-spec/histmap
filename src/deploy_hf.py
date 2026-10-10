#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键部署到 Hugging Face Spaces。

跑法：
    python src/deploy_hf.py --check                      # 只做部署前自检
    python src/deploy_hf.py --repo <用户名>/<Space名>      # 真推

    # 没登录过的话先设 token（只需要一次）
    set HF_TOKEN=hf_xxxxxxxx

为什么要有这个脚本，而不是照着 DEPLOY.md 敲两条 git 命令
----------------------------------------------------------
因为手工那两步**各有一个必踩的坑**，而且都不报错、只是不好使：

1. **HF 只认根目录的 `README.md` 里的 YAML front-matter。**
   我们的元数据在 `README_HF.md` 里 —— 直接推上去会得到一个
   **没有元数据的 Space**：sdk 不对、app_port 不对，构建完打不开。
   这个脚本会把 front-matter 拼到项目 README 的开头，
   推一个临时分支过去 —— 仓库里的 README.md 不动，Space 上两份内容都有。

2. **app_port 必须和容器监听的端口一致。**
   Dockerfile 里 `ENV PORT=7860`，所以这里也用 7860；不一致的表现是
   Space 一直不健康，日志里看不出任何错。

推之前会先跑一遍公开部署自检（`src/public_check.py` 的那套逻辑里
不依赖网络的几项），因为「本地能跑、推上去跑不了」是最贵的失败。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def run(cmd, cwd=None, check=True, capture=True):
    p = subprocess.run(cmd, cwd=cwd or ROOT, shell=isinstance(cmd, str),
                       capture_output=capture, text=True,
                       encoding="utf-8", errors="replace")
    if check and p.returncode != 0:
        raise SystemExit(f"命令失败：{cmd}\n{p.stdout}\n{p.stderr}")
    return p


def read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def front_matter() -> str:
    """从 README_HF.md 里抠出 YAML front-matter（去掉全是注释的行）。"""
    txt = read(os.path.join(ROOT, "README_HF.md"))
    keep = [ln for ln in txt.split("\n") if not ln.strip().startswith("#")]
    body = "\n".join(keep).strip()
    return "---\n" + body + "\n---\n"


def check() -> int:
    """部署前自检：这几项不对的话，推上去一定不work。"""
    print("=== 部署前自检 ===")
    bad = 0

    fm = front_matter()
    if "sdk: docker" not in fm:
        print("  ✗ README_HF.md 里没有 sdk: docker"); bad += 1
    else:
        print("  OK  front-matter 有 sdk: docker")
    m = re.search(r"app_port:\s*(\d+)", fm)
    if not m:
        print("  ✗ front-matter 里没有 app_port"); bad += 1
    else:
        port = m.group(1)
        df = read(os.path.join(ROOT, "Dockerfile"))
        d = re.search(r"ENV\s+PORT=(\d+)", df)
        if not d or d.group(1) != port:
            print(f"  ✗ 端口对不上：front-matter {port} vs Dockerfile "
                  f"{d.group(1) if d else '（没写）'}"); bad += 1
        else:
            print(f"  OK  app_port 与 Dockerfile 的 PORT 一致（{port}）")

    if not os.path.exists(os.path.join(ROOT, "Dockerfile")):
        print("  ✗ 没有 Dockerfile"); bad += 1
    else:
        print("  OK  有 Dockerfile")

    # 大件不该进仓库（进镜像由 Dockerfile 构建期下载）
    for junk in ("data/cache", "output", "out", ".env"):
        p = os.path.join(ROOT, junk)
        if os.path.exists(p):
            ignored = read(os.path.join(ROOT, ".dockerignore"))
            base = junk.split("/")[0]
            if not any(base in ln for ln in ignored.split("\n")):
                print(f"  ✗ {junk} 没有被 .dockerignore 排除 —— 会打进镜像")
                bad += 1
    print("  OK  大件与本机配置都在 .dockerignore 里")

    df = read(os.path.join(ROOT, "Dockerfile"))
    for need in ("fonts-noto-cjk", "导入通过", "CShapes"):
        if need not in df:
            print(f"  ! Dockerfile 里没看到「{need}」—— 确认一下是不是被删了")
    print()
    print("自检：", "通过" if not bad else f"**{bad} 项不过**")
    return bad


def assemble(work: str) -> None:
    """把要推的内容组装到 work 目录。

    用 `git archive` 导出**受版本控制的文件**，而不是直接拷目录：
    这样 __pycache__、out/、.env、以及任何被 ignore 的东西都不会混进去。
    手工 rsync/copy 很容易把本机的东西带上云。

    **解包用 Python 的 tarfile，不用 tar 命令**：仓库里有个中文文件名
    （`启动工作台.cmd`），Windows 自带的 bsdtar 解 `git archive` 出来的
    UTF-8 名字会报 `Invalid empty pathname` 然后整个失败 ——
    实测就是这么炸的（是 --dry-run 抓出来的，真推的时候才发现更贵）。
    tarfile 按 UTF-8 处理没问题，而且跨平台一致。
    """
    tar_path = os.path.join(work, "_src.tar")
    p = subprocess.run(["git", "archive", "--format=tar", "-o", tar_path, "HEAD"],
                       cwd=ROOT, capture_output=True)
    if p.returncode != 0:
        raise SystemExit("git archive 失败："
                         + p.stderr.decode("utf-8", "replace")[:300])
    import tarfile
    with tarfile.open(tar_path) as tf:
        # 只解普通文件/目录，跳掉符号链接之类（仓库里本来也没有）
        members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
        tf.extractall(work, members=members)
    os.remove(tar_path)

    # 关键一步：front-matter + 项目 README => Space 的 README.md
    proj_readme = read(os.path.join(ROOT, "README.md"))
    with open(os.path.join(work, "README.md"), "w", encoding="utf-8") as f:
        f.write(front_matter() + "\n" + proj_readme)
    hf = os.path.join(work, "README_HF.md")
    if os.path.exists(hf):
        os.remove(hf)


def dry_run() -> int:
    """只组装、不推。用来在花 token 之前确认「推上去的到底是什么」。"""
    work = tempfile.mkdtemp(prefix="histmap-hf-dry-")
    try:
        assemble(work)
        rd = read(os.path.join(work, "README.md"))
        files = [os.path.join(dp, f) for dp, _, fs in os.walk(work) for f in fs]
        total = sum(os.path.getsize(p) for p in files)
        print("=== 组装结果（临时目录）===")
        print(f"  文件 {len(files)} 个，共 {total/1024/1024:.1f} MB")
        print(f"  README_HF.md 已并入 README.md 并删除："
              f"{not os.path.exists(os.path.join(work, 'README_HF.md'))}")
        print("  README.md 开头（HF 就读这里的元数据）：")
        for line in rd.split("\n")[:11]:
            print("      " + line)
        bad = [p.replace(work, "") for p in files
               if any(s in p.replace("\\", "/")
                      for s in ("/.env", "/out/", "__pycache__", "/output/",
                                "/data/cache/", "/node_modules/"))]
        print("  不该带上的东西：", bad or "（无）")
        print()
        print("确认无误后，设好 HF_TOKEN 再跑：")
        print("    python src/deploy_hf.py --repo 你的用户名/histmap")
        return 1 if bad else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def push(repo: str) -> int:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN") or ""
    if not token:
        print("✗ 没找到 HF_TOKEN。先设一次：\n"
              "    set HF_TOKEN=hf_xxxxxxxx\n"
              "  token 在 https://huggingface.co/settings/tokens 建（选 write）")
        return 1

    url = f"https://user:{token}@huggingface.co/spaces/{repo}"
    work = tempfile.mkdtemp(prefix="histmap-hf-")
    print("在临时目录组装要推的内容…")
    try:
        assemble(work)
        print("已把 front-matter 拼到 README.md 开头（Space 才认元数据）")

        for cmd in (["git", "init", "-q"],
                    ["git", "remote", "add", "origin", url],
                    ["git", "add", "-A"],
                    ["git", "-c", "user.email=deploy@histmap",
                     "-c", "user.name=histmap", "commit", "-qm",
                     "deploy from local"]):
            subprocess.run(cmd, cwd=work, check=True,
                           capture_output=True, text=True)
        print("推送到 HF（force：Space 是镜像式部署，以本地为准）…")
        p = subprocess.run(["git", "push", "-f", "origin", "HEAD:main"],
                           cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            err = (p.stderr or "") .replace(token, "***")
            print("✗ 推送失败：", err[:600])
            return 1
        print("✓ 已推送。接下来：")
        print(f"    1. 打开 https://huggingface.co/spaces/{repo} 看 Build 日志")
        print(f"    2. 等构建完（首次 3–6 分钟，含下载 CShapes）")
        print(f"    3. 访问 https://huggingface.co/spaces/{repo} 里的 App 标签页")
        print(f"    4. 打开 <地址>/api/health，确认 font.ok 是 true")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", help="<用户名>/<Space名>，如 someone/histmap")
    ap.add_argument("--check", action="store_true", help="只做部署前自检")
    ap.add_argument("--dry-run", action="store_true",
                    help="组装出要推的内容但不推（花 token 之前先看一眼）")
    ap.add_argument("--skip-check", action="store_true")
    args = ap.parse_args()

    if not args.skip_check:
        if check() and not (args.check or args.dry_run):
            print("\n自检没过，先修了再推（或加 --skip-check 硬推）")
            return 1
    if args.check:
        return 0
    if args.dry_run:
        return dry_run()
    if not args.repo:
        print("要推的话必须给 --repo，例如：\n"
              "    python src/deploy_hf.py --repo 你的用户名/histmap\n"
              "先看一眼会推什么：\n"
              "    python src/deploy_hf.py --dry-run")
        return 1
    return push(args.repo)


if __name__ == "__main__":
    sys.exit(main())
