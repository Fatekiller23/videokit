# VideoKit

B 站视频的探测与下载。给一个链接，先告诉你有多少讲、哪些有字幕、有哪些清晰度可选，再按你的选择把视频或音频下下来。

有字幕的讲次可以直接产出交付文稿，一条命令跑完；无字幕的讲次记为待 ASR。本地语音识别本身是同一子项目的另一块，不在本包内。

## 它解决什么

一门课几十讲、几十小时，逐集点开看有哪些字幕不现实。而这个信息决定后面要花多少算力：有字幕的讲次直接取，零成本；没有的才需要跑语音识别。

同一门课里两种状态会混着出现。实测 `BV1Y5QgB5Eas`（徐高 2025 年宏观课，30 讲），21 讲有字幕、9 讲没有，且与讲次时长无相关性。所以必须逐讲判定，不能按整条视频二选一。

## 四个入口

```
probe(url, cookies=None) -> Video
    探测。不下载任何文件数据。

part_subtitles(url, part, language="ai-zh", cookies=None) -> list[Segment]
    取回并解析某一讲的平台字幕轨。无字幕轨时抛 SubtitleUnavailable。

transcript_text(segments) -> str
    把分段拼成交付文稿的连续正文。分隔规则见「交付文稿」。

transcripts(url, out_dir, parts=None, cookies=None) -> tuple[PartTranscript, ...]
    一条命令跑完。逐讲判定，有字幕的写出交付文件，无字幕的记为待 ASR。

download_video(url, out_dir, parts=None, quality="720p", cookies=None) -> list[Path]
    下视频。自动合并分开的音视频流。

download_audio(url, out_dir, parts=None, cookies=None) -> list[Path]
    只下音轨，已是语音识别需要的 16 kHz 单声道 WAV。
```

`parts` 是按序号选讲次，省略表示全部。

`cookies` 一般不用传。登录过一次之后入口自己找到凭据，见下面「登录态」。

### 为什么探测要单独一个入口

「先看看有什么再决定下什么」是真实用法。并进下载里，调用方就无法只问不取。

### 为什么音频要单独一个入口

同一讲，视频 130 MB 到 340 MB，音频 40 MB 左右，差一个数量级。只做文稿时不需要视频。

### 为什么字幕要单独一个入口

有字幕的讲次不需要跑语音识别，零成本。所以「这一讲走字幕还是走 ASR」是逐讲的决策，输入就是这次调用的结果。实测 `BV1Y5QgB5Eas` 有字幕的 21 讲与无字幕的 9 讲时长分布无差别，所以这个判断不能按整条视频做。

## 平台字幕

```
part_subtitles(url, part, language="ai-zh", cookies=None) -> list[Segment]
```

返回按起始时间有序的 `Segment` 列表，每个 `Segment` 有自己的 `start_sec`、`end_sec` 与 `text`。

```python
for segment in videokit.part_subtitles(url, part=12):
    print(segment.start_sec, segment.text)
```

`text` 不含时间戳。两条来源的时间戳粒度不同（平台字幕逐句，语音识别可到词级），混在字符串里下游没法统一处理。

`language` 默认 `ai-zh`，是这些视频唯一的机器生成中文轨。

### 「取不到字幕轨」与「解析出空列表」是两回事

这两种结果的处置方式完全不同，所以用异常分开。

- 取不到字幕轨，或该语言的轨不存在，或轨里没有合法时间块，**抛 `SubtitleUnavailable`**。这一讲的正确处置是转去跑语音识别，而不是产出一份空文稿。
- `parse_srt("")` 返回空列表。这是纯函数对空输入的回应。

异常里带讲次号，调用方能直接指出是哪一讲。

「解析出 0 段」只可能来自空输入，实测 21 份真实录制件没有一份解析出 0 段。

### 无标点是已接受的取舍

**`ai-zh` 字幕完全没有句读，这是一条实测事实，不是识别缺陷，也不是本包要修的问题。**

实测 21 份真实字幕轨，共 454245 个正文字符。

| 项 | 实测值 |
|---|---|
| 中文全角标点（，。、？！等） | 0 |
| ASCII `,` `?` `!` `;` | 0 |
| 分段首尾带空格 | 0 |
| 分段以句末标点结尾 | 0 |

