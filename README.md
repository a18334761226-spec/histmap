# histmap · 用代码画历史地图

> 输入「唐宪宗二年的军阀格局」，输出一张统一风格的历史地图。
> 不是让 AI 画图，是把史料变成结构化数据再用代码渲染。

**为什么不让 AI 直接画**：图像生成模型会给你一张漂亮但读不出信息的图。
实测 `Qwen-Image-Edit` 对一张 WWII 欧洲控制图做风格化，产出的羊皮纸质感非常漂亮，
但把德国红、苏联紫、英国蓝**全部抹成一片褐色**，中文标注变成乱码笔画，
还凭空重画了地理（结构相似度 NCC **−0.075**）。
模型分不清哪些像素是「信息」、哪些是「材质」。
**所以：几何与色块归代码，质感归后处理。**

---

## 题材是数据，不是代码

| 题材 | kind | 数据来源 | 日期点 |
|---|---|---|---|
| **二战欧洲** 1939–1945 | boundary | CShapes 真实国界 + 事件式控制时间线 | 146 |
| **一战欧洲** 1914–1918 | boundary | 同上，只换控制表 | 85 |
| **唐 · 藩镇割据** 763–875 | dynasty | 《新唐书·方镇表》+ 重建坐标 + 现代县界 | 5 |
| **宋 · 路制疆域** 980–1200 | dynasty | 《宋史·地理志》+ 重建坐标 + 现代县界 | 6 |

**加一个新题材 = 加一个 JSON，零 Python。** 渲染逻辑只有两种：

- **`kind: "boundary"`** —— 现成国界几何 + 控制时间线。近现代战争、国际体系。
  一战是**零代码**加进去的：写一份控制表 JSON，加一条题材记录。
- **`kind: "dynasty"`** —— 行政单元坐标表 + 控制表 → 合并现代县。中国古代断代。
  宋是**零代码**加进去的：37 个路级单元（北宋 24 路 + 辽 5 道 + 西夏 3 府 + 金 5 府）。

引擎不知道「唐朝」是什么，它只认 `Region / Frame / Series` 三个契约结构。

---

## 核心设计

```
用户提问「宋徽宗政和二年的疆域」 / 上传一张参考图
      │
      ▼
① 题材路由 ──── data/topics/topics.json（题材 = 数据）
      │
      ▼
② 数据适配层 ── 各来源统一成一种格式（适配器只认 manifest）
      │
      ▼
② 统一契约 ──── Region / Frame / Series，两端共用一份 Schema
      │
      ▼
③ 控制状态叠加 ─ 主权 ≠ 实际控制。控制关系是**属性**，换年份只换一张表
      │
      ▼
④ 渲染引擎 + 样式层 ── 投影/标注避让/图例/版面全部外置
      │
      ▼
⑤ 后期质感层 ── fBm 纸纹 + 乘性调色 + 暗角 + 做旧边（纯代码，确定性）
```

---

## 快速开始

```bash
git clone <repo> && cd history-map
pip install pillow numpy pyyaml imageio-ffmpeg shapely zhconv fastapi uvicorn python-multipart httpx

# ── Web 应用（推荐先跑这个，clone 下来就能用）──
set PYTHONPATH=packages\server;packages\core;src
python -m histmap_server.app          # 打开 http://127.0.0.1:8810
python src/smoke_api.py --video       # 全线自检：每个接口都真跑一遍

# 0. 拉数据（可再下载的大件不进仓库；clone 后无需执行也能跑唐宋）
set HTTPS_PROXY=http://127.0.0.1:7897     # 国内直连 GitHub 很慢
python src/fetch_data.py --all

# ── 唐代 ──
python src/fetch_fangzhen.py        # 抓《新唐书·方镇表》全 6 卷（维基文库）
python src/parse_fangzhen.py        # 解析 wikitext → fangzhen_raw.json
python src/parse_tang_fanzhen.py --year 807     # 藩镇 ← [州] 的逐年隶属
python src/build_tang_gazetteer.py  # 唐州治所坐标表（329 条）
python src/check_gazetteer.py data/processed/tang_zhou_gazetteer.json   # 逐点几何校验
python src/build_tang_map.py --year 807         # 县 → 州 → 藩镇
python src/render_tang.py --year 807 --size 4x3 # 出图

# ── 宋代（路制）──
python src/build_dynasty_map.py --topic song --all-years --force

# ── 二战 ──
python src/still.py --date 1942-11-01 --theme light
python src/make_ww2_video.py 16x9 9x16
python src/postfx.py output/maps/*.png --preset atlas

# ── 浏览器端 ──
python src/export_web_data.py
python -m http.server 8809      # 打开 http://127.0.0.1:8809/web/index.html
python src/gen_golden.py && node web/verify.mjs   # 两端投影一致性门禁

# ── 演示页 ──
# 打开 site/index.html
```

### 哪些数据在仓库里，哪些不在

原则：**能重新下回来的不进仓库，重新造不出来的必须进。**

