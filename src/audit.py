"""全项目自查：把能自动查的都查一遍。

跑法：
    python src/audit.py              # 离线检查（数据/配置/文档一致性）
    python src/audit.py --render     # 再加「每个题材每个日期都真渲染一遍」（慢）

为什么要有这个：交付时被指出「毛病太多」，而这些都是**靠人肉发现的** ——
README 有两个同名标题、模型加进了接口却没进界面、取景框裁掉内容、
我自己把齐齐哈尔的坐标写成哈尔滨。这些都有共同点：**只要真跑一遍就能发现**。
所以把它们变成脚本，而不是等下次再被指出。
"""
import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))
sys.path.insert(0, HERE)

PROC = os.path.join(ROOT, "data", "processed")
CTRL = os.path.join(ROOT, "data", "control")
TOPICS = os.path.join(ROOT, "data", "topics", "topics.json")

BAD, WARN, OK = [], [], []


def bad(msg):
    BAD.append(msg)
    print(f"  ✗ {msg}")


def warn(msg):
    WARN.append(msg)
    print(f"  ! {msg}")


def ok(msg):
    OK.append(msg)
    print(f"  ✓ {msg}")


def head(t):
    print(f"\n── {t} " + "─" * max(0, 58 - len(t)))


# ══════════════════════════════════════════════════════════════
def check_topics():
    head("题材配置")
    doc = json.load(open(TOPICS, encoding="utf-8"))
    seen = collections.Counter(t.get("id") for t in doc["topics"])
    dup = [k for k, v in seen.items() if v > 1]
    (bad if dup else ok)(f"题材 id 唯一" + (f"（重复：{dup}）" if dup else f"（{len(doc['topics'])} 个）"))
    for t in doc["topics"]:
        tid = t.get("id") or "?"
        for k in ("id", "title", "kind"):
            if not t.get(k):
                bad(f"{tid}: 缺字段 {k}")
        if not (t.get("years") or []):
            warn(f"{tid}: 没声明 years")
        # 引用的文件必须存在
        for key in ("units", "units_file", "control"):
            v = t.get(key)
            if not v:
                continue
            for d in (PROC, CTRL):
                if os.path.exists(os.path.join(d, v)):
                    break
            else:
                bad(f"{tid}: {key} 指向的文件不存在 —— {v}")
        # bbox 合法性
        b = t.get("bbox")
        if not (isinstance(b, list) and len(b) == 4 and b[0] < b[2] and b[1] < b[3]):
            bad(f"{tid}: bbox 不合法 —— {b}")
    return doc