文本会是一整条没有句号的连续流。使用者已接受这个结果，与「字幕直接用、不做来源标记」是同一条决策的两面。补救路径是把平台字幕也过一遍标点恢复，代价是会重算原有时间戳，不在本包范围内。

配套的两条实测事实，写在这里以免后来者当成解析 bug。

- **相邻重复分段保留，不去重。** 实测 21 份里 17 对相邻分段正文完全相同，例如 p12 第 418 与 419 段都是 `也不可能啊`。这是两个不同时段的真实内容，删掉任一段会丢一秒左右的话。`parse_srt` 因此不做事后去重。
- **小数点后的浮点泄漏原样保留。** 例如 `但中国是1.31000.30000美元了`（p03 第 2101 段）、`33.4000000000000`（p21 第 742 段）。这是 B 站自己的 ASR 流水线把 Python `float` 的 repr 写进了字幕，属于上游数据，本包不修。

### 字幕文本从 stdout 读，不从文件读

`yt-dlp` 会把字幕写成文件，而文件名本包推不出来。实测两条。

- `-o` 的占位符管不到字幕文件名，写出来的是 `BV1Y5QgB5Eas_p12.ai-zh.srt`。
- `--print after_move:filepath` 对字幕返回空，而下载视频时它是有输出的。

所以改用 `--print "%(requested_subtitles)j"`，字幕文本本身就在这个 JSON 里。**实测这份文本与 `yt-dlp` 写出的文件逐字节相同**，p12 两边 sha256 都是 `b13447822d78f2a02fe39101a66c5b17283602fdeca82c96725e0557f224c24c`，123777 字节。同时因为 `--print` 自带 `--simulate` 语义，磁盘上不会落文件，也就没有文件名编码问题。

这个载荷是纯 ASCII 的，中文被转义成 `\uXXXX`。这一点在本包里很重要，`_run` 有意按控制台代码页解码子进程输出，转义载荷不受影响。

## 交付文稿

一条命令，给一个 BV 号，逐讲判定并产出所有有字幕讲次的纯文本文稿。

```
uv run --python 3.12 python videokit.py transcript BV1Y5QgB5Eas --out transcripts
uv run --python 3.12 python videokit.py transcript https://www.bilibili.com/video/BV1Y5QgB5Eas --parts 3,4
```

从 Python 里调用是同一件事。

```python
records = videokit.transcripts("BV1Y5QgB5Eas", Path("transcripts"))
```

`parts` 是 `--parts` / 列表，省略表示全部讲次。`out_dir` / `--out` 省略时落在当前目录的 `transcripts/`。

交付文件名带讲次序号与标题，所以目录列表按课程顺序读。

```
03-【徐高2025年宏观课】中国宏观经济 p03 02-宏观经济数据和宏观逻辑的逻辑-1.txt
```

### `source` 只服务程序，不进交付文稿

**这一条必须写清楚，否则后来者会认为规格自相矛盾。**

使用者已定「字幕直接用，不做来源标记，不做质量分级」，规格 Out of Scope 里也写了「交付文稿不做来源区分，不做质量打分」。而中间产物 `PartTranscript` 有 `source` 字段。两处并不冲突，因为它们说的是两层不同的东西。

- **中间产物是给程序的。** 它要区分这一讲的正文从哪来，因为两条来源的时间戳粒度不同（平台字幕逐句，语音识别可到词级），标点风格也不同。代码里用得上，所以要留着。
- **交付文稿是给读者的。** 读者要的是能读的正文，来源标记与质量分级对他只是噪声，所以一律不写。

所以 `write_transcript` 只写 `transcript_text(record.segments)` 的结果，`source`、`language`、`status`、`failure_reason` 与任何时间戳都不进文件。测试 `DeliverableFiles.test_the_source_does_not_reach_the_file` 锁着这条。

### 逐讲判定，不按整条视频二选一

实测 `BV1Y5QgB5Eas` 21 讲有字幕、9 讲没有，且缺失的讲次夹在中间，第 1、2 讲就没有。所以判定逐讲做，且**无字幕的讲次不中止整批**。

