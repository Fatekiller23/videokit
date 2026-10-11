"""Behavior spec for VideoKit.

Imports the real module. The only thing stubbed is the yt-dlp subprocess:
the spec swaps in recorded output captured from a real run, so parsing and
data-shaping are exercised against bytes yt-dlp actually produces.

Run: python test/test_videokit.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videokit  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MULTI_SUBS = (FIXTURES / "list-subs-multipart.txt").read_text(encoding="utf-8")
SINGLE_SUBS = (FIXTURES / "list-subs-single.txt").read_text(encoding="utf-8")
MULTI_DUMP = (FIXTURES / "dump-multipart.json").read_text(encoding="utf-8")


class ParseSubtitleTable(unittest.TestCase):
    def test_multipart_yields_one_entry_per_part(self):
        parsed = videokit.parse_subtitle_table(MULTI_SUBS)
        self.assertEqual(len(parsed), 30)
        self.assertIn("BV1Y5QgB5Eas_p1", parsed)
        self.assertIn("BV1Y5QgB5Eas_p30", parsed)

    def test_part_with_only_danmaku_reports_no_subtitles(self):
        parsed = videokit.parse_subtitle_table(MULTI_SUBS)
        # p1 carries danmaku only. danmaku is a bullet-comment feed, not a
        # subtitle, so it must not be reported as one.
        self.assertEqual(parsed["BV1Y5QgB5Eas_p1"], ())

    def test_part_with_ai_subtitle_reports_the_language(self):
        parsed = videokit.parse_subtitle_table(MULTI_SUBS)
        self.assertEqual(parsed["BV1Y5QgB5Eas_p3"], ("ai-zh",))

    def test_single_video_has_no_part_suffix(self):
        parsed = videokit.parse_subtitle_table(SINGLE_SUBS)
        self.assertIn("BV1GJ411x7h7", parsed)
        langs = parsed["BV1GJ411x7h7"]
        # This video carries several human-made tracks; all must survive.
        self.assertIn("zh-CN", langs)
        self.assertIn("zh-Hant", langs)
        self.assertNotIn("danmaku", langs)

    def test_counts_match_the_known_distribution(self):
        parsed = videokit.parse_subtitle_table(MULTI_SUBS)
        with_subs = [k for k, v in parsed.items() if v]
        without = [k for k, v in parsed.items() if not v]
        self.assertEqual(len(with_subs), 21)
        self.assertEqual(len(without), 9)


class Probe(unittest.TestCase):
    def setUp(self):
        # Replace only the subprocess boundary; everything else is real.
        self.calls: list[list[str]] = []
        self.original = videokit._run

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return MULTI_DUMP
            return MULTI_SUBS

        videokit._run = fake_run

    def tearDown(self):
        videokit._run = self.original

    def test_returns_every_part_with_metadata(self):
        video = videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")
        self.assertEqual(len(video.parts), 30)
        first = video.parts[0]
        self.assertEqual(first.index, 1)
        self.assertEqual(first.id, "BV1Y5QgB5Eas_p1")
        self.assertGreater(first.duration_sec, 0)
        self.assertIn("徐高", first.title)

    def test_parts_are_ordered_by_index(self):
        video = videokit.probe("x")
        self.assertEqual([p.index for p in video.parts], list(range(1, 31)))

    def test_has_subtitles_reflects_the_probe(self):
        video = videokit.probe("x")
        by_index = {p.index: p for p in video.parts}
        self.assertFalse(by_index[1].has_subtitles)   # p1: danmaku only
        self.assertTrue(by_index[3].has_subtitles)    # p3: ai-zh
        self.assertEqual(by_index[3].subtitle_langs, ("ai-zh",))

    def test_uses_two_calls_because_json_hides_subtitles(self):
        videokit.probe("x")
        modes = [a for a in self.calls]
        self.assertTrue(any("--dump-single-json" in a for a in modes))
        self.assertTrue(any("--list-subs" in a for a in modes))

    def test_formats_are_exposed_for_quality_choice(self):
        video = videokit.probe("x")
        self.assertTrue(video.formats)
        self.assertIn("format_id", video.formats[0])


class FormatSelector(unittest.TestCase):
    def test_each_quality_names_a_video_and_an_audio_stream(self):
        # Bilibili serves these separately; a muxed stream does not exist.
        for quality in ("1080p", "720p", "480p", "360p"):
            selector = videokit._format_selector(quality)
            self.assertIn("+", selector)

    def test_unknown_quality_is_rejected(self):
        with self.assertRaises(ValueError):
            videokit._format_selector("4k")


class Download(unittest.TestCase):
    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run

        def fake_run(args, cookies):
            self.calls.append(args)
            # yt-dlp reports the paths it wrote; the module must return those
            # rather than guessing a filename from the output template.
            return "/out/16.wav\n"

        videokit._run = fake_run
        self.out = Path(__file__).resolve().parent / "_tmp_out"

    def tearDown(self):
        videokit._run = self.original
        if self.out.exists():
            for f in self.out.iterdir():
                f.unlink()
            self.out.rmdir()

    def test_returns_the_paths_yt_dlp_reports(self):
        # Post-processing can change the extension and the template holds
        # %(...)s placeholders, so the on-disk name is not derivable from the
        # template. The reported path is the only reliable source.
        files = videokit.download_audio("u", self.out, parts=[16])
        self.assertEqual(files, [Path("/out/16.wav")])

    def test_asks_yt_dlp_to_report_final_paths(self):
        videokit.download_audio("u", self.out, parts=[16])
        args = self.calls[-1]
        self.assertIn("--print", args)
        idx = args.index("--print")
        self.assertEqual(args[idx + 1], "after_move:filepath")

    def test_video_download_merges_and_selects_parts(self):
        videokit.download_video("u", self.out, parts=[1, 3], quality="720p")
        args = self.calls[-1]
        self.assertIn("--merge-output-format", args)
        self.assertIn("mp4", args)
        idx = args.index("--playlist-items")
        self.assertEqual(args[idx + 1], "1,3")
        idx = args.index("-f")
        self.assertEqual(args[idx + 1], "30066+30280")

    def test_audio_download_resamples_for_asr(self):
        videokit.download_audio("u", self.out, parts=[6])
        args = self.calls[-1]
        # 16 kHz mono is what the model expects; converting here means the
        # file on disk is already the file ASR wants.
        self.assertIn("--postprocessor-args", args)
        self.assertIn("-ar 16000 -ac 1", args)
        self.assertIn("--audio-format", args)

    def test_no_parts_means_every_part(self):
        videokit.download_video("u", self.out)
        self.assertNotIn("--playlist-items", self.calls[-1])


class ErrorHandling(unittest.TestCase):
    def test_failure_raises_with_stderr_attached(self):
        import subprocess as sp

        original = sp.run

        def fake_run(cmd, **kwargs):
            class R:
                returncode = 1
                stdout = ""
                stderr = "ERROR: something went wrong"
            return R()

        sp.run = fake_run
        try:
            with self.assertRaises(videokit.VideoKitError) as ctx:
                videokit._run(["--version"], None)
            self.assertIn("something went wrong", str(ctx.exception))
        finally:
            sp.run = original

    def test_does_not_force_utf8_on_subprocess_output(self):
        # yt-dlp emits the console code page (cp936 here), not UTF-8. Forcing
        # UTF-8 mangles Chinese titles, and those titles become filenames, so
        # the paths come back unusable.
        import subprocess as sp

        captured: dict = {}
        original = sp.run

        def fake_run(cmd, **kwargs):
            captured.update(kwargs)

            class R:
                returncode = 0
                stdout = ""
                stderr = ""
            return R()

        sp.run = fake_run
        try:
            videokit._run(["--version"], None)
        finally:
            sp.run = original
        self.assertNotIn("encoding", captured)

    def test_reports_a_clear_error_when_ytdlp_is_missing(self):
        import subprocess as sp

        original = sp.run

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError(2, "The system cannot find the file")

        sp.run = fake_run
        try:
            with self.assertRaises(videokit.VideoKitError) as ctx:
                videokit._run(["--version"], None)
            self.assertIn("could not execute", str(ctx.exception))
        finally:
            sp.run = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