def check_geometry(doc):
    head("几何与取景")
    sys.path.insert(0, HERE)
    import fit_bbox
    from histmap_server import topics as T

    for t in doc["topics"]:
        tid, years = t["id"], [int(y) for y in (t.get("years") or [])]
        if not years:
            continue
        kind = t.get("kind")

        # boundary 类（一战/二战欧洲）用的是现成国界数据集，**没有**逐年几何文件，
        # 每帧直接从 CShapes 裁出来。早先这里一律按 dynasty 要求逐年 geojson，
        # 结果对这两条报「缺 2 年的几何」—— 是自查脚本自己搞错了。
        if kind != "dynasty":
            tt = T.get(tid)
            ready, why = (T.data_ready(tt) if tt else (True, ""))
            if ready:
                ok(f"{tid}: 现成国界数据集就绪（{t.get('dataset', 'cshapes')}）")
            else:
                bad(f"{tid}: 运行期数据缺失 —— {why}")
            continue

        missing = [y for y in years
                   if not os.path.exists(os.path.join(PROC, f"{tid}_{y}_map.geojson"))]
        if missing:
            bad(f"{tid}: 缺 {len(missing)} 年的几何 {missing[:5]}")
            continue

        # 取景框必须是**几何算出来的**，不是人猜的。几何一改（换 max_km、
        # 加锚点、改控制表）范围就变，忘了重跑 fit_bbox 就会裁掉内容。
        bbox = t.get("bbox")
        fitted = fit_bbox.fit(tid, years)
        fill = fit_bbox.data_fill_ratio(bbox, tid, years)
        if fill > 1.02:
            bad(f"{tid}: 数据超出取景框 {fill:.0%}，内容被裁掉了"
                f"（跑 `python src/fit_bbox.py --topic {tid} --write`）")
        elif fill < 0.45:
            bad(f"{tid}: 数据只占取景框 {fill:.0%}，画面会有大片空白")
        elif fitted and bbox != fitted:
            warn(f"{tid}: 取景框与几何没同步 —— 存的 {bbox} 算出来是 {fitted}")
        else:
            ok(f"{tid}: 取景 {fill:.0%}，与几何同步")

        # 每帧的归属方集合
        owners = set()
        for y in years:
            gj = json.load(open(os.path.join(PROC, f"{tid}_{y}_map.geojson"), encoding="utf-8"))
            for f in gj["features"]:
                owners.add(f["properties"].get("owner"))
        if len(owners) < 2:
            warn(f"{tid}: 全年份只有 {len(owners)} 个归属方 —— 是不是少了？")
        # 归属方名语言：中文产品，图上标签不该是纯英文
        eng = [o for o in owners if o and not re.search(r"[\u4e00-\u9fff]", str(o))]
        if eng:
            warn(f"{tid}: {len(eng)} 个归属方名不是中文 —— {sorted(eng)[:6]}")

        # **模型编的单元名**：这些名字在数据集里不存在，图上就不会有那块区域。
        # 这是「地图不对、缺区域缺地名」最常见的原因，而它原本被 --quiet 吞掉，
        # 用户只能看到结果不对、不知道原因。build_dynasty_map 把它们记在
        # geometry 的 _meta.units_missing 里，这里报出来。
        allmiss: dict = {}
        for y in years:
            p = os.path.join(PROC, f"{tid}_{y}_map.geojson")
            try:
                meta = (json.load(open(p, encoding="utf-8")).get("_meta") or {})
            except Exception:
                continue
            for nm in (meta.get("units_missing") or []):
                allmiss.setdefault(str(nm), 0)
                allmiss[str(nm)] += 1
        if allmiss:
            names = sorted(allmiss)
            warn(f"{tid}: {len(names)} 个单元名在数据源里不存在，"
                 f"这些区域**图上不会出现**：{names[:8]}"
                 f"{'…' if len(names) > 8 else ''}（模型写错了名字）")

        # 配色：模型给的色板会盖掉自动配色，质量参差。这里量它的感知分离度。
        try:
            sys.path.insert(0, HERE)
            import build_dynasty_map as B
            tp = T.get(tid)
            ctrl = json.load(open(tp.control_path(), encoding="utf-8"))
            clean, issues = B.validate_palette(ctrl.get("_palette") or {}, [])
            if issues:
                warn(f"{tid}: 控制表里的配色有问题，构建时会改用自动配色：{issues[:2]}")
            elif clean:
                ok(f"{tid}: 配色 {len(clean)} 色通过校验")
        except Exception as e:
            warn(f"{tid}: 配色校验失败 {type(e).__name__}: {e}")


def check_control(doc):
    head("控制表与年份一致性")
    sys.path.insert(0, HERE)
    import reconcile_years as RY
    from histmap_server import topics as T
    for t in doc["topics"]:
        c = t.get("control")
        if not c:
            continue
        p = os.path.join(CTRL, c)
        if not os.path.exists(p):
            continue
        d = json.load(open(p, encoding="utf-8"))
        if t.get("kind") == "boundary":
            continue
        ctrl_years = sorted(int(k) for k in d
                            if not str(k).startswith("_") and str(k).isdigit())
        topic_years = sorted(int(y) for y in (t.get("years") or []))
        dead = [y for y in ctrl_years if y not in topic_years]
        if dead:
            bad(f"{t['id']}: 控制表里有多余年份 {dead}（永不渲染，是死数据）")
        else:
            ok(f"{t['id']}: 控制表 {len(ctrl_years)} 年，与题材声明一致")
        # 相邻年份状态完全相同 = 两帧画出来一模一样。
        # 这不是抽象担忧：实测 5 个题材都有 —— 宋的 1040 与 1080 控制表逐字相同，
        # 印度分治的 1947/1950/1960/1970 四年同态。出片时现在会自动合并成一帧
        # （标题写年份区间，见 app.merge_identical_frames），但根子上还是缺史实。
        tp = T.get(t["id"])
        dup = RY.duplicate_states(tp) if tp else []
        if dup:
            warn(f"{t['id']}: {len(dup)} 个年份与上一年状态完全相同 {dup}"
                 f" —— 出片会合并成一帧；要真正的演化得先补史实变化")
        # 全部年份同态 = 这个题材根本没有动画可言，出来是一张静止图。
        if tp:
            runs = T.identical_runs(tp)
            if len(runs) == 1 and len(runs[0]) > 1:
                bad(f"{t['id']}: 声明了 {len(runs[0])} 个年份但疆域全程没变"
                    f"（{runs[0][0]}–{runs[0][-1]}）—— 出片只能是一张静止图")