无字幕的讲次在返回的记录里是 `status = "pending"`，`source` 与 `language` 为 `None`，`segments` 为空，**不写文件**。这一讲等着后面的 ASR 路径补齐。

**`pending` 是规格那三个取值之外的第三个。** 它既不是 `ok` 也不是 `failed`。记成 `ok` 会宣称一份并不存在的文稿；记成 `failed` 配上 `failure_reason = "no_audio"` 会冤枉音轨，音轨本身是好的。所以单独一个值。

命令结尾打印一行汇总，包含产出了几讲、还剩几讲待 ASR 以及它们的序号。没有这一行，一次不完整的跑看起来和完整的一样。

### 平台字幕无标点，这一条落在交付文稿里

交付文稿会是一整条没有句号的连续流，段落划分要靠另加策略，本包不做。这是上游数据的样子，见「无标点是已接受的取舍」。

同一门课将来的 ASR 讲次会有标点，两种风格会在交付文稿里混着。这一取舍与补救路径（把平台字幕也过一遍标点恢复，代价是重算原有时间戳）记在规格 #10 与 #17。

### 拼接规则是量出来的，不是选的

**朴素 `"".join` 会把 55 个位置的词粘在一起，`" ".join` 会插入 38994 个原文没有的空格。** 两个都不能用。

实测 21 份真实字幕轨，共 40283 个分段、40262 个相邻边界，按两侧字符分类。

| 前段末 | 后段首 | 个数 | 分隔符 |
|---|---|---|---|
| 汉字 | 汉字 | 38994 | 无 |
| 汉字 | ASCII 字母数字 | 572 | 无 |
| ASCII 字母数字 | 汉字 | 324 | 无 |
| 标点或符号 | 汉字 | 294 | 无 |
| **ASCII 字母数字** | **ASCII 字母数字** | **55** | **一个半角空格** |
| 标点或符号 | ASCII 字母数字 | 23 | 无 |

规则因此是**两侧都是 ASCII 字母数字时插入一个半角空格，其余零分隔符**。55 处的真实例子。

| 前段尾部 | 后段头部 | 不插空格的后果 |
|---|---|---|
| `然后呢回答就very good` | `Good` | `very goodGood` |
| `60616263646566676869` | `70707172737475767778` | 两串数字连成 39 位 |

反面例子也要记。`农业占比7%` 后面接 `然后呢这个第二就是第一产`，`%` 不是字母数字，所以不插空格，拼出来读着正常。这类分段有 315 个。

这条规则写在 `_needs_separator`，改动它就要把这四个数字重新量一遍。

### 不去重，也不修上游的浮点泄漏

相邻重复分段 17 对原样保留，例如 p12 第 418 与 419 段都是 `也不可能啊`，交付文稿里读作 `...政治阻力极大啊也不可能啊也不可能啊`。它们是两段不同时段的真实内容，删掉任一段会丢一秒左右的话。

`大概只有三十六三十六.8000000000000` 这类浮点泄漏也原样进入文稿。那是 B 站自己的 ASR 流水线把 `float` 的 `repr` 写进了字幕，属于上游数据。

### 交付文稿不含时间戳

`Segment` 的 `start_sec` / `end_sec` 是独立字段，`transcript_text` 只用 `text`。测试同时断言交付内容里没有 `-->`、没有 `HH:MM:SS`、也没有秒数的小数写法。

## 登录态

B 站字幕未登录拿不到，所以要先登录一次。取凭据由程序引导，不需要手工导出 Cookie 文件。

```
uv run --python 3.12 python videokit.py login
```

这条命令拉起浏览器窗口，扫一次码，然后把凭据落盘并打印它的路径。之后 `probe` 与两个 `download_*` 自动使用它。

从 Python 里调用是同一件事。

```python
videokit.login()             # 拉起浏览器，扫码一次，落盘凭据
videokit.reuse_or_login()    # 已有凭据就复用，没有才拉起浏览器
```

显式传 `cookies=` 仍然生效，优先级高于默认发现。

