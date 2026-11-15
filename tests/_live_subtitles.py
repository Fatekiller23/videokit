"""Live check: real site, real credentials, real retrieval path.

Not part of the suite. It reaches the network, so it stays out of
test_videokit.py. Run it by hand from packages/VideoKit:

    uv run --python 3.12 python test/_live_subtitles.py

Run with no PYTHONIOENCODING. That variable breaks the probe path on this
machine, and the module's own comment explains why.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videokit  # noqa: E402

URL = "https://www.bilibili.com/video/BV1Y5QgB5Eas"

# Segment counts from the recorded tracks, measured across all 21 parts.
EXPECTED = {
    12: 1783,  # p12 06-内部不平衡与外部不平衡-2
    18: 1078,  # p18 09-财政政策与货币政策-2
    25: 1895,  # p25 13-"两手博弈"背后的世界观之争-1
    30: 2155,  # p30 15-宏观经济分析的逻辑框架-2
}


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    # The credential is found without being named, which is the path a caller
    # actually uses.
    cookies = videokit.default_cookies()
    print(f"credential discovered: {cookies}")

    failures = 0
    for part, expected in EXPECTED.items():
        segments = videokit.part_subtitles(URL, part)
        starts = [s.start_sec for s in segments]
        ordered = starts == sorted(starts)
        no_stamp = all("-->" not in s.text for s in segments)
        ok = len(segments) == expected and ordered and no_stamp
        failures += 0 if ok else 1
        print(
            f"part {part:2d}  segments={len(segments):5d}  expected={expected:5d}  "
            f"ordered={ordered}  no_timestamp={no_stamp}  "
            f"first={segments[0].start_sec}s  last_end={segments[-1].end_sec}s"
        )
        print(f"          first text={segments[0].text!r}")

    # Punctuation, measured on live bytes rather than on the recorded ones.
    segments = videokit.part_subtitles(URL, 12)
    text = "".join(s.text for s in segments)
    cjk_punct = sum(
        1
        for ch in text
        if 0x3000 <= ord(ch) <= 0x303F or 0xFF00 <= ord(ch) <= 0xFFEF
    )
    other = sum(1 for ch in text if ch in ",?!;")
    print(
        f"live punctuation check  chars={len(text)}  "
        f"cjk_punct={cjk_punct}  ascii_sentence_marks={other}"
    )

    # The distinction this ticket exists to protect. Part 6 has no track, so it
    # must raise rather than return [].
    for part, label in ((6, "no track at all"), (1, "danmaku only")):
        try:
            videokit.part_subtitles(URL, part)
        except videokit.SubtitleUnavailable as exc:
            print(f"part {part:2d} ({label}) -> SubtitleUnavailable: {exc}")
        else:
            print(f"part {part:2d} ({label}) -> NO RAISE, which is wrong")
            failures += 1

    # And the empty-list path stays a different outcome.
    print(f"parse_srt('') -> {videokit.parse_srt('')!r} (empty list, not an error)")

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