def check_orphans(doc):
    head("孤儿文件")
    used = set()
    for t in doc["topics"]:
        for key in ("units", "units_file", "control"):
            if t.get(key):
                used.add(t[key])
    # 这些是**构建管线的中间产物**：由某个脚本生成、又被下一个脚本按固定路径读写，
    # 所以不会被 topics.json 直接引用，但也不是垃圾。逐个列出来，别让真正的
    # 孤儿文件混在里面被忽略（早先每跑一次自查都刷 6 条噪声，真正的孤儿
    # control/ww2.json 反而淹没在里面，一直没人处理）。
    KEYS = {
        "processed/fangzhen_raw.json": "fetch_fangzhen.py 抓的《新唐书·方镇表》原文",
        "processed/tang_zhou_dict.json": "build_tang_gazetteer.py 的州治字典，tang 管线的中间产物",
        "processed/tang_zhou_sample.json": "同一个脚本的抽样检查输出",
        "processed/ops_807.json": "唐 807 归属推导的中间结果",
        "processed/state_807.json": "唐 807 归属推导的中间结果",
        "processed/wikidata_probe.json": "一次数据源探测的留档",
    }
    skipped = 0
    for d, label in ((PROC, "processed"), (CTRL, "control")):
        for fn in sorted(os.listdir(d)):
            if fn.startswith("_") or fn.endswith("_checked.json"):
                continue
            if fn in used:
                continue
            # 地图几何以「题材id_年份_map.geojson」命名
            if re.match(r"^.+_\d{3,4}_map\.geojson$", fn):
                tid = fn.rsplit("_", 2)[0]
                if any(t["id"] == tid for t in doc["topics"]):
                    continue
            if fn.endswith(".geojson") and any(
                    fn.startswith(t["id"] + "_") for t in doc["topics"]):
                continue
            if f"{label}/{fn}" in KEYS:
                skipped += 1
                continue
            warn(f"{label}/ 里没有被任何题材引用：{fn}")
    if skipped:
        ok(f"另有 {skipped} 个文件是已知的管线中间产物（见 KEYS 清单）")


def check_docs(doc):
    head("文档一致性")
    sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
    from histmap_server import topics as T
    ids = {t["id"] for t in doc["topics"]}
    rd = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    h = [l for l in rd.split("\n") if l.startswith("## ")]
    dup = [k for k, v in collections.Counter(h).items() if v > 1]
    (bad if dup else ok)("README 无同名二级标题" + (f"：{dup}" if dup else f"（{len(h)} 个）"))
    # README 的题材表用的是**中文标题**，不是 id —— 早先只按 id 找，
    # 于是每条都被报成「没提到」。按 id 或标题任一命中就算提到。
    miss = sorted(t["id"] for t in doc["topics"]
                  if t["id"] not in rd and (t.get("title") or "\0") not in rd)
    if miss:
        warn(f"README 没提到这些题材：{miss}")
    if chr(8) in rd:
        bad("README 里混进了退格符")
    # 前端：JS 取用的 id 必须都在 HTML 里
    ah = open(os.path.join(ROOT, "web", "app.html"), encoding="utf-8").read()
    have = set(re.findall(r'id="([^"]+)"', ah))
    used = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", ah))
    miss2 = sorted(used - have)
    (bad if miss2 else ok)(f"前端 DOM id 齐全" + (f"（缺 {miss2}）" if miss2 else f"（{len(used)} 个）"))
    # 前后端各写一份常量是踩过的坑，这里点名几个关键的
    if "max_frames" in ah:
        vals = set(re.findall(r"MAX_FRAMES\s*=\s*(\d+)", ah))
        srv = set(re.findall(r"max_frames:\s*int\s*=\s*(\d+)", open(
            os.path.join(ROOT, "packages", "server", "histmap_server", "app.py"),
            encoding="utf-8").read()))
        if vals and srv and vals != srv:
            bad(f"前后端 max_frames 不一致：前端 {vals} vs 服务端 {srv}")
        else:
            ok(f"前后端帧数上限一致（{vals or srv}）")


