# VideoKit

B 站视频的探测、下载与转文稿。给一个链接，先告诉你有多少讲、哪些有字幕、有哪些清晰度可选，再按你的选择把视频或音频下下来；没有字幕的讲次用本地语音识别转成带时间戳的文稿。

## 它解决什么

一门课几十讲、几十小时，逐集点开看有哪些字幕不现实。而这个信息决定后面要花多少算力：有字幕的讲次直接取，零成本；没有的才需要跑语音识别。

同一门课里两种状态会混着出现。实测 `BV1Y5QgB5Eas`（徐高 2025 年宏观课，30 讲），21 讲有字幕、9 讲没有，且与讲次时长无相关性。所以必须逐讲判定，不能按整条视频二选一。

## 五个入口

```
probe(url, cookies=None) -> Video
    探测。不下载任何文件数据。

part_subtitles(url, part, language="ai-zh", cookies=None) -> list[Segment]
    取回并解析某一讲的平台字幕轨。无字幕轨时抛 SubtitleUnavailable。

transcribe(wav, engine=None, device="cpu") -> list[Segment]
    把一讲的 16 kHz 单声道 WAV 转成带时间戳的分段。缺 ASR 依赖时抛
    TranscriptionUnavailable。

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

## 语音识别

```
transcribe(wav, engine=None, device="cpu") -> list[Segment]
```

输入是一讲的 16 kHz 单声道 WAV，就是 `download_audio` 落下来的那个文件。输出是与平台字幕路径同一个 `Segment` 结构，带 `start_sec`、`end_sec` 和正文。

引擎是 `paraformer-zh` + `fsmn-vad` + `ct-punc`，全部是必需项，见 `engine_kwargs`。

**`ct-punc` 不是可选优化。** 实测关掉它后输出变成逐字之间带空格，3023 个字符里 1511 个空格，零标点。它同时负责把逐字标记拼回连续文本。没有标点就没有可分段的位置，这条链路上游就没有产物。

**模型一次加载、反复使用。** 加载 29 到 50 秒，是每讲都要付的成本，所以引擎从 `engine=` 传进来。跑 30 讲时加载一次，不要每讲重来。

**CPU 够用。** 实测 47.5 分钟音频约 209 秒，13.7 倍实时。不必为 GPU 做额外适配。

### 时间戳配对规则

`result["timestamp"]` 是毫秒对的列表，但**它的长度不等于文本长度**。实测一讲 52.7 分钟的完整运行，`len(text)` 是 17123，`len(timestamp)` 是 15908。

规则是**一个汉字，或一整段连续的 ASCII 字母数字，消耗一对毫秒。标点与空白不消耗。**

实测算术。15883 个汉字加 25 段 ASCII 连续串等于 15908，与 `len(timestamp)` 精确相等。反证也干净，按「每字符一对」假设会多出 61 对，正好等于 25 段 ASCII 串内部多算的字母数（`GDP` 23 次、`WTO` 9 次等）。

所以配对不能按字符下标做。`recognised_units` 用一条正则切出单元，`asr_segments` 按单元下标取时间。

**两个数不相等时抛 `AsrError`，不猜。** 错配之后任何一种配对都能产出看着像样的文稿，这正是要拒绝的失败模式。

### 分段边界是标点

`text` 里每出现一个标点就断一段。实现是扫文本找标点，**不读 `sentence_info`**。

理由是 `sentence_info` 的文本在长音频上是错的。FunASR 1.4.16 里 `ct-punc` 的分词把 15088 个空格词展开成 15908 个标点 id，而句子拼接函数用 `text_postprocessed.split()` 去配这 15908 个 id，两边对不上。实测完整运行上从第 318 个字符起文本归属错位，拼接后 17165 字符对不上真实的 17123。**它的 `start` 与 `end` 毫秒是对的，文本不是。** 自算规则与它的 1152 段毫秒边界逐段相等，0 处不同，所以自己断没有损失。

**分界标点必须包含 `.` 与 `,`。** 它们是 `ct-punc` 给 ASCII 词加的西文标点。实测只用 `，。？！、` 得到 1148 段，加上这两个得到 1152 段，与 `sentence_info` 精确相等。

### 设备校验

**必须读实际设备，不能信传进去的参数。** 实测把 `device="cuda:0"` 传给一台 `torch.cuda.is_available()` 为 `False` 的机器，FunASR 静默降级到 CPU，不报错不警告，转写结果完全正常。

读法是 `next(model.model.parameters()).device`。三个看起来能用的属性都不可用。

| 属性 | 实际情况 |
|---|---|
| `model.device` | 抛 `AttributeError`，不存在 |
| `model.model.device` | 返回 `None`，**看着像「没有设备信息」，是假象** |
| `next(model.model.parameters()).device` | 正确，用这个 |

`verify_device(model, requested)` 在设备不符时抛 `AsrError`，`transcribe` 在读音频之前就调用它，所以降级会在启动时报出来，而不是几分钟工作量之后。

### 口语填充词保留，是有意的

**「啊」「呢」「就是」「这个」全部保留，不做清理。这不是识别缺陷，是本包的有意取舍，不要当 bug 修。**

实测一讲 52.7 分钟的完整运行里，「啊」出现 277 次、「这个」424 次、「就是」60 次、「呃」89 次，全部留在文本里。

理由是文稿的用途是让人读这门课讲了什么，而填充词是讲课风格的一部分。清掉它们会让不同讲次的文风看起来比实际更整齐，也会让引用原文时对不上音频。同一门课的平台字幕路径同样保留填充词，两条路径在这一点上一致。

清理若要加，应该是显式的后处理步骤、带自己的开关，而不是藏在转写里的默认行为。

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
  text            该段正文，不含时间戳

RecognisedUnit
  start           单元在正文里的起始字符下标
  end             结束下标
  text            该单元文字，一个汉字或一段 ASCII 字母数字
```