命令行入口目前只有 `login` 一个子命令。探测、下载、转写三块能力的命令行是 #20 的范围；这里单独有一个入口，是因为扫码这件事得能在不 import 本模块的前提下跑起来。

### 为什么必须走 CDP 导出

程序无法直接读浏览器的 Cookie 库。本机实测四种方式全部失败，记录在 `docs/research/measured-bilibili-subtitle-availability.md`。

| 方式 | 结果 |
|---|---|
| `yt-dlp --cookies-from-browser chrome` | `Could not copy Chrome cookie database`，数据库被进程锁住 |
| `yt-dlp --cookies-from-browser edge` | `Failed to decrypt with DPAPI`，新版 Chromium app-bound 加密 |
| `chrome-agent --copy-cookies` | `Chrome cookies file not found`，它查旧路径 `Default\Cookies`，实际在 `Default\Network\Cookies` |
| robocopy 备份语义复制 | 退出码 16，文件锁未绕过 |

所以路径是：程序拉起一个带远程调试端口的 Chrome，用户在其中扫码，程序通过 CDP 取 Cookie 落盘。

### 两个必须的 CDP 细节

漏掉任何一个都不报错，只是拿到 0 条 Cookie。这与「登录态过期」表现完全一样，所以没有测试覆盖就会静默失败。

- **必须附着到 bilibili 的 page target，且先发 `Network.enable`。** 附着到浏览器级 WebSocket 直接问，返回 0 条。
- **Chrome 启动必须带 `--remote-allow-origins=*`。** Chrome 111 起默认拒绝带 Origin 头的 CDP 连接，报 403，看起来像网络问题而不是缺参数。

这两条都落在 `videokit.chrome_argv` 与 `videokit.cdp_cookies`，测试直接对着断言。

### 凭据文件的位置与权限

凭据落在仓库之外的数据目录，默认 `%LOCALAPPDATA%\VideoKit\cookies.txt`。落盘时降权到只有当前账户可读写，文件与它所在的目录都降。

**放进仓库是不允许的，这一条编码成结构而不是靠记忆。** 两道独立的防线：

- 路径在仓库工作区之外，就地放进仓库这个错误本身不可用。
- `.gitignore` 里有 `cookies.txt`、`*cookies*.txt` 与 `browser-profile/`，万一有人手工把数据目录复制进来，`git check-ignore` 仍然拦住。

第三条容易漏。**浏览器 profile 与凭据文件同级敏感**，它带着同一个登录态，只挡 `cookies.txt` 会漏掉 profile 这一半。断言因此同时查目录本身与目录里的 Cookie 库文件。

`VIDEOKIT_DATA_DIR` 可覆盖数据目录，测试靠它保持隔离。

### profile 复用

浏览器 profile 落在数据目录下的 `browser-profile`。登录态跟着 profile 走，重启浏览器后仍然有效，所以扫码是一次性的。

### 导出失败时报的是哪一步

`LoginError` 的文案指出失败的环节，四种情况分开。

- 找不到 `chrome.exe`，报查过的路径。
- 远程调试端口连不上，报 URL 与原因。
- 没有 page target 可附着，报 Chrome 报了几个 target。
- Cookie 拿到了但没有 `SESSDATA`，报用户需要扫码。

最后一种最需要区分。匿名 profile 也会返回一堆 Cookie，只看「拿到的 Cookie 是否非空」会把未登录判成已登录。

## 数据结构

```
Part
  index           讲次序号，从 1 起
  id              平台标识，如 BV1Y5QgB5Eas_p3
  title           讲次标题
  duration_sec    秒
  subtitle_langs  字幕语言元组，空表示没有字幕

Video
  id, title
  parts           Part 元组，按 index 排序
  formats         yt-dlp 的原始格式表

Segment
  start_sec       分段起始秒，浮点
  end_sec         分段结束秒，浮点
  text            该段正文，不含时间戳，不含标点

PartTranscript
  bvid            如 BV1Y5QgB5Eas
  index           讲次序号，从 1 起
  title           讲次标题
  duration_sec    秒
  source          "platform" | "asr" | None。中间产物用，不进交付文稿
  language        "ai-zh" | "zh" | None
  segments        Segment 元组
  status          "ok" | "pending" | "failed"
  failure_reason  None | "no_audio" | "asr_error"
```

