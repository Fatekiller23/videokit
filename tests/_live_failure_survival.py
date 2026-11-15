"""Live proof that a batch survives a per-part failure, over BV1Y5QgB5Eas.

Not part of the suite. This uses the real network, the real credential, the
real 16 kHz audio, the real ASR engine and the real yt-dlp, and it is the only
check that can show a failure being recorded while the rest of the batch keeps
producing files.

Two scenarios, both over the real course.

A. A real batch where one part's ASR genuinely raises. The engine is the real
   `paraformer-zh` stack behind a wrapper that raises on one part's audio, so
   every other part is transcribed for real. This is the "broken engine" the
   failure has to survive, injected at the `engine=` seam the module already
   exposes for exactly this reason.

B. A real batch where one part genuinely has no audio. The audio-download call
   for that part asks the real yt-dlp for a 分P the platform does not have, and
   the real yt-dlp really answers with no file: measured separately, parts 0,
   31 and 99 each return an empty list and write nothing. So part 1 reaches
   `failure_reason = "no_audio"` through the production path rather than
   through a stubbed `_run`.

    uv run --python 3.12 --extra asr python test/_live_failure_survival.py

Do not set PYTHONIOENCODING. That variable breaks `probe` through the known
`_run` decoding bug recorded in recon.md; this script reconfigures its own
stdout instead, which is the half it is allowed to touch.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videokit  # noqa: E402

TARGET = "BV1Y5QgB5Eas"
SCRATCH = Path(r"D:\projects\EcoBuddy\.wayfinder-scratch\e2e-18")
SOURCE_AUDIO = Path(r"D:\projects\ecobuddy-notes\audio")
# Parts the recorded probe table says have no subtitle track, so they exercise
# the ASR path. Parts 3, 4, 5, 10 and 12 have one and are cheap to fetch.
ASR_PARTS = (1, 2)
PLATFORM_PARTS = (3, 4, 5, 10, 12)
# The part whose engine call raises in scenario A.
PART_THAT_FAILS = 2
# The 分P the real yt-dlp is asked for in scenario B, which does not exist.
NONEXISTENT_PART = 99


def stage(audio_dir: Path, indexes) -> None:
    """Copy the real WAVs into place, so the run spends its time on ASR."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    for index in indexes:
        source = SOURCE_AUDIO / f"{index:02d}.wav"
        target = videokit.audio_path(audio_dir, index)
        if source.is_file() and not target.is_file():
            target.write_bytes(source.read_bytes())


class FailOnOnePart:
    """The real engine, raising on one part's audio and working on the rest.

    `model` is forwarded because `transcribe` reads it to verify the device
    before any audio is handed over. Nothing else is wrapped.
    """

    def __init__(self, inner, fail_name: str, exc: Exception):
        self.inner = inner
        self.fail_name = fail_name
        self.exc = exc
        self.seen: list[str] = []

    def generate(self, **kwargs):
        name = Path(str(kwargs.get("input", ""))).name
        self.seen.append(name)
        if name == self.fail_name:
            raise self.exc
        return self.inner.generate(**kwargs)

    @property
    def model(self):
        return self.inner.model


def show(records, out_dir: Path) -> None:
    """The evidence: the summary line, then the per-part outcome on disk."""
    print()
    print(videokit.transcript_report(records, out_dir))
    print()
    print("per part")
    for record in records:
        path = out_dir / videokit.transcript_filename(record)
        size = path.stat().st_size if path.is_file() else None
        print(
            f"  {record.index:02d} status={record.status:7} "
            f"reason={str(record.failure_reason):9} "
            f"source={str(record.source):8} segs={len(record.segments):5} "
            f"file={'absent' if size is None else f'{size} bytes'}"
        )


def scenario_a() -> bool:
    """One part's ASR raises, the rest of the real batch still lands."""
    print("=" * 72)
    print("A. real batch, one part's ASR raises")
    out = SCRATCH / "a" / "transcripts"
    audio = SCRATCH / "a" / "audio"
    stage(audio, ASR_PARTS)

    real = videokit._DeferredEngine("cpu")
    engine = FailOnOnePart(
        real,
        fail_name=f"{PART_THAT_FAILS:02d}.wav",
        exc=RuntimeError("simulated CUDA out of memory on this part"),
    )
    started = time.perf_counter()
    records = videokit.transcripts(
        TARGET,
        out,
        parts=list(ASR_PARTS + PLATFORM_PARTS),
        audio_dir=audio,
        engine=engine,
    )
    elapsed = time.perf_counter() - started
    print(f"elapsed {elapsed:.1f}s ({elapsed / 60:.1f} min)")
    print(f"engine really saw {engine.seen} ({real.loads} model load)")
    show(records, out)
    del records, engine, real
    return check(
        out,
        want_files=[1, 3, 4, 5, 10, 12],
        want_absent=[2],
    )


def scenario_b() -> bool:
    """One part genuinely has no audio, and the batch reports why."""
    print()
    print("=" * 72)
    print("B. real batch, one part has no audio to fetch")
    out = SCRATCH / "b" / "transcripts"
    audio = SCRATCH / "b" / "audio"
    stage(audio, (3,))

    original = videokit._run

    def no_audio_for_part_1(args, cookies):
        """Real yt-dlp, asked for a 分P this course does not have."""
        args = list(args)
        if "--audio-format" in args:
            position = args.index("--playlist-items")
            args[position + 1] = str(NONEXISTENT_PART)
        return original(args, cookies)

    videokit._run = no_audio_for_part_1
    try:
        started = time.perf_counter()
        records = videokit.transcripts(
            TARGET, out, parts=[1, 3], audio_dir=audio
        )
        elapsed = time.perf_counter() - started
    finally:
        videokit._run = original
    print(f"elapsed {elapsed:.1f}s")
    show(records, out)
    return check(out, want_files=[3], want_absent=[1])


def check(out: Path, want_files, want_absent) -> bool:
    """The delivered files really exist, and the failed ones really do not."""
    names = sorted(p.name for p in out.iterdir()) if out.is_dir() else []
    print()
    print(f"delivered files {len(names)} in {out}")
    for name in names:
        print(f"  {name} ({len((out / name).read_text(encoding='utf-8'))} chars)")
    ok = True
    for index in want_files:
        if not any(n.startswith(f"{index:02d}-") for n in names):
            print(f"FAIL: part {index} should have a file and does not")
            ok = False
    for index in want_absent:
        if any(n.startswith(f"{index:02d}-") for n in names):
            print(f"FAIL: part {index} failed but left a file behind")
            ok = False
    print("OK" if ok else "FAIL")
    return ok


def main() -> int:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    results = {"A": scenario_a(), "B": scenario_b()}
    print()
    print("=" * 72)
    print("summary", results)
    print("PASS" if all(results.values()) else "FAIL")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
