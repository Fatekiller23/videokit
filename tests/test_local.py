import wave
from pathlib import Path

import numpy as np
import pytest

from videokit import Segment, local


def test_timestamp_formats_millis():
    assert local._timestamp(0) == "00:00:00,000"
    assert local._timestamp(3661.234) == "01:01:01,234"
    assert local._timestamp(-5) == "00:00:00,000"


def test_srt_text_numbers_blocks_and_keeps_order():
    segments = [
        Segment(start_sec=0.0, end_sec=1.5, text="hello there"),
        Segment(start_sec=2.0, end_sec=3.0, text="second line"),
    ]
    text = local.srt_text(segments)
    blocks = text.strip().split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].startswith("1\n00:00:00,000 --> 00:00:01,500\nhello there")
    assert blocks[1].startswith("2\n00:00:02,000 --> 00:00:03,000\nsecond line")


def test_empty_input_gives_empty_string():
    assert local.srt_text([]) == ""
    assert local.srt_text([Segment(start_sec=0, end_sec=1, text="   ")]) == ""


def test_long_segment_splits_into_several_cues():
    words = " ".join(["trench"] * 40)  # 279 characters, well over one cue
    cues = local.cues([Segment(start_sec=10.0, end_sec=40.0, text=words)])
    assert len(cues) > 1
    assert cues[0][0] == 10.0
    assert cues[-1][1] == pytest.approx(40.0)
    for _, _, lines in cues:
        assert len(lines) <= local.DEFAULT_MAX_LINES
        assert all(len(line) <= local.DEFAULT_MAX_CHARS for line in lines)
    for earlier, later in zip(cues, cues[1:]):
        assert earlier[1] <= later[0] + 1e-6


def test_cues_do_not_overlap_when_engine_reports_overlap():
    segments = [
        Segment(start_sec=0.0, end_sec=5.0, text="first"),
        Segment(start_sec=2.0, end_sec=4.0, text="second"),
    ]
    cues = local.cues(segments)
    assert cues[0][1] <= cues[1][0] + 1e-6
    assert cues[1][1] >= cues[1][0] + local._MIN_CUE_SEC


def test_zero_length_segment_gets_minimum_duration():
    cues = local.cues([Segment(start_sec=3.0, end_sec=3.0, text="blip")])
    assert cues[0][1] - cues[0][0] >= local._MIN_CUE_SEC - 1e-6


def test_sources_of_directory_keeps_only_known_containers(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "b.mkv").write_bytes(b"")
    (tmp_path / "notes.txt").write_text("skip me")
    (tmp_path / "c.SRT").write_text("skip me too")
    found = [p.name for p in local.sources_of(tmp_path)]
    assert found == ["a.mp4", "b.mkv"]


def test_sources_of_file_returns_it(tmp_path):
    target = tmp_path / "one.mp4"
    target.write_bytes(b"")
    assert local.sources_of(target) == [target]


def _write_wav(path: Path, samples: np.ndarray) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(pcm.tobytes())


def test_wav_samples_returns_float32_in_range(tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav, np.array([0.0, 0.5, -0.5], dtype=np.float32))
    samples = local.wav_samples(wav)
    assert samples.dtype == np.float32
    assert samples.shape == (3,)
    assert np.allclose(samples, [0.0, 0.5, -0.5], atol=1e-3)


def test_transcribe_audio_passes_an_array_not_a_path(tmp_path):
    wav = tmp_path / "a.wav"
    _write_wav(wav, np.zeros(1600, dtype=np.float32))

    class FakeInfo:
        language = "en"
        language_probability = 0.99

    class FakeItem:
        start = 0.0
        end = 1.0
        text = " hello world "

    class FakeEngine:
        def __init__(self):
            self.received = None

        def transcribe(self, audio, **kwargs):
            self.received = audio
            return [FakeItem()], FakeInfo()

    engine = FakeEngine()
    segments = local.transcribe_audio(wav, engine=engine, device="cpu", quiet=True)
    assert isinstance(engine.received, np.ndarray), "must not hand a path to faster-whisper"
    assert not isinstance(engine.received, (str, Path))
    assert [(s.start_sec, s.end_sec, s.text) for s in segments] == [(0.0, 1.0, "hello world")]


def test_unknown_container_is_refused(tmp_path):
    target = tmp_path / "a.xyz"
    target.write_bytes(b"")
    with pytest.raises(local.LocalError) as caught:
        local.subtitles(target)
    assert "不认这种扩展名" in str(caught.value)


def test_missing_file_is_refused(tmp_path):
    with pytest.raises(local.LocalError) as caught:
        local.subtitles(tmp_path / "nope.mp4")
    assert "文件不存在" in str(caught.value)


def test_srt_command_rejects_unknown_device():
    assert local.srt_command(["a.mp4", "--device", "tpu"]) == 2


def test_srt_command_needs_a_reference():
    assert local.srt_command([]) == 2