`Part.has_subtitles` 是判断有没有字幕的入口，不要直接看 `subtitle_langs` 是否非空以外的条件。

`Segment.text` 在两条路径上标点情况不同。平台字幕不含标点是上游数据本来的样子，见「平台字幕」一节；ASR 分段含标点，且边界就在标点上。

## 代码结构

`videokit.py` 是全部实现。分三层。

**`_run` 是唯一的网络边界。** 所有 `yt-dlp` 调用都从这里出去。**模型是第二个边界，在 `transcribe` 的 `engine=` 参数上。** 其余全是纯函数。

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

**`asr_segments` 是纯函数，输入是文本加时间戳列表。** 它不是从模型结果里直接取分段，而是按「一单元一对毫秒」重建。这样做是为了让配对与分段逻辑可以离线测，实测用保存的原始产物秒级返回，不需要网络、不需要 GPU、不需要模型。模型只出现在 `transcribe` 的一个参数上，周围的逻辑因此也能测。

`recognised_units` 单独暴露出来，因为下标偏移是配对契约的一部分，不是内部细节。

## 下载的两处硬约束

**路径来自 `yt-dlp` 的报告，不是猜文件名。** 用的是 `--print after_move:filepath`。输出模板里有 `%(...)s` 占位符，且后处理（合并、抽音频）会改扩展名，所以磁盘上的名字无法从模板推出来。

**不强制子进程输出编码。** `yt-dlp` 按控制台代码页输出（本机是 cp936），强制 UTF-8 会把中文标题变成替换字符。这些标题会进文件名，解错了返回的路径就打不开。

## 测试

```
uv run --python 3.12 python test/test_videokit.py
```

74 项断言，约 3.6 秒，不联网、不需要登录态。

语音识别那一层另有 31 项断言，合计 105 项、约 4 秒，同样不需要 funasr、不需要 GPU、不联网。

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
- `asr-23.txt`、`asr-23.timestamp.json`：**一次真实 ASR 运行的原始产物**，3160.2 秒音频，17123 字符文本配 15908 对毫秒，逐字节 sha256 与实测记录一致。两者的长度不等正是配对规则要解释的事实，所以这份夹具不能被手工构造的样本替代。

`LoginError` 的四种失败路径都有断言，且都用 `timeout_sec=0` 走完，所以测试不会真的等一个扫码超时。

命令行入口也有断言，覆盖成功、未知子命令被拒、失败时非零退出并指出环节。

