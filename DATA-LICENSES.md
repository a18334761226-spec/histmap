# 数据来源与许可风险

> 这份文档把**每个数据源的法律状态**和**本仓库实际承担的风险**写清楚。
> 写它的原因：地图数据的许可比代码许可复杂得多，而且**不同来源卡在不同条款上**
> （有的禁商用、有的禁再分发、有的会传染整个项目）。代码是开源的，但数据不是。

**一句话结论**：
- 代码（`packages/`、`src/`、`web/`）随本仓库的 LICENSE 走。
- **数据不是同一回事** —— 各源许可见下，商用前请逐条核对。
- ⚠️ **`ww1-europe` / `ww2-europe` 两个题材的边界数据是 CC BY-NC-SA 4.0，禁止商用。**
- ⚠️ **中国地图公开展示涉及《地图管理条例》的审图号要求**，与版权无关，另算一条。

---

## 一、仓库里实际用到的数据源

| 键 | 源 | 覆盖 | 许可 | 进仓库 | 商用 |
|---|---|---|---|---|---|
| `gb_chn` | geoBoundaries CHN ADM2 | 中国县级 2391 个 | **PDDL v1.0**（公有领域奉献） | ✓ | ✓ |
| `gb_chn_adm1` | geoBoundaries CHN ADM1 | 中国省级 31 个 | **PDDL v1.0** | ✓ | ✓ |
| `gb_vnm` | geoBoundaries VNM ADM2 | 越南县级 708 个 | **CC BY 3.0 IGO** | ✓ 须署名 | ✓ |
| `gb_usa` | geoBoundaries USA ADM1 | 美国州级 51 个 | **CC BY 4.0** | ✓ 须署名 | ✓ |
| `gb_*`（其他国） | geoBoundaries DEU/AUT/FRA/DNK/BEL/NLD/IND/PAK/BGD | 各国 ADM1 | **CC BY 4.0**（**逐国可能不同**） | ✓ 须署名 | ✓ |
| `atlaspi` | AtlasPI (atlaspi.it) | 1038 个历史政体，−4500–2024 | **Apache-2.0** | ✓ | ✓ |
| `cshapes` | CShapes 2.0 (ETH Zürich) | 主权国家边界 1886–2019 | **CC BY-NC-SA 4.0** | ✓ | **✗ 禁商用** |

### geoBoundaries 的许可**逐国不同**

不要以为"geoBoundaries 都是 CC BY 4.0"。本仓库实测：
- `CHN` → **PDDL v1.0**（公有领域奉献，无附加条件）
- `VNM` → **CC BY 3.0 IGO**（来自 OCHA，须署名 OCHA ROAP / Government of Viet Nam）
- `USA` / `DEU` / `AUT` / `FRA` / `DNK` / `BEL` / `NLD` / `IND` / `PAK` / `BGD` → **CC BY 4.0**

**新增国家前必须查它的实际许可**：`https://www.geoboundaries.org/api/current/gbOpen/{ISO3}/{ADM}/`
返回的 `boundaryLicense` 字段就是权威答案。代码里已经按国家记录（见 `src/fetch_data.py` 的 `SOURCES`）。

---

## 二、风险登记

### R1 ⚠️ CShapes 是 NC（禁商用）—— **已经影响到两个题材**

`ww1-europe` 和 `ww2-europe` 的几何来自 CShapes 2.0。

