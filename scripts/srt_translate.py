"""把 SRT 拆成可分段翻译的小块，再把译文拼回 SRT。

为什么拆：整篇字幕有几百条、上万字符，一个模型在一次生成里注意力会散，后段的
质量明显掉。拆成几十条一块，每块独立翻，前后块互不影响。

为什么拼要写代码：拼接的规则是确定的（编号对齐、时间轴来自原文、行宽重排），
让模型去拼就多一次出错的机会，而且错得不明显。代码拼完还会对编号做完整性检查。

用法：

    python scripts/srt_translate.py split <原文.srt> --out <目录> [--size 50]
    python scripts/srt_translate.py merge <目录> --srt <原文.srt> --out <译文.srt> [--bilingual]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
import unicodedata
from dataclasses import dataclass
from pathlib import Path

TIMESTAMP = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2}),(\d{3})")
LINE = re.compile(r"^(\d{4})\|(.*)$")

# 中文一行多少字。20 是通行的上界，超过就需要视线来回扫。
DEFAULT_WIDTH = 20


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


def _seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def _stamp(seconds: float) -> str:
    millis = max(0, int(round(seconds * 1000)))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def read_srt(path: Path) -> list[Cue]:
    """读 SRT，返回按时间排好的条目。空块与坏块跳过，不猜。"""
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace")
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        match = TIMESTAMP.search(block)
        if not match:
            continue
        body = " ".join(line.strip() for line in lines if not TIMESTAMP.search(line) and not line.strip().isdigit())
        cues.append(Cue(_seconds(*match.groups()[:4]), _seconds(*match.groups()[4:]), body.strip()))
    return cues


def split(srt_path: Path, out_dir: Path, size: int = 50) -> list[Path]:
    """把 SRT 切成若干块，每块一个文件，一行一条，前缀是四位编号。"""
    cues = read_srt(srt_path)
    if not cues:
        raise SystemExit(f"{srt_path} 里没有解析出任何条目")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.txt"):
        if re.match(r"^\d{4}\.txt$", stale.name):
            stale.unlink()
    written: list[Path] = []
    for offset in range(0, len(cues), size):
        number = offset // size + 1
        path = out_dir / f"{number:02d}.txt"
        rows = [
            f"{index:04d}|{cue.text}"
            for index, cue in enumerate(cues[offset : offset + size], start=offset + 1)
        ]
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        written.append(path)
    manifest = {
        "source": str(Path(srt_path).resolve()),
        "cues": len(cues),
        "size": size,
        "chunks": len(written),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return written


def _wrap_cjk(text: str, width: int = DEFAULT_WIDTH) -> list[str]:
    """中文按字数折行，优先在标点后断，其次硬断。"""
    text = " ".join(text.split())
    if len(text) <= width:
        return [text]
    lines: list[str] = []
    rest = text
    marks = "，。！？；：、,.!?;: "
    while len(rest) > width:
        window = rest[:width]
        cut = max(window.rfind(mark) for mark in marks) + 1
        if cut <= width // 2:  # 标点太靠前，按字数硬断
            cut = width
        lines.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        lines.append(rest)
    return lines


def _fit_lines(text: str, width: int, max_lines: int, latin: bool) -> list[str]:
    """把文本折成最多 max_lines 行，放不下就加宽，不丢字。

    先按给定宽度折。折出来超过行数上限时加宽到 `ceil(字数/行数)` 再折一次。
    两条路径都试过还超（极长的无空格串），宁可多出一行也不截断：字幕丢半句比
    多一行难发现得多。
    """
    text = " ".join(text.split())
    if not text:
        return []
    if max_lines <= 1:
        return [text]
    candidate = max(width, -(-len(text) // max_lines))
    wrapped: list[str] = [text]
    while candidate <= len(text):
        if latin:
            wrapped = textwrap.wrap(text, width=candidate, break_long_words=True)
        else:
            wrapped = _wrap_cjk(text, candidate)
        if len(wrapped) <= max_lines:
            return wrapped
        candidate = int(candidate * 1.2) + 1
    return wrapped


def _is_mostly_ascii(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    ascii_letters = [ch for ch in letters if ord(ch) < 128]
    return len(ascii_letters) / len(letters) > 0.9


def write_text(path: Path, text: str, bom: bool = True) -> Path:
    """写 SRT。含非 ASCII 字符时带 UTF-8 BOM。

    实测：VLC 在中文 Windows 上按系统代码页（cp936）读没有 BOM 的 SRT，UTF-8 的
    中文全部变成乱码（「我是」显示成「鎴戞槸」）。带 BOM 的同一份文件按 UTF-8 读，
    显示正常。纯 ASCII 的文件不加 BOM，免得老工具把 BOM 当正文。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_bom = bom and any(ord(char) > 127 for char in text)
    path.write_text(text, encoding="utf-8-sig" if needs_bom else "utf-8")
    return path


