# 云端部署指南

这个项目是**画图的**，不是静态站：出图要跑 Pillow + Shapely，出片要跑 ffmpeg。
所以不能丢到纯静态托管（GitHub Pages / Vercel 静态 / Netlify），
需要一个能跑 Python 和 ffmpeg 的容器环境。

---

## 一、先知道两件事

### 1. 二战/一战的国界数据不在仓库里

CShapes 2.0（25 MB）要求学术引用、商用条款未确认，所以按许可走**下载器模式**，
不进仓库。`Dockerfile` 会在**构建时**自动下载它。

本地开发时同理，需要：

```bash
python src/fetch_data.py --all        # 国内建议先设 HTTPS_PROXY
```

没下载也能跑：唐、宋两个题材是自带的；二战/一战会在顶栏显示「缺数据」，
并告诉你怎么补。**不会再出现「点开图就 500」**。

### 2. 出片很吃 CPU

一张 1920×1080 的图要经过 2 倍超采样（实际画 3840×2160）+ Shapely 合并多边形。
本机（i3-13100F）约 1–2 秒/张。免费档云主机通常是 0.1–0.5 vCPU，
**会慢 5–20 倍**，而且免费档常有请求超时（30–60 秒）。

所以云端建议：

- 出图：单张没问题；**「出全部图」把分镜压到 8–20 帧**
- 出片：146 帧的二战全区间在免费档基本会超时，别试
- 想认真出片就跑本地，或者上付费档（≥1 vCPU 常驻）

---

## 二、部署到 Hugging Face Spaces（推荐，免费且能跑 ffmpeg）

免费 CPU、原生支持 Docker、不需要信用卡，是目前最省事的选择。

1. 注册 https://huggingface.co 并登录
2. 右上头像 → **New Space**
   - Name：`histmap`
   - License：`MIT`
   - SDK：**Docker** → Template：**Blank**
   - Hardware：CPU basic（免费）
   - Visibility：Public
3. 把本仓库推上去（Space 会自动构建）：

```bash
git remote add hf https://huggingface.co/spaces/<你的用户名>/histmap
git push hf main
```

4. 等构建完成（首次约 3–6 分钟，其中包含下载 CShapes），
   访问 `https://huggingface.co/spaces/<你的用户名>/histmap`

> Space 的容器端口由 `PORT` 注入，`app.py` 见到 `PORT` 会自动绑 `0.0.0.0`。
> 如果界面打不开，先看 Space 的 Build/Logs 里有没有 `工作台已启动`。

---

## 三、部署到 Render（也免费，但会休眠）

1. 注册 https://render.com
2. **New → Web Service** → 连你的 GitHub 仓库
3. 配置：
   - Runtime：**Docker**
   - Instance Type：Free
   - Health Check Path：`/api/health`
4. 部署完成后的地址形如 `https://histmap-xxxx.onrender.com`

免费档 15 分钟无访问会休眠，下次访问冷启动要 30–60 秒。
仓库里带了 `render.yaml`，也可以在 Render 里选 **Blueprint** 直接读它。

---

## 四、部署到 Fly.io（要绑卡，但性能最好）

```bash
# 装 CLI 并登录
fly auth login
# 在仓库根目录
fly launch --no-deploy        # 认到 Dockerfile，生成 fly.toml
fly deploy
fly open
```

注意：出片吃 CPU，`shared-cpu-1x` 也偏慢；`performance-1x` 才比较顺手。
`fly.toml` 里建议把 `min_machines_running` 设成 1，避免冷启动。

---

## 五、自己确认部署是否成功

打开 `https://<你的地址>/api/health`，应该看到：

```json
{
  "ok": true,
  "topics": ["ww2-europe", "ww1-europe", "tang", "song"],
  "stale": []
}
```

再看 `https://<你的地址>/api/scenes`，每个题材都有 `"ready": true`。
哪个是 `false`，看它的 `missing` 字段就知道缺什么。

出图冒烟（不用开浏览器）：

```bash
curl -X POST https://<你的地址>/api/render \
  -H 'Content-Type: application/json' \
  -d '{"scene":"song","date":"1140-01-01","theme":"light","size":"16x9"}'
```

返回里 `image` 字段是个 `/media/api/...` 路径，拼到域名后面能打开就是通了。

---

## 六、几个坑（都已处理，遇到时知道原因就行）

| 现象 | 原因 | 处理 |
|---|---|---|
| 部署后访问不通 | 绑了 127.0.0.1 | `app.py` 见到 `PORT` 自动绑 `0.0.0.0` |
| 构建失败在 `COPY requirements.txt` | `.dockerignore` 的 `*.txt` 把它排除了 | 已加 `!requirements.txt` |
| 地图上中文是方框 | 容器里没中文字体 | Dockerfile 装了 `fonts-noto-cjk` |
| 二战点开报 500 | CShapes 没下载 | 构建期自动下载；失败会降级成「缺数据」提示 |
| 出片超时 | 免费档 CPU 太弱 | 压帧数或上付费档，见上文 |

---

## 七、这个项目不适合放的地方

- **GitHub Pages / Netlify / Vercel（静态）**：它是服务端渲染 + ffmpeg，跑不了。
- **Cloudflare Workers**：没有 Python + Pillow + ffmpeg 的运行时。
- **任何无状态、无本地磁盘的 Serverless**：渲染产物要落盘（`output/`），
  出片是「提交任务 → 轮询进度」的两段式，需要进程内存活。

如果要上纯 Serverless，得先把「渲染产物落盘」改成对象存储 + 任务状态外置，
那是另一个工程量，不在当前范围内。
