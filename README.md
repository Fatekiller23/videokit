# VideoKit

视频变成文字。两条入口，两种产物。

| 入口 | 输入 | 平台有没有字幕 | 产物 |
| --- | --- | --- | --- |
| B 站链接 | BV 号或链接 | 有就取，没有就跑语音识别 | 交付文稿（纯文本 .md） |
| 本地文件 | 视频或音频路径 | 没有，一律跑语音识别 | SRT 字幕（.srt） |

两条入口分开，是因为判断依据不同。链接那条要先问平台「这一讲有没有字幕」，
有字幕的零成本取回；本地文件没有平台可问，只有语音识别一条路。产物也不同：
文稿给人和 agent 读，字幕给播放器读。

## 安装

```
uv sync --extra whisper      # 本地文件这条路径
uv sync --extra asr          # 中文语音识别（B 站无字幕讲次）
```

基础安装只有 `websocket-client`。yt-dlp 与 ffmpeg 是外部程序，要在 PATH 上。

## 用法

```
videokit probe BV1Y5QgB5Eas
videokit video BV1Y5QgB5Eas --quality 720p --parts 3,4 --out videos
videokit audio BV1Y5QgB5Eas --out audio
videokit transcript BV1Y5QgB5Eas --out transcripts
videokit login

videokit srt "D:\videos\talk.mp4" --out subtitles
videokit srt "D:\videos" --language en
```

`videokit srt` 接受多个文件或目录。目录取其中所有认得的容器，产物默认落在视频旁边，
同名 `.srt`。已有字幕且比视频新时跳过，`--force` 重做。

退出码：0 全部成功或全部跳过，1 有文件失败，2 参数错。

## 两条链路的引擎不同

| 链路 | 引擎 | 理由 |
| --- | --- | --- |
| B 站无字幕讲次 | FunASR `paraformer-zh` + `fsmn-vad` + `ct-punc` | 中文为主，`ct-punc` 内建标点，CPU 上 13.7 倍实时 |
| 本地文件 | faster-whisper `large-v3` | 英语是 Whisper 的强项，非中文场景没有更便宜的选择 |

不做统一。两条链路的输入语言分布不同，共用一种引擎只会牺牲其中一条。

## 目录

- `src/videokit/__init__.py` B 站链路与命令行派发。规格见 [docs/bilibili-pipeline.md](docs/bilibili-pipeline.md)。
- `src/videokit/local.py` 本地文件链路。规格见 [docs/local-srt.md](docs/local-srt.md)。
- `docs/known-issues.md` 踩过的坑，含 faster-whisper 在 PyAV 19 上的崩溃与绕法。
- `tests/` 单元测试；`_live_*` 开头的是真机脚本，不打桩。

## 与 EcoBuddy 的关系

这个仓从 `EcoBuddy/packages/VideoKit` 拆出来独立成仓，历史一并带过来。
EcoBuddy 的工作台是调用方：它按路径依赖装这个包，页面上的「字幕提取」调的是这里。
方向只有一个，本仓不 import EcoBuddy 的任何东西，也不读它的状态。

## 已知限制

- 只做 B 站这条链接链路。其他站点未测。
- 本地文件这条链路不产出词级时间戳，字幕按句切。
- 中文走 Whisper 时标点质量低于 `ct-punc`。要中文文稿请走 B 站链路的 `transcript`。