ETH 官网的[许可声明](https://icr.ethz.ch/data/cshapes/)原文：

> CShapes by Schvitz, Rüegger, Girardin, Cederman, Weidmann, Gleditsch is licensed
> under a Creative Commons **Attribution-NonCommercial-ShareAlike 4.0** International License.

三个字母都要满足：

| 字母 | 含义 | 对本项目 |
|---|---|---|
| **BY** | 署名 | 必须引用 Schvitz et al. 2022, *JCR* 66(1):144–61 |
| **NC** | **非商业** | **任何商业用途都不行** —— 包括把成片发到有收益的短视频账号 |
| **SA** | 相同方式共享 | 衍生作品必须也用 CC BY-NC-SA 4.0 |

**代码里原来写的是「商用条款未确认」—— 这不是未确认，是确认了：不能商用。**
（`packages/core/histmap_core/datasets/cshapes.py`）

**想商用这两条题材，只有两条路：**
1. 改用 **AtlasPI**（Apache-2.0，覆盖 1886–2024 的国家边界）
2. 改用 aourednik `historical-basemaps` 的 `world_1914` / `world_1939`
   —— 但那条会把整个项目传染成 GPL-3.0（见 R3）

### R2 ⚠️ 中国地图的**审图号**要求（跟版权无关）

《地图管理条例》（国务院令第 664 号）规定：**向社会公开的地图应当报送审核**，
取得审图号后才能出版、展示、登载。

这条**不因为数据是公有领域就豁免** —— PDDL 给你的是数据权利，不是出版许可。
适用场景包括短视频、公众号、网站上的中国地图。

- 本仓库现有中国题材：`tang`、`song`、`mingqing`、`central-plains-war`、
  `india-pakistan-partition`（印度不含中国）、`china-atlaspi`
- **自用/研究/内部展示**：一般不涉及
- **公开传播**：需要评估是否要走审图流程

这一条**不是本仓库能替你解决的**，只是如实告知。

### R3 aourednik/historical-basemaps 是 GPL-3.0 —— 会传染整个项目

本仓库**目前没有使用**这个源，但如果要引入，须知：

[LICENSE](https://github.com/aourednik/historical-basemaps/blob/master/LICENSE) 原文（GPL-3.0）：

> **5(c)** You must license **the entire work, as a whole**, under this License to
> anyone who comes into possession of a copy.
>
> **5(a)** The work must carry prominent notices stating that you modified it.

- 商用 ✓（GPL 允许）
- **但你的整个仓库必须变成 GPL-3.0** —— 不能保持 MIT/Apache
- 下游自动获得 GPL 全部权利，你不能再加限制
- **另有更麻烦的问题**：它自己的 README 说数据是
  「从各种来源收集、改编、转换而来，有些只能从 wayback machine 找到，
  来源包括**匿名学生作品**」—— **出处不可追溯**。
  GPL 只能授权作者有权授权的部分。

**一个有用的细节**：GPL 第 2 条明确说「运行受保护作品产生的**输出**，
只有在……该输出本身构成受保护作品时才受本许可证约束」。
所以**成片一般不被传染，被传染的是仓库** —— 但如果图基本等于数据的复制品，
边界就模糊了。

### R4 二十四史文本：原文公有领域，**点校本有版权**

本仓库的史实依据来自《新唐书》《宋史》《明史》《清史稿》等。

| 用什么 | 状态 |
|---|---|
| **原文本身**（作者去世上千年） | **公有领域** ✓ |
| **百衲本等影印本** | 公有领域 ✓ |
| **中华书局点校本**的标点、校勘成果 | **有版权** ✗ |
| **维基文库**的转录文本 | 受其 CC BY-SA 约束（逐页可能不同） |

**区分标准**：史实、地名、人物属公有领域；**现代人的点校、标点、注释**是独立著作权。

本仓库的做法是**按史实自己编控制表**（谁在哪一年控制哪一块），不复制任何点校本的
文字或图，这是安全的做法。

### R5 历史边界本身就是学术近似 —— 数据质量风险

不是法律风险，但必须写在图上一并告知：

- **AtlasPI** 每条记录自带 `confidence_score`（实测 0.5–0.9），是**学术近似**，
  不是测绘界线。本仓库默认只画 ≥0.65 的，并在页脚标明。
- **geoBoundaries 的历史题材**底图是**现代行政区**，按治所归并反推历史疆域。
  页脚必须声明"几何是重建的，非史料记载的实际界线"。
- **CShapes** 是 1886 年起的**主权国家**边界，不适用于更早的时期。
- 重叠/空洞：不同源的多边形可能重叠（如 geoBoundaries 的印度
  `Jammu and Kashmīr` 覆盖整个克什米尔争议区，与巴基斯坦单元重叠）
  或留缝。本仓库对"未归属"区域的处理见 R6。

### R6 已修：同名单元被静默丢弃

**根因**：印度和巴基斯坦**各有一个省叫 `Punjab`**，合并单元表时撞名，
代码直接丢弃后一个 → 巴基斯坦整块旁遮普消失 → 图上一个大白洞。

**已修**（commit `73f3e9a`）：撞名不丢，拆成 `Punjab (IND)` / `Punjab (PAK)`，
两个都保留。`src/new_topic.py` 和 `src/add_units.py` 两处一起改。

**这条写在这里的原因**：它是一个**跨国家的命名冲突**问题，
任何多国题材都可能再遇到（德意志统一、法国大革命等）。
新增跨国题材后请检查 `build` 输出里的撞名警告。

---

## 三、署名要求（用到就必须照做）

| 数据源 | 要求的署名文字 |
|---|---|
| geoBoundaries CHN | `geoBoundaries CHN ADM1/ADM2 — PDDL v1.0` |
| geoBoundaries 其他国 | `Contains data from geoBoundaries (CC BY 4.0)` |
| geoBoundaries VNM | `OCHA ROAP / Government of Viet Nam (CC BY 3.0 IGO)` |
| AtlasPI | `AtlasPI (atlaspi.it) — Apache-2.0` |
| CShapes | `Schvitz, Guy, et al. 2022. "Mapping The International System, 1886–2017: The CShapes 2.0 Dataset." Journal of Conflict Resolution 66(1): 144–61.` |

本仓库已经把署名画在**每张图的页脚**（`source_note` / `license_note` 字段），
题材数据里逐条记录。新增题材时请照填。

---

## 四、明确**不能**用的源（附官方原文）

### CHGIS（哈佛-复旦中国历史地理信息系统）

[复旦大学史地所《用户协议》](https://yugong.fudan.edu.cn/CHGIS/bqsm.htm)原文：

> **(3)** CHGIS数据**仅限于使用在非商业的学术研究和教育上**。商业使用本数据需要一份
> 单列的，由CHGIS管理委员会提供的CHGIS商业数据协议。
>
> **(7)** ……**没有管理委员会的书面同意，不得以任何电子载体形式或通过因特网下载的
> 方式重新发布CHGIS数据。**
>
> **(4)** 如果你主要居住在中华人民共和国，或主要在该国内使用该数据，
> **需要得到复旦大学中国历史地理研究中心的许可**

**进仓库 ✗（第 7 条）、商用 ✗（第 3 条）、国内使用还要复旦书面许可 ✗（第 4 条）。**

### 《中国历史地图集》（谭其骧主编，中国地图出版社）

- 1982–1988 年出版，主编谭其骧 1992 年去世
- 中国著作权法：作者终身 + 死后 50 年 → **2042-12-31 前一直在保护期内**
- 按法人作品算（出版后 50 年）也要到 **2032 年**
- **描摹/数字化 = 制作衍生作品**；即使"重画"，实质相似仍属侵权
- 另叠加 R2 的审图号要求

**禁止使用其任何图像内容，也不要照着描。**

---

## 五、商用前的核对清单

- [ ] 这个题材的几何来自哪个源？（查 `data/topics/topics.json` 的 `license_note`）
- [ ] 那个源的许可允许商用吗？（CShapes ✗ / geoBoundaries ✓ / AtlasPI ✓）
- [ ] 中国地图 —— 是否涉及审图号？
- [ ] 图画出来了吗？页脚有没有该源的署名？
- [ ] 如果用到了 CC BY-SA / GPL 的源，**本仓库的许可是否已相应调整**？
- [ ] 引用了点校本吗？（史实可以用，点校文字和图不能抄）

---

## 六、许可变更历史

| 日期 | 变更 |
|---|---|
| 2026-10 | 接入 AtlasPI（Apache-2.0，可商用），中国题材多了一条能商用的路 |
| 2026-10 | 确认 CShapes 为 CC BY-NC-SA 4.0（原记录写"商用条款未确认"） |
| 2026-10 | 修复跨国同名单元被丢弃（`Punjab`），见 R6 |

---

**免责声明**：这份文档是工程侧的尽最大努力梳理，**不是法律意见**。
涉及公开传播或商业变现前，请就不确定的部分咨询专业法律人士。
