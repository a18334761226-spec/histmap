"""用视觉模型**审**代码渲染出来的图，输出可直接照做的缺陷清单。

为什么需要这一步（补上原架构缺的那一层）：
原来的链路是「代码渲染 → 质感层」，模型只负责质感。但代码渲染本身有很多
视觉问题（留白、字太小、标签挤在一起、配色分不开……），而这些**靠人肉看
很累、靠规则很难全**。

正确分工不是让模型重画像素（实测会把「盧龍」写成「盧西」、把 807 年画成
现代省界），而是：
    模型**看图挑毛病** → 输出结构化清单 → **代码照着改**
模型决定「怎么改」，代码决定「像素」。

用法
    python src/visual_audit.py out/look/paper_codebase.png
    python src/visual_audit.py <图片> --model doubao-seed-2-0-lite-260428
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [os.path.join(ROOT, "packages", "agent"),
                os.path.join(ROOT, "packages", "core"),
                os.path.join(ROOT, "packages", "server"), HERE]

from histmap_agent import LLMConfig, chat_json_vision, server_default  # noqa: E402

PROMPT = """你在给一个「历史地图视频」系统做**视觉质检**。这张图是确定性代码
渲染出来的成品（几何和中文标注都是精确的，不需要你判断史实对错）。

请只从**画面呈现**的角度挑毛病，按「改起来越容易、对观感影响越大」排序。
重点看这些方面：

1. 构图：地图在画面里是否太小 / 偏移 / 一侧大片空白；内容外框占画布比例
2. 标注：字号是否偏小；是否有地名互相压住、压在边界上、看不清；是否有地名
   被图例挡住；标注与所属色块的对应是否清楚
3. 图例：位置是否挡住地图；条目是否太密；与图面层次是否分明
4. 色彩：相邻政权是否撞色/太接近；是否有一块过亮/过暗跳出来；整体是否偏灰
5. 标题与页脚：大小、位置、与图面留白的关系；是否显得孤立或拥挤
6. 其他一眼可见的缺陷

**不要**提史实问题，**不要**建议"用 AI 重画整张图"，**不要**提无法量化的感觉。

只输出一个 JSON 对象，不要解释、不要围栏：

{
  "verdict": "一句话总评",
  "score": 0-10 的整数,
  "defects": [
    {"aspect": "构图|标注|图例|色彩|标题页脚|其他",
     "what": "具体问题（说清在哪、看着怎样）",
     "fix": "代码该怎么改（具体到参数方向，例如『字号 19→26』『留白边距收 40%』）",
     "severity": "high|medium|low"}
  ],
  "keep": ["做得对、不要动的地方"]
}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--model", default="")
    ap.add_argument("--base", default="")
    ap.add_argument("--key", default="")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    if not os.path.exists(a.image):
        raise SystemExit(f"没有这张图: {a.image}")

    d = server_default(model=a.model)
    cfg = LLMConfig(key=a.key or d.key, base=a.base or d.base,
                    model=a.model or d.model, temperature=0.2, timeout=240)
    print(f"审核用的模型：{cfg.model} @ {cfg.base}")
    print(f"审这张图    ：{a.image}")
    print()

    try:
        r = chat_json_vision(cfg, PROMPT, a.image)
    except Exception as e:
        raise SystemExit(f"视觉审核失败：{type(e).__name__}: {e}")

    print("总评：", r.get("verdict"))
    print("评分：", r.get("score"), "/ 10")
    print()
    sev_order = {"high": 0, "medium": 1, "low": 2}
    ds = sorted(r.get("defects") or [],
                key=lambda x: sev_order.get(str(x.get("severity")), 3))
    for i, x in enumerate(ds, 1):
        print(f"{i}. [{x.get('severity')}] {x.get('aspect')} —— {x.get('what')}")
        print(f"     改法：{x.get('fix')}")
    if r.get("keep"):
        print()
        print("不要动的：")
        for k in r["keep"]:
            print("   ·", k)

    if a.json_out:
        json.dump(r, open(a.json_out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print()
        print("已写出：", a.json_out)


if __name__ == "__main__":
    main()
