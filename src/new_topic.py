"""通用题材构建器：把一份「题材规格」变成能出图的新题材。

跑法
----
    # 看有哪些国家 / 某个国家有哪些一级行政区（给人工或模型核对用）
    python src/new_topic.py --units USA ADM1

    # 按规格构建（不注册）
    python src/new_topic.py --spec my_topic.json

    # 构建并注册进 data/topics/topics.json
    python src/new_topic.py --spec my_topic.json --install

    # 让模型起草规格（需要 SILICONFLOW_API_KEY，或 --key）
    python src/new_topic.py --draft "北美内战" --install

为什么要有这个文件
------------------
在这之前，加一个题材 = 手工裁行政区文件 + 手工敲坐标表 + 手工写归属表，
一个题材干一次。这不是「系统」，是人力。所以把「通用」的那部分抽出来：

    · 几何：任意国家的行政区从 geoBoundaries 按需拉（约 200 个国家）
    · 归属：由规格文件给出（可以人工写，也可以让模型起草后人工过一眼）
    · 构建：复用 build_dynasty_map.py 的两条几何来源
    · 校验：单元名对不上的**必须报出来**，不能悄悄出一张空一半的图

规格格式（spec）
----------------
{
  "id": "us-civil-war",
  "title": "美国内战",
  "kind": "partition",                 // partition=现成多边形；gazetteer=坐标表+县归并
  "geometry": { "source": "geoboundaries", "iso": "USA", "adm": "ADM1" },
  "bbox": [-126, 24, -66, 50],
  "years": [1861, 1865],
  "merge_by": "owner",
  "palette": { "联邦": "#4a6fa5", "邦联": "#9c5b52" },
  "control": { "1861": {"联邦": ["Maine"], "邦联": ["Texas"]} },
  "source_note": "……",
  "license_note": "geoBoundaries USA CC BY 4.0"
}

gazetteer 类用 "units": [{"name","lon","lat","modern"}, ...] 代替 geometry。
"""
import argparse
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "packages", "core"))
sys.path.insert(0, os.path.join(ROOT, "packages", "server"))
sys.path.insert(0, os.path.join(ROOT, "packages", "agent"))

# 取几何、名字匹配、许可声明都搬到了 histmap_agent.geodata（见那里的注释：
# 起草图要用这些函数，若它们留在这里就会形成 new_topic ↔ agent 的循环 import）。
# 这里再导出一次，是为了不改动 add_units.py 等已有的调用方。
from histmap_agent import (GB_LICENSE, ModelAuthError,   # noqa: E402,F401
                           fetch_adm, gb_license, match_names, units_of)
from histmap_agent.geodata import _norm                  # noqa: E402,F401
from histmap_agent.llm import LLMConfig                  # noqa: E402

CACHE = os.path.join(ROOT, "data", "cache")
PROC = os.path.join(ROOT, "data", "processed")
CTRL = os.path.join(ROOT, "data", "control")
TOPICS = os.path.join(ROOT, "data", "topics", "topics.json")
UA = {"User-Agent": "histmap/0.1 (+https://github.com/; topic builder)"}
GB_API = "https://www.geoboundaries.org/api/current/gbOpen/{iso}/{adm}/"


# 取几何 / 名字匹配 / 许可声明已移到 histmap_agent.geodata，
# 在本文件顶部 import 回来（见那里的注释）。


def _year(v) -> int:
    """从年份的各种写法里取出**年份数字**。

    为什么需要它：模型给的年份格式不固定，实测见过
    `1387`、`"1387"`、`"1387-01-01"`、`"1387年"`。
    而原来这里是直接 `int(y)`，于是模型一写完整日期就炸：

        构建失败：ValueError: invalid literal for int() with base 10: '1387-01-01'

    用户看到的是这句 ValueError，完全不知道是模型把年份写成了日期。
    控制表的**键**也有同样的问题，所以归一化时也用它。
    """
    s = str(v).strip()
    m = re.match(r"^(-?\d{1,4})", s)
    if not m:
        raise SystemExit(f"看不懂的年份写法：{v!r}（应是 1387 或 1387-01-01）")
    return int(m.group(1))


