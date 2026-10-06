"""对齐「题材声明的年份」与「控制表里的年份」。

跑法：
    python src/reconcile_years.py            # 只报告
    python src/reconcile_years.py --fix      # 把可证无损的冗余年份删掉

为什么有这个：题材的 `years` 决定**渲染哪几帧**，控制表决定**每帧画什么**。
两者不一致时会出现两类问题，而且都不会报错，只是悄悄不对：

1. 控制表有、题材没声明 → **死数据**。永远不渲染，白占体积。
   实测：法国大革命的 1793 就是这样躺着的。
2. 题材声明了、控制表没有 → 那一年的几何**构建必然失败**（new_topic 已取交集规避）。

还有一类更隐蔽的：相邻两年的控制状态**完全一样**，画出来是两张一模一样的图。
短视频里这是白占一帧，而且说明这段史料没抓到变化。
实测：法国大革命的 1792 = 1793 = 1794，三年同一个状态。

`--fix` 只删**能证明无损**的：控制表里多余、且与相邻的已声明年份状态完全相同的年份。
其余（比如多出来的年份确实是个新状态）只报告，由人决定是加进 years 还是删掉 ——
删数据不能自动化，那是把问题藏起来。
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
sys.path.insert(0, HERE)

CTRL = os.path.join(ROOT, "data", "control")
TOPICS = os.path.join(ROOT, "data", "topics", "topics.json")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def state_of(ctrl: dict, year: int):
    """某一年的控制状态，用于比较「这两年画出来一样吗」。"""
    v = ctrl.get(str(year))
    if not isinstance(v, dict):
        return None
    out = []
    for k, u in v.items():
        if isinstance(u, list):
            out.append((k, tuple(sorted(u))))
        elif isinstance(u, dict):
            out.append((k, tuple(sorted(u.items()))))
        else:
            out.append((k, u))
    return tuple(sorted(out, key=lambda x: str(x[0])))


def control_years(ctrl: dict) -> list[int]:
    return sorted(int(k) for k in ctrl
                  if not str(k).startswith("_") and str(k).isdigit())


def duplicate_states(ctrl: dict) -> list[int]:
    """哪些年份的控制状态与**上一年完全相同**（画出来是同一张图）。

    实测这是普遍问题，不是个例：宋的 1040 与 1080 控制表逐字相同，
    印度分治的 1950/1960/1970 三年同态。成片里就是同一张图连播两遍。
    """
    ys = control_years(ctrl)
    return [b for a, b in zip(ys, ys[1:])
            if state_of(ctrl, a) is not None and state_of(ctrl, a) == state_of(ctrl, b)]


def examine(t: dict):
    """返回 (控制表原文, 路径, 控制表年份, 题材年份, 死数据, 缺数据, 重复相邻年)。"""
    c = t.get("control")
    if not c or t.get("kind") == "boundary":
        return None
    p = os.path.join(CTRL, c)
    if not os.path.exists(p):
        return None
    ctrl = json.load(open(p, encoding="utf-8"))
    cy = control_years(ctrl)
    ty = sorted(int(y) for y in (t.get("years") or []))
    dead = [y for y in cy if y not in ty]
    miss = [y for y in ty if y not in cy]
    return ctrl, p, cy, ty, dead, miss, duplicate_states(ctrl)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="删掉可证明无损的冗余年份")
    args = ap.parse_args()

    doc = json.load(open(TOPICS, encoding="utf-8"))
    touched = 0
    for t in doc["topics"]:
        r = examine(t)
        if not r:
            continue
        ctrl, path, cy, ty, dead, miss, dup = r
        if not (dead or miss or dup):
            print(f"  ✓ {t['id']:24s} 控制表 {len(cy)} 年，与题材声明一致")
            continue

        print(f"\n  {t['id']}:")
        print(f"      控制表 {cy}")
        print(f"      题材   {ty}")
        if miss:
            print(f"      ✗ 题材声明了但控制表没有 {miss} —— 这几年建不出几何")
        if dead:
            print(f"      ! 控制表里多余（永不渲染）{dead}")
        if dup:
            print(f"      ! 相邻年份状态完全相同 {dup} —— 画出来是重复帧")

        if not args.fix:
            continue

        # 只删「多余 且 与相邻的某个已声明年份状态相同」的年份
        to_drop = []
        for y in dead:
            sig = state_of(ctrl, y)
            if sig is None:
                continue
            neighbours = [n for n in ty if abs(n - y) <= 3]
            if any(state_of(ctrl, n) == sig for n in neighbours):
                to_drop.append(y)
        if not to_drop:
            continue
        for y in to_drop:
            ctrl.pop(str(y), None)
        json.dump(ctrl, open(path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"      -> 已删 {to_drop}（与相邻已声明年份状态相同，无损）")
        touched += 1

    print()
    if args.fix:
        print(f"改了 {touched} 个控制表" if touched else "没有可无损删除的年份")
    else:
        print("（只报告。加 --fix 删除可证明无损的冗余年份）")


if __name__ == "__main__":
    main()
