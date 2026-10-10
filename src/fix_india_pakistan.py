"""一次性数据订正：印巴分治题材补上东巴基斯坦、克什米尔归属变化、孟加拉国独立。

为什么必须改：这张图原来只有 IND 36 + PAK 6 个单元，**没有东巴基斯坦**。
而控制表里 1970 年的 `_era` 写的却是「东巴基斯坦独立运动兴起」——
文案在说东巴，图上根本没有东巴。1947 年那张图里巴基斯坦缺了整个东部，
是史实错误，不只是不好看。

同时它声明了 1947/1950/1960/1970 四个年份，但四年控制状态逐字相同，
出片只能是一张静止图。补上下面两处真实变化后就有真正的演化了：

  1947        克什米尔（查谟-克什米尔土邦）归属未定 —— 未加入任一自治领
  1949        第一次印巴战争停火，沿停火线分占（今控制线）
  1972        东巴基斯坦独立为孟加拉国

1949/1950/1960/1970 四年状态相同，出片时会自动合并成一帧「1949–1970」。

只用**已存在的单元**表达，不新增任何几何（BGD 的 8 个专区由 add_units.py 加）。
归属是史实判断，写在这里以便复核。
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 东巴基斯坦 = 今孟加拉国，1971 年独立战争后成为孟加拉国（1972 年正式成立）
EAST_PAKISTAN = ["Barisal", "Chittagong", "Dhaka", "Khulna",
                 "Mymensingh", "Rajshani", "Rangpur", "Sylhet"]

# 查谟-克什米尔土邦：1947 年整个土邦未加入印度或巴基斯坦。
# 1947 年 10 月王公签署加入印度 → 第一次印巴战争 → 1949 年停火形成控制线：
# 印度得查谟/克什米尔谷地/拉达克，巴基斯坦得自由克什米尔/吉尔吉特-巴尔蒂斯坦。
# 所以**四个单元在 1947 年都算未定**，1949 年起才分属两国。
# （自由克什米尔 1947 年 10 月、吉尔吉特 1947 年 11 月事实上归巴，但那是
#   1947 年下半年的事；这一帧标的是「1947 年」，按土邦未定处理更自洽。）
KASHMIR_CORE = ["Jammu and Kashmīr", "Ladākh",
                "Azad Kashmir", "Gilgit-Baltistan"]

# 三份基准名单写死，让这个脚本**可重复跑**（幂等）。
# 如果从当前控制表里反推，跑第二遍时 1947 已经是改过的样子，会越改越乱。
INDIA_ALL = [
    "Andaman and Nicobar Islands", "Andhra Pradesh", "Arunāchal Pradesh", "Assam",
    "Bihār", "Chandīgarh", "Chhattīsgarh", "Delhi",
    "Dādra and Nagar Haveli and Damān and Diu", "Goa", "Gujarāt", "Haryāna",
    "Himāchal Pradesh", "Jammu and Kashmīr", "Jhārkhand", "Karnātaka", "Kerala",
    "Ladākh", "Lakshadweep", "Madhya Pradesh", "Mahārāshtra", "Manipur",
    "Meghālaya", "Mizoram", "Nāgāland", "Odisha", "Puducherry", "Punjab",
    "Rājasthān", "Sikkim", "Tamil Nādu", "Telangāna", "Tripura",
    "Uttar Pradesh", "Uttarākhand", "West Bengal"]
PAKISTAN_WEST = ["Azad Kashmir", "Balochistan", "Gilgit-Baltistan",
                 "Islamabad Capital Territory", "Khyber Pakhtunkhwa", "Sindh"]

YEARS = [1947, 1949, 1950, 1960, 1970, 1972]

ERA = {
    "1947": "印巴分治；查谟-克什米尔土邦归属未定",
    "1949": "第一次印巴战争停火，克什米尔沿停火线分占",
    "1950": "印度共和国成立",
    "1960": "印巴关系紧张，边界争议持续",
    "1970": "东巴基斯坦独立运动兴起",
    "1972": "东巴基斯坦独立为孟加拉国",
}


def main():
    from histmap_server import topics as T
    t = T.get("india-pakistan-partition")
    p = t.control_path()
    c = json.load(open(p, encoding="utf-8"))

    # 单元表里必须真有这些名字，拼错了要立刻发现，不能悄悄少一块
    uf = json.load(open(T._units_path(t), encoding="utf-8"))
    have = {(f.get("properties") or {}).get("name") for f in uf["features"]}
    allwant = set(INDIA_ALL) | set(PAKISTAN_WEST) | set(EAST_PAKISTAN)
    gone = sorted(allwant - have)
    if gone:
        raise SystemExit(f"单元表里没有这些名字，脚本要改：{gone}")
    extra = sorted(have - allwant)
    if extra:
        print(f"  注意：单元表里还有名单外的单元，未参与归属：{extra}")

    kash = [x for x in KASHMIR_CORE]
    ind_1947 = [x for x in INDIA_ALL if x not in kash]
    pak_1947 = [x for x in PAKISTAN_WEST if x not in kash]

    for y in YEARS:
        if y == 1947:
            c[str(y)] = {
                "印度": ind_1947,
                "巴基斯坦": pak_1947 + EAST_PAKISTAN,
                "克什米尔（未定）": kash,
            }
        elif y == 1972:
            c[str(y)] = {"印度": INDIA_ALL, "巴基斯坦": PAKISTAN_WEST,
                         "孟加拉国": EAST_PAKISTAN}
        else:
            c[str(y)] = {"印度": INDIA_ALL,
                         "巴基斯坦": PAKISTAN_WEST + EAST_PAKISTAN}

    for y in list(c):
        if not str(y).startswith("_") and str(y).isdigit() and int(y) not in YEARS:
            print(f"  删掉不再声明的年份 {y}")
            del c[y]

    c["_era"] = ERA
    c["_palette"] = {
        "印度": "#4a6fa5",          # 蓝
        "巴基斯坦": "#9c5b52",      # 砖红
        "孟加拉国": "#5f8f63",      # 绿（孟加拉国旗也是绿）
        "克什米尔（未定）": "#a08a5c",  # 土黄，沿用原「未分配」色
    }
    c["_footer"] = ("本地图使用现代一级行政区界线，而非当时的实际界线；"
                    "克什米尔按停火线近似，1947 年的东巴基斯坦即今孟加拉国。")

    json.dump(c, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"已写回 {p}")
    for y in YEARS:
        print(f"  {y}: " + "、".join(
            f"{o}{len(v)}" for o, v in c[str(y)].items()))

    # 题材声明的年份也要跟着改
    tp = os.path.join(ROOT, "data", "topics", "topics.json")
    doc = json.load(open(tp, encoding="utf-8"))
    for x in doc["topics"]:
        if x["id"] == "india-pakistan-partition":
            x["years"] = YEARS
            x["source_note"] = c["_footer"]
            # 许可说明用「；」分段，去重后重新拼 —— 追加式的写法即使加了
            # 存在性判断，之前跑坏了的历史副本也清不掉；按段去重才自愈。
            bdg = "geoBoundaries BGD 为 CC BY 4.0（须署名）"
            parts = [s.strip() for s in (x.get("license_note") or "").split("；")]
            parts = [s for s in parts if s]
            if bdg not in parts:
                parts.append(bdg)
            seen, uniq = set(), []
            for s in parts:
                if s not in seen:
                    seen.add(s)
                    uniq.append(s)
            x["license_note"] = "；".join(uniq)
            print("题材 years ->", x["years"])
    json.dump(doc, open(tp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"已写回 {tp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
