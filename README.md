# VideoKit

B 站视频的探测与下载。给一个链接，先告诉你有多少讲、哪些有字幕、有哪些清晰度可选，再按你的选择把视频或音频下下来。

转写（把无字幕的讲次跑成本地文稿）是同一子项目的另一块，不在本包内。

## 它解决什么

一门课几十讲、几十小时，逐集点开看有哪些字幕不现实。而这个信息决定后面要花多少算力：有字幕的讲次直接取，零成本；没有的才需要跑语音识别。

同一门课里两种状态会混着出现。实测 `BV1Y5QgB5Eas`（徐高 2025 年宏观课，30 讲），21 讲有字幕、9 讲没有，且与讲次时长无相关性。所以必须逐讲判定，不能按整条视频二选一。

## 三个入口

```
probe(url, cookies=None) -> Video
    探测。不下载任何文件数据。

download_video(url, out_dir, parts=None, quality="720p", cookies=None) -> list[Path]
    下视频。自动合并分开的音视频流。

download_audio(url, out_dir, parts=None, cookies=None) -> list[Path]
    只下音轨，已是语音识别需要的 16 kHz 单声道 WAV。
```

`parts` 是按序号选讲次，省略表示全部。

`cookies` 一般不用传。登录过一次之后三个入口自己找到凭据，见下面「登录态」。

### 为什么探测要单独一个入口

「先看看有什么再决定下什么」是真实用法。并进下载里，调用方就无法只问不取。

### 为什么音频要单独一个入口

同一讲，视频 130 MB 到 340 MB，音频 40 MB 左右，差一个数量级。只做文稿时不需要视频。

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
```

`Part.has_subtitles` 是判断有没有字幕的入口，不要直接看 `subtitle_langs` 是否非空以外的条件。

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

**`_format_selector` 把清晰度名字映射到格式编号。** B 站不提供合流的流，选择器要同时指名一路视频和一路音频。

## 下载的两处硬约束

**路径来自 `yt-dlp` 的报告，不是猜文件名。** 用的是 `--print after_move:filepath`。输出模板里有 `%(...)s` 占位符，且后处理（合并、抽音频）会改扩展名，所以磁盘上的名字无法从模板推出来。

**不强制子进程输出编码。** `yt-dlp` 按控制台代码页输出（本机是 cp936），强制 UTF-8 会把中文标题变成替换字符。这些标题会进文件名，解错了返回的路径就打不开。

## 测试

```
uv run --python 3.12 python test/test_videokit.py
```

51 项断言，约 3.5 秒，不联网、不需要登录态。

跑真实模块。已有的解析与下载测试**只替换 `_run`**，用录制好的真实 `yt-dlp` 输出做夹具。

凭据与默认发现这一层换了个接缝。**测试替换 `subprocess.run` 而不是 `_run`**，因为要断言的正是「命令行里到底有没有 `--cookies`」。替换 `_run` 就看不到这一点。

CDP 那一层替换的是 WebSocket 对象。`cdp_cookies(ws)` 收一个已连接的 socket，所以「先 `Network.enable` 再 `Network.getAllCookies`」这条顺序可以在没有浏览器的前提下定死。

夹具在 `test/fixtures/`，全部是真实录制的字节。

- `dump-multipart.json`、`list-subs-multipart.txt`、`list-subs-single.txt`：`yt-dlp` 的真实输出。
- `cdp-cookies.json`：真实 Chrome 的 `Network.getAllCookies` 回包，36 条，Cookie 值已抹掉，其余字段是浏览器原样报的。
- `cdp-targets.json`：真实 `/json/list` 回包，含非 page target 与另一个站点的 page，所以「选了第一个」和「选对了」能区分开。
- `netscape-reference.txt`：**这份期望值由 `yt-dlp` 自己的 Cookie 导出器写出**，是对同一次会话的独立结果。序列化测试断言逐字节相同，这样期望值不是把实现重算一遍。

`LoginError` 的四种失败路径都有断言，且都用 `timeout_sec=0` 走完，所以测试不会真的等一个扫码超时。

命令行入口也有断言，覆盖成功、未知子命令被拒、失败时非零退出并指出环节。

## 已知限制

**环境变量 `PYTHONIOENCODING` 会让 `probe` 崩。** 实测，与本 ticket 无关，主检出同样复现。

- 不设时，`yt-dlp` 按 cp936 写 stdout，父进程也按 cp936 解，正常。
- 设为 `utf-8` 时，`yt-dlp` 改成写 UTF-8，父进程仍按 cp936 解，于是 `_run` 抛 `UnicodeDecodeError`，`probe` 下一步拿到 `None` 再崩在 `parse_subtitle_table`。

根因是 `_run` 让平台决定编码，而 `PYTHONIOENCODING` 只影响子进程、不影响父进程的解码，两边就此不一致。修法不在本 ticket 范围内，记在这里以免下次当成新 bug 查。跑本包时不要设这个变量。

**三个曾经漏掉的 bug 说明测试的边界在哪。** 早期版本 20 项测试全过但真实调用立刻崩，因为三个问题全在 `_run` 及其返回值上，而测试把它替换掉了：`yt-dlp` 不在 PATH 上、下载返回空列表、中文路径烂掉。

现在这三条都有对应断言，但结论要记住：**替换掉边界之后，边界本身就没被验证。真实的端到端跑一次不能省。**

**清晰度是硬编码的格式编号。** 编号是站点的内部实现，会变。失效时的表现是下载报错，不是静默降级。

**1080P 高码率需要大会员。** 探测结果里能看到这一点。没有会员时拿到的是普通 1080P。

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
