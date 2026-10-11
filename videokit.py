"""VideoKit: probe and download Bilibili videos.

Two entry points, one per input shape:

    probe(url)          -> Video                  what exists at this URL
    download_video(...) -> list[Path]             the video files
    download_audio(...) -> list[Path]             the audio files, for ASR

Why probing is its own call: deciding what to download requires knowing what
is there first. Splitting it lets a caller ask "how many parts, which have
subtitles, what qualities" without touching the network for file data.

Why audio has its own call: transcript-only work needs 40 MB per part instead
of 130 MB or 340 MB. Same part, an order of magnitude apart.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# yt-dlp writes its subtitle table to stdout, not to JSON. This is a known
# Bilibili extractor limitation (yt-dlp issue #17527): `subtitles` comes back
# as an empty object even for parts that have them, while `--list-subs`
# reports them correctly. So probing uses two calls, not one.
_SUB_HEADER = re.compile(r"^\[info\] Available subtitles for (\S+):\s*$")
_SUB_ROW = re.compile(
    r"^\s*(\S+)\s+(json3|json|srt|vtt|xml|ass|srv1|srv2|srv3|ttml|dfxp)\s*$"
)

# The danmaku track is attached unconditionally by the extractor and is a
# bullet-comment XML feed, not a subtitle. A part carrying only danmaku has
# no subtitles at all.
_NOT_A_SUBTITLE = {"danmaku"}


@dataclass(frozen=True)
class Part:
    """One 分P of a Bilibili video."""

    index: int
    id: str
    title: str
    duration_sec: float
    subtitle_langs: tuple[str, ...]

    @property
    def has_subtitles(self) -> bool:
        return bool(self.subtitle_langs)


@dataclass(frozen=True)
class Video:
    """What probe found at a URL."""

    id: str
    title: str
    parts: tuple[Part, ...]
    formats: tuple[dict, ...] = field(default_factory=tuple)


class VideoKitError(RuntimeError):
    """Raised when yt-dlp fails in a way the caller must handle."""


@lru_cache(maxsize=1)
def _ytdlp_command() -> tuple[str, ...]:
    """Locate yt-dlp, preferring one on PATH and falling back to `uv tool run`.

    yt-dlp is not a Python library here, it is an external program, so it may
    be installed globally, available only through uv, or absent entirely. The
    fallback matters because requiring a global install would be an extra
    setup step for a tool that already depends on uv for its environment.
    """
    found = shutil.which("yt-dlp")
    if found:
        return (found,)
    if shutil.which("uv"):
        return ("uv", "tool", "run", "--from", "yt-dlp", "yt-dlp")
    raise VideoKitError(
        "yt-dlp not found. Install it with `uv tool install yt-dlp` "
        "or `pip install yt-dlp`, or install uv so the fallback can run it."
    )


def _run(args: list[str], cookies: Path | None) -> str:
    """Run yt-dlp and return stdout, or raise with its stderr attached.

    Encoding is left to the platform on purpose. yt-dlp writes its output in
    the console's code page (cp936 on this machine), not UTF-8, so forcing
    UTF-8 turns every Chinese title into replacement characters. Those titles
    end up in filenames, so a wrong guess here produces paths that cannot be
    opened.
    """
    cmd = list(_ytdlp_command())
    if cookies is not None:
        cmd += ["--cookies", str(cookies)]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise VideoKitError(f"could not execute {cmd[0]!r}: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-8:])
        raise VideoKitError(f"yt-dlp failed (exit {proc.returncode}):\n{tail}")
    return proc.stdout


def parse_subtitle_table(text: str) -> dict[str, tuple[str, ...]]:
    """Parse `--list-subs` output into {part id: (language, ...)}.

    Part ids appear in two shapes: `BV1Y5QgB5Eas_p3` for a multi-part video
    and `BV1GJ411x7h7` with no suffix for a single one. Both are handled by
    matching whatever identifier the header carries.
    """
    found: dict[str, set[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        header = _SUB_HEADER.match(line)
        if header:
            current = header.group(1)
            found.setdefault(current, set())
            continue
        if current is None:
            continue
        row = _SUB_ROW.match(line)
        if row:
            found[current].add(row.group(1))
            continue
        if line.strip() == "" and found.get(current):
            current = None
        elif line.startswith("["):
            current = None
    return {k: tuple(sorted(v - _NOT_A_SUBTITLE)) for k, v in found.items()}


def probe(url: str, cookies: Path | None = None) -> Video:
    """Report what exists at `url` without downloading file data.

    Two yt-dlp calls: JSON for structure and formats, `--list-subs` for
    subtitles, because the JSON path does not expose subtitles for Bilibili.
    """
    raw = _run(
        ["--dump-single-json", "--skip-download", url],
        cookies,
    )
    data = json.loads(raw)
    subs = parse_subtitle_table(
        _run(["--list-subs", "--skip-download", url], cookies)
    )

    entries = data.get("entries") or [data]
    parts = []
    for entry in entries:
        pid = entry.get("id", "")
        parts.append(
            Part(
                index=entry.get("playlist_index") or 1,
                id=pid,
                title=entry.get("title", ""),
                duration_sec=float(entry.get("duration") or 0.0),
                subtitle_langs=subs.get(pid, ()),
            )
        )

    return Video(
        id=data.get("id", ""),
        title=data.get("title", ""),
        parts=tuple(sorted(parts, key=lambda p: p.index)),
        formats=tuple(entries[0].get("formats") or ()),
    )


def _format_selector(quality: str) -> str:
    """Map a quality name to a yt-dlp format expression.

    Bilibili serves video and audio as separate streams and does not offer a
    muxed one, so both selectors name a video stream plus an audio stream.
    """
    video = {
        "1080p": "30080",   # avc1, ~336 MB/part
        "720p": "30066",    # hevc, ~131 MB/part
        "480p": "30033",    # hevc,  ~52 MB/part
        "360p": "30011",    # hevc,  ~35 MB/part
    }
    if quality not in video:
        raise ValueError(
            f"unknown quality {quality!r}; expected one of {sorted(video)}"
        )
    return f"{video[quality]}+30280"  # 30280 = highest-bitrate audio


def _download(
    url: str,
    parts: list[int] | None,
    selector: str,
    out_dir: Path,
    cookies: Path | None,
    template: str,
    extra: list[str],
) -> list[Path]:
    """Download, returning the paths yt-dlp reports it wrote.

    The paths come from yt-dlp's own `after_move:filepath` output rather than
    from globbing the output directory. Globbing cannot work here: the
    template contains `%(...)s` placeholders, and post-processing (merging,
    audio extraction) can change the extension, so the file on disk is not
    predictable from the template alone.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    if parts:
        args += ["--playlist-items", ",".join(str(p) for p in parts)]
    args += [
        "-f", selector,
        "-o", str(out_dir / template),
        "--print", "after_move:filepath",
        url,
    ]
    stdout = _run(args + extra, cookies)
    return [Path(line) for line in stdout.splitlines() if line.strip()]


def download_video(
    url: str,
    out_dir: Path,
    parts: list[int] | None = None,
    quality: str = "720p",
    cookies: Path | None = None,
) -> list[Path]:
    """Download video files, merging Bilibili's separate video and audio streams.

    `parts` selects 分P by index; None means every part.
    """
    return _download(
        url=url,
        parts=parts,
        selector=_format_selector(quality),
        out_dir=out_dir,
        cookies=cookies,
        template="%(playlist_index)02d-%(title).80s.%(ext)s",
        extra=["--merge-output-format", "mp4"],
    )


def download_audio(
    url: str,
    out_dir: Path,
    parts: list[int] | None = None,
    cookies: Path | None = None,
) -> list[Path]:
    """Download audio only, as 16 kHz mono WAV ready for ASR.

    Resampling happens here rather than in the transcriber so the file on disk
    is the file the model wants.
    """
    return _download(
        url=url,
        parts=parts,
        selector="30280",
        out_dir=out_dir,
        cookies=cookies,
        template="%(playlist_index)02d.%(ext)s",
        extra=[
            "-x",
            "--audio-format", "wav",
            "--postprocessor-args", "-ar 16000 -ac 1",
        ],
    )
