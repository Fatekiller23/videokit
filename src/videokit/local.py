"""本地视频文件 → SRT 字幕。

B 站那条链路见 `docs/bilibili-pipeline.md`：输入是链接，判断依据是平台字幕表，
有字幕的讲次零成本取回。本地文件没有平台字幕可查，所以只有一条路——
抽音频、跑语音识别、按时间戳写 SRT。

引擎用 faster-whisper，不用 B 站链接那条链路的 FunASR paraformer。原因是语种：
paraformer-zh 加 ct-punc 是给中文调的，Whisper 的英语是它最强的一档。两条链路各留
各的引擎，不做统一，因为它们的输入语言分布不同。这里不预设语言，交给引擎自己判定。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Iterable, Sequence

from . import Segment, VideoKitError

# 能被 ffmpeg 抽出音轨的容器。不在这里的扩展名会被明确拒绝，而不是交给 ffmpeg
# 报一句底层错误。
SOURCE_EXTENSIONS = (
    ".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts", ".m4v", ".wmv",
    ".mp3", ".wav", ".m4a", ".flac", ".aac", ".ogg", ".opus",
)

DEFAULT_MODEL = "large-v3"

# 一行多宽、一条字幕最多几行。42 是英文影视字幕的通行宽度，两行是通行上限；
# 中文在这个宽度下同样合适，因为一条 42 字的字幕已经接近阅读上限。
DEFAULT_MAX_CHARS = 42
DEFAULT_MAX_LINES = 2

# 一条字幕再短也要看得见。语音识别给出的段落在停顿处可能只有零点几秒。
_MIN_CUE_SEC = 0.4

# 一条字幕挂得最久多久。Whisper 的分段按语义走，一句讲得很慢的台词能到十几秒，
# 读者来不及在它消失前读完。按 7 秒切，是英语阅读速度下两行 84 字符的舒适上限。
_MAX_CUE_SEC = 7.0


class LocalError(VideoKitError):
    """本地文件这条路走不通，且原因不是调用方的参数写错。"""


class WhisperUnavailable(LocalError):
    """faster-whisper 没装。提示里给出确切的安装命令。"""


def _preload_cuda_libs() -> None:
    """把 pip 装的 CUDA 运行库加进 DLL 搜索路径。

    ctranslate2 在 Windows 上直接 LoadLibrary 找 cublas 与 cudnn。pip 装的
    nvidia-*-cu12 把 DLL 放在 site-packages/nvidia/<库>/bin 下，不在 PATH 上，
    所以不预先登记就会报 "Could not load library cublas64_12.dll"。
    """
    if sys.platform != "win32":
        return
    import site

    roots: list[str] = []
    try:
        roots.extend(site.getsitepackages())
    except AttributeError:  # 某些嵌入环境没有这个函数
        pass
    user_site = site.getusersitepackages()
    if user_site:
        roots.append(user_site)
    for root in roots:
        nvidia = Path(root) / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            if not bin_dir.is_dir():
                continue
            try:
                os.add_dll_directory(str(bin_dir))
            except (AttributeError, OSError):
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


def _self_test(engine) -> None:
    """在构造成功后真跑一次一秒钟的静音，把设备问题在这里暴露出来。

    只构造不跑是不够的，本机实测过：ctranslate2 报 cuda 设备数为 1，
    WhisperModel(device="cuda") 也构造成功，但第一次 encode 才抛
    "Library cublas64_12.dll is not found"。没有这次自检的话，退 CPU 的兜底
    永远不会触发，失败会拖到第一个真实文件上，几十秒之后才出现。
    """
    import numpy as np

    silence = np.zeros(16000, dtype=np.float32)
    segments, _ = engine.transcribe(
        silence, language="en", vad_filter=False, beam_size=1
    )
    list(segments)  # 生成器不消费就不跑 encoder


def load_engine(
    model: str = DEFAULT_MODEL,
    device: str = "auto",
    compute_type: str | None = None,
):
    """加载 Whisper 模型，返回 (engine, 实际设备)。

    传 device="auto" 时先用 CUDA，起不来再退 CPU。退的时候在 stderr 说一句，
    因为 CPU 上跑 large-v3 慢一个数量级，调用方要知道自己付的是哪种代价。
    """
    try:
        import faster_whisper  # noqa: F401
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise WhisperUnavailable(
            "faster-whisper 没装。装它：uv pip install faster-whisper\n"
            "本机首次跑还要 CUDA 运行库：uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
        ) from exc

    _preload_cuda_libs()
    from faster_whisper import WhisperModel

    if device == "auto":
        attempts = [("cuda", "float16"), ("cpu", "int8")]
    elif device == "cuda":
        attempts = [("cuda", compute_type or "float16")]
    else:
        attempts = [("cpu", compute_type or "int8")]

    last: Exception | None = None
    for index, (dev, kind) in enumerate(attempts):
        try:
            engine = WhisperModel(model, device=dev, compute_type=compute_type or kind)
            _self_test(engine)
            return engine, dev
        except Exception as exc:  # ctranslate2 抛的是 RuntimeError 子类，无法枚举全
            last = exc
            if index + 1 < len(attempts):
                print(
                    f"{dev} 起不来（{exc}），退到下一个设备。",
                    file=sys.stderr,
                )
    raise LocalError(f"Whisper 模型 {model} 加载失败：{last}")


def audio_wav(source: Path, wav: Path) -> Path:
    """抽出 16 kHz 单声道 WAV，也就是 Whisper 要的格式。"""
    wav.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(wav),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError as exc:
        raise LocalError("ffmpeg 不在 PATH 上，抽不了音频。") from exc
    except subprocess.TimeoutExpired as exc:
        raise LocalError(f"{source.name} 抽音频超过 1 小时，放弃。") from exc
    if result.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
        detail = (result.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"ffmpeg 退出码 {result.returncode}"
        raise LocalError(f"{source.name} 抽不出音轨：{reason}")
    return wav


def duration_sec(source: Path) -> float:
    """容器时长，秒。算不出来时返回 0，调用方按 0 处理。"""
    command = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 0.0
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def wav_samples(wav: Path) -> "object":
    """把 16 kHz 单声道 16 bit WAV 读成 float32 数组，值域 [-1, 1]。

    走这条路而不是把路径交给 faster-whisper，是为了绕开它的 PyAV 解码。
    faster-whisper 1.2.1 在 PyAV 19 上必然崩：`av.open(..., metadata_errors=...)`
    的参数在 PyAV 19 被删掉了，而上游的修复还没发版。传数组则完全不进 av.open。
    实测记录见 docs/known-issues.md。
    """
    import wave

    import numpy as np

    with wave.open(str(wav), "rb") as handle:
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if channels != 1 or width != 2:
        raise LocalError(f"{wav.name} 不是 16 bit 单声道 WAV，与抽音频那步的约定不符。")
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def transcribe_audio(
    wav: Path,
    engine=None,
    device: str = "cpu",
    language: str | None = None,
    quiet: bool = False,
) -> list[Segment]:
    """把一个 16 kHz 单声道 WAV 转成带时间戳的分段。

    开 vad_filter，用内置的 Silero VAD 掐掉静音。长视频里静音段很多，不掐会让
    Whisper 在静音上产生幻觉文本，这是它已知的行为。
    """
    if engine is None:
        engine, device = load_engine(device=device)
    segments, info = engine.transcribe(
        wav_samples(wav),
        language=language,
        vad_filter=True,
        beam_size=5,
        word_timestamps=False,
    )
    if not quiet:
        detected = getattr(info, "language", None)
        probability = getattr(info, "language_probability", None)
        if detected:
            note = f"判定语种 {detected}"
            if isinstance(probability, float):
                note += f"（置信度 {probability:.2f}）"
            print(note, file=sys.stderr)
    out: list[Segment] = []
    for item in segments:
        text = (item.text or "").strip()
        if not text:
            continue
        out.append(Segment(start_sec=float(item.start), end_sec=float(item.end), text=text))
    return out


def _timestamp(seconds: float) -> str:
    """SRT 的时间戳：HH:MM:SS,mmm。负数按 0 处理，不进时间轴。"""
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _split_into(text: str, count: int) -> list[str]:
    """把一段话按词均分成 count 份，靠近词边界，尽量等长。"""
    words = text.split()
    if count <= 1 or len(words) <= 1:
        return [text]
    total = sum(len(word) for word in words) + len(words) - 1
    target = total / count
    chunks: list[str] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        if len(chunks) < count - 1 and len(" ".join(current)) >= target:
            chunks.append(" ".join(current))
            current = []
    if current:
        chunks.append(" ".join(current))
    return chunks


def cues(
    segments: Iterable[Segment],
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> list[tuple[float, float, list[str]]]:
    """把识别分段整理成 SRT 的条目：时间对加折好行的文本。

    一条识别段落长于两行时，按词切成几条，时间按字符数按比例分。不切的话，
    屏幕上会出现一条挂十几秒、读者来不及在它消失前读完的字幕。
    """
    limit = max_chars * max_lines
    built: list[tuple[float, float, list[str]]] = []
    previous_end = 0.0
    for segment in segments:
        text = " ".join(segment.text.split())
        if not text:
            continue
        start = max(segment.start_sec, previous_end)
        end = max(segment.end_sec, start + _MIN_CUE_SEC)
        by_chars = -(-len(text) // limit)  # 向上取整
        by_time = -(-int((end - start) * 1000) // int(_MAX_CUE_SEC * 1000))
        chunks = _split_into(text, max(1, by_chars, by_time))
        total = sum(len(chunk) for chunk in chunks) or 1
        cursor = start
        for index, chunk in enumerate(chunks):
            stop = end if index + 1 == len(chunks) else cursor + (end - start) * len(chunk) / total
            stop = max(stop, cursor + _MIN_CUE_SEC)
            lines = textwrap.wrap(
                chunk,
                width=max_chars,
                break_long_words=True,
                break_on_hyphens=False,
            )
            built.append((cursor, stop, lines or [chunk]))
            cursor = stop
        if built:
            previous_end = built[-1][1]
    return built


def srt_text(
    segments: Iterable[Segment],
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """SRT 全文。空输入返回空字符串，别人拿到它就知道没内容。"""
    blocks: list[str] = []
    for number, (start, end, lines) in enumerate(cues(segments, max_chars, max_lines), 1):
        blocks.append(f"{number}\n{_timestamp(start)} --> {_timestamp(end)}\n" + "\n".join(lines))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def subtitle_path(source: Path, out_dir: Path | None = None) -> Path:
    """字幕落点。默认落在视频旁边，同名 .srt。"""
    return (Path(out_dir) if out_dir else Path(source).parent) / f"{Path(source).stem}.srt"


def write_srt_text(path: Path, text: str) -> Path:
    """写 SRT 文件。含非 ASCII 字符时带 UTF-8 BOM。

    实测：VLC 在中文 Windows 上按系统代码页（cp936）读没有 BOM 的 SRT，UTF-8 的中文
    全部变成乱码（「我是」显示成「鎴戞槸」）。带 BOM 的同一份文件按 UTF-8 读，显示正常。
    纯 ASCII 不加 BOM，免得老工具把 BOM 当正文。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_bom = any(ord(char) > 127 for char in text)
    path.write_text(text, encoding="utf-8-sig" if needs_bom else "utf-8")
    return path