`source` 与交付文稿的分层见「`source` 只服务程序，不进交付文稿」。

`status` 与 `failure_reason` 分开，因为失败原因决定重跑策略。`no_audio` 重试多少次都不会成功，`asr_error` 可以重试。混成一个字段，调用方就无从判断该重试还是放弃。本票不产出 `failed` 记录，那条路径属于 #18。

`Part.has_subtitles` 是判断有没有字幕的入口，不要直接看 `subtitle_langs` 是否非空以外的条件。

`Segment.text` 不含标点是上游数据本来的样子，见「平台字幕」一节。

## 代码结构

`videokit.py` 是全部实现。分三层。

**`_run` 是唯一的边界。** 所有外部调用都从这里出去，其余全是纯函数。测试只替换这一个地方。

**`probe` 用两次 `yt-dlp` 调用，这是 B 站提取器的现状，不是设计偏好。**

- `--dump-single-json` 给出全部讲次的结构和格式表，**但字幕字段对 B 站返回空对象**，即使那一讲确实有字幕。
- `--list-subs` 能正确报出字幕，但只输出文本版，且会遍历每个讲次。

如果不清楚这一点，很容易想「优化成一次调用」，然后得到「所有视频都没字幕」的错误结论，接着给每一讲都跑一遍语音识别。相关上游问题见 [yt-dlp #17527](https://github.com/yt-dlp/yt-dlp/issues/17527)，未修。

**`parse_subtitle_table` 用正则解析文本块。** 三条规则：块起始、表头、语言行。实测三种输入形态（30 讲合集、5 讲视频、单视频）形状一致，同一套规则全部通过。

两种必须处理的边界：

- **讲次标识符有两种形态。** 合集是 `BV1Y5QgB5Eas_p3`，单视频是 `BV1GJ411x7h7` 没有后缀。规则不得假定后缀存在。
- **`danmaku` 必须排除。** 它是被无条件附加的弹幕 XML，与字幕无关。只含 `danmaku` 等价于没有字幕。漏掉这一条，所有视频都会被判成有字幕。

**`parse_srt` 解析字幕轨。** 结构是严格四行一块，`index / timestamp / text / blank`，全文件 `4N+1` 行，以一个空行收尾。实测 21 份共 40283 个分段，零例外。两条实现上的要求。

- **索引行不解析。** 有 10 个分段的正文本身就是纯数字（p18 第 366 到 370 段等），所以「整数行」和「索引行」从字面上分不开。改用时间行作锚点，而正文里 `-->` 出现 0 次，锚点不会误判。
- **按时间行的下一行取正文，不按空行切块。** 空行只做块分隔、不承载内容，跳过即可。这样也容忍被字节区间裁掉末行空行的字幕轨。

浮点秒由 `毫秒 / 1000` 得到，不加舍入。实测这个除法在真实取值上与原文本等值，例如 `835719 / 1000 == 835.719`。

**`_format_selector` 把清晰度名字映射到格式编号。** B 站不提供合流的流，选择器要同时指名一路视频和一路音频。

**转文稿这一层是纯函数加一个循环。** `_needs_separator` 与 `transcript_text` 是纯函数，吃 `Segment` 列表出字符串；`transcript_filename` 与 `write_transcript` 碰文件系统；`part_transcript` 判定一讲；`transcripts` 遍历全部讲次并在有字幕的讲次上落文件。

`_preferred_language` 在 `ai-zh`、`zh-CN`、`zh` 里按序取第一个存在的。这 21 讲只有 `ai-zh`，但入口收的是 `Part.subtitle_langs`，不该假定只有一个。

`video_url` 把裸 BV 号补成 URL，两者都收，使用者不用自己拼前缀。

**凭据问题与「这一讲没字幕」必须分开。** `part_transcript` 只捕获 `SubtitleUnavailable`，那是「这一讲要 ASR」。登录失效时每一讲都表现为无字幕轨，若把它也记成待 ASR，整门课会被交给 ASR 路径，而真正的补救动作是重新导出一次凭据。#13 为这种情况抛 `LoginRequired`，这里有意不接，让它带着下一步提示往上走。

## 下载的两处硬约束

**路径来自 `yt-dlp` 的报告，不是猜文件名。** 用的是 `--print after_move:filepath`。输出模板里有 `%(...)s` 占位符，且后处理（合并、抽音频）会改扩展名，所以磁盘上的名字无法从模板推出来。

**不强制子进程输出编码。** `yt-dlp` 按控制台代码页输出（本机是 cp936），强制 UTF-8 会把中文标题变成替换字符。这些标题会进文件名，解错了返回的路径就打不开。

## 测试

```
uv run --python 3.12 python test/test_videokit.py
```

113 项断言，约 5 秒，不联网、不需要登录态。

跑真实模块。已有的解析与下载测试**只替换 `_run`**，用录制好的真实 `yt-dlp` 输出做夹具。

字幕这一层分成两半，接缝不一样。

- **`parse_srt` 直接吃真实字节，不经任何替换。** 夹具是两份真字幕轨的连续切片，`subtitles-p12-slice.srt`（55 段，3606 字节）与 `subtitles-p18-slice.srt`（9 段，493 字节），逐字节 sha256 与实测记录一致。期望值按夹具手读后写死在断言里，所以少一段或多一段都会失败。p12 那份特意包含相邻重复段（第 418 与 419 段都是 `也不可能啊`）、ASCII 字母与空格、`%` 与小数点；p18 那份包含五段纯数字正文与 4630 毫秒空档。
- **`part_subtitles` 替换 `_run`。** 夹具是真实录制的一次 `--print "%(requested_subtitles)j"` 回包，`subtitle-print-part.txt`（p18 整讲，1078 段）与 `subtitle-print-no-track.txt`（p1，无字幕轨，内容是字面量 `NA`）。「无轨」与「空列表」的区分在这一层断言。

凭据与默认发现这一层换了个接缝。**测试替换 `subprocess.run` 而不是 `_run`**，因为要断言的正是「命令行里到底有没有 `--cookies`」。替换 `_run` 就看不到这一点。

CDP 那一层替换的是 WebSocket 对象。`cdp_cookies(ws)` 收一个已连接的 socket，所以「先 `Network.enable` 再 `Network.getAllCookies`」这条顺序可以在没有浏览器的前提下定死。

夹具在 `test/fixtures/`，全部是真实录制的字节。

- `dump-multipart.json`、`list-subs-multipart.txt`、`list-subs-single.txt`：`yt-dlp` 的真实输出。
- `cdp-cookies.json`：真实 Chrome 的 `Network.getAllCookies` 回包，36 条，Cookie 值已抹掉，其余字段是浏览器原样报的。
- `cdp-targets.json`：真实 `/json/list` 回包，含非 page target 与另一个站点的 page，所以「选了第一个」和「选对了」能区分开。
- `netscape-reference.txt`：**这份期望值由 `yt-dlp` 自己的 Cookie 导出器写出**，是对同一次会话的独立结果。序列化测试断言逐字节相同，这样期望值不是把实现重算一遍。
- `subtitles-p12-slice.srt`、`subtitles-p18-slice.srt`：真字幕轨的连续切片，按行区间裁，保留末尾空行，所以 `4N+1` 成立。
- `subtitle-print-part.txt`、`subtitle-print-no-track.txt`：真实的一次 `--print "%(requested_subtitles)j"` 回包，有轨与无轨各一份。无轨那份只有 3 字节，内容是 `NA`。

`LoginError` 的四种失败路径都有断言，且都用 `timeout_sec=0` 走完，所以测试不会真的等一个扫码超时。

命令行入口也有断言，覆盖成功、未知子命令被拒、失败时非零退出并指出环节。

### 交付文稿这一层

`transcript_text` 直接吃 `Segment` 值，不碰 `_run`，所以分隔规则可以逐条钉死。期望值来自实测数字与手读夹具。

- **`_needs_separator` / `transcript_text` 用构造的 `Segment`。** 每个方向一条断言，含汉字邻接、ASCII 邻接、数字邻接、`%` 结尾，以及真实切片两份。p12 那 55 段 608 字符一个空格都不插，p18 那 9 段 103 字符插 4 个，出 107。
- **`part_transcript` 替换 `_run`。** 替身按讲次回放有轨与无轨两种真实回包，所以「有轨的组装成结构」与「无轨的记为待 ASR」都在真实字节上走。
- **`transcripts` 与 `transcript` 子命令替换 `_run`，文件系统是真的。** 断言 30 讲全部被判定、21 讲落文件 9 讲不落、文件名按序、文件内容是连续正文。

`subtitle-print-part.txt` 本来是 p18 的回包，测试里被当成「有轨」讲次的通用回包，所以那些讲次都得到 p18 的 1078 段。判定与产物的形状因此可断言，讲次之间不串味。

**真实端到端另有两个手工脚本，不进测试套件。**

```
uv run --python 3.12 python test/_live_subtitles.py
uv run --python 3.12 python videokit.py transcript BV1Y5QgB5Eas --out transcripts
```

前一个联网、用真实凭据，取回 4 讲的字幕并与实测分段数逐个对照，再验一次「无轨抛异常」与「空输入返回空列表」的区分。后一个是交付路径的真实端到端，实测约数分钟，产出 21 个文件。跑之前不要设 `PYTHONIOENCODING`。

## 已知限制

**环境变量 `PYTHONIOENCODING` 会让 `probe` 崩。** 实测，与本 ticket 无关，主检出同样复现。

- 不设时，`yt-dlp` 按 cp936 写 stdout，父进程也按 cp936 解，正常。
- 设为 `utf-8` 时，`yt-dlp` 改成写 UTF-8，父进程仍按 cp936 解，于是 `_run` 抛 `UnicodeDecodeError`，`probe` 下一步拿到 `None` 再崩在 `parse_subtitle_table`。

根因是 `_run` 让平台决定编码，而 `PYTHONIOENCODING` 只影响子进程、不影响父进程的解码，两边就此不一致。修法不在本 ticket 范围内，记在这里以免下次当成新 bug 查。跑本包时不要设这个变量。

**三个曾经漏掉的 bug 说明测试的边界在哪。** 早期版本 20 项测试全过但真实调用立刻崩，因为三个问题全在 `_run` 及其返回值上，而测试把它替换掉了：`yt-dlp` 不在 PATH 上、下载返回空列表、中文路径烂掉。

现在这三条都有对应断言，但结论要记住：**替换掉边界之后，边界本身就没被验证。真实的端到端跑一次不能省。**

**清晰度是硬编码的格式编号。** 编号是站点的内部实现，会变。失效时的表现是下载报错，不是静默降级。

**1080P 高码率需要大会员。** 探测结果里能看到这一点。没有会员时拿到的是普通 1080P。

**平台字幕没有标点。** 这是已接受的取舍，不是本包的缺陷。详见「无标点是已接受的取舍」。交付文稿会是整条连续流，段落划分要靠另加策略，不在本包范围内。

**只做 B 站。** 其他站点未测。

**扫码超时默认 180 秒。** 超时后报「没检测到登录」，浏览器窗口会被关掉。重新跑一次即可。

## 依赖

Python 侧只有 `websocket-client`，用于 CDP，写在 `pyproject.toml` 里。

`yt-dlp` 与 `ffmpeg` 是两个外部程序，都不是 Python 包。

`yt-dlp` 优先用 PATH 上的，找不到就回退到 `uv tool run --from yt-dlp yt-dlp`。两者都没有时抛出带安装指引的错误。

`ffmpeg` 需要已经在 PATH 上。Windows 可用 `winget install Gyan.FFmpeg`。

Chrome 用于引导式登录。找不到时 `login()` 报出查过的路径，也可以显式传 `chrome=`。

## 相关

- 规格与全部实现决策见 [issue #10](https://github.com/Fatekiller23/EcoBuddy/issues/10)。
- 决策的调研过程见 [wayfinder 地图 #3](https://github.com/Fatekiller23/EcoBuddy/issues/3)。
- 实测原始数据在 `docs/research/` 下两份 `measured-*` 文档。