**真实端到端另有一个手工脚本，不进测试套件。**

```
uv run --python 3.12 python test/_live_subtitles.py
```

它会联网、用真实凭据，取回 4 讲的字幕并与实测分段数逐个对照，再验一次「无轨抛异常」与「空输入返回空列表」的区分。跑之前不要设 `PYTHONIOENCODING`。

**真实语音识别另有手工脚本，不进测试套件。**

```
uv run --python 3.12 --extra asr python test/_live_asr.py
```

它跑真实的 47.5 分钟音频，约 4 分钟。默认读 `D:\projects\ecobuddy-notes\audio\06.wav`，即 `BV1Y5QgB5Eas_p6`。会打印加载与转写耗时、校验后的设备、分段数、标点覆盖率与填充词计数。跑之前不要设 `PYTHONIOENCODING`。

**测试不依赖 funasr。** 这一条是硬要求，所以分两层处理。

- 配对与分段是纯函数，夹具是真实运行的原始产物，不需要模型。
- 设备校验与 `transcribe` 用一个假的引擎对象，它有 `model.model.parameters()` 与 `generate()` 两个被读到的属性。真实引擎由手工脚本覆盖。
- 「缺依赖时说什么」这条测试用 `sys.modules["funasr"] = None` 模拟导入失败，所以在一台真没装 funasr 的机器上同样通过。

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

**两条路径的文稿风格不同。** 平台字幕无标点，ASR 有标点，同一门课的交付文稿里会混着两种风格。把平台字幕也过一遍标点恢复是可行的补救路径，代价是会重算原有时间戳，本期不做。

**不做简繁转换。** 若将来需要，正确的包名是 `opencc`。另两个同名包会把「头发」错转成「頭發」，这个命名陷阱要记住。

**ASR 分段粒度是句，不是词。** 对照过 `sentence_info` 的毫秒边界，逐段相等。词级时间戳虽然从 `timestamp` 能拿到，本包不产出。

**只做 B 站。** 其他站点未测。

**扫码超时默认 180 秒。** 超时后报「没检测到登录」，浏览器窗口会被关掉。重新跑一次即可。

## 依赖

Python 侧基础依赖只有 `websocket-client`，用于 CDP，写在 `pyproject.toml` 里。

**语音识别是可选 extra `asr`，约 2 GB，不进基础安装。** 探测、下载、字幕取回都不需要它。

```
uv sync --extra asr
```

三个包全部必需。

| 包 | 作用 |
|---|---|
| `funasr` | 引擎，提供 `AutoModel` 与三个模型 |
| `torch` | `funasr` 的张量后端 |
| `kaldi-native-fbank` | 提特征的后端 |

**第三个最容易漏。** 只装前两个时安装干净、模型也能加载，一直到送音频进去才抛错。实测 FunASR 在那种情况下的原文是

```
ImportError: torchaudio is not installed and neither is the kaldi-native-fbank fallback backend.
FunASR needs one fbank backend for feature extraction.
```

两个后端任选其一都行。本包选 `kaldi-native-fbank`，因为它不绑 torch 版本，装 `torchaudio` 则必须与所用 torch 版本匹配。

模型首次使用时从 ModelScope 下载，本机缓存在 `%USERPROFILE%\.cache\modelscope\models\iic--*`。之后靠 `disable_update=True` 跳过联网检查。

`yt-dlp` 与 `ffmpeg` 是两个外部程序，都不是 Python 包。

`yt-dlp` 优先用 PATH 上的，找不到就回退到 `uv tool run --from yt-dlp yt-dlp`。两者都没有时抛出带安装指引的错误。

`ffmpeg` 需要已经在 PATH 上。Windows 可用 `winget install Gyan.FFmpeg`。

Chrome 用于引导式登录。找不到时 `login()` 报出查过的路径，也可以显式传 `chrome=`。

## 相关

- 规格与全部实现决策见 [issue #10](https://github.com/Fatekiller23/EcoBuddy/issues/10)。
- 决策的调研过程见 [wayfinder 地图 #3](https://github.com/Fatekiller23/EcoBuddy/issues/3)。
- 实测原始数据在 `docs/research/` 下两份 `measured-*` 文档。
