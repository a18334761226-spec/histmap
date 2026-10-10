"""LangGraph Studio 入口。

为什么需要这个文件：`langgraph dev` 是从**仓库根目录**加载图的，而本项目的
两个图住在 packages/agent 里，并且它们自己又依赖 packages/core、packages/server
和 src（例如 style_graph 里 `import stylize`）。Studio 不会替我们把这些目录
放进 sys.path，所以在这里显式放好，再把两个图暴露成无参构造函数。
放在根目录只是为了让 langgraph.json 的路径简单，它不参与服务端运行。
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
for _sub in ("packages/agent", "packages/core", "packages/server", "src"):
    _p = os.path.join(ROOT, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from histmap_agent.draft_graph import build_draft_graph  # noqa: E402
from histmap_agent.style_graph import build_style_graph  # noqa: E402


def draft():
    """起草题材的图：LLM 定几何来源与年份 → 取真实单元名 → 分配归属 → 校验/修复。"""
    return build_draft_graph()


def style():
    """风格的图：纯代码提材质 → 视觉模型写描述与提示词 → 生成纸纹 → 合成 → 闸门。"""
    return build_style_graph()


# 给 `langgraph dev` 预热的别名：它有时会直接找同名对象
graph_draft = draft
graph_style = style
