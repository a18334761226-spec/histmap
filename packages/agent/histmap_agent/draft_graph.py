"""题材起草图（LangGraph）。

为什么这里**应该**用图，而不是一串 if
--------------------------------------
原来的 `new_topic.draft()` 是一段手写的流程，它其实已经具备图的三要素，
只是这三样都藏在代码的缩进和 for 循环里：

  1. **步骤会变**：partition 类要「定来源 → 拉真名 → 分配归属」三步；
     gazetteer 类（中国朝代）走的是另一条路，第三步完全不同。
  2. **有重试循环**：模型经常漏年份，原代码用 `for attempt in (1, 2)` 补救；
     但**单元名编错**时它只报告、不重试 —— 而那正是这套系统最初做两阶段
     起草要解决的问题。
  3. **分支取决于上一步的输出**：kind 是第一半才由模型决定的，
     在写下第一行代码时还不知道要走哪条路。

写成图之后，这三件事变成显式的节点和边，看得见也测得到：
`validate` 是唯一判断「行不行」的地方，`repair` 是唯一决定「怎么补救」的地方。
这比散在各处的 if 更好改 —— 而且要加第三种题材类型时只加节点，不动别的。

图长这样：

    START → draft_geometry ─┬─(kind=partition)→ fetch_units → assign_owners → validate
                            │                                                  │
                            │                          (还有问题且没超次数) ────┤
                            │                                                  ↓
                            │                                            repair_prompt
                            │                                                  │
                            │                                    回到 assign_owners
                            │                                                  │
                            │                          (没问题 / 超次数) → finalize
                            └─(kind=gazetteer)→ draft_gazetteer ────────────────┘
                                                                                 ↓
                                                                                END
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .geodata import fetch_adm, match_names, units_of
from .llm import LLMConfig, chat_json
from .prompts import (DRAFT_PROMPT, DRAFT_PROMPT_2, DRAFT_PROMPT_3,
                      DRAFT_PROMPT_REPAIR_YEARS)

# 名字太多时截断，避免撑爆上下文（德意志各州才 16 个，够用）
MAX_UNITS_SHOWN = 120
# 最多补几次。两次足够：实测漏年份一次就能补上；再多说明模型没在配合，
# 继续问只是烧钱和让用户等。
MAX_REPAIR = 2
# 单元名命中率低于这个就认为模型在编名字，要修
MIN_HIT_RATE = 0.85


class DraftState(TypedDict, total=False):
    """图里流动的状态。每个节点只返回它改动的字段。"""
    ask: str
    cfg: LLMConfig
    # log 用 operator.add 累加：每个节点往里加几行，最终按顺序拼成过程记录
    log: Annotated[list[str], operator.add]

    # 第一半的产物
    spec: dict[str, Any]
    kind: str
    want_years: list[str]
    sources: list[dict]

    # 第二半
    units: list[str]
    shown: list[str]
    got: dict[str, Any]
    missing_years: list[str]
    bad_names: list[str]
    attempts: int
    # repair 节点写给 assign_owners 的额外说明
    repair_note: str


# ══════════════════════════════════════════════════════════════
#  节点
# ══════════════════════════════════════════════════════════════
def draft_geometry(state: DraftState) -> dict:
    """第一半：只让模型定几何来源与年份。

    这一半**故意不要** control/palette —— 单元的真实名字要等系统把数据拉下来
    才知道。先让它编名字再纠正，不如一开始就不给它编的机会。
    """
    spec = chat_json(state["cfg"], DRAFT_PROMPT.format(ask=state["ask"]))
    kind = (spec.get("kind") or "partition").strip()
    geo = spec.get("geometry") or {}
    sources = geo.get("sources") or ([geo] if geo.get("iso") else [])
    years = [str(y) for y in (spec.get("years") or [])]
    return {
        "spec": spec,
        "kind": kind,
        "sources": sources,
        "want_years": years,
        "log": [f"第一半：kind={kind}，几何来源 {len(sources)} 个，"
                f"年份 {years}"],
    }


def fetch_units(state: DraftState) -> dict:
    """系统去拉**真实**行政区名。这一步不调模型 —— 这是它不可能凭记忆知道的东西。"""
    names: list[str] = []
    failed = []
    for g in state.get("sources") or []:
        iso = (g.get("iso") or "").upper()
        if not iso:
            continue
        try:
            p = fetch_adm(iso, (g.get("adm") or "ADM1").upper())
            for n in units_of(p, g.get("name_field") or "shapeName"):
                if n not in names:
                    names.append(n)
        except SystemExit as e:
            failed.append(f"{iso}: {e}")
    if not names:
        raise SystemExit("没有从任何几何来源拿到单元名，无法继续。"
                         + ("；".join(failed) if failed else ""))
    shown = names if len(names) <= MAX_UNITS_SHOWN else names[:MAX_UNITS_SHOWN]
    return {
        "units": names,
        "shown": shown,
        "log": [f"系统拉到 {len(names)} 个真实单元名"
                + (f"（前 {len(shown)} 个交给模型）" if len(shown) < len(names) else "")
                + (f"；{len(failed)} 个来源失败：{'；'.join(failed)}" if failed else "")],
    }


def assign_owners(state: DraftState) -> dict:
    """第二半：把真名交给模型分配归属。

    修复提示（repair_note）挂在这里，所以「第一次分配」和「带错误重试」
    是同一个节点 —— 它们要做的事完全一样，只是提示词多一段。
    """
    units = state.get("shown") or []
    tail = "" if len(state.get("units") or []) <= MAX_UNITS_SHOWN else \
        f"\n（还有 {len(state['units']) - MAX_UNITS_SHOWN} 个未列出）"
    prompt = DRAFT_PROMPT_2.format(ask=state["ask"],
                                   units="、".join(units) + tail)
    note = state.get("repair_note") or ""
    if note:
        prompt += "\n\n" + note
    got = chat_json(state["cfg"], prompt)
    n_years = len([k for k in (got.get("control") or {}) if not str(k).startswith("_")])
    return {
        "got": got,
        "attempts": (state.get("attempts") or 0) + 1,
        "log": [f"第二半：模型给出 {n_years} 个年份的归属"
                + ("（带修复提示重试）" if note else "")],
    }


def draft_gazetteer(state: DraftState) -> dict:
    """gazetteer 类（中国朝代）：这一支走完全不同的第三步 —— 给治所坐标。"""
    got = chat_json(state["cfg"], DRAFT_PROMPT_3.format(ask=state["ask"]))
    n = len(got.get("units") or [])
    return {
        "got": got,
        "attempts": (state.get("attempts") or 0) + 1,
        "log": [f"gazetteer 支路：模型给出 {n} 个单元的治所坐标"],
    }


def validate(state: DraftState) -> dict:
    """**唯一**判断「这份草案行不行」的地方。

    查两件事，都是实测踩过的：
      1. 年份漏给 —— 声明 4 个年份只给 1 个，那几年的几何就构建不出来。
      2. 单元名编造 —— 模型凭印象拼名字（"Bavaria" vs 数据里的 "Bayern"），
         对不上的会被丢弃，图悄悄缺一大块。原代码只**报告**这个，
         现在让它触发重试。
    """
    got = state.get("got") or {}
    control = got.get("control") or {}
    have = [k for k in control if not str(k).startswith("_")]
    want = state.get("want_years") or []
    missing = [y for y in want if y not in have]

    bad: list[str] = []
    units = state.get("units") or []
    if units and state.get("kind") == "partition":
        used: list[str] = []
        for row in control.values():
            if isinstance(row, dict):
                for v in row.values():
                    used += v if isinstance(v, list) else [v]
            elif isinstance(row, list):
                used += row
        if used:
            _, bad = match_names(sorted(set(map(str, used))), units)
            hit = 1 - len(bad) / max(1, len(set(map(str, used))))
            if hit < MIN_HIT_RATE:
                bad = bad[:12]
            else:
                bad = []          # 少量对不上是正常的，不值得为它重试

    log = []
    if missing:
        log.append(f"校验：漏了 {missing} 年")
    if bad:
        log.append(f"校验：{len(bad)} 个单元名在数据集里找不到，例如 {bad[:5]}")
    if not missing and not bad:
        log.append("校验：通过")
    return {"missing_years": missing, "bad_names": bad, "log": log}


def repair_prompt(state: DraftState) -> dict:
    """**唯一**决定「怎么补救」的地方：把具体错在哪写回给模型。"""
    parts = []
    if state.get("missing_years"):
        have = [k for k in (state.get("got") or {}).get("control", {})
                if not str(k).startswith("_")]
        parts.append(DRAFT_PROMPT_REPAIR_YEARS.format(
            have=have or "（空）", missing=state["missing_years"],
            want=state.get("want_years")).strip())
    if state.get("bad_names"):
        parts.append(
            "另外，这些单元名在你上一次的回答里出现了，但**数据集里没有**：\n"
            f"{state['bad_names']}\n"
            "下面这份才是可用的真实名字，只能从中逐字照抄：\n"
            f"{'、'.join(state.get('shown') or [])}")
    return {"repair_note": "\n\n".join(parts),
            "log": [f"修复：把 {len(parts)} 处问题写回给模型"]}


def finalize(state: DraftState) -> dict:
    """把两半拼成最终规格。"""
    spec = dict(state.get("spec") or {})
    got = state.get("got") or {}
    if state.get("kind") == "partition":
        spec["palette"] = got.get("palette") or {}
        spec["era"] = got.get("era") or {}
        spec["control"] = got.get("control") or {}
    else:
        spec.update(got)
    left = state.get("missing_years") or []
    note = f"（仍有 {left} 年缺失，已交人工过目）" if left else ""
    return {"spec": spec,
            "log": [f"完成：{spec.get('title') or spec.get('id')} "
                    f"共 {len(spec.get('years') or [])} 个年份{note}"]}


# ══════════════════════════════════════════════════════════════
#  条件边
# ══════════════════════════════════════════════════════════════
def route_kind(state: DraftState) -> str:
    return "gazetteer" if state.get("kind") == "gazetteer" else "partition"


def need_repair(state: DraftState) -> str:
    problem = bool(state.get("missing_years") or state.get("bad_names"))
    if problem and (state.get("attempts") or 0) <= MAX_REPAIR:
        return "repair"
    return "done"


def build_draft_graph():
    """组装并编译题材起草图。"""
    from langgraph.graph import END, START, StateGraph

    g = StateGraph(DraftState)
    g.add_node("draft_geometry", draft_geometry)
    g.add_node("fetch_units", fetch_units)
    g.add_node("assign_owners", assign_owners)
    g.add_node("draft_gazetteer", draft_gazetteer)
    g.add_node("validate", validate)
    g.add_node("repair_prompt", repair_prompt)
    g.add_node("finalize", finalize)

    g.add_edge(START, "draft_geometry")
    g.add_conditional_edges("draft_geometry", route_kind,
                            {"partition": "fetch_units",
                             "gazetteer": "draft_gazetteer"})
    g.add_edge("fetch_units", "assign_owners")
    g.add_edge("assign_owners", "validate")
    g.add_conditional_edges("validate", need_repair,
                            {"repair": "repair_prompt", "done": "finalize"})
    # 修复提示绕回 assign_owners —— 这就是那个重试环
    g.add_edge("repair_prompt", "assign_owners")
    g.add_edge("draft_gazetteer", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = None


def draft_with_graph(ask: str, cfg: LLMConfig,
                     verbose: bool = True) -> tuple[dict, list[str]]:
    """跑一遍起草图。返回 (规格, 过程记录)。"""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_draft_graph()
    out = _GRAPH.invoke({"ask": ask, "cfg": cfg, "attempts": 0, "log": []},
                        {"recursion_limit": 40})
    log = list(out.get("log") or [])
    if verbose:
        for line in log:
            print("  " + line)
    return out.get("spec") or {}, log
