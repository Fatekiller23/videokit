"""Real end-to-end run of the joined pipeline over BV1Y5QgB5Eas.

Not part of the suite. This is the acceptance check that no unit test can make:
it uses the real network, the real credential, the real 16 kHz audio and the
real ASR engine, and it asserts that all 30 parts of the first target produce a
delivered file, 21 from platform subtitles and 9 from ASR.

Roughly 45 minutes, of which about 39 are speech recognition. Run it as a
background job.

    uv run --python 3.12 --extra asr python test/_live_asr_pipeline.py

Do not set PYTHONIOENCODING. That variable breaks `probe` through the known
`_run` decoding bug recorded in recon.md; this script reconfigures its own
stdout instead, which is the half it is allowed to touch.

Why this script stages the audio instead of downloading it. The nine real WAVs
were fetched in an earlier round and total 1.1 GB, so copying them in spends
the run on recognition rather than on the same download again. `part_audio`
would return those same files without a network call if they were already in
`audio_dir`, which is the reuse path this script is exercising.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videokit  # noqa: E402

TARGET = "BV1Y5QgB5Eas"
# The nine parts the recorded probe table says have no subtitle track.
MISSING = (1, 2, 6, 7, 8, 9, 13, 23, 28)
HERE = Path(__file__).resolve().parent.parent
OUT = Path(r"D:\projects\EcoBuddy\.wayfinder-scratch\e2e-17\transcripts")
AUDIO = Path(r"D:\projects\EcoBuddy\.wayfinder-scratch\e2e-17\audio")
# The nine real WAVs, already downloaded in an earlier round. Copied in rather
# than re-fetched, so the run spends its time on recognition instead of 1.1 GB
# of download.
SOURCE_AUDIO = Path(r"D:\projects\ecobuddy-notes\audio")


def stage_audio() -> list[str]:
    """Put the real 16 kHz WAVs where the pipeline looks for them."""
    AUDIO.mkdir(parents=True, exist_ok=True)
    notes = []
    for index in MISSING:
        source = SOURCE_AUDIO / f"{index:02d}.wav"
        target = videokit.audio_path(AUDIO, index)
        if not source.is_file():
            notes.append(f"part {index}: no source WAV at {source}")
            continue
        if not target.is_file():
            target.write_bytes(source.read_bytes())
        notes.append(f"part {index}: {target.stat().st_size} bytes")
    return notes


def main() -> int:
    print(f"target {TARGET}", flush=True)
    for note in stage_audio():
        print(f"  audio {note}", flush=True)

    started = time.perf_counter()
    records = videokit.transcripts(TARGET, OUT, audio_dir=AUDIO)
    elapsed = time.perf_counter() - started
    print(f"elapsed {elapsed:.1f}s ({elapsed / 60:.1f} min)", flush=True)

    print()
    print(videokit.transcript_report(records, OUT))
    print(f"elapsed {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    platform = [r for r in records if r.source == "platform"]
    asr = [r for r in records if r.source == "asr"]
    written = sorted(OUT.iterdir())
    print()
    print(f"records {len(records)}  ok {sum(r.status == 'ok' for r in records)}")
    print(f"platform {len(platform)}  asr {len(asr)}  files {len(written)}")
    print(f"asr parts {[r.index for r in asr]}")

    print()
    print("per part")
    for record in records:
        path = OUT / videokit.transcript_filename(record)
        size = path.stat().st_size if path.is_file() else -1
        print(
            f"  {record.index:02d} source={str(record.source):8} "
            f"lang={str(record.language):6} segs={len(record.segments):5} "
            f"bytes={size:8} {record.title[:34]}"
        )

    ok = True
    if len(records) != 30 or len(written) != 30:
        print(f"FAIL: {len(records)} records and {len(written)} files, want 30")
        ok = False
    if len(platform) != 21 or len(asr) != 9:
        print(f"FAIL: {len(platform)} platform and {len(asr)} asr, want 21 and 9")
        ok = False
    if tuple(sorted(r.index for r in asr)) != MISSING:
        print("FAIL: the ASR parts are not the nine recorded as trackless")
        ok = False

    print()
    print("one ASR excerpt, showing the punctuation the platform path lacks")
    if asr:
        head = videokit.transcript_text(asr[0].segments)
        print(f"  part {asr[0].index}: {head[:220]}")
        marks = sum(head.count(m) for m in "，。？！")
        print(f"  sentence marks in this part: {marks}")
    if platform:
        head = videokit.transcript_text(platform[0].segments)
        print(f"  part {platform[0].index} (platform): {head[:120]}")
        marks = sum(head.count(m) for m in "，。？！")
        print(f"  sentence marks in this part: {marks}")

    # Delivered files carry no source marker and no timestamp, on both paths.
    for path in written:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("-->", "platform", "asr"):
            if forbidden in text:
                print(f"FAIL: {path.name} contains {forbidden!r}")
                ok = False

    print()
    print("PASS" if ok else "FAIL")
    (OUT.parent / "result.json").write_text(
        json.dumps(
            {
                "elapsed_sec": elapsed,
                "parts": [
                    {
                        "index": r.index,
                        "source": r.source,
                        "language": r.language,
                        "segments": len(r.segments),
                        "status": r.status,
                        "title": r.title,
                    }
                    for r in records
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
