"""
histmap-core · 控制层（Occupation / Control Overlay）

**为什么需要这一层**
主权边界数据（如 CShapes）记录的是「哪个国家在法律上拥有这块地」，
而不是「谁在实际上控制它」。二战地图若只画主权边界，会出现
「1941 年的波兰是同盟国」这类严重失实——历史区观众一眼就能挑出来。

控制层把控制关系作为**属性叠加**在既有几何之上：
    主权国界（真实几何） + 控制状态表（史料整理） = 可交付的格局图

这样做的好处：**不需要重做几何**，换年份只需换一张控制表。

数据结构（JSON）
----------------
{
  "1941": {
     "<数据集中的实体名>": {
        "control_by":   "德国",          // 实际控制方
        "control_type": "occupied",      // sovereign|annexed|occupied|axis_ally|neutral|...
        "label":        "德苏瓜分"       // 图例/标注用
     }
  },
  "_palette": { "德国": {"sovereign":"#b03030","occupied":"#d98a8a"},
                "neutral": "#5a6570", ... }
}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


def to_light(hex_color: str, sat: float = 1.0, v_target: float = 0.86,
             s_min: float = 0.16, s_max: float = 0.46) -> str:
    """把一个「深色底友好」的颜色转成「浅色纸底友好」的淡彩。

    两次做错才定下来的方向，写在这儿免得再走一遍：

    1. 初版「降饱和 + 压亮度」（sat .72 / val .82）
       → 白纸上贴深色块，又重又闷。
    2. 二版「整体提亮 + 降饱和」（等比缩放）
       → 仍然不对：调色板里各色的**原始明度差很大**
         （德国 #b03030 明度 0.69，中立国 #5a6570 只有 0.44），
         等比缩放只是把这个差原样保留，于是浅红块和深灰块并存，很脏。

    正确做法是**统一明度 + 保留色相 + 饱和度夹进一个 band**：
        · v_target 把所有区域压到同一亮度 → 整幅图像一层淡淡的水彩
        · s_min 保证低饱和的灰蓝（中立国）在纸上仍看得见
        · s_max 保证高饱和的朱红（德国）不跳出来刺眼
    这样任意题材的调色板丢进来都能得到协调的淡彩，不必逐色手调。
    """
    import colorsys
    h = str(hex_color).lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return hex_color
    hh, ss, _vv = colorsys.rgb_to_hsv(r, g, b)
    ss = min(max(ss, s_min), s_max) * sat
    r, g, b = colorsys.hsv_to_rgb(hh, min(1.0, ss), v_target)
    return "#%02x%02x%02x" % (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))


def lighten_palette(pal: dict, **kw) -> dict:
    """对整个调色板（含「按控制方分组」的嵌套结构）递归做浅色变换。"""
    out = {}
    for k, v in (pal or {}).items():
        if k.startswith("_"):
            out[k] = v                      # 注释与说明字段原样保留
        elif isinstance(v, dict):
            out[k] = {kk: (to_light(vv, **kw)
                           if isinstance(vv, str) and vv.startswith("#") else vv)
                      for kk, vv in v.items()}
        elif isinstance(v, str) and v.startswith("#"):
            out[k] = to_light(v, **kw)
        else:
            out[k] = v
    return out


@dataclass
class ControlStats:
    matched: int = 0
    unmatched: int = 0
    by_control: dict = field(default_factory=dict)
    by_type: dict = field(default_factory=dict)
    unmatched_names: list = field(default_factory=list)

    def as_dict(self):
        return {
            "matched": self.matched, "unmatched": self.unmatched,
            "by_control": self.by_control, "by_type": self.by_type,
            "unmatched_names": self.unmatched_names[:40],
        }


class ControlLayer:
    """按年份把控制状态叠加到地图要素上。"""

    def __init__(self, table: dict, year: int):
        self.raw = table
        self.year = int(year)
        self.rules = table.get(str(year)) or table.get(year) or {}
        self.palette = table.get("_palette") or {}
        # 长键优先，避免 "France" 抢先匹配 "French Indochina"
        self._keys = sorted(self.rules.keys(), key=len, reverse=True)

    # ── 载入 ─────────────────────────────────────────────────
    @staticmethod
    def load(path: str, year: int) -> "ControlLayer":
        with open(path, encoding="utf-8") as f:
            return ControlLayer(json.load(f), year)

    # ── 匹配 ─────────────────────────────────────────────────
    def lookup(self, name: str):
        if not name:
            return None, None
        if name in self.rules:
            return name, self.rules[name]
        for k in self._keys:
            if k in name:
                return k, self.rules[k]
        return None, None

    # ── 颜色解析 ─────────────────────────────────────────────
    def color_for(self, rule: dict) -> str:
        ctype = rule.get("control_type") or ""
        cby = rule.get("control_by") or ""
        pal = self.palette
        entry = pal.get(cby)
        if isinstance(entry, dict) and ctype in entry:
            return entry[ctype]
        if ctype in pal and isinstance(pal[ctype], str):
            return pal[ctype]
        if isinstance(entry, str):
            return entry
        return pal.get("default", "#3a4048")

    # ── 应用 ─────────────────────────────────────────────────
    def apply(self, frame, use_label_as_name: bool = True) -> ControlStats:
        """把控制状态写入每个 region.props，并设置颜色。

        `use_label_as_name=True` 时，若规则里给了 `cn`（中文短名），
        就用它替换 region.name —— 地图标注不该显示 "Germany (Prussia)"。
        本方法可对同一批 region **重复调用**（逐帧渲染场景）：原始名存在
        props["orig_name"] 里，匹配永远走原始名，不会被上一次的结果污染。
        """
        from collections import Counter
        st = ControlStats()
        by_c = Counter()
        by_t = Counter()

        for reg in frame.regions:
            reg.props = dict(reg.props or {})
            orig = reg.props.get("orig_name")
            if not orig:
                orig = reg.name
                reg.props["orig_name"] = orig
            # 先按实体名匹配，再按 members 逐个试（合并实体用）
            names = [orig] + list((reg.props or {}).get("members") or [])
            rule = None
            for n in names:
                _, r = self.lookup(n)
                if r:
                    rule = r
                    break
            if rule:
                st.matched += 1
                reg.props["control_by"] = rule.get("control_by")
                reg.props["control_type"] = rule.get("control_type")
                reg.props["control_label"] = rule.get("label")
                if use_label_as_name and rule.get("cn"):
                    reg.name = rule["cn"]
                if isinstance(rule.get("color"), str):
                    reg.color = rule["color"]
                else:
                    reg.color = self.color_for(rule)
                by_c[rule.get("control_by") or "?"] += 1
                by_t[rule.get("control_type") or "?"] += 1
            else:
                st.unmatched += 1
                st.unmatched_names.append(orig)
                reg.props["control_by"] = None
                reg.color = self.palette.get("default", "#3a4048")

        st.by_control = dict(by_c)
        st.by_type = dict(by_t)
        return st


class ControlTimeline:
    """事件式控制时间线：给定任意日期求各国控制状态。

    与 `ControlLayer`（一年一张静态表）互补：时间线只记录**变化事件**，
    因此可以渲染任意日期，而不是只能渲染预先整理好的那几个年份——
    这是「1939→1945 逐年演变」这类需求的数据基础。

    数据结构（JSON）
    ----------------
    {
      "_baseline_1939": { "<实体名>": {"control_by":..., "control_type":..., "label":...} },
      "events": [ {"date":"1940-06-22", "entity":"France",
                   "by":"德国", "type":"occupied", "label":"法国沦陷"}, ... ],
      "_palette": { ... }
    }

    事件语义：`date <= 查询日期` 的事件按时间顺序依次覆盖基线。
    事件里 `by` 为空字符串表示「恢复独立」（回到 control_by = 自身）。
    """

    def __init__(self, data: dict, theme: str = "dark"):
        self.data = data or {}
        self.theme = theme
        raw = self.data.get("_baseline_1939") or self.data.get("_baseline") or {}
        # 基线允许用事件式简写键（by/type），这里统一规范化成
        # ControlLayer 认识的 control_by/control_type。
        self.baseline = {k: self._norm(v) for k, v in raw.items()}
        self.palette = self.data.get("_palette") or {}
        # 浅色主题：调色板是给深色画布选的，直接放宣纸上会发飘，
        # 统一做一次「统一明度 + 降饱和」的通用变换；
        # 通用变换解决不了的**色相撞车**（本题材里中立国/其他独立国的蓝
        # 与英国的蓝太近）用 _light_overrides 显式覆盖。
        if theme == "light":
            kw = (self.data.get("_light_adjust") or {})
            self.palette = lighten_palette(self.palette, **kw)
            for k, v in (self.data.get("_light_overrides") or {}).items():
                if k in self.palette and isinstance(self.palette[k], str):
                    self.palette[k] = v
        evs = list(self.data.get("events") or [])
        # 稳定排序：同一天的事件保持文件中的原始顺序（后者覆盖前者）
        self.events = sorted(evs, key=lambda e: str(e.get("date") or ""))
        self._dates = [str(e.get("date") or "") for e in self.events]

    @staticmethod
    def _norm(rule: dict) -> dict:
        """把 {by,type,label} 规范化成 {control_by,control_type,label}。"""
        r = dict(rule or {})
        if "by" in r and "control_by" not in r:
            r["control_by"] = r.pop("by")
        if "type" in r and "control_type" not in r:
            r["control_type"] = r.pop("type")
        return r

    # ── 载入 ─────────────────────────────────────────────────
    @staticmethod
    def load(path: str, theme: str = "dark") -> "ControlTimeline":
        with open(path, encoding="utf-8") as f:
            return ControlTimeline(json.load(f), theme=theme)

    # ── 查询 ─────────────────────────────────────────────────
    def state_at(self, date: str) -> dict:
        """返回该日期时点的控制状态表 {实体名: 规则}。"""
        d = str(date)[:10]
        state = {k: dict(v) for k, v in self.baseline.items()}
        for e in self.events:
            if str(e.get("date") or "")[:10] > d:
                break
            ent = e.get("entity")
            if not ent:
                continue
            if e.get("drop"):
                state.pop(ent, None)
                continue
            rule = dict(state.get(ent) or {})
            ev = self._norm(e)
            if "control_by" in ev:
                rule["control_by"] = ev["control_by"] or ent
            if "control_type" in ev:
                rule["control_type"] = ev["control_type"]
            if "label" in ev:
                rule["label"] = ev["label"]
            state[ent] = rule
        return state

    def layer_at(self, date: str) -> ControlLayer:
        """返回该日期时点的 ControlLayer（可直接 apply 到 Frame）。"""
        year = int(str(date)[:4])
        table = {"_palette": self.palette, str(year): self.state_at(date)}
        return ControlLayer(table, year)

    def events_until(self, date: str) -> list:
        d = str(date)[:10]
        return [e for e in self.events if str(e.get("date") or "")[:10] <= d]

    def events_between(self, date_a: str, date_b: str) -> list:
        """(date_a, date_b] 区间内发生的事件，用于做「近期动态」标注。"""
        a, b = str(date_a)[:10], str(date_b)[:10]
        return [e for e in self.events if a < str(e.get("date") or "")[:10] <= b]

    def date_range(self):
        if not self._dates:
            return None
        return (self._dates[0], self._dates[-1])

    def entities(self) -> list:
        names = set(self.baseline.keys())
        for e in self.events:
            if e.get("entity"):
                names.add(e["entity"])
        return sorted(names)


def build_legend(stats: ControlStats, layer: ControlLayer):
    """按控制方生成图例项 [(标签, 颜色)]。"""
    items, seen = [], set()
    for reg_name, rule in sorted(layer.rules.items()):
        cby = rule.get("control_by") or ""
        ctype = rule.get("control_type") or ""
        key = (cby, ctype)
        if key in seen:
            continue
        seen.add(key)
        label = rule.get("label") or f"{cby}({ctype})"
        items.append((label, layer.color_for(rule)))
    return items
