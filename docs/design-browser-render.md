# 设计稿 · 渲染下沉到浏览器（方案 A）

> 目标：把「出图/出片」这件事从服务器（或开发者本机）搬到**用户浏览器**里，
> 云端只留静态数据和极薄 API。这是本项目「开源 + 上云」的形态选择，不是性能优化。

## 0. 为什么要这么做（用实测数据说话）

| 事实 | 数值 | 推论 |
|---|---|---|
| 单帧渲染耗时（Python + Pillow，1920×1080，supersample=2） | **~1.1 s** | 算力本身不是瓶颈 |
| 欧洲裁剪框内实体数（1941） | **41 个** | 顶点量可控 |
| 苏联单实体顶点数（含 136 个环） | **21 083** | 全球全量会到 ~21 万顶点，需分级策略 |
| 现有视频任务关键帧数 | **131 帧** | 一次出片 = 131 次渲染 |
| 本机并发（Python 渲染 + DSH + Chrome + 代理） | 内存被打满，工具报 `Allocation error` | 瓶颈是「抢同一台机器」，不是渲染慢 |

结论：瓶颈在**部署位置**。放到用户浏览器后，N 个用户出图 = N 台机器的算力，
服务器成本与并发数**解耦**（这是开源地图工具唯一能长期免费的形态）。

## 1. 核心原则

1. **契约不变，实现换语言**。Python 端的 `Region / Frame / Series` 就是浏览器端的
   `Region / Frame / Series`，用同一份 JSON Schema 锁住，两端各有实现、互为参考。
2. **几何与状态分离**。几何只按年变化（CShapes 一年一个切片），控制状态是属性叠加。
   换月份只换一张状态表 → 浏览器端可以做到「拖时间轴 60 fps」。
3. **云上只有静态文件**。没有渲染服务、没有任务队列、没有数据库。
4. **渲染结果可校验**。浏览器输出必须能与 Python 输出做像素级对齐，否则两端会漂移。

## 2. 技术选型：为什么是 Canvas 2D

| 方案 | 5 万顶点/帧 | 21 万顶点/帧 | 判断 |
|---|---|---|---|
| SVG | DOM 节点爆炸，卡死 | 不可用 | ✗ |
| **Canvas 2D + Path2D 缓存** | **~10–20 ms** | ~60–90 ms（掉帧） | **✓ 第一版** |
| WebGL / PIXI | <5 ms | <10 ms | 预留后端，第二版 |

关键技巧：`Path2D` 可以**按实体缓存**，每帧只改 `fillStyle` 再 `ctx.fill(path)`，
不必每帧重新走 `moveTo/lineTo`。投影只在 bbox 变化时重算一次。

预留接口：`Renderer` 抽象成 3 个方法 —— `project(bbox)`、`drawFrame(frame, style)`、
`export()`，Canvas 与 WebGL 各实现一份。

## 3. 数据分层与目录（与云端托管结构一致）

```
/data/{dataset}/{dataset}-{year}.json     几何切片（按年，构建期生成）
/data/control/{topic}.json                控制时间线（事件式）
/styles/{style_id}.json                   样式
/scenes/{scene_id}.json                   场景 = bbox + 数据集 + 控制表 + 样式 + 布局
/schema/*.schema.json                     两端共用的契约
```

`scene.json` 是**唯一入口**，浏览器只认它：

```json
{
  "id": "ww2-europe",
  "bbox": [-11, 33, 62, 62],
  "projection": "mercator",
  "dataset": "cshapes",
  "years": [1939, 1945],
  "control": "ww2_timeline",
  "style": "ww2_dark",
  "layout": { "mode": "full", "width": 1920, "height": 1080 },
  "labels": { "language": "zh", "min_area_ratio": 0.0011 }
}
```

用户输入「唐宪宗二年的军阀格局图」→ 由**适配层**解析成这样一个 scene，再交给渲染器。
渲染器永远不知道「唐朝」是什么，它只认 scene。这是整套系统能应对任意题材的原因。

## 4. 浏览器端要移植的四个模块

| 模块 | Python 现实现 | 浏览器实现 | 校验方式 |
|---|---|---|---|
| 投影 | `projection.py` | 同公式 | 黄金文件：lon/lat → 归一化 x/y，容差 1e-9 |
| 渲染 | `render.py` | Canvas 2D | 像素对比，容差 <1% |
| 控制层 | `control.py` (`ControlTimeline`) | 同状态机 | 同一日期求值结果逐字段比对 |
| 标注避让 | `render.py` 内 | 同算法 | 同一帧的标注坐标列表比对 |

**标注避让必须两端一致**，否则同一个 scene 在两端出来的图不一样，用户会认为是 bug。
算法（已在 Python 端定型）：面积降序 → 锚点（label_pos > 环质心 > 包围盒交点）
→ 11 个候选位依次试探 → 全部冲突才丢弃。

## 5. 云端只做三件事

1. **静态托管**：GitHub Pages / Cloudflare Pages / OSS+CDN。`index.html` + JS + 数据切片。
2. **数据构建（离线，不是服务）**：`build-data.py` 把 CShapes 切成年度 GeoJSON 产物，
   推到 CDN。**数据集不进仓库**（downloader 模式），仓库只放构建脚本与 manifest。
3. **可选：一个 CORS 静态 JSON 接口**，用于后续指标/版本信息。

没有渲染服务 ⇒ 没有服务器费用 ⇒ 开源后不会因为别人用得多而破产。

## 6. 导出（回答「不占本机怎么出片」）

| 目标 | 浏览器 API | 说明 |
|---|---|---|
| PNG 单图 | `canvas.toBlob()` | 秒级 |
| WebM 视频 | `MediaRecorder` + `canvas.captureStream()` | 原生、零依赖，Safari 外全支持 |
| MP4 视频 | `WebCodecs` + `mp4-muxer` | Chrome/Edge 支持，需转封装 |
| 高码率长片 | 回落 GitHub Actions 跑 ffmpeg | 免费额度内，产出 MP4 制品 |

出片时间轴与 Python 端同规则：**事件驱动**（不只月初切帧）+ 停留时长自适应
（有事件 0.55 s / 无事件 0.12 s）+ 交叉溶解 0.20 s。

## 7. 迁移里程碑（每一步都可验证，不做「大爆炸重写」）

- **M1 · 像素对齐**：浏览器把 1941 静止帧画出来，与
  `output/maps/ww2_1941_control_horizontal_16x9.png` 做像素对比，容差 <1%。
  *未通过不进入下一步。*
- **M2 · 时间轴**：移植 `ControlTimeline`，拖动时间轴 1939→1945 实时变色（目标 60 fps）。
- **M3 · 标注**：移植避让算法，与 Python 端同一帧的标注坐标逐条比对。
- **M4 · 出片**：MediaRecorder 导出 WebM，与 Python + ffmpeg 版本做同帧对比。
- **M5 · 场景编辑器**：可视化改 bbox / 样式 / 图例文案，导出 scene.json。

## 8. 已知限制（浏览器端同样存在，不要假装没有）

- `Region.rings` 是**拍平的环列表**，不支持真正的洞（如南非内的莱索托）。
  修它要改契约成 `MultiPolygon` 结构，两端都要改，单独排期。
- 苏联是单一几何体，内部加盟共和国/1941 年占领区无法细分。
- 波罗的海三国只在 1939–1940 有独立几何，1941 起并入苏联多边形。
- CShapes 商用授权未确认（`commercial_ok=False`），云端对外提供前必须解决。
