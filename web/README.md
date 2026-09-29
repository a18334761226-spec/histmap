# histmap-web · 浏览器端渲染

`packages/core/histmap_core` 的 JS 移植版：把出图/出片从服务器搬到用户浏览器。
云端只托管静态数据，**没有任何渲染服务**。

## 为什么不能用 `file://` 直接双击打开

`index.html` 用 `fetch` 读 `../data/web/` 下的 JSON，用 ES module 导入 `histmap.js`。
浏览器的同源策略会拦掉 `file://` 下的这两件事。所以起一个静态服务：

```powershell
cd D:\history-map
python -m http.server 8809
# 然后打开 http://127.0.0.1:8809/web/index.html
```

只需要一个静态文件服务（GitHub Pages / Nginx / OSS 都行），不需要 Python、不需要 Node 运行时。

## 先有数据，再有页面

`data/web/` 是**构建产物**，不在版本库里。生成它：

```powershell
cd D:\history-map
python src\export_web_data.py
```

产物结构：

```
data/web/scene/ww2-europe.json        场景入口：bbox + 数据集 + 控制表 + 样式 + 布局
data/web/geometry/cshapes-YYYY.json   年度几何切片（已按 bbox 裁剪）
data/web/control/ww2_timeline.json    控制时间线（事件式）
data/web/style/ww2_dark.json          样式（就是 Python 端那一份）
```

许可隔离：CShapes 原始数据与产物都不入库，使用者本地或 CI 生成。

## 页面能力

| 功能 | 说明 |
|---|---|
| 播放演变 | 时间轴 = 月度帧 ∪ 事件帧（131 个），与 Python 端 `build_dates` 同规则 |
| 停留节奏 | 有事件 0.55s、无事件 0.12s、交叉溶解 0.20s，与 Python 端同一套参数 |
| 拖时间轴 | 几何按年缓存 + Path2D 复用，换月份只重算填充色 |
| 导出 PNG | `canvas.toDataURL()` |
| 录制 WebM | `MediaRecorder` + `canvas.captureStream(30)`，零依赖 |
| 叠加 Python 输出 | 与本机 Python 渲染结果半透明叠加，用来做像素对齐校验 |
| 运行指标 | 帧耗时 / FPS / 实体数 / 顶点数 / 路径构建耗时 |

## 与 Python 端的一致性约束

两条实现必须给出同一个结果，否则用户会当成 bug：

1. **投影公式**逐行照搬 `projection.py`（含 25×25 采样求范围、`padding=8*ss`）。
2. **标注避让**同一套：面积降序 → 锚点（最大环中质心在视口内者 → 包围盒∩视口）
   → 11 个候选位 → 全部冲突才丢弃。面积必须累计**所有环**，
   只看首环会让苏联、英国这类「首环是小岛」的实体被面积闸门滤掉。

## 已知限制

- 契约里 `rings` 是拍平的环列表 → 不支持真正的洞（如南非内的莱索托）。
- 苏联是单一几何体，无法细分加盟共和国/占领区。
- 波罗的海三国只在 1939–1940 有独立几何。
- WebM 录制会实时消耗播放时长；长片仍建议回落 GitHub Actions + ffmpeg 出 MP4。
