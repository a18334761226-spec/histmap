"""histmap-server · 把历史地图引擎包成一个可用的应用

模块分工：
  topics.py  题材加载器（题材 = data/topics/topics.json 里的数据）+ 两种渲染器
  jobs.py    后台任务（视频渲染走「提交 → 轮询」两段式）
  app.py     FastAPI 路由

注意：这里**不要** import app，否则 `python -m histmap_server.app`
会触发循环导入（踩过）。用 `from histmap_server import topics, jobs` 即可。
"""

__version__ = "0.3.0"