| 文件 | 进仓库 | 理由 |
| --- | --- | --- |
| `data/topics/topics.json` | ✅ | 题材定义即产品配置 |
| `data/control/*.json` | ✅ | 控制表是史实结论，手工校订的 |
| `data/raw/juan0*.json` | ✅ | 《新唐书·方镇表》卷 64–69 原文（公有领域）。再抓一次成本高且站点可能变 |
| `data/processed/song_lu_gazetteer.json` | ✅ | 宋·路制单元坐标表，**没有任何脚本能重新生成** |
| `data/processed/tang_fanzhen_timeline.json` | ✅ | 藩镇逐年隶属，含手工 `_display`/`_footer` 覆盖层 |
| `data/processed/*_map.geojson` | ✅ | 已经算好的逐年几何。不带上它，clone 后唐宋 `dates()` 返回空、点开是空白页 |
| `data/cache/gb_*.geojson` | ❌ | geoBoundaries 下载件，12 MB，`fetch_data.py` 可取 |
| `data/processed/fangzhen_raw.json`、`ops_807.json`、`*_probe.json` | ❌ | 中间产物/探针输出，可重跑 |

改了 `data/control/*.json` 之后，几何不会自动跟着变，必须重跑：

```bash
python src/build_dynasty_map.py --topic <id> --all-years --force
```

忘了跑也不会悄悄错下去——服务启动时和 `GET /api/health` 都会点名哪几年是旧的：

```
[自检] song 有 6 年的几何是旧的（[980, 1040, 1080, 1120, 1140, 1200]）。
       重跑：python src/build_dynasty_map.py --topic song --all-years --force
```

判定用的是**语义**指纹（解析后的 JSON 规范化哈希），只改缩进或换行不会误报。

---

## 目录

```
packages/core/histmap_core/    核心库
  contract.py                  Region / Frame / Series（统一契约）
  projection.py                等距圆柱 / 墨卡托 / 兰伯特
  style.py                     样式层（含明暗主题推导）
  render.py                    渲染器（标注避让、图例、版面）
  control.py                   控制层 + 事件式时间线 + 浅色变换
  quality.py                   几何质量闸门
  datasets/                    数据适配器（CShapes / AtlasPI）

src/
  # 唐代管线
  fetch_fangzhen.py            抓《新唐书·方镇表》
  parse_fangzhen.py            wikitext → 原始表格
  parse_tang_fanzhen.py        ★ T1：解析成「藩镇 ← [州]」逐年隶属
  build_tang_gazetteer.py      ★ T2：唐州治所坐标表
  check_gazetteer.py           ★ 坐标几何校验（点在真实县境内）
  build_tang_map.py            ★ T3：县 → 州 → 藩镇合并
  render_tang.py               ★ T4：出图

  # 二战管线
  still.py                     单帧输出
  make_ww2_video.py            演变视频（含 MP4 完整性校验）
  postfx.py                    后期质感层（纯代码）
  stylize.py                   模型风格化（双后端 + 保真度闸门）

  # 通用
  fetch_data.py                数据下载器
  export_web_data.py           导出浏览器端静态数据
  gen_golden.py / web/verify.mjs   两端投影一致性门禁

web/                           浏览器端渲染引擎（Canvas 2D，零依赖）
site/                          开源演示页
docs/                          设计稿与实测记录
```

---

## 两道闸门

### 几何质量闸门

很多「历史地图」数据集用合成占位几何——一个 33 顶点的圆饼冒充德国。
我们用**形态变异系数 + 顶点数 + 环数**三重判据识别它。

| 数据源 | 年份 | 实体 | 真实边界 | 真实率 |
|---|---|---|---|---|
| AtlasPI | 1941 | 81 | 16 | **19.8%** |
| AtlasPI | 1250 | 299 | 14 | **4.7%** |
| CShapes 2.0 | 1941 | 169 | 101 | 59.8% |

⚠️ `confidence_score` 之类的自带字段**不能**用来判断几何质量
（某数据源给一个 15 顶点的日本打了 0.9 分）。

### 保真度闸门

任何对成图做后处理的环节都必须先过：尺寸一致 + NCC ≥ 0.90 + 色块保真 ≥ 80%。

```
同一张图对比自己        → NCC 1.000  通过
拿 1939 的图冒充 1941   → NCC 0.841  正确判失败
代码质感版              → NCC 0.955  通过
模型风格化版            → NCC −0.075 正确判失败
```

---

## 唐图的构建路径（C 类：史料构建）

唐代**没有**任何许可证干净的现成边界数据，几何必须重建：

```
《新唐书·方镇表》原文（维基文库，公有领域）   6 卷 / 566 个有效变化记录
     │  解析：藩镇 ← [州] 的逐年隶属
     ▼
唐州治所坐标表 329 条（按《地理志》州县沿革重建）
     │  逐点几何校验：必须落在真实县境内         329/329 通过
     ▼
现代县多边形 3099 个（geoBoundaries CHN + VNM）
     │  每个县归到最近的州治
     ▼
按藩镇-州隶属合并                              2278 县 → 263 州 → 37 藩镇
```

**为什么用「县归属」而不是 Voronoi**：Voronoi 出来是直线多边形、合成感强、
不会沿海岸线切断。用真实县界拼面，形状自然、天然贴海岸线。