def build(spec: dict, install: bool = False, quiet: bool = False) -> dict:
    tid = spec.get("id") or _slug(spec.get("title") or "topic")
    kind = spec.get("kind") or "partition"
    if kind not in ("partition", "gazetteer", "atlaspi"):
        raise SystemExit(
            f"kind 只能是 partition / gazetteer / atlaspi，收到 {kind}"
            f"（atlaspi = 用 AtlasPI 的真实历史边界，按年现查）")

    os.makedirs(PROC, exist_ok=True)
    os.makedirs(CTRL, exist_ok=True)
    report = {"id": tid, "kind": kind, "warnings": []}

    # ── 0) AtlasPI：真实历史边界，不需要控制表也不需要预构建几何 ──
    # 为什么单独一条：这条路的数据源是**按年查询的历史政体多边形**
    # （atlaspi.it，Apache-2.0 可商用，覆盖公元前 4500 – 2024），
    # 拿到的就是当年的真实边界，而不是「现代县界反推」。
    # 而且因为按年查，「任何年份都能出图」天然成立。
    if kind == "atlaspi":
        entry = {
            "id": tid, "title": spec.get("title") or tid,
            "subtitle": spec.get("subtitle") or "",
            "kind": "atlaspi",
            "bbox": spec.get("bbox") or [70, 15, 140, 55],
            "focus": spec.get("focus") or spec.get("bbox") or [70, 15, 140, 55],
            "include": spec.get("include") or [],
            "exclude": spec.get("exclude") or [],
            "min_confidence": spec.get("min_confidence", 0.5),
            "max_entities": spec.get("max_entities", 12),
            "years": spec.get("years") or [],
            "themes": spec.get("themes") or ["light"],
            "default_date": _pick_default_date(spec.get("default_date"),
                                               [_year(y) for y in (spec.get("years") or [])]
                                               or [1900]),
            "default_size": spec.get("default_size") or "16x9",
            "source_note": spec.get("source_note") or (
                "边界数据来自 AtlasPI（atlaspi.it，Apache-2.0），"
                "是学术近似的历史政体多边形，不是测绘界线。"),
            "license_note": "AtlasPI · Apache-2.0 · 可商用",
        }
        _register(entry)
        report["entry"] = entry
        report["built_years"] = 0      # 按年现查，不需要预构建
        report["atlaspi"] = True
        report["warnings"].append(
            "AtlasPI 题材：边界按年份**现查**，所以任何年份都能出图，"
            "不需要预先构建；首次取某一年要联网（约 5–20 秒），之后走缓存。")
        return report

    # ── 1) 几何：单元文件或坐标表 ──
    if kind == "partition":
        geo = spec.get("geometry") or {}
        if (geo.get("source") or "geoboundaries") != "geoboundaries":
            raise SystemExit("partition 类目前只支持 geometry.source = geoboundaries")
        # 支持多国：德意志统一涉及德/奥/法/丹，单国 ADM1 根本装不下。
        #   "sources": [{"iso":"DEU","adm":"ADM1"}, {"iso":"AUT","adm":"ADM1"}, ...]
        sources = geo.get("sources") or [geo]
        fields = {}
        merged = os.path.join(CACHE, f"_merged_{tid}.geojson")
        all_feats, name_origin = [], {}
        for g in sources:
            iso = (g.get("iso") or "").upper()
            adm = (g.get("adm") or "ADM1").upper()
            nf = g.get("name_field") or "shapeName"
            src = fetch_adm(iso, adm, force=bool(g.get("force")))
            gj = json.load(open(src, encoding="utf-8"))
            for f in gj["features"]:
                nm = str((f.get("properties") or {}).get(nf) or "").strip()
                if not nm:
                    continue
                if nm in name_origin and name_origin[nm] != iso:
                    # **撞名不要丢掉，要拆开。**
                    # 原来这里是 `continue` —— 直接丢弃后一个，只留一条警告。
                    # 后果很严重而且很隐蔽：印度和巴基斯坦**各有一个省叫 Punjab**，
                    # 于是巴基斯坦的旁遮普整块消失了。图上表现为克什米尔旁边
                    # 一个大白洞 —— 看起来像渲染坏了，其实是数据在合并时被悄悄删掉。
                    # 现在改成带国家后缀的独立单元，两个都保留。
                    nm2 = f"{nm} ({iso})"
                    report["warnings"].append(
                        f"单元名「{nm}」在 {name_origin[nm]} 和 {iso} 里都有"
                        f"（例：印度与巴基斯坦各有一个 Punjab）—— 已拆成"
                        f"「{nm}」（{name_origin[nm]}）和「{nm2}」，**两个都保留**。"
                        f"控制表里要分别引用这两个名字。")
                    nm = nm2
                name_origin[nm] = iso
                f2 = dict(f)
                f2["properties"] = {"id": nm, "name": nm, "iso": iso}
                all_feats.append(f2)
        json.dump({"type": "FeatureCollection", "features": all_feats},
                  open(merged, "w", encoding="utf-8"), ensure_ascii=False)
        report["units_in_source"] = len(name_origin)
        report["sources"] = [f"{g.get('iso')} {g.get('adm', 'ADM1')}" for g in sources]

        # 规格里出现的所有单元名（从 control 里收集），对到数据集上的真名
        want = []
        for yr, groups in (spec.get("control") or {}).items():
            if yr.startswith("_"):
                continue
            for _owner, names in groups.items():
                want += list(names)
        want = list(dict.fromkeys(want))
        mapping, bad = match_names(want, list(name_origin))
        if bad:
            report["unmatched"] = bad
            report["warnings"].append(
                f"有 {len(bad)} 个单元名在数据集里找不到，这些地方会是空白：{bad[:12]}")
        if not mapping:
            raise SystemExit(
                "一个单元名都没对上。数据集里的真名是：\n  " +
                "、".join(sorted(name_origin)[:40]) +
                "\n（用 --units ISO ADM 可以完整列出来）")

        keep = sorted(set(mapping.values()))
        uf = os.path.join(PROC, f"{tid}_units.geojson")
        _write_units(merged, uf, keep, "name",
                     float(geo.get("simplify") or 0.02))
        report["units_file"] = os.path.relpath(uf, ROOT).replace("\\", "/")
        report["units_kept"] = len(keep)
        report["coverage"] = f"{len(keep)}/{len(want)} 个规格单元找到了几何"

        # control 里的名字换成真名
        ctrl_data = _remap_control(spec, mapping)
        units_key, units_val = "units_file", os.path.basename(uf)
    else:
        units = spec.get("units") or []
        if not units:
            raise SystemExit("gazetteer 类必须给 units（name/lon/lat）")

        # **逐点几何校验**。这是整条链路最要紧的一步，原来它是**手工**跑的
        # （src/check_gazetteer.py），没接进管线 —— 也就是说模型给的坐标
        # 一个都没验就落盘了。后果正是「内容不对」里最难查的那两种：
        #   · 坐标写错（落到隔壁省/海里）→ 县归错了 → **边界错**
        #   · 坐标落在任何县境之外 → 那个单元分不到县 → **缺区域**
        # 现在在本流程里直接验，并按结果决定是继续还是拦下。
        try:
            import check_gazetteer as CG
            counties = CG.load_counties()
            okpts, badpts = CG.check(units, counties, verbose=False)
            report["units_checked"] = len(units)
            report["units_offshore"] = len(badpts)
            if badpts:
                names = [r.get("name") for r in badpts]
                report["units_offshore_names"] = names
                frac = len(badpts) / max(1, len(units))
                report["warnings"].append(
                    f"{len(badpts)}/{len(units)} 个治所坐标**不落在任何中国县境内**"
                    f"（{frac:.0%}）：{names[:8]}{'…' if len(names) > 8 else ''}"
                    f" —— 这些坐标几乎可以确定是错的，它们周边的县会被归错，"
                    f"图上表现为边界错或整块区域消失")
                if frac >= 0.25:
                    raise SystemExit(
                        f"超过 1/4 的治所坐标是错的（{len(badpts)}/{len(units)}）：{names[:8]}。"
                        f"用这份坐标建出来的图**边界是错的**，不建。"
                        f"请换一个模型重起草，或手工订正这些坐标。")
                # 少量错点：剔掉它们，别让错误坐标把周边的县也带歪
                units = okpts
                report["warnings"].append(
                    f"已剔除这 {len(badpts)} 个错点（少几块区域，好过边界错）")
            elif not quiet:
                print(f"  坐标校验通过：{len(okpts)}/{len(units)} 个治所落在真实县境内")
        except SystemExit:
            raise
        except Exception as e:
            report["warnings"].append(
                f"坐标校验没跑成（{type(e).__name__}: {e}）—— "
                f"坐标可能有问题但这次没验出来，请手工跑 "
                f"python src/check_gazetteer.py <坐标表>")

        if not units:
            raise SystemExit("坐标校验后一个可用单元都不剩，无法建图")
        gf = os.path.join(PROC, f"{tid}_province_gazetteer.json")
        json.dump({"_comment": f"{spec.get('title')} 单元坐标表（由 new_topic.py 生成）",
                   "_source": spec.get("units_source") or "见 source_note",
                   "points": units},
                  open(gf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        report["gazetteer"] = os.path.relpath(gf, ROOT).replace("\\", "/")
        report["units_kept"] = len(units)
        ctrl_data = _remap_control(spec, {})
        units_key, units_val = "units", os.path.basename(gf)

    # ── 2) 控制表 ──
    ctrl_name = spec.get("control_file") or f"{tid}_control.json"
    # **把控制表的年份键统一成纯年份。**
    # 模型经常把键写成完整日期（实测 `"1387-01-01"`），而全系统都是按
    # 纯年份查控制表的。不归一化的话，那一年的几何会以各种方式崩 ——
    # 用户看到的就是「构建失败：invalid literal for int() with base 10:
    # '1387-01-01'」，完全不知道是模型写错了格式。
    _nk = {}
    for k, v in list(ctrl_data.items()):
        if str(k).startswith("_"):
            _nk[k] = v
            continue
        try:
            _nk[str(_year(k))] = v
        except SystemExit:
            _nk[k] = v            # 不是年份的键（别的东西）原样留着
    ctrl_data = _nk
    ctrl_path = os.path.join(CTRL, ctrl_name)
    ctrl_data.setdefault("_comment", "由 new_topic.py 生成的题材规格，可直接手改。")
    if spec.get("source_note"):
        ctrl_data.setdefault("_footer", spec["source_note"])
    if spec.get("palette"):
        ctrl_data["_palette"] = spec["palette"]
    if spec.get("era"):
        ctrl_data["_era"] = spec["era"]
    json.dump(ctrl_data, open(ctrl_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    report["control"] = ctrl_name

    # ── 年份必须和控制表对得上 ──
    # 模型很容易在 years 里写一个控制表里没有的年份（实测漏了 1870），
    # 那一年的几何就会构建失败。以控制表为准取交集，并把差异报出来。
    have_years = sorted(int(k) for k in ctrl_data
                        if not str(k).startswith("_") and str(k).isdigit())
    # 模型的 years 可能是 [1387] / ["1387"] / ["1387-01-01"]，统一取年份。
    # **必须去重**：实测模型给过 [1929, 1930, 1930, 1931] —— 同一个年份两遍，
    # 界面上就出现两个一模一样的页签年份，用户点哪个都一样。
    want_years = list(dict.fromkeys(_year(y) for y in (spec.get("years") or [])))
    if len(want_years) != len(spec.get("years") or []):
        report["warnings"].append(
            f"草案里的年份有重复，已去重：{[y for y in (spec.get('years') or [])]}"
            f" → {want_years}")
    if want_years and have_years and set(want_years) - set(have_years):
        dropped = sorted(set(want_years) - set(have_years))
        report["warnings"].append(
            f"声明了 {dropped} 但控制表里没有这些年份，已从题材里去掉"
            f"（以控制表为准）")
    # **以控制表为准，取它的全部年份。**
    # 原来这里是「取交集，交集空才退回控制表」：
    #     years = [y for y in want_years if y in have_years] or have_years
    # 实测踩坑：「明代九边」模型声明 [1380,1442,1506,1550,1640]，
    # 控制表里是 [1392,1424,1449,1550,1616,1630,1644]，
    # **交集恰好只有 1550 一个** —— 于是题材只建了 1 帧，出片是一张静止图，
    # 而控制表里明明有 7 年真实数据。交集"非空"不等于"够用"。
    # 控制表是自己写下来/构建出来的数据，模型声明的年份只是建议，所以直接用它。
    years = have_years

    # 反方向也要报：控制表里多出来的年份。早先只查了一个方向，结果法国大革命
    # 的控制表里留着一个 1793 —— 题材不声明它，所以永远不渲染，成了死数据，
    # 自查时报「控制表年份与题材 years 不一致」我才发现。
    extra = sorted(set(have_years) - set(want_years))
    if want_years and extra:
        report["warnings"].append(
            f"控制表里还有 {extra} 这些年份，题材没声明 —— 要么加进 years 去渲染，"
            f"要么从控制表删掉")

    # 相邻年份的控制状态完全相同 = 这一帧和上一帧长得一模一样，白占一帧。
    # 短视频里这是明显的浪费，而且说明这段史料没抓到变化。
    def _sig(y):
        v = ctrl_data.get(str(y))
        if not isinstance(v, dict):
            return None
        return tuple(sorted((o, tuple(sorted(u)) if isinstance(u, list) else u)
                            for o, u in v.items()))
    dups = [f"{a}={b}" for a, b in zip(years, years[1:])
            if _sig(a) is not None and _sig(a) == _sig(b)]
    if dups:
        report["warnings"].append(
            f"这些相邻年份的控制状态完全一样，画出来是重复帧：{'、'.join(dups)}")

    # ── 3) 注册进 topics.json ──
    entry = {
        "id": tid, "title": spec.get("title") or tid,
        "subtitle": spec.get("subtitle") or "",
        "kind": "dynasty",                     # 渲染复用 dynasty 那条路
        "bbox": spec.get("bbox") or [-180, -60, 180, 75],
        "years": years,
        units_key: units_val,
        "control": ctrl_name,
        "themes": spec.get("themes") or ["light"],
        "default_date": _pick_default_date(spec.get("default_date"), years),
        "default_size": spec.get("default_size") or "16x9",
        "merge_by": spec.get("merge_by") or "owner",
        "source_note": spec.get("source_note") or "",
        # 许可由系统按数据源给，不信模型（见 gb_license 的注释）
        "license_note": (spec.get("license_note") if kind != "partition"
                         else gb_license(sources)),
    }
    if not entry["default_date"]:
        entry.pop("default_date")
    report["entry"] = entry

    # 注册必须在构建**之前** —— build_dynasty_map 是按题材 id 去 topics.json 里
    # 找配置的。先构建后注册的话每次都报「没有这个题材」，几何一年也出不来（踩过）。
    if install:
        _register(entry)
        report["installed"] = True

    # ── 4) 真构建一遍，把几何算出来（不构建就没图可出）──
    if install:
        _rebuild(tid, entry, report, quiet)
        _fit_bbox_after(tid, report, quiet)
    else:
        report["warnings"].append("未加 --install，几何没构建；加 --install 才会真正可用")
    return report


def _fit_bbox_after(tid: str, report: dict, quiet: bool) -> None:
    """几何算完立刻校准取景框。

    必须自动化：模型给的 bbox 实测经常大错 —— 德意志统一只框住德国
    （法奥丹全被裁，数据占比 513%）、法国大革命 390%、明清算出来只有 56%
    （大片空白）。这些都不是我事后发现才修的，而是**每个自动生成的题材都会犯**。
    """
    try:
        import fit_bbox
        doc = json.load(open(TOPICS, encoding="utf-8"))
        for t in doc["topics"]:
            if t.get("id") != tid:
                continue
            years = [_year(y) for y in (t.get("years") or [])]
            if not years:
                return
            old = t.get("bbox")
            new = fit_bbox.fit(tid, years)
            if new and old != new:
                t["bbox"] = new
                json.dump(doc, open(TOPICS, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=2)
                report["bbox"] = new
                report["bbox_note"] = f"取景框由几何算出（模型给的 {old} 会裁掉内容）"
                if not quiet:
                    print(f"  取景框校准 {old} -> {new}")
            return
    except Exception as e:
        report["warnings"].append(f"取景框校准失败：{type(e).__name__}: {e}")


def _slug(s: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "-", str(s)).strip("-").lower()
    return s or "topic"


def _write_units(src: str, dst: str, keep: list[str], field: str,
                 simplify: float) -> None:
    from shapely.geometry import shape, mapping
    gj = json.load(open(src, encoding="utf-8"))
    feats = []
    for f in gj["features"]:
        nm = str((f.get("properties") or {}).get(field) or "").strip()
        if nm not in keep:
            continue
        try:
            g = shape(f["geometry"])
        except Exception:
            continue
        if not g.is_valid:
            g = g.buffer(0)
        if g.is_empty:
            continue
        if simplify > 0:
            g = g.simplify(simplify, preserve_topology=True)
        feats.append({"type": "Feature",
                      "properties": {"id": nm, "name": nm},
                      "geometry": mapping(g)})
    json.dump({"type": "FeatureCollection",
               "_meta": {"source": os.path.basename(src), "simplify": simplify,
                         "count": len(feats)},
               "features": feats},
              open(dst, "w", encoding="utf-8"), ensure_ascii=False)


def _remap_control(spec: dict, mapping: dict) -> dict:
    out = {}
    for k, v in (spec.get("control") or {}).items():
        if k.startswith("_"):
            out[k] = v
            continue
        out[k] = {o: [mapping.get(n, n) for n in names] for o, names in v.items()}
    return out


LIB = os.path.join(ROOT, "data", "library")


def _lib_index() -> dict:
    p = os.path.join(LIB, "index.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    return {"_comment": "素材库：从界面删掉的题材会整份搬到这里，可以再装回来。",
            "items": []}


def _lib_save(idx: dict) -> None:
    os.makedirs(LIB, exist_ok=True)
    json.dump(idx, open(os.path.join(LIB, "index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)


def remove(tid: str, quiet: bool = False) -> dict:
    """把一个题材**移进素材库**（不是销毁）。

    为什么要归档而不是直接删：题材是「模型起草 + 逐年构建」出来的，
    重建一次要一两分钟、还可能因为模型每次写的年份不一样而结果不同 ——
    删掉就真的没了。而界面上又必须能清理（试几次就积一排半成品）。
    所以删 = 整份搬进 data/library/<id>/，随时能装回来。

    搬走的东西（缺一个都会留下孤儿 / 装不回来）：
      · topics.json 里的注册条目  → 存进 library/<id>/entry.json
      · data/control/<id>_*.json
      · data/processed/<id>_*.geojson / *_gazetteer.json
    """
    doc = json.load(open(TOPICS, encoding="utf-8"))
    items = doc["topics"] if isinstance(doc, dict) else doc
    hit = [t for t in items if str(t.get("id")) == tid]
    if not hit:
        raise SystemExit(f"没有这个题材：{tid}")
    entry = dict(hit[0])

    dest = os.path.join(LIB, tid)
    os.makedirs(dest, exist_ok=True)
    moved = []
    for d in (CTRL, PROC):
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.startswith(tid + "_") or f == f"{tid}.json":
                src = os.path.join(d, f)
                tgt = os.path.join(dest, f)
                if os.path.exists(tgt):
                    os.remove(tgt)
                os.replace(src, tgt)          # 移动，不是复制
                moved.append(f)
    json.dump(entry, open(os.path.join(dest, "entry.json"), "w",
                          encoding="utf-8"), ensure_ascii=False, indent=2)

    items[:] = [t for t in items if str(t.get("id")) != tid]
    json.dump(doc, open(TOPICS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    idx = _lib_index()
    idx["items"] = [x for x in idx["items"] if x.get("id") != tid]
    idx["items"].append({"id": tid, "title": entry.get("title") or tid,
                         "years": entry.get("years") or [],
                         "files": len(moved) + 1,
                         "at": __import__("datetime").datetime.now()
                         .strftime("%Y-%m-%d %H:%M")})
    _lib_save(idx)
    if not quiet:
        print(f"  已把题材 {tid} 移入素材库（{len(moved) + 1} 个文件）")
    return {"id": tid, "moved": moved, "library": f"data/library/{tid}",
            "restorable": True}


def library_items() -> list:
    return _lib_index().get("items") or []


def restore(tid: str, quiet: bool = False) -> dict:
    """把素材库里的题材装回来（注册条目 + 数据文件一起归位）。"""
    dest = os.path.join(LIB, tid)
    ep = os.path.join(dest, "entry.json")
    if not os.path.exists(ep):
        raise SystemExit(f"素材库里没有 {tid}")
    entry = json.load(open(ep, encoding="utf-8"))

    back = []
    for f in sorted(os.listdir(dest)):
        if f == "entry.json":
            continue
        for d in (CTRL, PROC):
            if not os.path.isdir(d):
                continue
            # 按文件名判断该回哪个目录：控制表是 *_control.json
            if f.endswith("_control.json") and d == CTRL:
                os.replace(os.path.join(dest, f), os.path.join(d, f))
                back.append(f)
            elif not f.endswith("_control.json") and d == PROC:
                os.replace(os.path.join(dest, f), os.path.join(d, f))
                back.append(f)

    doc = json.load(open(TOPICS, encoding="utf-8"))
    items = doc["topics"] if isinstance(doc, dict) else doc
    items[:] = [t for t in items if str(t.get("id")) != tid]
    items.append(entry)
    json.dump(doc, open(TOPICS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)

    idx = _lib_index()
    idx["items"] = [x for x in idx["items"] if x.get("id") != tid]
    _lib_save(idx)
    try:
        os.rmdir(dest)
    except OSError:
        pass
    if not quiet:
        print(f"  已从素材库装回题材 {tid}（{len(back)} 个文件）")
    return {"id": tid, "restored": back}


def _register(entry: dict) -> None:
    doc = json.load(open(TOPICS, encoding="utf-8"))
    doc["topics"] = [t for t in doc["topics"] if t.get("id") != entry["id"]]
    doc["topics"].append(entry)
    json.dump(doc, open(TOPICS, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"  已注册题材 {entry['id']} -> {TOPICS}")


def _rebuild(tid: str, entry: dict, report: dict, quiet: bool) -> None:
    """调 build_dynasty_map 把每一年的几何算出来。

    这里必须真跑一遍：题材注册了但 *_map.geojson 不在，界面上就是
    「有题材、点开空白」。宁可构建时报错，也不要留着那种状态。
    """
    import subprocess
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([
        os.path.join(ROOT, "packages", "server"),
        os.path.join(ROOT, "packages", "core"), HERE])
    years = entry.get("years") or []
    ok, failed = 0, []
    for y in years:
        r = subprocess.run([sys.executable, os.path.join(HERE, "build_dynasty_map.py"),
                            "--topic", tid, "--year", str(y), "--force", "--quiet"],
                           capture_output=True, text=True, env=env, cwd=ROOT,
                           errors="replace")
        if r.returncode == 0:
            ok += 1
        else:
            failed.append((y, (r.stderr or r.stdout or "").strip().splitlines()[-1:]))
    report["built_years"] = ok
    if failed:
        report["warnings"].append(f"有 {len(failed)} 个年份没构建成功：{failed[:3]}")
    if not quiet:
        print(f"  几何构建 {ok}/{len(years)} 年")

    # **把「模型编的单元名」捞出来报给用户。**
    # build_dynasty_map 会把这些名字记在 geometry 的 _meta 里，但 _rebuild
    # 是用 --quiet 跑的，那些信息**全被吞掉了**。后果是：模型编错几个单元名，
    # 图上就静悄悄少几块区域，用户只看到「地图不对、缺地名」，
    # 完全不知道是模型把名字写错了。
    miss: dict = {}
    for y in years:
        p = os.path.join(PROC, f"{tid}_{y}_map.geojson")
        if not os.path.exists(p):
            continue
        try:
            meta = (json.load(open(p, encoding="utf-8")).get("_meta") or {})
        except Exception:
            continue
        for nm in (meta.get("units_missing") or []):
            miss.setdefault(str(nm), []).append(y)
    if miss:
        names = sorted(miss)
        report["units_missing"] = names
        report["warnings"].append(
            f"有 {len(names)} 个单元名在数据集里找不到，这些区域**图上不会出现**："
            f"{names[:8]}{'…' if len(names) > 8 else ''}"
            f"（模型写错了名字；数据源里没有的单元无法绘制，"
            f"要么改控制表换成真实单元名，要么去掉）")
    if miss and not quiet:
        print(f"  ！{len(miss)} 个单元名对不上，图上会缺这些区域：{sorted(miss)[:8]}")


# ── 让模型起草规格 ────────────────────────────────────────────────────
# 起草提示词已移到 histmap_agent.prompts（那边是唯一一份）。

def draft(ask: str, key: str = "", model: str = "Qwen/Qwen2.5-72B-Instruct",
          verbose: bool = True, allow_env_key: bool = True,
          base: str = "") -> dict:
    """两阶段起草 —— 由 histmap_agent 的 LangGraph 图执行。

    流程本身在 packages/agent/histmap_agent/draft_graph.py，节点和边都写在那儿。
    这个函数只负责**凑齐参数**：key 从哪来、base URL 用哪个、日志往哪打。

    为什么值得改成图：原先是手写的一段流程，而它本来就具备图的三要素 ——
    partition 与 gazetteer 走不同的第三步（步骤会变）、模型漏年份时要重试
    （有循环）、走哪条路取决于第一半模型给出的 kind（分支靠运行结果）。
    换成节点和边之后，「判断行不行」只有 validate 一处、
    「怎么补救」只有 repair 一处，而不是散在缩进和 for 循环里。

    参数含义（对外签名保持兼容，调用方不用改）：
      allow_env_key：命令行用（自己的机器，读 .env 天经地义）；
        **服务端必须传 False** —— 否则任何访客的请求都会拿站长 .env 里的 key
        去调模型，等于把 key 开放给所有人。
      base：接口地址，必须能由调用方指定。界面上选了火山方舟/阿里百炼时，
        它们的 key 只能打自己的域名；早先这个参数根本不存在、客户端选的 base URL
        被丢掉，于是别家的 key 被发到硅基流动，必然 401。
    """
    from histmap_agent.draft_graph import draft_with_graph
    from histmap_agent.llm import PROVIDERS, server_default

    # key 和 base **必须成对来自同一家**。
    # 早先这里是两段独立的兜底：
    #     key  = key or os.environ["SILICONFLOW_API_KEY"] or .env 里的
    #     base = base or os.environ["SILICONFLOW_BASE"] or "api.siliconflow.cn"
    # 于是「服务端配的是火山方舟」时会凑出**硅基的 key + 火山的地址**，
    # 火山回 `401 AuthenticationError: The API key format is incorrect` ——
    # 看着像 key 错了，其实是**拿错了那一家**。同一个 Key 在不同服务商之间
    # 不通用，所以这两件事不能分开兜底。
    base = (base or "").strip()
    if not key and not base:
        d = server_default()
        key, base, model = d.key, d.base, (model or d.model)
    elif not base:
        raise SystemExit(
            "给了 Key 却没给接口地址 —— 同一个 Key 在各家服务商之间不通用，"
            "我不能替你猜是哪一家。请把 base 一起传（网页端会自动带上）。")
    elif not key:
        b = base.rstrip("/")
        hit = next((n for n, p in PROVIDERS.items()
                    if p["base"].rstrip("/") == b), None)
        if not hit:
            raise SystemExit(f"服务端没有配 {b} 这一家的 Key")
        d = server_default(provider=hit)
        key = d.key
        model = model or d.model
    base = base.rstrip("/")
    if not key:
        raise SystemExit("起草规格需要模型 Key：在 .env 里配一家，或用 --key 传")
    cfg = LLMConfig(key=key, model=model, base=base)
    spec, _ = draft_with_graph(ask, cfg, verbose=verbose)
    return spec



def _pick_default_date(want, years: list) -> str | None:
    """挑一个**真的建出来了**的年份当默认日期。

    为什么不能直接用模型给的值：模型的 `years` 和控制表的键经常对不上
    （实测「明代九边」模型声明 1376/1435/1550/1600/1642，控制表里却是
    1375/1442/1529/1640），而 `default_date` 是模型单独给的 —— 它给的
    1550 属于被丢弃的那一套。照抄下来的后果是：题材注册成功、点击也正常，
    但**打开就报「没有这一年的几何」**，用户完全不知道为什么。
    """
    if want:
        try:
            y = _year(want)
            if y in years:
                return str(want)
        except SystemExit:
            pass
    return f"{years[len(years)//2]}-01-01" if years else None


def _key_from_env_file() -> str:
    p = os.path.join(ROOT, ".env")
    if not os.path.exists(p):
        return ""
    for line in open(p, encoding="utf-8"):
        if line.strip().startswith("SILICONFLOW_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="题材规格 JSON 文件")
    ap.add_argument("--draft", help="用一句话让模型起草规格")
    ap.add_argument("--key", default="", help="模型 Key（缺省读 .env / 环境变量）")
    ap.add_argument("--model", default="Qwen/Qwen2.5-72B-Instruct")
    ap.add_argument("--save-draft", help="把起草出来的规格存到文件，便于人工改")
    ap.add_argument("--install", action="store_true", help="注册进 topics.json")
    ap.add_argument("--units", nargs=2, metavar=("ISO", "ADM"),
                    help="列出某国某层级的单元名，例如 --units USA ADM1")
    ap.add_argument("--countries", action="store_true", help="列出有数据的国家（慢）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.countries:
        try:
            d = json.loads(urllib.request.urlopen(
                urllib.request.Request(
                    "https://www.geoboundaries.org/api/current/gbOpen/",
                    headers=UA), timeout=120).read())
            print(f"geoBoundaries 覆盖 {len(d)} 个国家和地区")
            print(", ".join(sorted(d)[:200]))
        except Exception as e:
            raise SystemExit(f"取国家列表失败：{e}")
        return

    if args.units:
        iso, adm = args.units
        p = fetch_adm(iso, adm)
        us = units_of(p)
        print(f"{iso} {adm}：{len(us)} 个单元")
        for i in range(0, len(us), 6):
            print("  " + ", ".join(us[i:i + 6]))
        return

    spec = None
    if args.spec:
        spec = json.load(open(args.spec, encoding="utf-8"))
    elif args.draft:
        print(f"让模型起草「{args.draft}」的题材规格…")
        spec = draft(args.draft, args.key, args.model)
        print(json.dumps(spec, ensure_ascii=False, indent=1)[:1200])
        if args.save_draft:
            json.dump(spec, open(args.save_draft, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            print(f"  已存到 {args.save_draft}（可以手改后再 --spec）")
    else:
        ap.error("要么给 --spec，要么给 --draft")

    print(f"\n构建题材 {spec.get('id')} …")
    rep = build(spec, install=args.install, quiet=args.quiet)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    if rep.get("warnings"):
        print("\n注意：")
        for w in rep["warnings"]:
            print("  !", w)


if __name__ == "__main__":
    main()
