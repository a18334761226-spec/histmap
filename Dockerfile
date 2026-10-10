# histmap 工作台 —— 云端部署镜像
#
# 设计取舍：
#   · 用 python:3.12-slim 而非 3.14：Shapely/Pillow 在 3.14 上轮子还不齐，
#     云端构建很可能要现场编译。3.12 是这些库覆盖最好的版本。
#   · ffmpeg 不装系统包 —— imageio-ffmpeg 自带一个静态二进制，
#     少一层系统依赖，镜像也小一截。
#   · 中文字体必须装：地图上全是中文标注，容器里没字体就是一片方框。
#   · CShapes(25MB) 在**构建时**下载：它的许可要求学术引用、商用条款未确认，
#     不适合随仓分发，所以走下载器模式。
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 中文字体。fonts-noto-cjk 覆盖简繁，且是自由许可（SIL OFL）
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

# 先只拷依赖清单：源码改动不会让依赖层失效，重建快很多
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 中文字体。地图上全是中文标注，容器里没字体就是一片方框 ——
# 这条比上面那条更容易被忽略：导入一切正常，出的图却全是豆腐块。
RUN python -c "import glob; \
f=glob.glob('/usr/share/fonts/**/*CJK*', recursive=True)+glob.glob('/usr/share/fonts/**/*cjk*', recursive=True); \
assert f, '容器里没有中文字体，出的图会全是方框'; \
print('中文字体 OK：', f[:2])"

# 构建期拉数据集。失败不阻断构建 —— 缺数据的题材会在启动日志和界面上
# 明确标出来，而不是整个镜像构建失败让人一头雾水。
RUN python -c "import urllib.request; \
url='https://icr.ethz.ch/data/cshapes/CShapes-2.0.geojson'; \
out='packages/core/data/cache/CShapes-2.0.geojson'; \
import os; os.makedirs(os.path.dirname(out), exist_ok=True); \
urllib.request.urlretrieve(url, out); \
print('CShapes 已下载', os.path.getsize(out)//1024//1024, 'MB')" \
 || echo "!! CShapes 下载失败：二战/一战题材会显示为缺数据，其余题材可用"

# 构建期就把「能不能跑起来」验掉。
# 为什么必须有这一步：`COPY . .` 会把整个仓库打进去，少了 packages/agent、
# 依赖装漏了、题材数据没带上，**镜像照样能构建成功**，但容器一启动就崩 ——
# 那种失败留到部署当天才发现代价最大。这里在构建阶段就导入一遍、
# 把两个状态图都编译一遍、把题材列出来、确认中文字体在。
RUN python -c "import sys; \
sys.path[:0]=['packages/server','packages/core','packages/agent','src']; \
from histmap_server import app; \
from histmap_agent.draft_graph import build_draft_graph; \
from histmap_agent.style_graph import build_style_graph; \
build_draft_graph(); build_style_graph(); \
from histmap_server import topics; \
ts=topics.load_topics(); \
print('OK 导入通过，题材', len(ts), '个：', '、'.join(sorted(ts))); \
rep=topics.missing_data_report(); \
print('缺运行期数据的题材：', [r['topic'] for r in rep] or '（无）')"

# 运行期要写 output/（渲染产物、任务目录），建好并确保可写
RUN mkdir -p output/api output/jobs output/style_previews

ENV PORT=8810 \
    HOST=0.0.0.0
EXPOSE 8810

# 云平台会注入 $PORT；app.py 见到 PORT 就绑 0.0.0.0
CMD ["python", "packages/server/histmap_server/app.py"]