def merge(
    chunk_dir: Path,
    srt_path: Path,
    out_path: Path,
    width: int = DEFAULT_WIDTH,
    bilingual: bool = False,
    bom: bool = True,
    lines: int = 1,
) -> tuple[Path, dict]:
    """把各块的译文拼回 SRT。时间轴与编号来自原文，只换文本。"""
    cues = read_srt(srt_path)
    translations: dict[int, str] = {}
    sources: dict[int, Path] = {}
    # 只读译文文件。*.txt 是原文块，读错它们会把英文当成译文拼回去。
    for path in sorted(Path(chunk_dir).glob("[0-9][0-9].zh.txt")):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not raw.strip():
                continue
            match = LINE.match(raw.strip())
            if not match:
                continue
            index = int(match.group(1))
            translations[index] = match.group(2).strip()
            sources[index] = path

    missing = [index for index in range(1, len(cues) + 1) if not translations.get(index)]
    report = {
        "cues": len(cues),
        "translated": len(cues) - len(missing),
        "missing": missing[:20],
        "missing_count": len(missing),
        "untranslated_looking": [],
    }

    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        chinese = translations.get(index) or cue.text
        if index in translations and _is_mostly_ascii(chinese) and not _is_mostly_ascii(cue.text):
            report["untranslated_looking"].append(index)
        body = "\n".join(_fit_lines(chinese, width, lines, latin=False))
        if bilingual:
            body += "\n" + "\n".join(_fit_lines(cue.text, 42, lines, latin=True))
        blocks.append(f"{index}\n{_stamp(cue.start)} --> {_stamp(cue.end)}\n{body}")
    write_text(Path(out_path), "\n\n".join(blocks) + "\n", bom)
    return Path(out_path), report


def rewrap(srt_path: Path, out_path: Path, lines: int = 1, width: int = 42) -> Path:
    """只改折行，不改文本与时间轴。用来把已经生成的 SRT 收成一行。"""
    cues = read_srt(srt_path)
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        body = "\n".join(_fit_lines(cue.text, width, lines, latin=_is_mostly_ascii(cue.text)))
        blocks.append(f"{index}\n{_stamp(cue.start)} --> {_stamp(cue.end)}\n{body}")
    write_text(Path(out_path), "\n\n".join(blocks) + "\n")
    return Path(out_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SRT 分段翻译的拆分与拼接")
    sub = parser.add_subparsers(dest="command", required=True)

    split_cmd = sub.add_parser("split", help="把 SRT 拆成每块一个文件")
    split_cmd.add_argument("srt")
    split_cmd.add_argument("--out", required=True)
    split_cmd.add_argument("--size", type=int, default=50)

    merge_cmd = sub.add_parser("merge", help="把译文拼回 SRT")
    merge_cmd.add_argument("dir")
    merge_cmd.add_argument("--srt", required=True)
    merge_cmd.add_argument("--out", required=True)
    merge_cmd.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    merge_cmd.add_argument("--bilingual", action="store_true")
    merge_cmd.add_argument("--no-bom", action="store_true", help="不写 UTF-8 BOM")
    merge_cmd.add_argument(
        "--lines", type=int, default=1, choices=(1, 2),
        help="每个语种最多几行，默认 1（一条字幕一行）",
    )

    rewrap_cmd = sub.add_parser("rewrap", help="只改折行，不改文本与时间轴")
    rewrap_cmd.add_argument("srt")
    rewrap_cmd.add_argument("--out", required=True)
    rewrap_cmd.add_argument("--lines", type=int, default=1, choices=(1, 2))
    rewrap_cmd.add_argument("--width", type=int, default=42)

    args = parser.parse_args(argv)
    if args.command == "rewrap":
        path = rewrap(Path(args.srt), Path(args.out), args.lines, args.width)
        print(f"写出 {path}")
        return 0
    if args.command == "split":
        written = split(Path(args.srt), Path(args.out), args.size)
        print(f"拆成 {len(written)} 块：{Path(args.out)}")
        return 0

    path, report = merge(
        Path(args.dir), Path(args.srt), Path(args.out), args.width,
        args.bilingual, not args.no_bom, args.lines,
    )
    print(f"写出 {path}")
    print(json.dumps(report, ensure_ascii=False))
    if report["missing_count"]:
        print(f"缺 {report['missing_count']} 条译文，对应编号见上", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
