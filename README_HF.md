# Hugging Face Spaces 的元数据（YAML front-matter）。
#
# ⚠️ 这个文件名**必须变成 README.md** HF 才会认 —— HF 只读仓库根目录的
#    README.md 里的 front-matter。叫 README_HF.md 是**不生效**的，
#    推上去会得到一个没有元数据的 Space（SDK 不对、端口不对，构建完打不开）。
#    别手工改名：仓库自己的 README.md 是给人看的项目说明，也值得出现在
#    Space 页面上。用 `python src/deploy_hf.py` 推，它会把下面这段
#    front-matter 拼到 README.md 开头再推过去。
#
# app_port 必须和 Dockerfile 里的 ENV PORT 一致。两边都是 7860
# （HF Spaces 的 Docker 约定端口）。早先这里写 8810、Dockerfile 也写 8810，
# 看着自洽，但只要平台注入了 PORT=7860 就会对不上。
title: histmap 历史地图工作台
emoji: 🗺
colorFrom: yellow
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: 参考图抽风格 → 生成统一风格的历史地图与演变视频