解析器开发中踩过的坑（都写在 `parse_tang_fanzhen.py` 注释里）：
1. 单字正则切碎多字名（「京兆」→「兆」）
2. 从整句扫字典会把藩镇名（**劍**南）和使职名（支度**營田**）的字当成州
3. 转移范围失控（`cl[:隸]` 取整个子句，一次搬走十几个州）
4. 「廢節度使」多指官职类型变化，不是地盘消失
5. 郡名（義陽、潁川…）不匹配州词典 → **蔡州整条丢失**，淮西藩镇消失

---

## 已知限制（不粉饰）

- **唐代几何是重建的，不是史料记载的界线。** 行政层级古今不对应，
  归属判据是「县治到州治直线距离最近」。用于示意/教学/短视频可以，
  **学术引用请另找权威来源**。
- **宋代同理**，而且路是**顶层行政区**（约当今省），比唐的州县粗一档；
  辽、金、西夏只按大区治所单列，不是完整的路/道体系。
- **宋金边界只取通行分期。** 京西南路（治襄阳）跨在绍兴和议的
  淮河—大散关界上，本图按襄阳属南宋处理，邓州、唐州等地不细分。
  1120 年按女真已取辽上京道（1120）、东京道（1117）处理，故辽只剩三京道。
- **980 年不出现西夏**（西夏 1038 年才建国），此时兴庆府一带按定难军属宋、
  河西走廊按吐蕃六部与回鹘合称「河西诸部」示意。
- **唐州治坐标表是 AI 重建 + 几何校验**（329 条全部落在真实县境内），
  但不是权威测绘数据。
- 方镇表解析仍有长尾误差：约 24 个州名未能定位；别名重复
  （蒲州＝河中府、辽州＝仪州、鄚州＝莫州、隋州＝随州）会让同一地点出现两次。
- 被瓜分/分区占领的国家（维希法国、德苏瓜分波兰、南斯拉夫）CShapes 视作
  **单一几何体**，无法按军事分界线切开，只能用独立颜色示意。
- 契约里 `Region.rings` 是**拍平的环列表**，不支持真正的洞（如南非内的莱索托）。
- **古罗马尚未接入**（Pleiades 点位需建面）。

---

## 许可

**代码 MIT**（见 LICENSE）。

**能重新下回来的数据不入库**（见上文「哪些数据在仓库里」表格）——大件走
`src/fetch_data.py` 下载器模式，重生不出来的源数据（宋路制坐标表、新唐书原文）进仓库：

| 数据源 | 许可 | 可商用 | 用途 |
|---|---|---|---|
| geoBoundaries CHN ADM2 | **PDDL v1.0**（≈公有领域） | ✓ | 现代县界 |
| geoBoundaries VNM ADM2 | **CC BY 3.0 IGO**（须署名） | ✓ | 越南县界 |
| CShapes 2.0 | 学术引用要求；**商用条款未确认** | ⚠️ | 近现代国界 |
| AtlasPI | Apache-2.0 | ✓ | 宏观索引（几何质量低） |
| 《新唐书·方镇表》 | 公有领域（古籍） | ✓ | 唐代 |
| 《宋史·地理志》 | 公有领域（古籍） | ✓ | 宋代 |
| 唐州治所坐标表 / 宋路制坐标表 | 本项目重建 | ✓ | 唐、宋 |

### 明确排除的数据源

| 数据源 | 原因 |
|---|---|
| **CHGIS**（哈佛/复旦） | 官方许可明令「no commercial use, resale, or redistribution permitted」 |
| 《中国历史地图集》（谭其骧） | 有版权 |
| `aourednik/historical-basemaps` | GPL-3.0，具传染性 |
| 维基文库 CC BY-SA 篇目 | 具传染性，须逐篇确认 |

---

## 演示页

`site/index.html` —— **页内所有图片与视频均为真实运行结果，非效果图。**
其中包含一张**故意放上去的失败案例**（模型风格化把地图数据抹掉），
因为「模型会美化掉数据」这件事，看一张图比读一段话管用。

---

## Web 应用

把引擎包成一个能用的应用（对话改参数 / 传图提风格 / 出图出片 / 配 Key）：

\\\ash
pip install fastapi uvicorn python-multipart
set PYTHONPATH=packages\\server;packages\\core;src
python -m histmap_server.app          # 打开 http://127.0.0.1:8810
\\\

**五块能力**

| 能力 | 说明 | 需要 Key 吗 |
|---|---|---|
| 对话指定/修改 | 自然语言 → 结构化参数（LLM 只做翻译，不碰渲染） | 需要（对话模型） |
| 图片提风格 | 上传参考图 → 提取色调/纹理/颗粒（纯代码，确定性） | 不需要 |
| 出图 | 服务器端渲染，1–2 秒 | **不需要** |
| 出片 | 后台任务 + 进度轮询 + MP4 | **不需要** |
| Key 与模型 | 前端填写，存 localStorage，服务端不落盘 | — |

**Key 的处理原则**：前端存 localStorage，随请求用 header 传；服务端不写盘、不记日志。