def write_subtitles(
    source: Path,
    segments: Sequence[Segment],
    out_dir: Path | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> Path:
    return write_srt_text(
        subtitle_path(Path(source), out_dir),
        srt_text(segments, max_chars, max_lines),
    )


def sources_of(reference: str | Path) -> list[Path]:
    """把参数展开成待处理文件：目录取其中所有视频，文件原样返回。"""
    path = Path(reference)
    if path.is_dir():
        return sorted(
            child for child in path.iterdir()
            if child.is_file() and child.suffix.lower() in SOURCE_EXTENSIONS
        )
    return [path]


def subtitles(
    source: Path,
    out_dir: Path | None = None,
    engine=None,
    model: str = DEFAULT_MODEL,
    device: str = "auto",
    language: str | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_lines: int = DEFAULT_MAX_LINES,
) -> Path:
    """一条命令跑完：抽音频、转写、写 SRT，返回 SRT 路径。"""
    source = Path(source)
    if not source.exists():
        raise LocalError(f"文件不存在：{source}")
    if source.suffix.lower() not in SOURCE_EXTENSIONS:
        raise LocalError(f"不认这种扩展名：{source.suffix}。支持 {' '.join(SOURCE_EXTENSIONS)}")
    if engine is None:
        engine, device = load_engine(model, device)
    with tempfile.TemporaryDirectory(prefix="videokit-") as tmp:
        wav = audio_wav(source, Path(tmp) / "audio.wav")
        segments = transcribe_audio(wav, engine=engine, device=device, language=language)
    if not segments:
        raise LocalError(f"{source.name} 没有识别出任何语音，未写字幕。")
    return write_subtitles(source, segments, out_dir, max_chars, max_lines)

def _positionals(rest: Sequence[str], options: Sequence[str]) -> list[str]:
    """取命令行里的位置参数，跳过带值的选项和它们的值。"""
    found: list[str] = []
    skip = False
    for arg in rest:
        if skip:
            skip = False
            continue
        if arg in options:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        found.append(arg)
    return found


def _cue_count(path: Path) -> int:
    """数产物里的条目，不是数内存里的对象。报告的证据取自落盘的文件。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    return sum(1 for block in text.split("\n\n") if " --> " in block)


def srt_command(rest: Sequence[str]) -> int:
    """`videokit srt <文件或目录> [--out 目录] [--model ...] [--device ...]`。"""
    from . import _option, _usage_error

    out = _option(rest, "--out")
    model = _option(rest, "--model") or DEFAULT_MODEL
    device = _option(rest, "--device") or "auto"
    language = _option(rest, "--language")
    force = "--force" in rest

    if device not in ("auto", "cuda", "cpu"):
        return _usage_error(f"--device 只能是 auto / cuda / cpu，收到 {device}")
    references = _positionals(rest, ("--out", "--model", "--device", "--language"))
    if not references:
        return _usage_error("srt 需要至少一个本地文件或目录")

    targets: list[Path] = []
    for reference in references:
        found = sources_of(reference)
        if not found:
            print(f"{reference} 里没有可处理的视频文件。", file=sys.stderr)
        targets.extend(found)
    if not targets:
        return _usage_error("没有可处理的文件")

    out_dir = Path(out) if out else None
    print(f"{len(targets)} 个文件，模型 {model}，设备 {device}", file=sys.stderr)

    started = time.perf_counter()
    try:
        engine, actual = load_engine(model, device)
    except LocalError as exc:
        print(f"引擎起不来：{exc}", file=sys.stderr)
        return 1
    print(f"模型已加载，实际设备 {actual}，用时 {time.perf_counter() - started:.1f} 秒", file=sys.stderr)

    failures: list[str] = []
    done = 0
    for index, source in enumerate(targets, 1):
        label = f"[{index}/{len(targets)}] {source.name}"
        target = subtitle_path(source, out_dir)
        if target.exists() and not force and target.stat().st_mtime >= source.stat().st_mtime:
            done += 1
            print(f"{label} 已有字幕，跳过（--force 可重做）", file=sys.stderr)
            continue
        begin = time.perf_counter()
        try:
            with tempfile.TemporaryDirectory(prefix="videokit-") as tmp:
                wav = audio_wav(source, Path(tmp) / "audio.wav")
                segments = transcribe_audio(wav, engine=engine, device=actual, language=language)
            if not segments:
                raise LocalError("没有识别出任何语音")
            path = write_subtitles(source, segments, out_dir)
        except LocalError as exc:
            failures.append(source.name)
            print(f"{label} 失败：{exc}", file=sys.stderr)
            continue
        elapsed = time.perf_counter() - begin
        duration = duration_sec(source)
        speed = f"{duration / elapsed:.1f}x 实时" if duration > 0 and elapsed > 0 else ""
        chars = sum(len(segment.text) for segment in segments)
        print(
            f"{label} 完成，{_cue_count(path)} 条字幕，{chars} 字符，"
            f"{elapsed:.0f} 秒 {speed} → {path}",
            file=sys.stderr,
        )
        done += 1

    print(f"完成 {done} 个，失败 {len(failures)} 个", file=sys.stderr)
    if failures:
        print("失败：" + "、".join(failures), file=sys.stderr)
        return 1
    return 0
