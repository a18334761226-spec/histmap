"""histmap 的模型编排层。

这一层只做「跟模型打交道」的事，不画图、不碰几何数据文件：

  llm.py          —— 全项目唯一一处分派模型调用（鉴权错误分类、JSON 兜底解析）
  geodata.py      —— geoBoundaries 取数与名字匹配（不调模型）
  prompts.py      —— 所有提示词集中一处
  draft_graph.py  —— 题材起草图（LangGraph）
  style_graph.py  —— 风格与提示词抽取图（LangGraph）：读参考图 → 风格描述 + 生图提示词
                    → 图生图取质感样张 → 保真度闸门 → 拆出可复用的确定性材质层

为什么单独一个包：这些是**同一条依赖链**，原来散在 src/new_topic.py 里，
取数函数和 LLM 调用混在一起，于是「让模型干活」和「读文件」互相 import。
分开之后方向是单向的：agent 依赖 core，server 和 src 依赖 agent。
"""

from .llm import (LLMConfig, ModelAuthError, chat_json,      # noqa: F401
                  chat_json_vision)
from .geodata import (GB_LICENSE, fetch_adm, gb_license,     # noqa: F401
                      match_names, units_of)

__all__ = ["LLMConfig", "ModelAuthError", "chat_json", "chat_json_vision",
           "GB_LICENSE", "fetch_adm", "gb_license", "match_names", "units_of"]
