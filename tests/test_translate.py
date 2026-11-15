import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import srt_translate as st  # noqa: E402

SAMPLE = """1
00:00:01,000 --> 00:00:03,000
Hello there, this is a test.

2
00:00:03,000 --> 00:00:05,500
Second line
that spans two rows.

3
00:00:06,000 --> 00:00:08,000
Third one.
"""


def _write(tmp_path: Path) -> Path:
    path = tmp_path / "a.srt"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


def test_read_srt_joins_multiline_text_and_keeps_times(tmp_path):
    cues = st.read_srt(_write(tmp_path))
    assert len(cues) == 3
    assert cues[0].start == 1.0 and cues[0].end == 3.0
    assert cues[0].text == "Hello there, this is a test."
    assert cues[1].text == "Second line that spans two rows."


def test_split_writes_numbered_lines(tmp_path):
    chunks = st.split(_write(tmp_path), tmp_path / "chunks", size=2)
    assert [p.name for p in chunks] == ["01.txt", "02.txt"]
    first = (tmp_path / "chunks" / "01.txt").read_text(encoding="utf-8").splitlines()
    assert first[0] == "0001|Hello there, this is a test."
    assert first[1].startswith("0002|Second line")
    second = (tmp_path / "chunks" / "02.txt").read_text(encoding="utf-8").splitlines()
    assert second == ["0003|Third one."]


def test_merge_rebuilds_srt_with_original_timings(tmp_path):
    source = _write(tmp_path)
    directory = tmp_path / "chunks"
    st.split(source, directory, size=2)
    (directory / "01.zh.txt").write_text(
        "0001|你好，这是一次测试。\n0002|第二行\n", encoding="utf-8"
    )
    (directory / "02.zh.txt").write_text("0003|第三条。\n", encoding="utf-8")
    out, report = st.merge(directory, source, tmp_path / "zh.srt")
    text = out.read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:03,000" in text
    assert "你好，这是一次测试。" in text
    assert report["missing_count"] == 0
    assert report["cues"] == 3


def test_merge_reports_missing_translations(tmp_path):
    source = _write(tmp_path)
    directory = tmp_path / "chunks"
    st.split(source, directory, size=2)
    (directory / "01.zh.txt").write_text("0001|只有这一条\n", encoding="utf-8")
    (directory / "02.zh.txt").write_text("", encoding="utf-8")
    _, report = st.merge(directory, source, tmp_path / "zh.srt")
    assert report["missing_count"] == 2
    assert report["missing"] == [2, 3]


def test_missing_translation_falls_back_to_source(tmp_path):
    source = _write(tmp_path)
    directory = tmp_path / "chunks"
    st.split(source, directory, size=3)
    (directory / "01.zh.txt").write_text("0001|第一条\n", encoding="utf-8")
    out, _ = st.merge(directory, source, tmp_path / "zh.srt")
    text = out.read_text(encoding="utf-8")
    assert "第一条" in text
    assert "Second line" in text


def test_cjk_wrap_prefers_punctuation():
    lines = st._wrap_cjk("我们今天来讲内围搏斗，这是这门课的核心内容之一。", width=10)
    assert all(len(line) <= 12 for line in lines)
    assert "".join(lines).replace(" ", "") == "我们今天来讲内围搏斗，这是这门课的核心内容之一。"


def test_cjk_wrap_hard_breaks_without_punctuation():
    lines = st._wrap_cjk("一" * 45, width=20)
    assert [len(line) for line in lines] == [20, 20, 5]


def test_fit_lines_never_truncates_chinese():
    # 78 个字按 20 字一行需要 4 行，超过两行上限。折完必须一个字都不少。
    text = "这是一句很长的中文字幕" * 7
    lines = st._fit_lines(text, 20, 2, latin=False)
    assert len(lines) <= 2
    assert "".join(lines).replace(" ", "") == text


def test_fit_lines_never_truncates_latin():
    text = " ".join(["instructional"] * 9)
    lines = st._fit_lines(text, 42, 2, latin=True)
    assert len(lines) <= 2
    assert " ".join(lines).split() == text.split()


def test_bilingual_cue_has_at_most_four_body_lines(tmp_path):
    source = _write(tmp_path)
    directory = tmp_path / "chunks"
    st.split(source, directory, size=3)
    long_zh = "这是一句很长的中文字幕" * 5
    (directory / "01.zh.txt").write_text(
        f"0001|{long_zh}\n0002|第二行\n0003|第三条。\n", encoding="utf-8"
    )
    out, _ = st.merge(directory, source, tmp_path / "bi.srt", bilingual=True)
    for block in out.read_text(encoding="utf-8").strip().split("\n\n"):
        body = block.splitlines()[2:]
        assert len(body) <= 4, body


def test_mostly_ascii_detects_untranslated_lines():
    assert st._is_mostly_ascii("this is still english")
    assert not st._is_mostly_ascii("这是中文")
    assert not st._is_mostly_ascii("内围 fighting 的技术")


def test_bilingual_keeps_source_text(tmp_path):
    source = _write(tmp_path)
    directory = tmp_path / "chunks"
    st.split(source, directory, size=3)
    (directory / "01.zh.txt").write_text(
        "0001|你好。\n0002|第二行。\n0003|第三条。\n", encoding="utf-8"
    )
    out, _ = st.merge(directory, source, tmp_path / "bi.srt", bilingual=True)
    text = out.read_text(encoding="utf-8")
    assert "你好。" in text and "Hello there, this is a test." in text
