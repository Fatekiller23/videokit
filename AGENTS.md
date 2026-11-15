# VideoKit

视频转文稿与字幕。两条入口：B 站链接（探测、下载、平台字幕、语音识别），
本地文件（抽音频、语音识别、写 SRT）。

## 回复原则

始终使用中文。英文回复遵循 ASD-STE100。

## 常用命令

- 装环境：`uv sync --extra whisper`（本地文件），`uv sync --extra asr`（中文路径）
- 测试：`uv run pytest -q`
- 转换本地视频：`uv run videokit srt <文件或目录> --out <目录>`
- 本地文件这条路径的证据见 `docs/local-srt.md`

## 结构

- `src/videokit/__init__.py` B 站链路与命令行派发。规格见 `docs/bilibili-pipeline.md`。
- `src/videokit/local.py` 本地文件链路。规格见 `docs/local-srt.md`。
- `tests/` 单元测试与 `_live_*` 真机脚本。真机脚本不打桩，需要登录态和网络。

## 边界

这个仓只管「视频变成文字」。不建网页，不做任务队列，不读工作台的状态。
谁要一个页面，谁在自己那边调用这里。
