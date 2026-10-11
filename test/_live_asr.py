"""Live check: the real engine, a real 47.5 minute lecture, real timings.

Not part of the suite. Loading the model takes 29 to 50 seconds and a real
transcription takes minutes, so this stays out of test_videokit.py. Run it by
hand from packages/VideoKit, and expect about five minutes:

    uv run --python 3.12 --extra asr python test/_live_asr.py

Defaults to the part 6 audio, which is the acceptance criterion for ticket #16:
`BV1Y5QgB5Eas_p6` is 47.5 minutes and is one of the nine parts with no
subtitle track. Pass another WAV as argv[1] to run a different part.

Do not set PYTHONIOENCODING. It breaks the probe path elsewhere in this
module, and the module's own comment explains why.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videokit  # noqa: E402

# Part 6 of BV1Y5QgB5Eas, already downloaded as 16 kHz mono WAV by
# `download_audio`. Held outside the repository because it is 91 MB.
DEFAULT_WAV = Path(r"D:\projects\ecobuddy-notes\audio\06.wav")

EXPECTED_DURATION_SEC = 2850.1


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    wav = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_WAV
    if not wav.is_file():
        print(f"no audio at {wav}", file=sys.stderr)
        return 2
    print(f"audio      {wav}  ({wav.stat().st_size / 1e6:.1f} MB)")

    # The engine is built once and passed in, which is how a caller working
    # through 30 parts should use this. Loading per part would add 30 to 50
    # seconds to each one.
    load_start = time.perf_counter()
    engine = videokit.load_engine("cpu")
    load_sec = time.perf_counter() - load_start

    # Read off the parameters rather than trusting the argument, which is the
    # thing ticket #16 exists to require.
    device = videokit.detect_device(engine)
    print(f"load       {load_sec:.1f} s")
    print(f"device     {device}  (verified, not the argument)")

    run_start = time.perf_counter()
    segments = videokit.transcribe(wav, engine=engine, device="cpu")
    run_sec = time.perf_counter() - run_start

    ratio = EXPECTED_DURATION_SEC / run_sec
    print(f"transcribe {run_sec:.1f} s  ({ratio:.1f}x real time)")
    print(f"segments   {len(segments)}")

    text = "".join(s.text for s in segments)
    marks = set("，。？！、；：,.;:!?")
    print(f"text       {len(text)} chars")
    print(f"punctuated {sum(1 for s in segments if s.text[-1] in marks)}"
          f" / {len(segments)} segments end on a mark")
    print(f"spaces     {text.count(' ')} (a ct-punc failure shows up here)")
    print(f"fillers    啊={text.count('啊')} 这个={text.count('这个')} "
          f"就是={text.count('就是')} 呢={text.count('呢')}")

    print("--- first 5 segments ---")
    for segment in segments[:5]:
        print(f"  [{segment.start_sec:9.2f} -> {segment.end_sec:9.2f}] {segment.text}")
    print("--- one middle excerpt ---")
    for segment in segments[len(segments) // 2 : len(segments) // 2 + 5]:
        print(f"  [{segment.start_sec:9.2f} -> {segment.end_sec:9.2f}] {segment.text}")
    print("--- last segment ---")
    last = segments[-1]
    print(f"  [{last.start_sec:9.2f} -> {last.end_sec:9.2f}] {last.text}")

    # Shape checks that would be visible to a reader of the finished transcript.
    failures = 0
    checks = {
        "segments produced": len(segments) > 0,
        "covers the audio": last.end_sec <= EXPECTED_DURATION_SEC + 1,
        "strictly ordered": all(
            segments[i].end_sec <= segments[i + 1].start_sec
            for i in range(len(segments) - 1)
        ),
        "no empty text": all(s.text.strip() for s in segments),
        "every segment punctuated": all(s.text[-1] in marks for s in segments),
        "no inter-character spacing": text.count(" ") < len(text) / 100,
    }
    for label, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        failures += 0 if ok else 1

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