def check_render(doc):
    head("每个题材每个日期真渲染（慢）")
    import urllib.request, urllib.error
    B = "http://127.0.0.1:8810"
    tot = fail = 0
    for t in doc["topics"]:
        tid = t["id"]
        try:
            sc = json.loads(urllib.request.urlopen(f"{B}/api/scenes", timeout=60).read())
            s = next((x for x in sc["scenes"] if x["id"] == tid), None)
        except Exception as e:
            bad(f"服务连不上：{e}")
            return
        if not s:
            bad(f"{tid}: 服务端题材列表里没有它")
            continue
        ds = s.get("dates") or []
        for d in ds:
            tot += 1
            r = urllib.request.Request(f"{B}/api/render", method="POST",
                                       data=json.dumps({"scene": tid, "date": d,
                                                        "theme": (s.get("themes") or ["light"])[0],
                                                        "size": "16x9"}).encode(),
                                       headers={"Content-Type": "application/json"})
            try:
                j = json.loads(urllib.request.urlopen(r, timeout=600).read())
                if not j.get("image"):
                    fail += 1
                    bad(f"{tid} {d}: 没返回图片")
            except urllib.error.HTTPError as e:
                fail += 1
                bad(f"{tid} {d}: HTTP {e.code} {e.read().decode()[:120]}")
            except Exception as e:
                fail += 1
                bad(f"{tid} {d}: {type(e).__name__}: {e}")
        print(f"  · {tid}: {len(ds)} 个日期" + (" 全过" if not fail else ""))
    ok(f"渲染 {tot - fail}/{tot} 帧成功")


def check_agent_graph():
    """模型编排图的结构自查。

    为什么要查拓扑：起草流程改成 LangGraph 之后，「哪一步接哪一步」变成了
    运行期的数据，而不是读代码就能看出来的控制流。改错一条边（比如
    validate 直连 finalize，去掉修复环）不会报错，只会让「模型漏年份时
    不再重试」—— 那种退化靠读代码很难发现，跑一遍图看边就一眼可见。
    """
    head("模型编排图")
    try:
        sys.path.insert(0, os.path.join(ROOT, "packages", "agent"))
        from histmap_agent.draft_graph import build_draft_graph
    except Exception as e:
        bad(f"起草图导入失败：{type(e).__name__}: {e}")
        return
    try:
        g = build_draft_graph().get_graph()
    except Exception as e:
        bad(f"起草图编译失败：{type(e).__name__}: {e}")
        return
    nodes = set(g.nodes)
    want_nodes = {"draft_geometry", "fetch_units", "assign_owners",
                  "draft_gazetteer", "validate", "repair_prompt", "finalize"}
    miss = sorted(want_nodes - nodes)
    if miss:
        bad(f"起草图缺节点：{miss}")
    else:
        ok(f"起草图 {len(want_nodes)} 个节点齐全")

    pairs = {(e.source, e.target) for e in g.edges}
    must = {
        ("__start__", "draft_geometry"): "入口",
        ("draft_geometry", "fetch_units"): "partition 支路",
        ("draft_geometry", "draft_gazetteer"): "gazetteer 支路",
        ("fetch_units", "assign_owners"): "拿到真名后分配归属",
        ("assign_owners", "validate"): "分配完必须校验",
        ("repair_prompt", "assign_owners"): "修复环（重试）",
        ("validate", "repair_prompt"): "校验不过走修复",
        ("validate", "finalize"): "校验通过收尾",
        ("finalize", "__end__"): "出口",
    }
    gone = [why for p, why in must.items() if p not in pairs]
    if gone:
        bad(f"起草图少了这些边：{gone}")
    else:
        ok(f"起草图 {len(must)} 条关键边都在（含重试环）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--render", action="store_true")
    args = ap.parse_args()

    print("=" * 62)
    print("  histmap 全项目自查")
    print("=" * 62)

    doc = check_topics()
    check_geometry(doc)
    check_control(doc)
    check_agent_graph()
    check_orphans(doc)
    check_docs(doc)
    if args.render:
        check_render(doc)
    else:
        print("\n（跳过逐帧渲染，加 --render 开启）")

    print("\n" + "=" * 62)
    print(f"  通过 {len(OK)} 项 · 警告 {len(WARN)} 项 · **错误 {len(BAD)} 项**")
    if BAD:
        print("\n  必须修：")
        for b in BAD:
            print(f"    ✗ {b}")
    if WARN:
        print("\n  值得看一眼：")
        for w in WARN:
            print(f"    ! {w}")
    print("=" * 62)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
