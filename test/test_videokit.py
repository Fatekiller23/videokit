"""Behavior spec for VideoKit.

Imports the real module. The only thing stubbed is the yt-dlp subprocess:
the spec swaps in recorded output captured from a real run, so parsing and
data-shaping are exercised against bytes yt-dlp actually produces.

Run: python test/test_videokit.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videokit  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MULTI_SUBS = (FIXTURES / "list-subs-multipart.txt").read_text(encoding="utf-8")
SINGLE_SUBS = (FIXTURES / "list-subs-single.txt").read_text(encoding="utf-8")
MULTI_DUMP = (FIXTURES / "dump-multipart.json").read_text(encoding="utf-8")

# The six inputs recon.md measured, recorded as two streams. These three are
# the ones that separate the three cases: a logged-out run, a valid login on a
# part with no subtitles, and a valid login on a part that has one.
LOGGED_OUT_SUBS = (FIXTURES / "list-subs-logged-out.txt").read_text(encoding="utf-8")
VALID_NO_SUBS = (FIXTURES / "list-subs-valid-no-subs.txt").read_text(encoding="utf-8")
LOGIN_WARNING = (FIXTURES / "list-subs-login-warning.txt").read_text(encoding="utf-8")

# A real `Network.getAllCookies` response, recorded from a logged-in Chrome.
# Cookie values are redacted; every other field is what the browser reported.
CDP_COOKIES = json.loads(
    (FIXTURES / "cdp-cookies.json").read_text(encoding="utf-8")
)["result"]["cookies"]

# The same session as written by yt-dlp's own cookie exporter, which is the
# program that will consume this file. Expected output comes from that tool
# rather than from our own serializer, so the two can disagree.
NETSCAPE_REFERENCE = [
    line
    for line in (FIXTURES / "netscape-reference.txt").read_text(encoding="utf-8").splitlines()
    if line
]

# The /json/list reply a real Chrome gave, decoy targets included.
CDP_TARGETS = json.loads((FIXTURES / "cdp-targets.json").read_text(encoding="utf-8"))

# Real platform subtitle bytes, cut from the tracks yt-dlp wrote for parts 12
# and 18. Cut by line range rather than by byte offset, because a byte slice
# can drop the blank line that terminates the last block, which changes the
# segment count.
#   p12  55 segments, 3606 bytes, sha256 ee045224bfeec20c31868763d7334a4990bcbe7b2545504f27ba3449a62c1d27
#   p18   9 segments,  493 bytes, sha256 20c02c4a80129718c3d0bb9e74629d43b622b3b6a295af92d1c72bfff9d86fb0
SRT_P12 = (FIXTURES / "subtitles-p12-slice.srt").read_text(encoding="utf-8")
SRT_P18 = (FIXTURES / "subtitles-p18-slice.srt").read_text(encoding="utf-8")

# What `--print "%(requested_subtitles)j"` wrote for two real parts. The SRT
# text arrives inside the JSON, so there is no file to find afterwards.
SUB_PRINT_PART = (FIXTURES / "subtitle-print-part.txt").read_text(encoding="utf-8")
SUB_PRINT_NO_TRACK = (FIXTURES / "subtitle-print-no-track.txt").read_text(
    encoding="utf-8"
)


def rows(text: str) -> list[str]:
    """The data lines of a Netscape cookie file, comments dropped."""
    return [line for line in text.splitlines() if line and not line.startswith("#")]


def reference_keys() -> list[tuple[str, str]]:
    """(domain, name) for every cookie yt-dlp wrote, in its order."""
    return [(r.split("\t")[0], r.split("\t")[5]) for r in NETSCAPE_REFERENCE]


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
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(MULTI_SUBS, "")

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
            return videokit.RunOutput("/out/16.wav\n", "")

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


class RunBoundary(unittest.TestCase):
    """What `_run` hands back, given what the subprocess actually produced."""

    def _fake_subprocess(self, stdout: str, stderr: str, returncode: int = 0):
        import subprocess as sp

        original = sp.run

        def fake_run(cmd, **kwargs):
            class R:
                pass

            R.returncode = returncode
            R.stdout = stdout
            R.stderr = stderr
            return R()

        sp.run = fake_run
        self.addCleanup(setattr, sp, "run", original)

    def test_stderr_survives_a_successful_run(self):
        # The login warning is written to stderr while the exit code stays 0.
        # A boundary that hands back stdout alone loses the only signal that
        # separates "logged out" from "this video has no subtitles".
        self._fake_subprocess("", "WARNING: no login\n")
        result = videokit._run(["--list-subs"], None)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "WARNING: no login\n")

    def test_both_streams_survive_a_successful_run(self):
        self._fake_subprocess("[info] Available subtitles for BV1x_p1:\n", "WARNING: x\n")
        result = videokit._run(["--list-subs"], None)
        self.assertIn("BV1x_p1", result.stdout)
        self.assertIn("WARNING: x", result.stderr)


class LoginSignal(unittest.TestCase):
    """Which of the three inputs this is, judged from one --list-subs run.

    The ticket says to judge by whether real subtitle tracks come back, and not
    by the exit code. That alone cannot work: recon.md measured that a valid
    login on a part with no subtitles returns an empty table with exit 0, the
    same as a logged-out run. The warning yt-dlp prints to stderr is the only
    signal that separates those two.
    """

    def test_an_empty_table_with_the_warning_is_a_login_problem(self):
        subs = videokit.parse_subtitle_table(LOGGED_OUT_SUBS)
        self.assertTrue(videokit.login_needed(subs, LOGIN_WARNING))

    def test_an_empty_table_without_the_warning_is_a_video_without_subtitles(self):
        # p1 of the classroom course genuinely has no subtitles. A valid login
        # returns an empty table for it too, and must not be called a failure.
        subs = videokit.parse_subtitle_table(VALID_NO_SUBS)
        self.assertFalse(videokit.login_needed(subs, ""))

    def test_a_real_track_wins_over_the_warning(self):
        # Judged across the whole video: one part with a real track proves the
        # credentials work, whatever stderr says.
        subs = videokit.parse_subtitle_table(MULTI_SUBS)
        self.assertFalse(videokit.login_needed(subs, LOGIN_WARNING))

    def test_one_real_part_among_many_empty_ones_is_still_a_valid_login(self):
        subs = {
            "BV1x_p1": (),
            "BV1x_p2": (),
            "BV1x_p3": ("ai-zh",),
            "BV1x_p4": (),
        }
        self.assertFalse(videokit.login_needed(subs, LOGIN_WARNING))

    def test_no_parts_at_all_with_the_warning_is_a_login_problem(self):
        # A URL that resolves to nothing still means the run got nowhere.
        self.assertTrue(videokit.login_needed({}, LOGIN_WARNING))


class LoginRequiredMessage(unittest.TestCase):
    """What the user is handed instead of a silently empty transcript.

    Measured constraint: expired and never-logged-in are externally identical,
    so the message must not claim a cause. It names what to do instead.
    """

    def setUp(self):
        self.original = videokit._run
        self.addCleanup(setattr, videokit, "_run", self.original)

    def _logged_out(self):
        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(LOGGED_OUT_SUBS, LOGIN_WARNING)

        videokit._run = fake_run

    def test_probe_refuses_rather_than_reporting_an_empty_transcript(self):
        self._logged_out()
        with self.assertRaises(videokit.LoginRequired) as ctx:
            videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")
        self.assertIn("login", str(ctx.exception).lower())

    def test_the_message_names_the_re_login_entry_point(self):
        self._logged_out()
        with self.assertRaises(videokit.LoginRequired) as ctx:
            videokit.probe("u")
        self.assertIn("videokit.py login", str(ctx.exception))

    def test_the_message_does_not_claim_the_login_expired(self):
        # Expired and never-logged-in produce identical output, so naming a
        # cause would be a guess dressed as a finding.
        self._logged_out()
        with self.assertRaises(videokit.LoginRequired) as ctx:
            videokit.probe("u")
        self.assertNotIn("expired", str(ctx.exception).lower())

    def test_the_message_does_not_paste_the_raw_yt_dlp_warning(self):
        self._logged_out()
        with self.assertRaises(videokit.LoginRequired) as ctx:
            videokit.probe("u")
        self.assertNotIn("WARNING: [BiliBili]", str(ctx.exception))

    def test_a_valid_login_that_finds_no_subtitles_is_not_a_login_problem(self):
        # Same empty table, no warning. This video simply has no subtitles, so
        # it must come back as an ordinary Video rather than as an error.
        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(VALID_NO_SUBS, "")

        videokit._run = fake_run
        video = videokit.probe("u")
        self.assertEqual(len(video.parts), 30)

    def test_the_zero_signal_still_holds_for_a_real_course(self):
        # 21 of the 30 parts have subtitles, so a valid login is judged valid
        # even though 9 parts come back empty.
        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(MULTI_SUBS, "")

        videokit._run = fake_run
        video = videokit.probe("u")
        self.assertEqual(sum(1 for p in video.parts if p.has_subtitles), 21)


class NetscapeSerialization(unittest.TestCase):
    def test_reproduces_what_yt_dlp_writes_for_the_same_session(self):
        # Expected bytes come from yt-dlp's own cookie exporter, run against
        # the same browser session. It is the program that reads this file, so
        # its format wins over anything we would invent.
        text = videokit.netscape_text(CDP_COOKIES)
        self.assertEqual(rows(text), NETSCAPE_REFERENCE)


class CredentialFile(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_writes_the_netscape_format_yt_dlp_reads(self):
        target = self.tmp / "cookies.txt"
        videokit.save_cookies(CDP_COOKIES, target)
        self.assertEqual(rows(target.read_text(encoding="utf-8")), NETSCAPE_REFERENCE)

    def test_other_accounts_on_the_machine_cannot_read_it(self):
        # The file holds a live session token, and the directory holds the
        # browser profile that yields it. Either one readable by another
        # account hands over the login.
        target = self.tmp / "cookies.txt"
        videokit.save_cookies(CDP_COOKIES, target)
        for path in (target, target.parent):
            with self.subTest(path=path.name):
                out = subprocess.run(
                    ["icacls", str(path)], capture_output=True, text=True
                ).stdout
                # Inherited entries name SYSTEM and the Administrators group.
                # Both must be gone, not merely accompanied by our own grant.
                self.assertNotIn("NT AUTHORITY", out)
                self.assertNotIn("BUILTIN", out)
                self.assertIn(os.environ["USERNAME"], out)

    def test_reads_the_credential_file_back(self):
        target = self.tmp / "cookies.txt"
        videokit.save_cookies(CDP_COOKIES, target)
        self.assertTrue(videokit.credentials_usable(target))

    def test_credential_lives_outside_the_repository(self):
        # A credential file inside the working tree depends on every future
        # contributor noticing it. Outside the tree it cannot be committed.
        repo = Path(__file__).resolve().parents[3]
        self.assertFalse(videokit.default_data_dir().is_relative_to(repo))

    def test_finds_saved_credentials_without_being_told_where(self):
        os.environ["VIDEOKIT_DATA_DIR"] = str(self.tmp)
        self.addCleanup(os.environ.pop, "VIDEOKIT_DATA_DIR", None)
        self.assertIsNone(videokit.default_cookies())
        videokit.save_cookies(CDP_COOKIES, videokit.credentials_path())
        self.assertEqual(videokit.default_cookies(), self.tmp / "cookies.txt")

    def test_no_credentials_returns_none_rather_than_raising(self):
        os.environ["VIDEOKIT_DATA_DIR"] = str(self.tmp)
        self.addCleanup(os.environ.pop, "VIDEOKIT_DATA_DIR", None)
        self.assertIsNone(videokit.default_cookies())


class DefaultCredentials(unittest.TestCase):
    """After one login, the three entry points find the credential on their own.

    `subprocess.run` is stubbed rather than `_run`, so the command the module
    actually builds is the thing under test. A test that stubs `_run` cannot
    see whether `--cookies` was passed at all.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        os.environ["VIDEOKIT_DATA_DIR"] = str(self.tmp)
        self.addCleanup(os.environ.pop, "VIDEOKIT_DATA_DIR", None)

        self.commands: list[list[str]] = []
        original = subprocess.run

        def fake_run(cmd, **kwargs):
            self.commands.append(list(cmd))

            class R:
                returncode = 0
                # probe makes two calls and needs a real answer for each.
                stdout = MULTI_DUMP if "--dump-single-json" in cmd else MULTI_SUBS
                stderr = ""

            return R()

        subprocess.run = fake_run
        self.addCleanup(setattr, subprocess, "run", original)
        self.out = self.tmp / "out"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _cookies_used(self) -> set[str]:
        """Every value passed as --cookies across the recorded commands."""
        used = set()
        for cmd in self.commands:
            while "--cookies" in cmd:
                idx = cmd.index("--cookies")
                used.add(cmd[idx + 1])
                cmd = cmd[idx + 2 :]
        return used

    def test_probe_uses_the_stored_credential_without_being_asked(self):
        videokit.save_cookies(CDP_COOKIES, videokit.credentials_path())
        video = videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")
        self.assertEqual(len(video.parts), 30)
        self.assertEqual(
            self._cookies_used(), {str(videokit.credentials_path())}
        )

    def test_both_downloads_use_the_stored_credential(self):
        videokit.save_cookies(CDP_COOKIES, videokit.credentials_path())
        videokit.download_audio("u", self.out, parts=[1])
        videokit.download_video("u", self.out, parts=[1])
        self.assertEqual(
            self._cookies_used(), {str(videokit.credentials_path())}
        )

    def test_an_explicit_argument_overrides_the_stored_credential(self):
        videokit.save_cookies(CDP_COOKIES, videokit.credentials_path())
        other = self.tmp / "other.txt"
        videokit.save_cookies(CDP_COOKIES, other)
        videokit.download_audio("u", self.out, cookies=other)
        self.assertEqual(self._cookies_used(), {str(other)})

    def test_no_stored_credential_means_an_anonymous_call(self):
        # Never having logged in is a normal state. It must not raise, and it
        # must not point yt-dlp at a file that does not exist.
        videokit.probe("u")
        self.assertEqual(self._cookies_used(), set())


class GitignoreCoverage(unittest.TestCase):
    """The credential cannot be committed, by construction rather than memory.

    Two independent guards are checked, because either alone is a single point
    of failure: the path is outside the repository, and the name is ignored if
    someone copies it in anyway.
    """

    def setUp(self):
        self.repo = Path(__file__).resolve().parents[3]
        self.gitignore = (self.repo / ".gitignore").read_text(encoding="utf-8")

    def test_credential_lives_outside_the_working_tree(self):
        self.assertFalse(videokit.default_data_dir().is_relative_to(self.repo))

    def test_a_copied_credential_would_still_be_ignored(self):
        copied = self.repo / "cookies.txt"
        proc = subprocess.run(
            ["git", "check-ignore", "-v", str(copied)],
            cwd=self.repo,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("cookies", proc.stdout)

    def test_a_copied_browser_profile_would_still_be_ignored(self):
        # The profile holds the same login the cookie file does, so copying the
        # data directory into the repo must not be committable either. Checked
        # as a directory and as the cookie store inside it, because a pattern
        # that catches one need not catch the other.
        for copied in (
            f"{videokit.PROFILE_DIRNAME}/",
            f"{videokit.PROFILE_DIRNAME}/Default/Network/Cookies",
        ):
            with self.subTest(path=copied):
                proc = subprocess.run(
                    ["git", "check-ignore", "-v", copied],
                    cwd=self.repo,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, f"{copied} not ignored")
                self.assertIn(videokit.PROFILE_DIRNAME, proc.stdout)


class FakeSocket:
    """A stand-in for the CDP WebSocket that records what was sent."""

    def __init__(self, replies: list[dict]):
        self.sent: list[dict] = []
        self._replies = list(replies)

    def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def recv(self) -> str:
        return json.dumps(self._replies.pop(0))

    def close(self) -> None:
        pass


class CdpProtocol(unittest.TestCase):
    """The two CDP details that decide whether any cookie comes back at all.

    Both were established by measurement: attaching at browser level, or
    skipping `Network.enable`, yields zero cookies with no error, which is
    indistinguishable from an expired login.
    """

    def test_enables_the_network_domain_before_asking_for_cookies(self):
        ws = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"cookies": CDP_COOKIES}},
            ]
        )
        videokit.cdp_cookies(ws)
        self.assertEqual(
            [m["method"] for m in ws.sent],
            ["Network.enable", "Network.getAllCookies"],
        )

    def test_returns_the_cookies_the_browser_reported(self):
        ws = FakeSocket(
            [
                {"id": 1, "result": {}},
                {"id": 2, "result": {"cookies": CDP_COOKIES}},
            ]
        )
        self.assertEqual(videokit.cdp_cookies(ws), CDP_COOKIES)

    def test_skips_events_that_are_not_the_reply(self):
        # Network.enable floods the socket with events before the reply. A
        # reader that treats the first frame as the answer gets nothing.
        ws = FakeSocket(
            [
                {"method": "Network.requestWillBeSent", "params": {}},
                {"id": 1, "result": {}},
                {"method": "Network.loadingFinished", "params": {}},
                {"id": 2, "result": {"cookies": CDP_COOKIES}},
            ]
        )
        self.assertEqual(len(videokit.cdp_cookies(ws)), len(CDP_COOKIES))


class TargetSelection(unittest.TestCase):
    def test_chooses_the_bilibili_page_from_a_real_target_list(self):
        # Recorded from Chrome. The list holds non-page targets and a page for
        # another site, so picking index 0 is not the same as picking right.
        chosen = videokit.select_page_target(CDP_TARGETS)
        self.assertIn("bilibili", chosen["url"])
        self.assertEqual(chosen["type"], "page")
        self.assertTrue(chosen["webSocketDebuggerUrl"])

    def test_ignores_non_page_targets(self):
        only_service_workers = [t for t in CDP_TARGETS if t["type"] != "page"]
        with self.assertRaises(videokit.LoginError) as ctx:
            videokit.select_page_target(only_service_workers)
        self.assertIn("page", str(ctx.exception))

    def test_spots_a_missing_session_cookie(self):
        # Chrome answers for an anonymous profile too, so a non-empty cookie
        # list is not proof of a login.
        session = next(c for c in CDP_COOKIES if c["name"] == "SESSDATA")
        anonymous = [c for c in CDP_COOKIES if c["name"] != "SESSDATA"]
        self.assertTrue(videokit.has_login(CDP_COOKIES))
        self.assertFalse(videokit.has_login(anonymous))
        self.assertTrue(videokit.has_login(anonymous + [session]))


class BrowserLaunch(unittest.TestCase):
    def test_startup_flags_allow_the_cdp_handshake(self):
        # Chrome 111 rejects a CDP WebSocket that carries an Origin header.
        # Without this flag the login fails at the handshake with a 403, which
        # reads as a network problem rather than a missing flag.
        argv = videokit.chrome_argv(
            Path("chrome.exe"), Path("C:/data/profile"), 9223, "https://www.bilibili.com/"
        )
        self.assertIn("--remote-allow-origins=*", argv)

    def test_startup_opens_remote_debugging_on_a_reused_profile(self):
        # The profile is what makes a second run skip the scan. A fresh
        # directory each time would force a QR scan on every invocation.
        argv = videokit.chrome_argv(
            Path("chrome.exe"), Path("C:/data/profile"), 9223, "https://www.bilibili.com/"
        )
        self.assertIn("--remote-debugging-port=9223", argv)
        self.assertIn("--user-data-dir=C:\\data\\profile", argv)
        self.assertEqual(argv[-1], "https://www.bilibili.com/")


class GuidedLogin(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        os.environ["VIDEOKIT_DATA_DIR"] = str(self.tmp)
        self.addCleanup(os.environ.pop, "VIDEOKIT_DATA_DIR", None)

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _patch(self, name, value):
        original = getattr(videokit, name)
        setattr(videokit, name, value)
        self.addCleanup(setattr, videokit, name, original)

    def _browser(self, cookies, targets=None):
        """Stub the three things that need a real machine and a real Chrome."""
        self._patch("_launch_browser", lambda argv: object())
        self._patch(
            "_fetch_targets",
            lambda port: targets if targets is not None else CDP_TARGETS,
        )
        self._patch(
            "_connect",
            lambda url: FakeSocket(
                [
                    {"id": 1, "result": {}},  # Network.enable
                    {"id": 2, "result": {"cookies": cookies}},
                ]
            ),
        )
        self._patch("_stop_browser", lambda proc: None)

    def test_writes_the_credential_after_a_scan(self):
        self._browser(CDP_COOKIES)
        path = videokit.login()
        self.assertEqual(path, videokit.credentials_path())
        self.assertEqual(rows(path.read_text(encoding="utf-8")), NETSCAPE_REFERENCE)
        self.assertTrue(videokit.has_login(CDP_COOKIES))

    def test_a_second_run_reuses_the_stored_credential(self):
        self._browser(CDP_COOKIES)
        first = videokit.login()
        called = []
        self._patch("_launch_browser", lambda argv: called.append(argv))
        self.assertEqual(videokit.reuse_or_login(), first)
        self.assertEqual(called, [], "a stored credential must not open a browser")

    def test_reports_the_scan_step_when_no_login_was_completed(self):
        # Chrome answers for an anonymous profile, so this is the silent case:
        # cookies come back, none of them a session. The message has to name
        # the step and give the user something to do about it.
        self._browser([c for c in CDP_COOKIES if c["name"] != "SESSDATA"])
        with self.assertRaises(videokit.LoginError) as ctx:
            videokit.login(timeout_sec=0)
        message = str(ctx.exception)
        self.assertIn("login", message)
        self.assertIn("Scan", message)

    def test_reports_the_launch_step_when_chrome_is_absent(self):
        with self.assertRaises(videokit.LoginError) as ctx:
            videokit.login(chrome=Path("C:/nope/chrome.exe"))
        self.assertIn("chrome.exe", str(ctx.exception))

    def test_reports_the_attach_step_when_no_page_target_appears(self):
        self._browser(CDP_COOKIES, targets=[{"type": "service_worker", "url": "x"}])
        with self.assertRaises(videokit.LoginError) as ctx:
            videokit.login(timeout_sec=0)
        self.assertIn("page", str(ctx.exception))

    def test_reports_the_discovery_step_when_browser_is_unreachable(self):
        self._patch("_launch_browser", lambda argv: object())
        self._patch("_stop_browser", lambda proc: None)
        # Port 1 is reserved and cannot be listening, so this is a real refusal
        # rather than whatever happens to be running on the default port.
        self._patch("_debug_port", lambda: 1)
        with self.assertRaises(videokit.LoginError) as ctx:
            videokit.login(timeout_sec=0)
        self.assertIn("could not reach the browser", str(ctx.exception))


class LoginCommand(unittest.TestCase):
    """The one command the user runs to get a credential.

    Without an entry point the guided login is only reachable from Python, and
    the ticket asks for a command. Ticket #20 owns the general CLI; this is the
    single subcommand that belongs to the credential work.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        os.environ["VIDEOKIT_DATA_DIR"] = str(self.tmp)
        self.addCleanup(os.environ.pop, "VIDEOKIT_DATA_DIR", None)

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_login_subcommand_stores_a_credential_and_reports_where(self):
        called = []

        def fake_login(**kwargs):
            path = videokit.credentials_path()
            videokit.save_cookies(CDP_COOKIES, path)
            called.append(kwargs)
            return path

        original = videokit.login
        videokit.login = fake_login
        self.addCleanup(setattr, videokit, "login", original)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = videokit.main(["login"])

        self.assertEqual(code, 0)
        self.assertEqual(len(called), 1)
        self.assertIn(str(videokit.credentials_path()), buffer.getvalue())

    def test_an_unknown_subcommand_is_refused(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = videokit.main(["frobnicate"])
        self.assertNotEqual(code, 0)
        self.assertIn("login", buffer.getvalue())

    def test_a_failed_login_exits_nonzero_with_the_step_named(self):
        def failing_login(**kwargs):
            raise videokit.LoginError("chrome.exe not found at C:/nope")

        original = videokit.login
        videokit.login = failing_login
        self.addCleanup(setattr, videokit, "login", original)

        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = videokit.main(["login"])
        self.assertNotEqual(code, 0)
        self.assertIn("chrome.exe not found", buffer.getvalue())


class ParseSrt(unittest.TestCase):
    """Parsing real platform subtitle bytes into timed segments.

    Expected values are read off the fixture by hand and written here as
    literals, so a parser that quietly drops or invents segments fails. The
    counts match the per-file figures measured across all 21 real tracks.
    """

    def test_yields_one_segment_per_block(self):
        segments = videokit.parse_srt(SRT_P12)
        self.assertEqual(len(segments), 55)

    def test_second_fixture_yields_its_own_block_count(self):
        # A second file, different length, so a parser cannot pass by
        # returning a constant.
        self.assertEqual(len(videokit.parse_srt(SRT_P18)), 9)

    def test_parses_the_first_segment_exactly(self):
        first = videokit.parse_srt(SRT_P12)[0]
        self.assertEqual(first.start_sec, 835.719)
        self.assertEqual(first.end_sec, 837.859)
        self.assertEqual(first.text, "SDR是special drawing rights啊")

    def test_parses_the_last_segment_exactly(self):
        last = videokit.parse_srt(SRT_P12)[-1]
        self.assertEqual(last.start_sec, 956.969)
        self.assertEqual(last.end_sec, 958.649)
        self.assertEqual(last.text, "也不可能啊")

    def test_text_carries_no_timestamp(self):
        # The two are separate fields because the two transcript sources have
        # different timestamp granularity; mixed together, downstream cannot
        # treat them alike.
        for segment in videokit.parse_srt(SRT_P12):
            with self.subTest(text=segment.text):
                self.assertNotIn("-->", segment.text)
                self.assertNotIn(":", segment.text[:12])

    def test_segments_are_ordered_by_start_time(self):
        starts = [s.start_sec for s in videokit.parse_srt(SRT_P12)]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), len(starts))

    def test_keeps_adjacent_duplicate_segments(self):
        # Segments 418 and 419 both read 也不可能啊. They are two different
        # stretches of speech. Dropping one loses a second of real content, so
        # the parser must not deduplicate.
        texts = [s.text for s in videokit.parse_srt(SRT_P12)]
        run = [t for t in texts[52:] if t == "也不可能啊"]
        self.assertEqual(len(run), 2)

    def test_keeps_text_that_is_only_digits(self):
        # A digit-only line must not be mistaken for an index line. p18 has
        # five such segments in a row.
        segments = videokit.parse_srt(SRT_P18)
        self.assertIn("60616263646566676869", [s.text for s in segments])
        self.assertIn("38485868687888990", [s.text for s in segments])

    def test_reads_milliseconds_that_are_not_multiples_of_ten(self):
        # Real tracks end on values like 01:48:24,749 and 01:16:30,999. A parser
        # that assumes a rounded millisecond truncates them.
        self.assertEqual(videokit.parse_srt(SRT_P18)[0].start_sec, 864.58)

    def test_handles_segments_that_abut_exactly(self):
        # 46 of the 54 boundaries in p12 are zero-gap. The end of one segment
        # is the start of the next, with nothing between.
        segments = videokit.parse_srt(SRT_P12)
        second = segments[1]
        self.assertEqual(second.start_sec, segments[0].end_sec)

    def test_keeps_a_segment_as_short_as_a_third_of_a_second(self):
        shortest = min(s.end_sec - s.start_sec for s in videokit.parse_srt(SRT_P12))
        self.assertAlmostEqual(shortest, 0.32, places=6)

    def test_empty_input_yields_an_empty_list(self):
        # Not the same outcome as a missing track. See SubtitleRetrieval.
        self.assertEqual(videokit.parse_srt(""), [])

    def test_text_is_not_a_container_for_the_index(self):
        # The index line is not part of the transcript.
        self.assertNotIn("419", [s.text for s in videokit.parse_srt(SRT_P12)])


class SubtitleRetrieval(unittest.TestCase):
    """Fetching a part's track through the one external seam.

    `_run` is what reaches yt-dlp, so the fake goes there. The distinction the
    ticket asks for lives in this class: no track raises, an empty parse
    returns an empty list.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run

        def fake_run(args, cookies):
            self.calls.append(args)
            return videokit.RunOutput(SUB_PRINT_PART, "")

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", self.original)

    def test_returns_the_segments_of_the_requested_part(self):
        segments = videokit.part_subtitles("https://www.bilibili.com/video/BV1Y5QgB5Eas", 18)
        self.assertEqual(len(segments), 1078)
        self.assertEqual(segments[0].text, "呃现在我们来进入到这个货币经济")
        self.assertEqual(segments[-1].text, "我们就讲到这儿下课")

    def test_segments_are_ordered_and_free_of_timestamps(self):
        segments = videokit.part_subtitles("u", 18)
        starts = [s.start_sec for s in segments]
        self.assertEqual(starts, sorted(starts))

    def test_asks_for_the_part_and_its_language(self):
        videokit.part_subtitles("https://www.bilibili.com/video/BV1Y5QgB5Eas", 12)
        args = self.calls[-1]
        self.assertIn("--write-subs", args)
        self.assertIn("--sub-langs", args)
        self.assertEqual(args[args.index("--sub-langs") + 1], "ai-zh")
        self.assertEqual(args[args.index("--playlist-items") + 1], "12")

    def test_reads_the_track_from_the_printed_payload(self):
        # yt-dlp writes subtitles to a file, and the file name is not
        # predictable from the template. The payload on stdout carries the
        # track text itself, so there is no file to find afterwards.
        videokit.part_subtitles("u", 12)
        args = self.calls[-1]
        idx = args.index("--print")
        self.assertEqual(args[idx + 1], "%(requested_subtitles)j")

    def test_a_language_other_than_ai_zh_can_be_named(self):
        # The language is a parameter, not a constant. A payload for another
        # track must be read back, not rejected because it is not `ai-zh`.
        def other_language(args, cookies):
            self.calls.append(args)
            return videokit.RunOutput(
                json.dumps(
                    {
                        "zh-CN": {
                            "ext": "srt",
                            "data": (
                                "1\n00:00:01,000 --> 00:00:02,500\n人工字幕\n\n"
                                "2\n00:00:02,500 --> 00:00:04,000\n第二行\n\n"
                            ),
                        }
                    }
                ),
                "",
            )

        videokit._run = other_language
        segments = videokit.part_subtitles("u", 12, language="zh-CN")
        self.assertEqual([s.text for s in segments], ["人工字幕", "第二行"])
        args = self.calls[-1]
        self.assertEqual(args[args.index("--sub-langs") + 1], "zh-CN")

    def test_a_part_without_a_track_raises_rather_than_returning_empty(self):
        # The failure mode this ticket exists to prevent. A caller told "an
        # empty list" would write an empty transcript and never notice.
        def no_track(args, cookies):
            self.calls.append(args)
            return videokit.RunOutput(SUB_PRINT_NO_TRACK, "")

        videokit._run = no_track
        with self.assertRaises(videokit.SubtitleUnavailable) as ctx:
            videokit.part_subtitles("u", 1)
        self.assertIn("1", str(ctx.exception))

    def test_the_two_outcomes_are_not_easily_confused(self):
        # Same part, same call shape. One outcome raises, the other returns a
        # list. Asserting both here keeps them from being conflated later.
        def no_track(args, cookies):
            return videokit.RunOutput(SUB_PRINT_NO_TRACK, "")

        videokit._run = no_track
        raised = False
        try:
            videokit.part_subtitles("u", 1)
        except videokit.SubtitleUnavailable:
            raised = True
        self.assertTrue(raised)
        self.assertIsInstance(videokit.parse_srt(""), list)

    def test_a_track_that_parses_to_nothing_is_also_reported_as_missing(self):
        # A payload carrying an empty track is not a transcript. Returning []
        # here would put an empty transcript into the pipeline.
        def empty_track(args, cookies):
            return videokit.RunOutput(
                json.dumps({"ai-zh": {"ext": "srt", "data": ""}}), ""
            )

        videokit._run = empty_track
        with self.assertRaises(videokit.SubtitleUnavailable):
            videokit.part_subtitles("u", 12)

    def test_danmaku_is_not_treated_as_a_subtitle_track(self):
        # A danmaku-only payload carries no transcribable track, so it means
        # the same thing as no track at all.
        def danmaku_only(args, cookies):
            return videokit.RunOutput(
                json.dumps({"danmaku": {"ext": "xml", "url": "https://x"}}), ""
            )

        videokit._run = danmaku_only
        with self.assertRaises(videokit.SubtitleUnavailable):
            videokit.part_subtitles("u", 12)

    def test_a_logged_out_run_is_not_reported_as_a_missing_track(self):
        # Both outcomes look like "no payload" on stdout. With the login
        # warning on stderr this is the credential's fault, not the part's, and
        # the two send the caller to different remedies: one re-logs in, the
        # other queues speech recognition.
        def logged_out(args, cookies):
            self.calls.append(args)
            return videokit.RunOutput(SUB_PRINT_NO_TRACK, LOGIN_WARNING)

        videokit._run = logged_out
        with self.assertRaises(videokit.LoginRequired) as ctx:
            videokit.part_subtitles("u", 3)
        self.assertIn("videokit.py login", str(ctx.exception))

    def test_the_track_data_is_not_read_from_the_filesystem(self):
        # The fixture is a payload, not a directory. A retrieval path that
        # relied on a written file could not work from these bytes.
        segments = videokit.part_subtitles("u", 18)
        self.assertTrue(all(isinstance(s, videokit.Segment) for s in segments))


# Which parts of BV1Y5QgB5Eas carry an `ai-zh` track, read off the recorded
# `--list-subs` output. The nine without one are interleaved among the rest and
# two of them lead the course, so a batch that stopped on the first would
# produce no file at all.
PARTS_WITH_SUBTITLES = (3, 4, 5, 10, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 24, 25, 26, 27, 29, 30)
PARTS_WITHOUT_SUBTITLES = (1, 2, 6, 7, 8, 9, 13, 23, 28)

# The fields of `PartTranscript`, written out as a literal. Both sources must
# produce exactly these, so "the same structure" is asserted against a known
# list rather than against the other record, which would pass by construction.
MODEL_FIELDS = (
    "bvid", "duration_sec", "failure_reason", "index",
    "language", "segments", "source", "status", "title",
)


def segment(text: str) -> videokit.Segment:
    """A segment carrying only the text under test."""
    return videokit.Segment(start_sec=0.0, end_sec=1.0, text=text)


def stub_run(payload: str, stderr: str = ""):
    """A stubbed `_run` result carrying one recorded payload.

    `_run` returns both streams, because yt-dlp writes its login warning to
    stderr and still exits 0, so a stub returning a bare string would hide the
    signal that tells "not logged in" apart from "this video has no
    subtitles". The subtitle payloads were recorded with a working login and
    carry no warning, so stderr defaults to empty and only the login tests
    pass one.
    """
    return videokit.RunOutput(stdout=payload, stderr=stderr)


class TranscriptText(unittest.TestCase):
    """Turning timed segments into the continuous prose that gets delivered.

    The separator rule is measured, not chosen. Over all 21 recorded tracks,
    40283 segments produce 40262 boundaries and exactly 55 of them carry an
    ASCII alphanumeric character on both sides. Those 55 are where a bare
    `"".join` welds two words together, measured example `very good` followed
    by `Good` becoming `very goodGood`. The other 40207 boundaries carry no
    separator in the source, and 38994 of them sit between two Chinese
    characters, so `" ".join` would inject 38994 spaces that are not there.
    """

    def test_empty_input_yields_empty_text(self):
        self.assertEqual(videokit.transcript_text([]), "")

    def test_a_single_segment_is_its_own_text(self):
        self.assertEqual(videokit.transcript_text([segment("呃现在我们来")]), "呃现在我们来")

    def test_two_chinese_segments_are_joined_with_nothing(self):
        # 38994 of the 40262 real boundaries look like this. A space here is
        # not in the source text.
        joined = videokit.transcript_text([segment("政治阻力极大啊"), segment("也不可能啊")])
        self.assertEqual(joined, "政治阻力极大啊也不可能啊")

    def test_two_ascii_words_get_one_half_width_space(self):
        # Measured on p03 segments 2012 and 2013: `very good` followed by
        # `Good` welds into `very goodGood` without one.
        joined = videokit.transcript_text([segment("然后呢回答就very good"), segment("Good")])
        self.assertEqual(joined, "然后呢回答就very good Good")

    def test_digits_on_both_sides_get_one_space(self):
        joined = videokit.transcript_text(
            [segment("60616263646566676869"), segment("70707172737475767778")]
        )
        self.assertEqual(joined, "60616263646566676869 70707172737475767778")

    def test_a_chinese_character_on_either_side_gets_no_space(self):
        # 572 real boundaries run Chinese into ASCII and 324 run ASCII into
        # Chinese. Neither takes a separator.
        self.assertEqual(
            videokit.transcript_text([segment("含金量定为0"), segment("克黄金啊")]),
            "含金量定为0克黄金啊",
        )
        self.assertEqual(
            videokit.transcript_text([segment("叫in the long on"), segment("斯有句名言")]),
            "叫in the long on斯有句名言",
        )

    def test_a_punctuation_side_gets_no_space(self):
        # Measured: `农业占比7%` followed by `然后呢这个第二就是第一产` reads
        # correctly as one run. 315 real segments end in `%`.
        self.assertEqual(
            videokit.transcript_text([segment("农业占比7%"), segment("然后呢这个第二就是第一产")]),
            "农业占比7%然后呢这个第二就是第一产",
        )

    def test_the_real_p12_slice_has_no_inserted_space(self):
        # 55 real segments, 608 text characters, and every boundary carries a
        # Chinese character on at least one side, so the output is the bare
        # concatenation.
        segments = videokit.parse_srt(SRT_P12)
        joined = videokit.transcript_text(segments)
        self.assertEqual(len(joined), 608)
        self.assertEqual(joined, "".join(s.text for s in segments))

    def test_the_real_p18_slice_gets_exactly_four_spaces(self):
        # 9 real segments, 103 text characters, 4 boundaries with digits on
        # both sides. 103 + 4 = 107.
        joined = videokit.transcript_text(videokit.parse_srt(SRT_P18))
        self.assertEqual(len(joined), 107)
        self.assertIn(
            "60616263646566676869 70707172737475767778 79808 1828 38485868687888990",
            joined,
        )

    def test_keeps_adjacent_duplicate_segments(self):
        # p12 segments 418 and 419 both read 也不可能啊. They are two different
        # stretches of speech; deduplicating would drop a second of content.
        joined = videokit.transcript_text(videokit.parse_srt(SRT_P12))
        self.assertIn("也不可能啊也不可能啊", joined)

    def test_the_delivered_text_carries_no_timestamp(self):
        joined = videokit.transcript_text(videokit.parse_srt(SRT_P12))
        self.assertNotIn("-->", joined)
        self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", joined))
        self.assertNotIn("835.719", joined)


class PartJudgment(unittest.TestCase):
    """Judging one part, per part rather than per video.

    Each part is asked separately because the real course mixes the two states:
    21 parts carry a track, 9 do not, and the state is uncorrelated with
    length. A part with no track is recorded as awaiting ASR. That is not an
    error, so nothing here raises.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.audio = self.tmp / "audio"
        # The ASR engine is the second seam. It is injected here because a part
        # with no track now completes itself from its audio, and the real
        # engine costs 29 to 50 seconds to load.
        self.engine = _Engine({"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS})

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run(str(self.audio / f"{part:02d}.wav"))
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", self.original)
        self.video = videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _part(self, index: int) -> videokit.Part:
        return next(p for p in self.video.parts if p.index == index)

    def _transcript(self, index: int) -> videokit.PartTranscript:
        """One part through the real judgment, with the audio step stubbed."""
        return videokit.part_transcript(
            self.video, self._part(index), engine=self.engine, audio_dir=self.audio
        )

    def test_a_part_with_a_track_is_assembled_from_its_segments(self):
        record = videokit.part_transcript(self.video, self._part(18))
        self.assertEqual(record.bvid, "BV1Y5QgB5Eas")
        self.assertEqual(record.index, 18)
        self.assertEqual(record.source, "platform")
        self.assertEqual(record.language, "ai-zh")
        self.assertEqual(record.status, "ok")
        self.assertIsNone(record.failure_reason)
        self.assertEqual(len(record.segments), 1078)
        self.assertEqual(record.segments[0].text, "呃现在我们来进入到这个货币经济")

    def test_the_record_carries_the_probe_metadata(self):
        record = videokit.part_transcript(self.video, self._part(3))
        self.assertEqual(record.index, 3)
        self.assertIn("宏观经济数据和宏观逻辑的逻辑", record.title)
        self.assertAlmostEqual(record.duration_sec, 5338.569, places=3)

    def test_the_requested_language_is_the_one_the_probe_reported(self):
        videokit.part_transcript(self.video, self._part(12))
        args = self.calls[-1]
        self.assertEqual(args[args.index("--sub-langs") + 1], "ai-zh")
        self.assertEqual(args[args.index("--playlist-items") + 1], "12")

    def test_the_fetch_gets_a_url_and_not_the_bare_bv_number(self):
        # `Video.id` is `BV1Y5QgB5Eas`. Handing that to yt-dlp in the URL
        # position fails with `'BV1Y5QgB5Eas' is not a valid URL`, measured
        # against the real program. A stub that answers whatever it is asked
        # cannot see this, so the argument itself is asserted.
        videokit.part_transcript(self.video, self._part(12))
        self.assertEqual(
            self.calls[-1][-1], "https://www.bilibili.com/video/BV1Y5QgB5Eas"
        )

    def test_a_part_without_a_track_is_completed_by_asr(self):
        # The join this ticket exists for. A part the probe says has no track
        # is no longer left waiting; it is transcribed from its own audio in
        # the same call, with no second command.
        record = self._transcript(1)
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.source, "asr")
        self.assertEqual(record.language, "zh")
        self.assertEqual(record.index, 1)
        self.assertIsNone(record.failure_reason)
        self.assertEqual(len(record.segments), ASR_SEGMENTS)

    def test_both_sources_land_in_the_same_structure(self):
        # Only `source` differs. Every other field is filled either way, so a
        # caller cannot tell the two apart by shape.
        platform = videokit.part_transcript(self.video, self._part(3))
        asr = self._transcript(6)
        self.assertEqual(platform.source, "platform")
        self.assertEqual(asr.source, "asr")
        for record in (platform, asr):
            with self.subTest(source=record.source):
                self.assertIsInstance(record, videokit.PartTranscript)
                self.assertEqual(record.bvid, "BV1Y5QgB5Eas")
                self.assertEqual(record.status, "ok")
                self.assertIsNone(record.failure_reason)
                self.assertTrue(record.segments)
                self.assertTrue(all(isinstance(s, videokit.Segment) for s in record.segments))
                self.assertTrue(record.title)
                self.assertGreater(record.duration_sec, 0)
        # The field sets are identical, which is what "same structure" means.
        # Asserted against a literal rather than against the sibling record, so
        # a field added to the dataclass fails here instead of passing by
        # construction.
        self.assertEqual(tuple(sorted(platform.__dataclass_fields__)), MODEL_FIELDS)
        self.assertEqual(tuple(sorted(asr.__dataclass_fields__)), MODEL_FIELDS)

    def test_the_two_sources_carry_the_same_text_shape(self):
        # Both paths deliver prose with no timestamp in it. The ASR side has
        # punctuation and the platform side does not, and that difference is
        # accepted rather than repaired.
        platform_text = videokit.transcript_text(
            videokit.part_transcript(self.video, self._part(3)).segments
        )
        asr_text = videokit.transcript_text(self._transcript(6).segments)
        for prose in (platform_text, asr_text):
            with self.subTest(head=prose[:12]):
                self.assertTrue(prose.strip())
                self.assertNotIn("-->", prose)
                self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", prose))
        self.assertEqual(re.search(r"[，。]", platform_text), None)
        self.assertIsNotNone(re.search(r"[，。]", asr_text))

    def test_the_audio_for_the_missing_part_is_downloaded_at_16k_mono(self):
        # Not re-downloaded by hand and not converted later: `download_audio`
        # is the step that produces the WAV the engine wants.
        self._transcript(28)
        audio_calls = [args for args in self.calls if "--audio-format" in args]
        self.assertEqual(len(audio_calls), 1)
        self.assertEqual(audio_calls[0][audio_calls[0].index("--playlist-items") + 1], "28")
        self.assertEqual(
            audio_calls[0][audio_calls[0].index("--postprocessor-args") + 1],
            "-ar 16000 -ac 1",
        )

    def test_no_audio_is_downloaded_for_a_part_that_has_a_track(self):
        self._transcript(18)
        self.assertEqual([args for args in self.calls if "--audio-format" in args], [])

    def test_the_engine_receives_the_downloaded_audio(self):
        self._transcript(2)
        self.assertEqual(len(self.engine.calls), 1)
        self.assertEqual(self.engine.calls[0]["input"], str(self.audio / "02.wav"))

    def test_an_engine_that_is_already_loaded_is_reused_rather_than_rebuilt(self):
        # Loading costs 29 to 50 seconds, so one batch loads once. The engine
        # arrives through the argument for exactly this reason.
        loads = []
        original = videokit.load_engine
        videokit.load_engine = lambda device="cpu": loads.append(device) or self.engine
        self.addCleanup(setattr, videokit, "load_engine", original)
        for index in (1, 2, 6):
            videokit.part_transcript(
                self.video, self._part(index), engine=self.engine, audio_dir=self.audio
            )
        self.assertEqual(loads, [])

    def test_a_part_with_a_track_never_reaches_the_engine(self):
        self._transcript(3)
        self.assertEqual(self.engine.calls, [])

    def test_a_part_without_a_track_does_not_raise(self):
        # An empty transcript written to disk looks finished, and a raised
        # error would abort the batch. Neither is what a part without a track
        # means, so the call returns a record instead.
        record = self._transcript(6)
        self.assertIsInstance(record, videokit.PartTranscript)

    def test_an_audio_file_already_on_disk_is_not_downloaded_again(self):
        # The nine audio files of the first target were fetched once and are
        # still there. Re-fetching them for a rerun would cost the download
        # time for files that already exist.
        marker = self.audio / "06.wav"
        self.audio.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"RIFF")
        before = len(self.calls)
        self._transcript(6)
        self.assertEqual([a for a in self.calls[before:] if "--audio-format" in a], [])

    def test_a_track_that_fails_to_arrive_is_also_completed_by_asr(self):
        # The probe table and the fetch can disagree, for instance if the
        # track was withdrawn between the two calls. The outcome is the same:
        # this part needs a transcript from somewhere, and ASR is that.
        def empty_track(args, cookies):
            self.calls.append(args)
            if "--audio-format" in args:
                return stub_run(str(self.audio / "12.wav"))
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = empty_track
        record = videokit.part_transcript(
            self.video, self._part(12), engine=self.engine, audio_dir=self.audio
        )
        self.assertEqual(record.status, "ok")
        self.assertEqual(record.source, "asr")

    def test_a_credential_problem_is_not_swallowed_into_a_transcript(self):
        # The two look alike from inside this method and are opposite in
        # remedy. A part with no track waits for ASR, which will always work.
        # A stale login makes every part read as trackless, so treating that
        # as "transcribe it" would hand the whole 30-part course to the ASR
        # path, and cost about 39 minutes, in response to a problem that
        # re-exporting the credential fixes. `probe` is what sees the warning
        # first, and it refuses there.
        def logged_out(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            return stub_run(LOGGED_OUT_SUBS, LOGIN_WARNING)

        videokit._run = logged_out
        with self.assertRaises(videokit.LoginRequired):
            videokit.transcripts("u", self.tmp / "transcripts")

    def test_a_stale_login_reaches_the_batch_from_the_fetch_too(self):
        # The probe table says part 12 has a track and the fetch disagrees
        # because the login went stale between the two calls. That is not a
        # part needing ASR either, so it must not be silently transcribed.
        def stale_on_fetch(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            raise videokit.LoginRequired("login required")

        videokit._run = stale_on_fetch
        with self.assertRaises(videokit.LoginRequired):
            videokit.part_transcript(self.video, self._part(12))


class DeliverableFiles(unittest.TestCase):
    """The file a reader opens, named so a directory listing reads in order."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _record(self, index: int, title: str, texts: list[str]) -> videokit.PartTranscript:
        return videokit.PartTranscript(
            bvid="BV1Y5QgB5Eas",
            index=index,
            title=title,
            duration_sec=100.0,
            source="platform",
            language="ai-zh",
            segments=tuple(segment(t) for t in texts),
            status="ok",
        )

    def test_the_name_carries_the_index_and_the_title(self):
        record = self._record(3, "03-宏观经济数据和宏观逻辑的逻辑-1", ["呃"])
        path = videokit.write_transcript(record, self.out)
        self.assertEqual(path.name, "03-03-宏观经济数据和宏观逻辑的逻辑-1.txt")

    def test_the_file_holds_the_continuous_prose(self):
        record = self._record(3, "t", ["政治阻力极大啊", "也不可能啊"])
        path = videokit.write_transcript(record, self.out)
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "政治阻力极大啊也不可能啊")

    def test_the_source_does_not_reach_the_file(self):
        # `source` serves the program, the delivered file serves the reader.
        # The accepted decision "use the subtitles as they are, no source
        # marking and no quality grading" is about this file, so a record that
        # keeps a source is not in tension with it.
        record = self._record(3, "t", ["呃现在我们来进入到这个货币经济"])
        delivered = videokit.write_transcript(record, self.out).read_text(encoding="utf-8")
        self.assertNotIn("platform", delivered)
        self.assertNotIn("asr", delivered)
        self.assertNotIn("source", delivered)

    def test_the_file_carries_no_timestamp(self):
        record = self._record(3, "t", ["呃现在我们来进入到这个货币经济"])
        delivered = videokit.write_transcript(record, self.out).read_text(encoding="utf-8")
        self.assertNotIn("-->", delivered)
        self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", delivered))
        self.assertNotIn("0.0", delivered)

    def test_the_output_directory_is_created_when_absent(self):
        self.assertFalse(self.out.exists())
        videokit.write_transcript(self._record(3, "t", ["呃"]), self.out)
        self.assertTrue(self.out.is_dir())

    def test_a_title_cannot_escape_the_output_directory(self):
        # A title is external data, so a separator in it must not silently
        # turn into a new directory. None of the 30 real titles carries one,
        # which is why this is a guard rather than a repair.
        record = self._record(3, "a/b", ["呃"])
        path = videokit.write_transcript(record, self.out)
        self.assertEqual(path.parent, self.out)


# Part 23's delivered artifact, byte for byte. The committed ASR fixture is
# that same file: 51185 bytes, 17123 characters, sha256
# d1f57fbeee2c9bdb0cafa79bdfb7da6497458e52da476e761e3227e22d2c5a7c, which is
# what `.wayfinder-scratch/e2e-17/transcripts/23-….txt` holds. Its probed
# duration is 3160.212 seconds, so a finished artifact of this course carries
# 5.418 characters per second of its part.
PART23_SECONDS = 3160.212
PART23_CHARS = 17123

# Part 30's probed duration, and the character count of its delivered artifact.
# It is the slowest part of the course at 4.239 characters per second, so it is
# the artifact that decides how low the completeness floor may sit.
PART30_SECONDS = 5761.01
PART30_CHARS = 24423

# Characters per second of part, measured over all 30 delivered artifacts of
# the #17 real run. The low end is 4.239 on part 30 and the high end is 5.418
# on part 23, a spread of 1.28 times. A previous run's artifact is judged
# against the duration a fresh probe reports, and this band is why the floor
# `transcript_complete` uses sits below 4.239 rather than at it.
DELIVERED_MIN_RATE = 4.239
DELIVERED_MAX_RATE = 5.418
DELIVERED_MEAN_RATE = 4.8


def delivered_text(duration_sec: float) -> str:
    """Text with the shape a finished run leaves for a part of this length.

    Built from the real part 23 transcript at the measured mean density rather
    than from hand-written filler, so a fixture artifact has the order of size
    a real run produces. Length is the property under test here.
    """
    target = int(DELIVERED_MEAN_RATE * duration_sec)
    repeats = -(-target // len(ASR_TEXT))
    return (ASR_TEXT * repeats)[:target]


class TranscriptCompleteness(unittest.TestCase):
    """Whether a delivered artifact is finished, decided by reading it.

    The delivered file is continuous UTF-8 text with no trailing newline and no
    end marker, so nothing inside it says "this is all of it". A file left by
    an interrupted write is therefore a clean prefix of the real thing, and the
    only signals available are that it decodes and that it is long enough for
    the part it belongs to. Both are measured in the tests below.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.artifact = self.tmp / "23-artifact.txt"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_a_real_delivered_artifact_is_complete(self):
        # The real file itself, not a sample of it, judged against the duration
        # the probe reports for that part.
        self.artifact.write_bytes(ASR_TEXT.encode("utf-8"))
        self.assertEqual(
            videokit.transcript_complete(self.artifact, PART23_SECONDS), True
        )

    def test_an_interrupted_write_is_not_complete(self):
        # Half of the real artifact, cut at a character boundary so it decodes
        # as clean UTF-8 and reads as a transcript. Measured: the first 25591
        # bytes are 8537 characters, or 2.701 characters per second of part 23,
        # against 4.239 to 5.418 for the 30 finished artifacts. Decoding alone
        # accepts this file, and accepting it loses 8586 characters.
        data = ASR_TEXT.encode("utf-8")
        half = data[:25591]
        self.assertEqual(len(half), 25591)  # A clean boundary, not a torn one.
        self.assertEqual(len(half.decode("utf-8")), 8537)
        self.artifact.write_bytes(half)
        self.assertEqual(
            videokit.transcript_complete(self.artifact, PART23_SECONDS), False
        )


    def test_a_torn_file_is_not_complete(self):
        # The other half of the 330 measured cuts: this one splits a three byte
        # character, so the file is not even valid text.
        data = ASR_TEXT.encode("utf-8")
        torn = data[:25592]
        with self.assertRaises(UnicodeDecodeError):
            torn.decode("utf-8")
        self.artifact.write_bytes(torn)
        self.assertEqual(
            videokit.transcript_complete(self.artifact, PART23_SECONDS), False
        )

    def test_an_absent_artifact_is_not_complete(self):
        self.assertEqual(
            videokit.transcript_complete(self.artifact, PART23_SECONDS), False
        )

    def test_an_empty_artifact_is_not_complete(self):
        self.artifact.write_bytes(b"")
        self.assertEqual(
            videokit.transcript_complete(self.artifact, PART23_SECONDS), False
        )

    def test_the_lowest_real_density_in_the_course_still_counts_as_complete(self):
        # Part 30 delivered 24423 characters over 5761.01 seconds, 4.239
        # characters per second, the lowest of the 30 artifacts. The floor has
        # to sit below this or a finished part is re-transcribed next run.
        slowest = (ASR_TEXT * 2)[:PART30_CHARS]
        self.assertEqual(len(slowest), 24423)
        self.assertEqual(round(len(slowest) / PART30_SECONDS, 3), 4.239)
        self.artifact.write_bytes(slowest.encode("utf-8"))
        self.assertEqual(
            videokit.transcript_complete(self.artifact, PART30_SECONDS), True
        )

    def test_a_zero_duration_part_is_still_judged_on_its_bytes(self):
        # A part the probe reports as zero seconds long has no duration to
        # measure against, so the length check alone would accept anything.
        # The decode check still applies, and that is what keeps a torn file
        # from passing when the duration is missing.
        self.artifact.write_bytes("政策是很重要的".encode("utf-8"))
        self.assertEqual(videokit.transcript_complete(self.artifact, 0.0), True)
        self.artifact.write_bytes(ASR_TEXT.encode("utf-8")[:25592])
        self.assertEqual(videokit.transcript_complete(self.artifact, 0.0), False)


class SkipExisting(unittest.TestCase):
    """A part whose artifact is already on disk is not processed again.

    The seam is the same one `BatchRun` uses: `_run` and the engine. A skipped
    part must produce no subtitle fetch, no audio download and no engine call,
    and the run must still probe, because the probe result is never persisted.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run
        self.original_engine = videokit.load_engine
        self.engine = _Engine({"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS})
        self.loads: list[str] = []

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run(str(self.audio / f"{part:02d}.wav"))
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = fake_run
        videokit.load_engine = lambda device="cpu": self.loads.append(device) or self.engine
        self.addCleanup(setattr, videokit, "_run", self.original)
        self.addCleanup(setattr, videokit, "load_engine", self.original_engine)
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"
        self.audio = self.tmp / "audio"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _titles(self) -> dict[int, str]:
        """Each part's title, read off the recorded probe dump."""
        dump = json.loads(MULTI_DUMP)
        entries = dump.get("entries") or [dump]
        return {e["playlist_index"] or 1: e["title"] for e in entries}

    def _place(self, index: int, text: str) -> Path:
        """Put finished text where the delivered artifact for a part belongs.

        The name is built here from the index and the title rather than by
        calling `transcript_filename`, so the test does not agree with the
        implementation by construction.
        """
        self.out.mkdir(parents=True, exist_ok=True)
        path = self.out / f"{index:02d}-{self._titles()[index]}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def _run_all(self, parts: list[int] | None = None, out: Path | None = None):
        return videokit.transcripts(
            "https://www.bilibili.com/video/BV1Y5QgB5Eas",
            out or self.out,
            parts=parts,
            audio_dir=self.audio,
        )

    def _fetches(self) -> list[list[str]]:
        """The `_run` calls that are neither of the two probe calls."""
        return [
            a for a in self.calls
            if "--dump-single-json" not in a and "--list-subs" not in a
        ]

    def test_a_part_whose_artifact_is_complete_is_not_processed_again(self):
        self._place(23, ASR_TEXT)
        records = self._run_all(parts=[23])
        self.assertEqual([r.status for r in records], ["skipped"])
        # No subtitle fetch, no audio download, no engine call.
        self.assertEqual(self._fetches(), [])
        self.assertEqual(self.loads, [])
        self.assertEqual(self.engine.calls, [])

    def test_a_complete_part_is_skipped_alongside_parts_that_are_processed(self):
        self._place(23, ASR_TEXT)
        records = self._run_all(parts=[3, 23])
        self.assertEqual([r.index for r in records], [3, 23])
        self.assertEqual([r.status for r in records], ["ok", "skipped"])
        # Part 3 has a track, so exactly one fetch happened and it was part 3.
        self.assertEqual(
            [a[a.index("--playlist-items") + 1] for a in self._fetches()], ["3"]
        )

    def test_the_skip_leaves_the_existing_artifact_untouched(self):
        path = self._place(23, ASR_TEXT)
        before = path.stat().st_mtime_ns
        self._run_all(parts=[23])
        self.assertEqual(path.read_text(encoding="utf-8"), ASR_TEXT)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_a_half_written_artifact_is_processed_again(self):
        # The subtle half of the ticket. This file decodes as clean UTF-8 and
        # reads like a transcript, so "the name exists" would call it finished
        # and silently keep 8586 characters short of the real thing.
        half = ASR_TEXT.encode("utf-8")[:25591]
        self.assertEqual(len(half.decode("utf-8")), 8537)
        path = self._place(23, half.decode("utf-8"))
        records = self._run_all(parts=[23])
        self.assertEqual([r.status for r in records], ["ok"])
        # A rewritten file is the evidence it was reprocessed, and the audio
        # download is the evidence the ASR path really ran.
        self.assertEqual(path.read_text(encoding="utf-8"), ASR_TEXT)
        self.assertEqual(
            [a[a.index("--playlist-items") + 1] for a in self._fetches()], ["23"]
        )
        self.assertEqual(len(self.engine.calls), 1)

    def test_a_torn_artifact_is_processed_again(self):
        path = self._place(23, "ignored")
        path.write_bytes(ASR_TEXT.encode("utf-8")[:25592])
        records = self._run_all(parts=[23])
        self.assertEqual([r.status for r in records], ["ok"])
        self.assertEqual(path.read_text(encoding="utf-8"), ASR_TEXT)

    def test_the_run_probes_again_even_when_every_part_is_skipped(self):
        # The spec's one explicit exclusion. The probe result is not persisted,
        # so a run that skips every part still probes. Measured at about 38
        # seconds for 30 parts, which is cheaper than state that can go stale.
        self._place(23, ASR_TEXT)
        before = len(self.calls)
        self._run_all(parts=[23])
        probed = [a for a in self.calls[before:] if "--dump-single-json" in a]
        self.assertEqual(len(probed), 1)
        listed = [a for a in self.calls[before:] if "--list-subs" in a]
        self.assertEqual(len(listed), 1)

    def test_two_runs_leave_no_state_behind_in_the_output_directory(self):
        # The only thing persisted is the artifact itself. A state file beside
        # it would be the caching the spec rules out.
        self._place(23, ASR_TEXT)
        self._run_all(parts=[23])
        self.assertEqual(
            sorted(p.name for p in self.out.iterdir()),
            [f"23-{self._titles()[23]}.txt"],
        )


class TranscriptReport(unittest.TestCase):
    """The line printed after a batch, which must separate the two populations.

    A reader coming back to finish a nearly complete course needs to see that
    most parts were already done and only a few were produced now. Counting
    both as "delivered" would hide that.
    """

    def _record(self, index: int, status: str, source: str | None = "platform"):
        return videokit.PartTranscript(
            bvid="BV1Y5QgB5Eas",
            index=index,
            title=f"{index:02d}-t",
            duration_sec=100.0,
            source=source,
            language="ai-zh" if source else None,
            segments=(),
            status=status,
        )

    def test_skipped_parts_are_counted_apart_from_this_run_s_parts(self):
        report = videokit.transcript_report(
            [
                self._record(1, "skipped", None),
                self._record(2, "skipped", None),
                self._record(3, "ok"),
            ],
            Path("out"),
        )
        self.assertIn("skipped 2", report)
        self.assertIn("delivered 1", report)

    def test_the_skipped_parts_are_named_so_the_reader_can_check_them(self):
        report = videokit.transcript_report(
            [self._record(18, "skipped", None), self._record(23, "skipped", None)],
            Path("out"),
        )
        self.assertIn("18", report)
        self.assertIn("23", report)


class AtomicWrite(unittest.TestCase):
    """The delivered name only ever holds a finished artifact.

    `Path.write_text` opens the target with mode `w`, which truncates it before
    a byte of the new payload is written. Measured on this machine, a finished
    1500 byte artifact is left at zero bytes when the rewrite fails at the
    encode step, and a process killed mid-write leaves the same thing. That is
    the file `transcript_complete` then has to guess about. Making the write a
    build-then-swap means the guess is only ever about a foreign file, not one
    this module just created.

    The crash is produced in a child process at the swap syscall, which is the
    one place a real interruption can land between "the payload is on disk" and
    "the payload is the delivered file".
    """

    CRASH_AT_SWAP = """
import os, sys
sys.path.insert(0, sys.argv[1])
import videokit
out = sys.argv[2]
record = videokit.PartTranscript(
    bvid="BV1Y5QgB5Eas", index=23, title="23-crash", duration_sec=1.0,
    source="asr", language="zh",
    segments=(videokit.Segment(0.0, 1.0, "政策很重要" * 400),), status="ok",
)
os.replace = lambda src, dst: os._exit(7)
videokit.write_transcript(record, out)
raise SystemExit(0)  # Reached only if the write never swapped.
"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"
        self.out.mkdir(parents=True)
        self.package = str(Path(__file__).resolve().parent.parent)

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _crash(self) -> int:
        done = subprocess.run(
            [sys.executable, "-c", self.CRASH_AT_SWAP, self.package, str(self.out)],
            capture_output=True,
        )
        return done.returncode

    def test_an_interrupted_write_leaves_nothing_at_the_delivered_name(self):
        # A hard kill at the swap cannot run cleanup, so the working file stays.
        # What matters is that nothing sits at the delivered name, and that the
        # leftover is the same path every time, so a later run consumes it by
        # swapping over it. One working file per part, never a growing pile.
        self.assertEqual(self._crash(), 7)
        self.assertEqual([p.name for p in self.out.iterdir()],
                         [".23-23-crash.txt.partial"])
        self.assertFalse((self.out / "23-23-crash.txt").exists())

    def test_the_working_file_is_not_mistaken_for_an_artifact(self):
        # The leftover is dot-prefixed and carries a `.partial` suffix, so a
        # later run's skip check, which probes the delivered name exactly,
        # cannot see it.
        self._crash()
        self.assertEqual(
            videokit.transcript_complete(self.out / "23-23-crash.txt", 300.0),
            False,
        )

    def test_an_interrupted_write_does_not_destroy_the_previous_artifact(self):
        # The delivered file exists and is finished. A crash during the rewrite
        # must leave it as it was, because the alternative is a zero byte file
        # under a name that looks delivered.
        path = self.out / "23-23-crash.txt"
        path.write_text(delivered_text(300.0), encoding="utf-8")
        before = path.read_bytes()
        self.assertEqual(self._crash(), 7)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(videokit.transcript_complete(path, 300.0))

    def test_a_successful_write_leaves_no_working_file_behind(self):
        record = videokit.PartTranscript(
            bvid="BV1Y5QgB5Eas", index=23, title="23-crash", duration_sec=1.0,
            source="asr", language="zh",
            segments=(videokit.Segment(0.0, 1.0, "政策很重要"),), status="ok",
        )
        path = videokit.write_transcript(record, self.out)
        self.assertEqual(path.read_text(encoding="utf-8"), "政策很重要")
        self.assertEqual([p.name for p in self.out.iterdir()], ["23-23-crash.txt"])

    def test_a_payload_that_cannot_be_encoded_leaves_the_artifact_alone(self):
        # Measured: the old in-place write truncated a finished artifact to
        # zero bytes and then raised.
        path = self.out / "23-23-crash.txt"
        path.write_text("政策是很重要的", encoding="utf-8")
        record = videokit.PartTranscript(
            bvid="BV1Y5QgB5Eas", index=23, title="23-crash", duration_sec=1.0,
            source="asr", language="zh",
            # A lone surrogate is not encodable as UTF-8.
            segments=(videokit.Segment(0.0, 1.0, "政策\ud800"),), status="ok",
        )
        with self.assertRaises(UnicodeEncodeError):
            videokit.write_transcript(record, self.out)
        self.assertEqual(path.read_text(encoding="utf-8"), "政策是很重要的")
        self.assertEqual([p.name for p in self.out.iterdir()], ["23-23-crash.txt"])


class BatchRun(unittest.TestCase):
    """One command, one BV number, every part judged, none of them fatal.

    `_run` and the ASR engine are the only stubs, so probe, the per-part fetch
    and the per-part audio download run the real code paths over recorded
    output. A part with no track is completed from its audio in the same pass,
    which is the join this ticket adds.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run
        self.original_engine = videokit.load_engine
        self.engine = _Engine({"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS})
        self.loads: list[str] = []

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run(str(self.audio / f"{part:02d}.wav"))
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = fake_run
        # The engine is handed over instead of built, so `transcripts` must
        # accept one. Counting the loads is how a per-part reload is caught.
        videokit.load_engine = lambda device="cpu": self.loads.append(device) or self.engine
        self.addCleanup(setattr, videokit, "_run", self.original)
        self.addCleanup(setattr, videokit, "load_engine", self.original_engine)
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"
        self.audio = self.tmp / "audio"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _run_all(self, parts: list[int] | None = None):
        # No engine is passed in on purpose, so the loading policy itself is
        # what runs here: build it on the first part that needs it, and reuse
        # it for every part after that.
        return videokit.transcripts(
            "https://www.bilibili.com/video/BV1Y5QgB5Eas",
            self.out,
            parts=parts,
            audio_dir=self.audio,
        )

    def test_all_thirty_parts_are_delivered(self):
        records = self._run_all()
        self.assertEqual(len(records), 30)
        self.assertEqual([r.index for r in records], list(range(1, 31)))
        self.assertEqual([r for r in records if r.status != "ok"], [])
        written = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(written), 30)
        self.assertEqual(written[0][:2], "01")
        self.assertEqual(written[-1][:2], "30")
        # The listing reads in part order, which is what the prefix is for.
        self.assertEqual(written, sorted(written))

    def test_the_two_sources_split_twenty_one_and_nine(self):
        records = self._run_all()
        platform = [r for r in records if r.source == "platform"]
        asr = [r for r in records if r.source == "asr"]
        self.assertEqual(len(platform), 21)
        self.assertEqual(len(asr), 9)
        self.assertEqual(tuple(r.index for r in platform), PARTS_WITH_SUBTITLES)
        self.assertEqual(tuple(r.index for r in asr), PARTS_WITHOUT_SUBTITLES)
        self.assertEqual(
            [r.language for r in platform], ["ai-zh"] * 21
        )
        self.assertEqual([r.language for r in asr], ["zh"] * 9)

    def test_the_engine_is_loaded_once_for_the_whole_batch(self):
        # 29 to 50 seconds per load. Over 30 parts a per-part load would add
        # about 20 minutes to a batch that is already 40 minutes of compute.
        self._run_all()
        self.assertEqual(self.loads, ["cpu"])
        self.assertEqual(len(self.engine.calls), 9)

    def test_the_run_does_not_abort_on_the_leading_parts_without_tracks(self):
        # Parts 1 and 2 come first and have no track. Stopping there would
        # produce nothing at all.
        records = self._run_all()
        self.assertEqual(records[-1].index, 30)
        self.assertEqual(records[-1].status, "ok")

    def test_every_delivered_file_reads_as_prose_with_no_source_marker(self):
        self._run_all()
        for path in sorted(self.out.iterdir()):
            with self.subTest(name=path.name):
                delivered = path.read_text(encoding="utf-8")
                self.assertTrue(delivered.strip())
                self.assertNotIn("-->", delivered)
                self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", delivered))
                self.assertNotIn("platform", delivered)
                self.assertNotIn("asr", delivered)

    def test_the_batch_accepts_a_bare_bv_number_like_the_documented_call(self):
        # The README documents `transcripts("BV1Y5QgB5Eas", ...)`, and a real
        # run of it failed with `'BV1Y5QgB5Eas' is not a valid URL`. The CLI
        # was already normalizing through `video_url`, so only the library
        # entry point was affected. Commit 72333ef fixed the same class of bug
        # for the per-part subtitle fetch.
        videokit.transcripts("BV1Y5QgB5Eas", self.out, parts=[3], audio_dir=self.audio)
        self.assertEqual(
            self.calls[0][-1], "https://www.bilibili.com/video/BV1Y5QgB5Eas"
        )

    def test_every_entry_point_normalizes_the_reference_the_same_way(self):
        for reference in (
            "BV1Y5QgB5Eas",
            "https://www.bilibili.com/video/BV1Y5QgB5Eas",
        ):
            with self.subTest(reference=reference):
                self.assertEqual(
                    videokit.video_url(reference),
                    "https://www.bilibili.com/video/BV1Y5QgB5Eas",
                )

    def test_a_restricted_part_selection_judges_only_those_parts(self):
        records = self._run_all(parts=[1, 3])
        self.assertEqual([r.index for r in records], [1, 3])
        self.assertEqual([r.status for r in records], ["ok", "ok"])
        self.assertEqual([r.source for r in records], ["asr", "platform"])
        self.assertEqual(len(list(self.out.iterdir())), 2)

    def test_a_part_whose_audio_cannot_be_fetched_is_recorded_as_no_audio(self):
        # No audio means no transcript, the part is recorded as `failed` with
        # `failure_reason = "no_audio"`, the batch carries on, and nothing is
        # written for it. `transcribe` must not be reached, because there is no
        # file to hand it.
        def no_audio(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run("")
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = no_audio
        records = self._run_all(parts=[1, 3])
        self.assertEqual([r.status for r in records], ["failed", "ok"])
        # The two fields are separate and the reason names the audio, not the
        # ASR. That distinction is the whole point of carrying both.
        self.assertEqual(records[0].failure_reason, "no_audio")
        self.assertIsNone(records[1].failure_reason)
        self.assertIsNone(records[0].source)
        self.assertEqual(records[0].segments, ())
        self.assertEqual(self.engine.calls, [])
        # The part that worked is still delivered. One failure does not undo it.
        self.assertEqual(
            [p.name[:2] for p in sorted(self.out.iterdir())], ["03"]
        )

    def test_a_batch_with_nothing_to_transcribe_never_loads_the_engine(self):
        # A course whose every part has a track must not pay 30 seconds for a
        # model it will not use.
        records = self._run_all(parts=[3, 4])
        self.assertEqual(self.loads, [])
        self.assertEqual([r.source for r in records], ["platform", "platform"])

    def test_the_audio_is_fetched_once_per_missing_part_on_a_first_run(self):
        self._run_all()
        audio_calls = [a for a in self.calls if "--audio-format" in a]
        self.assertEqual(len(audio_calls), 9)
        indexes = sorted(
            int(a[a.index("--playlist-items") + 1]) for a in audio_calls
        )
        self.assertEqual(indexes, list(PARTS_WITHOUT_SUBTITLES))


class TranscriptCommand(unittest.TestCase):
    """One command, given a BV number, produces the delivered transcripts.

    One command covers both sources. A part without a track is filled from its
    audio in the same run, so there is no second command to remember.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run
        self.original_engine = videokit.load_engine
        self.engine = _Engine({"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS})
        self.loads: list[str] = []

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run(str(self.audio / f"{part:02d}.wav"))
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = fake_run
        videokit.load_engine = lambda device="cpu": self.loads.append(device) or self.engine
        self.addCleanup(setattr, videokit, "_run", self.original)
        self.addCleanup(setattr, videokit, "load_engine", self.original_engine)
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"
        self.audio = self.tmp / "audio"
        self.buffer = io.StringIO()

    def _argv(self, *extra: str) -> list[str]:
        return [
            "transcript", "BV1Y5QgB5Eas",
            "--out", str(self.out),
            "--audio-out", str(self.audio),
            *extra,
        ]

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_a_bv_number_is_enough_to_produce_the_transcripts(self):
        with contextlib.redirect_stdout(self.buffer):
            code = videokit.main(self._argv())
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.out.iterdir())), 30)

    def test_the_missing_parts_are_filled_without_a_second_command(self):
        # This is the acceptance criterion. 21 parts come from the platform
        # tracks and the 9 without one are transcribed in the same run.
        with contextlib.redirect_stdout(self.buffer):
            videokit.main(self._argv())
        written = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(written), 30)
        for index in PARTS_WITHOUT_SUBTITLES:
            with self.subTest(index=index):
                self.assertTrue([n for n in written if n.startswith(f"{index:02d}-")])
        self.assertEqual(self.loads, ["cpu"])

    def test_the_command_reports_the_split_between_the_two_sources(self):
        # The reader has to see where each part came from, or a transcript
        # filled by ASR looks identical to one taken from a platform track.
        with contextlib.redirect_stdout(self.buffer):
            videokit.main(self._argv())
        printed = self.buffer.getvalue()
        self.assertIn("21", printed)
        self.assertIn("9", printed)
        self.assertIn("platform", printed)
        self.assertIn("ASR", printed)

    def test_a_failed_part_is_reported_on_the_command_line(self):
        # The acceptance criterion as the user meets it: one part fails, the
        # command still finishes, and the summary says which part failed and
        # whether running it again would help.
        raising = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "01.wav"),
            exc=RuntimeError("boom"),
        )
        videokit.load_engine = lambda device="cpu": raising
        with contextlib.redirect_stdout(self.buffer):
            code = videokit.main(self._argv("--parts", "1,3"))
        self.assertEqual(code, 0)
        printed = self.buffer.getvalue()
        self.assertIn("delivered 1 of 2", printed)
        self.assertIn("1 (asr_error, retryable)", printed)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual([n[:2] for n in names], ["03"])

    def test_a_url_works_as_well_as_a_bare_bv_number(self):
        with contextlib.redirect_stdout(self.buffer):
            code = videokit.main(
                [
                    "transcript",
                    "https://www.bilibili.com/video/BV1Y5QgB5Eas",
                    "--out", str(self.out),
                    "--audio-out", str(self.audio),
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("BV1Y5QgB5Eas", self.calls[0][-1])

    def test_a_bare_bv_number_is_expanded_to_a_url(self):
        # A BV number is what a user has in hand, and the extractor needs the
        # URL. Nobody should have to paste the prefix.
        with contextlib.redirect_stdout(self.buffer):
            videokit.main(self._argv())
        self.assertEqual(self.calls[0][-1], "https://www.bilibili.com/video/BV1Y5QgB5Eas")

    def test_a_part_selection_can_be_named(self):
        with contextlib.redirect_stdout(self.buffer):
            code = videokit.main(self._argv("--parts", "3,4"))
        self.assertEqual(code, 0)
        self.assertEqual(len(list(self.out.iterdir())), 2)

    def test_a_selection_of_missing_parts_is_still_completed(self):
        with contextlib.redirect_stdout(self.buffer):
            code = videokit.main(self._argv("--parts", "1,3"))
        self.assertEqual(code, 0)
        names = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith("01-"))
        self.assertTrue(names[1].startswith("03-"))

    def test_the_output_directory_can_be_omitted(self):
        with contextlib.chdir(self.tmp):
            with contextlib.redirect_stdout(self.buffer):
                code = videokit.main(["transcript", "BV1Y5QgB5Eas"])
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.tmp / "transcripts").iterdir())), 30)

    def test_a_missing_video_reference_is_refused_with_usage(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = videokit.main(["transcript"])
        self.assertNotEqual(code, 0)
        self.assertIn("transcript", buffer.getvalue())

    def test_an_unavailable_video_exits_nonzero_with_the_reason(self):
        def failing(args, cookies):
            raise videokit.VideoKitError("yt-dlp failed (exit 1): boom")

        videokit._run = failing
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = videokit.main(["transcript", "BV1Y5QgB5Eas", "--out", str(self.out)])
        self.assertNotEqual(code, 0)
        self.assertIn("boom", buffer.getvalue())

    def test_a_credential_problem_is_reported_rather_than_transcribed(self):
        # A stale login makes every part read as trackless. Transcribing in
        # that state would cost about 39 minutes and produce a full set of
        # transcripts from the wrong cause, so the run refuses instead.
        def logged_out(args, cookies):
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            return stub_run(LOGGED_OUT_SUBS, LOGIN_WARNING)

        videokit._run = logged_out
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            code = videokit.main(self._argv())
        self.assertNotEqual(code, 0)
        self.assertIn("login", buffer.getvalue().lower())
        self.assertEqual(self.engine.calls, [])


class AsrPipeline(unittest.TestCase):
    """The two paths converging on one structure, with neither the network,
    the engine, nor a GPU in the room."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.engine = _Engine({"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS})

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def test_a_filled_part_matches_the_shape_of_a_platform_part(self):
        # Built by the two real paths rather than by hand, so a field added to
        # one and not the other fails here.
        video = videokit.Video(
            id="BV1Y5QgB5Eas",
            title="t",
            parts=(
                videokit.Part(1, "BV1Y5QgB5Eas_p1", "01-a", 100.0, ()),
                videokit.Part(3, "BV1Y5QgB5Eas_p3", "03-b", 200.0, ("ai-zh",)),
            ),
        )
        original = videokit._run

        def fake_run(args, cookies):
            if "--audio-format" in args:
                return stub_run(str(self.tmp / "01.wav"))
            return stub_run(SUB_PRINT_PART)

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", original)

        asr = videokit.part_transcript(
            video, video.parts[0], engine=self.engine, audio_dir=self.tmp
        )
        platform = videokit.part_transcript(video, video.parts[1])
        self.assertEqual(asr.source, "asr")
        self.assertEqual(platform.source, "platform")
        for record in (asr, platform):
            with self.subTest(source=record.source):
                self.assertEqual(record.status, "ok")
                self.assertIsNone(record.failure_reason)
                self.assertTrue(record.segments)
                self.assertEqual(tuple(sorted(record.__dataclass_fields__)), MODEL_FIELDS)

    def test_the_delivered_file_does_not_say_which_path_produced_it(self):
        for source, segments in (
            ("platform", videokit.parse_srt(SRT_P18)),
            ("asr", videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)),
        ):
            with self.subTest(source=source):
                record = videokit.PartTranscript(
                    bvid="BV1Y5QgB5Eas", index=9, title="09-t", duration_sec=1.0,
                    source=source, language="ai-zh",
                    segments=tuple(segments), status="ok",
                )
                delivered = videokit.write_transcript(
                    record, self.tmp / source
                ).read_text(encoding="utf-8")
                self.assertTrue(delivered.strip())
                self.assertNotIn(source, delivered)
                self.assertNotIn("-->", delivered)
                self.assertIsNone(re.search(r"\d{2}:\d{2}:\d{2}", delivered))


# Raw output of one real ASR run, saved verbatim from the engine. This is the
# only ASR material that is not a hand-written sample: 3160.2 seconds of real
# audio produced 17123 characters of text beside 15908 millisecond pairs, and
# the two counts differ, which is the fact the pairing rule has to explain.
#   asr-23.txt              sha256 d1f57fbeee2c9bdb0cafa79bdfb7da6497458e52da476e761e3227e22d2c5a7c
#   asr-23.timestamp.json   sha256 55933e229414f4b40976ac399292e4d574b3e65f4099709bd9a77f79c2a272da
ASR_TEXT = (FIXTURES / "asr-23.txt").read_text(encoding="utf-8")
ASR_TIMESTAMPS = json.loads(
    (FIXTURES / "asr-23.timestamp.json").read_text(encoding="utf-8")
)

# The catalogued totals for that run, measured and recorded in the ASR study.
# Written as literals so a regression in any of them fails a test instead of
# silently changing the segmentation.
ASR_CHARS = 17123
ASR_UNITS = 15908
ASR_SEGMENTS = 1152


class _Device:
    """The two fields the module may read off a torch device."""

    def __init__(self, type_: str, index: int | None = None):
        self.type = type_
        self.index = index

    def __str__(self) -> str:
        return self.type if self.index is None else f"{self.type}:{self.index}"


class _Parameter:
    def __init__(self, device: _Device):
        self.device = device


class _SubModel:
    """Stands in for a torch module, which yields parameters."""

    def __init__(self, device: _Device):
        self._device = device

    def parameters(self):
        return iter([_Parameter(self._device)])


class _Engine:
    """An AutoModel-shaped object. Fakes the two attributes the module reads."""

    def __init__(self, result: dict, device: _Device = None):
        self._result = result
        self.model = _SubModel(device or _Device("cpu"))
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return [self._result]


class _RaisingEngine(_Engine):
    """An engine that fails on one part's audio and works on every other.

    A real per-part ASR failure, modelled on what the stack actually raises:
    the model is loaded and healthy, and one file it is given cannot be
    processed. The failure is a property of that part, which is what makes it
    distinguishable from a missing install or a stale login.
    """

    def __init__(self, result: dict, fails_on: str, exc: Exception):
        super().__init__(result)
        self.fails_on = fails_on
        self.exc = exc

    def generate(self, **kwargs):
        if str(kwargs.get("input", "")).startswith(self.fails_on):
            raise self.exc
        return super().generate(**kwargs)


class FailureSurvival(unittest.TestCase):
    """One part failing is recorded, reported, and does not end the batch.

    The two failure reasons are separate values because they call for opposite
    responses. `no_audio` cannot be fixed by retrying, `asr_error` can, and a
    caller that cannot tell them apart either wastes an hour on the first or
    abandons a transcript it could have had on the second.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original_run = videokit._run
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"
        self.audio = self.tmp / "audio"

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run(str(self.audio / f"{part:02d}.wav"))
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", self.original_run)

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _run_all(self, engine=None, parts=None):
        return videokit.transcripts(
            "BV1Y5QgB5Eas",
            self.out,
            parts=parts or [1, 2, 3],
            audio_dir=self.audio,
            engine=engine,
        )

    def test_one_part_that_fails_is_recorded_and_the_rest_continue(self):
        # Parts 1, 2 and 3 all have no track, so all three go through ASR and
        # the middle one fails. The other two must still be delivered.
        engine = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "02.wav"),
            exc=RuntimeError("CUDA out of memory"),
        )
        records = self._run_all(engine=engine)
        self.assertEqual([r.index for r in records], [1, 2, 3])
        self.assertEqual([r.status for r in records], ["ok", "failed", "ok"])
        self.assertEqual(
            [r.failure_reason for r in records], [None, "asr_error", None]
        )
        # A failed part carries no transcript and claims no source, so nothing
        # downstream can mistake it for a delivered one.
        failed = records[1]
        self.assertEqual(failed.segments, ())
        self.assertIsNone(failed.source)
        self.assertEqual(failed.bvid, "BV1Y5QgB5Eas")
        self.assertIn("02-", videokit.transcript_filename(failed))
        # The two healthy parts are really on disk, with real prose in them.
        written = sorted(p.name for p in self.out.iterdir())
        self.assertEqual([n[:2] for n in written], ["01", "03"])
        self.assertTrue((self.out / written[0]).read_text(encoding="utf-8").strip())
        self.assertTrue((self.out / written[1]).read_text(encoding="utf-8").strip())

    def test_the_batch_summary_names_the_failed_parts_and_their_reasons(self):
        engine = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "02.wav"),
            exc=RuntimeError("boom"),
        )
        records = self._run_all(engine=engine)
        report = videokit.transcript_report(records, self.out)
        self.assertIn("2 (asr_error, retryable)", report)
        self.assertIn("delivered 2 of 3", report)

    def test_the_two_reasons_stay_apart_in_one_batch(self):
        # Part 1 fails for want of audio, part 2 fails inside the engine. A
        # single field could not tell the caller that only one of the two is
        # worth retrying.
        def one_part_has_no_audio(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return stub_run(MULTI_DUMP)
            if "--list-subs" in args:
                return stub_run(MULTI_SUBS)
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return stub_run("" if part == 1 else str(self.audio / f"{part:02d}.wav"))
            if part in PARTS_WITH_SUBTITLES:
                return stub_run(SUB_PRINT_PART)
            return stub_run(SUB_PRINT_NO_TRACK)

        videokit._run = one_part_has_no_audio
        engine = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "02.wav"),
            exc=RuntimeError("boom"),
        )
        records = self._run_all(engine=engine)
        self.assertEqual(
            [r.failure_reason for r in records], ["no_audio", "asr_error", None]
        )
        self.assertEqual([r.status for r in records], ["failed", "failed", "ok"])
        report = videokit.transcript_report(records, self.out)
        self.assertIn("1 (no_audio, not retryable)", report)
        self.assertIn("2 (asr_error, retryable)", report)
        self.assertEqual([p.name[:2] for p in sorted(self.out.iterdir())], ["03"])

    def test_the_summary_says_which_failures_a_rerun_would_fix(self):
        # The reason the two fields exist, made visible to the person reading
        # the summary. They have just waited 40 minutes; the line has to tell
        # them which parts are worth running again and which are not, without
        # their having to look up what the reason strings mean.
        engine = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "02.wav"),
            exc=RuntimeError("boom"),
        )
        report = videokit.transcript_report(self._run_all(engine=engine), self.out)
        self.assertIn("2 (asr_error, retryable)", report)
        self.assertNotIn("no_audio", report)
        self.assertIn("delivered 2 of 3", report)

    def test_a_failed_part_is_not_counted_as_delivered(self):
        engine = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "02.wav"),
            exc=RuntimeError("boom"),
        )
        records = self._run_all(engine=engine)
        report = videokit.transcript_report(records, self.out)
        self.assertIn("delivered 2 of 3 parts", report)
        self.assertEqual(len(list(self.out.iterdir())), 2)

    def test_a_missing_asr_install_is_not_recorded_as_thirty_failed_parts(self):
        # The install fault is one machine-level problem, not one per part. The
        # message naming the three packages is the only thing that fixes it, so
        # it has to arrive intact instead of being turned into 30 records that
        # all say `asr_error` and read like the course is at fault.
        def no_engine(device="cpu"):
            raise videokit.TranscriptionUnavailable("the ASR stack is not installed")

        original = videokit.load_engine
        videokit.load_engine = no_engine
        self.addCleanup(setattr, videokit, "load_engine", original)
        with self.assertRaises(videokit.TranscriptionUnavailable) as ctx:
            self._run_all()
        self.assertIn("not installed", str(ctx.exception))
        # Untouched: nothing was written and nothing was reported as a part
        # that failed on its own.
        self.assertFalse(self.out.exists())

    def test_the_only_statuses_are_the_ones_the_spec_names(self):
        # `pending` was ticket #17's placeholder for "this part has no audio
        # yet". With failures recorded there is no third state left to name, so
        # the value is gone rather than left as vocabulary nothing produces.
        record = videokit._failed_record(
            "BV1Y5QgB5Eas",
            videokit.Part(7, "BV1Y5QgB5Eas_p7", "07-t", 10.0, ()),
            "no_audio",
        )
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.failure_reason, "no_audio")
        source = (
            Path(__file__).resolve().parent.parent / "videokit.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('"pending"', source)

    def test_a_failure_does_not_invalidate_a_part_already_completed(self):
        # The part that was delivered before the failure keeps its file and its
        # text. Failing one part must not retroactively withdraw another.
        engine = _RaisingEngine(
            {"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS},
            fails_on=str(self.audio / "01.wav"),
            exc=RuntimeError("boom"),
        )
        records = self._run_all(engine=engine, parts=[1, 3])
        self.assertEqual([r.status for r in records], ["failed", "ok"])
        delivered = self.out / videokit.transcript_filename(records[1])
        self.assertTrue(delivered.is_file())
        self.assertEqual(
            delivered.read_text(encoding="utf-8"),
            videokit.transcript_text(records[1].segments),
        )
        # The failed part's file is absent rather than empty, because an empty
        # file reads as a finished transcript.
        self.assertFalse(
            (self.out / videokit.transcript_filename(records[0])).exists()
        )


class AsrSegmentation(unittest.TestCase):
    """Pairing timestamps with text, on the real recorded output.

    The model is absent here by construction. Every assertion below reads the
    saved artifacts of a run that already happened, so the suite needs neither
    a GPU nor a network, and it still asserts against bytes the engine really
    produced rather than against a hand-written sample.
    """

    def test_one_timestamp_per_recognised_unit(self):
        # The count that makes the pairing possible. A Han character or one
        # whole run of ASCII alphanumerics spends one pair; punctuation and
        # whitespace spend none. 15883 characters plus 25 ASCII runs is 15908.
        units = videokit.recognised_units(ASR_TEXT)
        self.assertEqual(len(units), ASR_UNITS)
        self.assertEqual(len(ASR_TIMESTAMPS), ASR_UNITS)
        self.assertNotEqual(len(ASR_TEXT), ASR_UNITS)

    def test_an_ascii_run_spends_one_pair_not_one_per_letter(self):
        # `GDP` is 23 occurrences in this run and each spends a single pair.
        # Pairing per character instead would overrun by 61 units.
        units = videokit.recognised_units("我们把GDP这个讲完了")
        self.assertEqual([u.text for u in units], ["我", "们", "把", "GDP", "这", "个", "讲", "完", "了"])

    def test_produces_the_measured_segment_count(self):
        self.assertEqual(len(videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)), ASR_SEGMENTS)

    def test_first_segment_is_the_recorded_opening(self):
        segment = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)[0]
        self.assertEqual(segment, videokit.Segment(0.11, 0.25, "呃，"))

    def test_last_segment_is_the_recorded_ending(self):
        segment = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)[-1]
        self.assertEqual(
            segment,
            videokit.Segment(3154.9, 3157.945, "就会导致你内需不足的问题长期存在。"),
        )

    def test_seconds_are_whole_milliseconds_over_a_thousand(self):
        # No rounding is applied. 110 ms is 0.11 s exactly, and the last
        # segment's end is 3157945 ms.
        segments = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)
        self.assertEqual(segments[0].start_sec, 110 / 1000)
        self.assertEqual(segments[-1].end_sec, 3157945 / 1000)

    def test_the_segment_texts_rebuild_the_raw_output(self):
        # Nothing is dropped, nothing is invented. Punctuation stays on the
        # segment it closes.
        segments = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)
        self.assertEqual("".join(s.text for s in segments), ASR_TEXT)

    def test_every_segment_closes_on_punctuation(self):
        # The cut points are exactly where the engine put a mark, which is why
        # ct-punc is a requirement rather than a refinement.
        marks = set("，。？！、；：,.;:!?")
        segments = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)
        self.assertTrue(all(s.text[-1] in marks for s in segments))

    def test_the_western_marks_are_cut_points_too(self):
        # ct-punc writes `.` and `,` after ASCII words. Measured on this run:
        # 3 full stops and 1 comma. Leaving them out yields 1148 segments
        # instead of 1152, which is how that mistake was found.
        segments = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)
        western = [s for s in segments if s.text[-1] in ".,"]
        self.assertEqual(len(western), 4)
        self.assertEqual(
            western[0],
            videokit.Segment(1412.62, 1413.48, "我们加入了WTO."),
        )

    def test_no_segment_is_empty_or_overlaps_the_next(self):
        segments = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)
        self.assertEqual([s for s in segments if not s.text.strip()], [])
        self.assertEqual([s for s in segments if s.end_sec < s.start_sec], [])
        for earlier, later in zip(segments, segments[1:]):
            self.assertLessEqual(earlier.end_sec, later.start_sec)

    def test_a_segment_always_carries_at_least_one_recognised_unit(self):
        # A segment holding punctuation alone would be an artifact of the cut
        # rule rather than something the engine said, so there are none.
        segments = videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS)
        self.assertEqual(
            [s for s in segments if not videokit.recognised_units(s.text)], []
        )

    def test_the_filler_words_are_kept_because_that_is_the_decision(self):
        # Colloquial fillers survive on purpose. This is not a recognition
        # defect and this module does not clean them up.
        text = "".join(s.text for s in videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS))
        self.assertEqual(text.count("啊"), 277)
        self.assertEqual(text.count("这个"), 424)
        self.assertEqual(text.count("就是"), 60)

    def test_a_count_mismatch_is_refused_rather_than_guessed(self):
        # If the two sides disagree, any pairing is a guess. Stopping here is
        # what keeps a silent misalignment out of a transcript.
        with self.assertRaises(videokit.AsrError) as ctx:
            videokit.asr_segments(ASR_TEXT, ASR_TIMESTAMPS[:-1])
        self.assertIn(str(ASR_UNITS), str(ctx.exception))

    def test_a_trailing_run_without_a_mark_is_still_a_segment(self):
        text = "呃，我们把GDP这个讲完了"
        pairs = [[110, 250]] + [[250 + 60 * i, 310 + 60 * i] for i in range(9)]
        segments = videokit.asr_segments(text, pairs)
        self.assertEqual(segments[0], videokit.Segment(0.11, 0.25, "呃，"))
        self.assertEqual(segments[1].text, "我们把GDP这个讲完了")
        self.assertEqual(segments[1].start_sec, 0.25)
        self.assertEqual(segments[1].end_sec, pairs[-1][1] / 1000)

    def test_recognised_units_report_where_they_sit_in_the_text(self):
        # The offsets are what the cut rule indexes by, so they are contract,
        # not an internal detail.
        units = videokit.recognised_units("呃，GDP。")
        self.assertEqual(
            [(u.start, u.end, u.text) for u in units],
            [(0, 1, "呃"), (2, 5, "GDP")],
        )


class AsrDevice(unittest.TestCase):
    """Where the model actually runs, read rather than assumed.

    FunASR accepts `device="cuda:0"` on a machine with no CUDA and quietly
    runs on the CPU. Every assertion here exists because a wrong device is
    invisible from the outside: the transcript looks the same either way.
    """

    def test_reads_the_device_off_the_models_parameters(self):
        engine = _Engine({}, _Device("cuda", 0))
        self.assertEqual(videokit.detect_device(engine), "cuda:0")

    def test_a_cuda_request_honoured_is_accepted(self):
        engine = _Engine({}, _Device("cuda", 0))
        self.assertEqual(videokit.verify_device(engine, "cuda:0"), "cuda:0")

    def test_a_cuda_request_quietly_downgraded_is_reported(self):
        # The measured failure. The engine returns a perfectly good result, so
        # the only place this can be caught is here.
        engine = _Engine({}, _Device("cpu"))
        with self.assertRaises(videokit.AsrError) as ctx:
            videokit.verify_device(engine, "cuda:0")
        self.assertIn("cuda:0", str(ctx.exception))
        self.assertIn("cpu", str(ctx.exception))

    def test_a_cpu_request_is_the_honest_path_on_this_machine(self):
        engine = _Engine({}, _Device("cpu"))
        self.assertEqual(videokit.verify_device(engine, "cpu"), "cpu")

    def test_the_misleading_attribute_is_not_consulted(self):
        # `model.model.device` reads back None even when the model is on the
        # CPU, so consulting it would report "unknown" instead of the truth.
        engine = _Engine({}, _Device("cpu"))
        engine.model.device = None
        self.assertEqual(videokit.detect_device(engine), "cpu")

    def test_an_engine_without_parameters_is_reported_not_ignored(self):
        class Bare:
            pass

        with self.assertRaises(videokit.AsrError) as ctx:
            videokit.detect_device(Bare())
        self.assertIn("parameters", str(ctx.exception))

    def test_the_engine_configuration_names_all_three_models(self):
        # ct-punc is required, not optional. Without it the output is
        # characters separated by spaces with no punctuation at all, and there
        # are no cut points to segment on.
        kwargs = videokit.engine_kwargs("cpu")
        self.assertEqual(kwargs["model"], "paraformer-zh")
        self.assertEqual(kwargs["vad_model"], "fsmn-vad")
        self.assertEqual(kwargs["punc_model"], "ct-punc")
        self.assertEqual(kwargs["device"], "cpu")
        self.assertIs(kwargs["disable_update"], True)

    def test_the_engine_configuration_carries_the_requested_device(self):
        self.assertEqual(videokit.engine_kwargs("cuda:1")["device"], "cuda:1")


class AsrTranscription(unittest.TestCase):
    """One call in, segments out, with the model stubbed.

    The engine is the slow part and it cannot be in a unit test. It is reached
    through one argument, so the logic around it is exercised here while the
    real engine is exercised by the live script.
    """

    def test_returns_segments_built_from_the_engine_result(self):
        engine = _Engine({"text": "呃，我们把GDP这个讲完了。", "timestamp": [
            [110, 250], [250, 350], [350, 430], [430, 500], [500, 600],
            [600, 700], [700, 800], [800, 900], [900, 1000], [1000, 1100],
        ]})
        segments = videokit.transcribe(Path("06.wav"), engine=engine)
        self.assertEqual(
            segments,
            [
                videokit.Segment(0.11, 0.25, "呃，"),
                videokit.Segment(0.25, 1.1, "我们把GDP这个讲完了。"),
            ],
        )

    def test_asks_the_engine_for_the_audio_file(self):
        engine = _Engine({"text": "呃，", "timestamp": [[110, 250]]})
        videokit.transcribe(Path("D:/audio/06.wav"), engine=engine)
        self.assertEqual(engine.calls[0]["input"], str(Path("D:/audio/06.wav")))

    def test_the_buggy_sentence_info_is_not_used(self):
        # FunASR 1.4.16 groups sentences against the wrong text on long audio;
        # the saved full-length run drifts from the top-level text at character
        # 318. The result carries the key here on purpose, and the output must
        # not move.
        engine = _Engine({
            "text": "呃，我们把GDP这个讲完了。",
            "timestamp": [
                [110, 250], [250, 350], [350, 430], [430, 500], [500, 600],
                [600, 700], [700, 800], [800, 900], [900, 1000], [1000, 1100],
            ],
            "sentence_info": [{"text": "wrong", "start": 0, "end": 5}],
        })
        segments = videokit.transcribe(Path("06.wav"), engine=engine)
        self.assertEqual([s.text for s in segments], ["呃，", "我们把GDP这个讲完了。"])

    def test_a_result_with_no_timestamps_is_refused(self):
        engine = _Engine({"text": "呃，", "timestamp": []})
        with self.assertRaises(videokit.AsrError):
            videokit.transcribe(Path("06.wav"), engine=engine)

    def test_the_requested_device_is_checked_before_any_audio_is_read(self):
        engine = _Engine({"text": "呃，", "timestamp": [[110, 250]]}, _Device("cpu"))
        with self.assertRaises(videokit.AsrError):
            videokit.transcribe(Path("06.wav"), engine=engine, device="cuda:0")
        self.assertEqual(engine.calls, [])


class AsrDependencies(unittest.TestCase):
    """What has to be installed, and what the user is told when it is not.

    These tests run on a machine without funasr, which is the point: the suite
    must not depend on the ASR stack to describe its absence.
    """

    def test_a_missing_funasr_names_the_three_packages(self):
        import sys as _sys

        saved = _sys.modules.get("funasr")
        _sys.modules["funasr"] = None
        try:
            with self.assertRaises(videokit.TranscriptionUnavailable) as ctx:
                videokit.load_engine()
        finally:
            if saved is None:
                _sys.modules.pop("funasr", None)
            else:
                _sys.modules["funasr"] = saved
        message = str(ctx.exception)
        self.assertIn("funasr", message)
        self.assertIn("torch", message)
        # Without a fbank backend, feature extraction raises at run time
        # rather than at import time, so the message has to name it too.
        self.assertIn("kaldi-native-fbank", message)

    def test_the_three_asr_dependencies_are_declared(self):
        # Recorded in the package metadata, because a machine that installs
        # this package otherwise fails at feature extraction.
        manifest = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        for package in ("funasr", "torch", "kaldi-native-fbank"):
            self.assertIn(package, manifest)

    def test_the_filler_word_decision_is_written_down(self):
        # The ticket asks for this in the README, and a decision that is only
        # in a commit message gets reverted by the next person.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("填充词", readme)

    def test_both_accepted_tradeoffs_are_written_down(self):
        # The ticket names two, and each has a remedy worth recording: no
        # punctuation on the platform side, and no Simplified to Traditional
        # conversion. A tradeoff that lives only in a commit message gets
        # reverted by the next person.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("无标点", readme)
        self.assertIn("简繁", readme)

    def test_the_conversion_package_name_trap_is_written_down(self):
        # `opencc` is the only correct name. Two similarly named packages
        # convert character by character and mistranslate 头发 into 頭發, so
        # naming the wrong one is a silent error rather than a crash.
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("opencc", readme)
        self.assertIn("头发", readme)
        self.assertIn("頭發", readme)

    def test_the_punctuation_difference_is_written_down_in_the_source(self):
        # The same two tradeoffs belong beside the code that embodies them,
        # not only in the README. The module's comments are in English, so the
        # markers asserted here are the English ones.
        source = (
            Path(__file__).resolve().parent.parent / "videokit.py"
        ).read_text(encoding="utf-8")
        self.assertIn("opencc", source)
        self.assertIn("头发", source)
        self.assertIn("punctuation", source)


# --- Ticket #20: the command line entry point -----------------------------

# The premium-tier line, recorded live from `yt-dlp --list-subs` on
# BV1Y5QgB5Eas with a working credential. It is a separate fixture because the
# older `list-subs-*.txt` files store the tier name as six U+FFFD replacement
# characters, so the real name cannot be asserted against them.
#
# The measured stream matters and is counter to the reconnaissance note: on
# this machine yt-dlp writes this line to **stdout**, not stderr. Captured with
# `subprocess.run(capture_output=True)` into separate files, the logged-in
# `--list-subs` run produced 30 of these on stdout and 0 bytes on stderr. The
# stderr copy that the note describes belongs to the logged-out run, whose
# stderr carries the login warning instead. The CLI therefore scans both
# streams rather than one.
PREMIUM_LINE = (
    FIXTURES / "list-subs-premium-line.txt"
).read_text(encoding="utf-8")

# The recorded subtitle table with its mangled premium lines removed. Used
# where the premium signal is not what is under test, so the assertions do not
# depend on a fixture whose tier name is unreadable.
SUBS_TABLE = "\n".join(
    line for line in MULTI_SUBS.splitlines() if "Format(s)" not in line
)


class DownloadProgress(unittest.TestCase):
    """`videokit video` and `videokit audio` report per part as they go.

    The ticket asks for progress that is visible rather than a long silence,
    because one part is minutes of waiting. Nothing here claims a percentage:
    the download path reports only the files yt-dlp says it finished, so the
    count of finished parts is the only progress figure that is measurable, and
    that is what is printed.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            if "--list-subs" in args:
                return videokit.RunOutput(SUBS_TABLE, "")
            part = args[args.index("--playlist-items") + 1]
            return videokit.RunOutput(str(self.tmp / f"{part}.bin"), "")

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", self.original)
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "out"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = videokit.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _finished_lines(self, printed: str) -> list[str]:
        """The lines reporting a part that finished, one per written file."""
        return [l for l in printed.splitlines() if "完成" in l and "[" in l]

    def test_audio_reports_one_line_per_selected_part(self):
        code, printed, err = self._run_main(
            ["audio", "BV1Y5QgB5Eas", "--parts", "1,6", "--out", str(self.out)]
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self._finished_lines(printed)), 2)

    def test_audio_progress_names_the_part_and_the_total(self):
        _, printed, _ = self._run_main(
            ["audio", "BV1Y5QgB5Eas", "--parts", "1,6", "--out", str(self.out)]
        )
        self.assertIn("[1/2]", printed)
        self.assertIn("[2/2]", printed)

    def test_video_progress_reports_each_part_it_wrote(self):
        code, printed, err = self._run_main(
            ["video", "BV1Y5QgB5Eas", "--parts", "3,4", "--out", str(self.out)]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("[1/2]", printed)
        self.assertIn("[2/2]", printed)
        self.assertIn("2", printed)

    def test_video_passes_the_quality_selector_through(self):
        self._run_main(
            ["video", "BV1Y5QgB5Eas", "--parts", "3", "--quality", "1080p",
             "--out", str(self.out)]
        )
        downloads = [a for a in self.calls if "-f" in a]
        self.assertEqual(len(downloads), 1)
        self.assertEqual(downloads[0][downloads[0].index("-f") + 1], "30080+30280")

    def test_the_audio_command_asks_for_the_asr_format(self):
        self._run_main(["audio", "BV1Y5QgB5Eas", "--parts", "6", "--out", str(self.out)])
        downloads = [a for a in self.calls if "--audio-format" in a]
        self.assertEqual(len(downloads), 1)
        self.assertIn("16000", " ".join(downloads[0]))

    def test_an_unknown_quality_is_refused_and_the_valid_ones_named(self):
        code, out, err = self._run_main(
            ["video", "BV1Y5QgB5Eas", "--quality", "4320p", "--out", str(self.out)]
        )
        self.assertNotEqual(code, 0)
        printed = out + err
        self.assertIn("4320p", printed)
        self.assertIn("1080p", printed)

    def test_a_download_that_writes_no_file_is_reported_not_hidden(self):
        # yt-dlp can exit 0 having written nothing, which is what the library
        # reports as an empty list. Silence there would look like success.
        def writes_nothing(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            if "--list-subs" in args:
                return videokit.RunOutput(SUBS_TABLE, "")
            return videokit.RunOutput("", "")

        videokit._run = writes_nothing
        code, printed, err = self._run_main(
            ["audio", "BV1Y5QgB5Eas", "--parts", "1", "--out", str(self.out)]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("没有报告写出文件", printed + err)

    def test_no_part_selection_covers_every_part(self):
        _, printed, _ = self._run_main(
            ["audio", "BV1Y5QgB5Eas", "--out", str(self.out)]
        )
        self.assertIn("[30/30]", printed)
        self.assertEqual(
            len([a for a in self.calls if "--audio-format" in a]), 30
        )


class MissingTiers(unittest.TestCase):
    """Which quality tiers the platform refused, read off yt-dlp's own words.

    The JSON format table cannot answer this. Measured on the same video with a
    working credential and with no credential at all, the two tables are
    identical row for row, 15 rows each, same `format_id`, `quality`, `format`
    and `tbr`, and the highest `quality` is 80 in both. So a caller that reads
    only `formats` cannot tell whether the high-bitrate tier is missing, and
    would report the normal 1080P as if it were the premium one. The sentence
    yt-dlp prints is the only signal there is.
    """

    def test_reads_the_tier_yt_dlp_named(self):
        # Expected value read off the recorded line, not recomputed.
        self.assertEqual(videokit.missing_tiers(PREMIUM_LINE), ("1080P 高码率",))

    def test_several_tiers_are_all_returned(self):
        # The logged-out wording names three. Both shapes occur, so the parser
        # reads the list rather than one fixed tier.
        text = (
            "[BiliBili] Format(s) 1080P 高码率, 1080P 高清, 720P 准高清 are "
            "missing; you have to become a premium member to download them.\n"
        )
        self.assertEqual(
            videokit.missing_tiers(text),
            ("1080P 高码率", "1080P 高清", "720P 准高清"),
        )

    def test_nothing_is_reported_when_no_tier_is_missing(self):
        self.assertEqual(videokit.missing_tiers(""), ())
        self.assertEqual(
            videokit.missing_tiers("[BiliBili] Extracting URL: https://x\n"), ()
        )


class PremiumTier(unittest.TestCase):
    """Whether the tier above the one on offer is out of reach.

    `_format_selector("1080p")` names format 30080, whose `format` field is
    `1080P 高清`. The membership tier is a different, higher-bitrate stream
    called `1080P 高码率`, measured at 820.8 kbps on the same part where
    `1080P 高清` is 416.6. A user who asks for 1080p and gets the normal tier
    must be told, and the only evidence available is the tier name yt-dlp
    reported missing.
    """

    def test_the_premium_tier_is_reported_as_unavailable(self):
        self.assertTrue(videokit.premium_unavailable(("1080P 高码率",)))

    def test_a_missing_normal_tier_is_not_a_premium_problem(self):
        # 720P 准高清 is not the membership tier, so its absence says nothing
        # about whether the high-bitrate one was withheld.
        self.assertFalse(videokit.premium_unavailable(("720P 准高清",)))

    def test_nothing_missing_means_nothing_to_warn_about(self):
        self.assertFalse(videokit.premium_unavailable(()))

    def test_a_missing_tier_that_is_not_the_premium_one_is_not_reported(self):
        # "1080P 高清" is the normal tier the selector already names. Calling it
        # premium would tell the user they lost a tier they never asked for.
        self.assertFalse(videokit.premium_unavailable(("1080P 高清",)))


class ProbeMissingTiers(unittest.TestCase):
    """`probe` must hand the withheld-tier evidence to its caller.

    The evidence is not in the JSON, so `probe` reads it off the text of the
    calls it already makes. Both streams are scanned because the sentence's
    location is not fixed: measured today it arrives on stdout, and the
    recorded `list-subs-login-warning.txt` shows the sibling warning arriving
    on stderr. Keying on one stream alone would drop the warning on whichever
    run puts it elsewhere.
    """

    # `list-subs-multipart.txt` carries its own copy of the sentence, but with
    # the tier name stored as six U+FFFD replacement characters. That mangled
    # name is not the premium marker, so the recorded table cannot be used as
    # the measured signal. The lines are dropped here and the live-recorded
    # `PREMIUM_LINE` is used instead, which is what a run on this machine
    # produces today.
    PLAIN_SUBS = "\n".join(
        line for line in MULTI_SUBS.splitlines() if "Format(s)" not in line
    )

    def setUp(self):
        self.original = videokit._run

    def tearDown(self):
        videokit._run = self.original

    def _stub(self, premium_stream: str):
        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            if premium_stream == "stdout":
                return videokit.RunOutput(
                    self.PLAIN_SUBS + PREMIUM_LINE, ""
                )
            return videokit.RunOutput(self.PLAIN_SUBS, PREMIUM_LINE)

        videokit._run = fake_run

    def test_probe_reports_the_withheld_tier_from_stdout(self):
        self._stub("stdout")
        video = videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")
        self.assertEqual(video.missing_tiers, ("1080P 高码率",))
        self.assertTrue(videokit.premium_unavailable(video.missing_tiers))

    def test_probe_finds_the_line_on_stderr_too(self):
        self._stub("stderr")
        video = videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")
        self.assertEqual(video.missing_tiers, ("1080P 高码率",))

    def test_a_run_that_withholds_nothing_reports_no_tier(self):
        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(self.PLAIN_SUBS, "")

        videokit._run = fake_run
        video = videokit.probe("https://www.bilibili.com/video/BV1Y5QgB5Eas")
        self.assertEqual(video.missing_tiers, ())
        self.assertFalse(videokit.premium_unavailable(video.missing_tiers))

    def test_a_mangled_tier_name_does_not_raise_the_premium_warning(self):
        # A known limit, recorded rather than left to be discovered. The tier
        # name comes from yt-dlp and has to survive this module's decoding of
        # the subprocess; when it does not, the name is replacement characters
        # and the premium marker is not in it, so the warning is missed. The
        # recorded fixture is exactly this case. Fixing it belongs with the
        # decoding defect recorded in the README, not here.
        tiers = videokit.missing_tiers(MULTI_SUBS)
        self.assertEqual(len(tiers), 1)
        self.assertFalse(videokit.premium_unavailable(tiers))


class CommandLine(unittest.TestCase):
    """One entry point from which all four capabilities are reachable.

    The ticket's first criterion. A caller who has not read the source must be
    able to discover what exists, so the help text is asserted rather than
    assumed, and an unknown subcommand must be refused rather than silently
    doing the wrong thing.
    """

    def _run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = videokit.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_all_four_capabilities_are_reachable_and_named(self):
        code, out, err = self._run_main(["--help"])
        self.assertEqual(code, 0)
        printed = out + err
        for name in ("probe", "video", "audio", "transcript"):
            with self.subTest(name=name):
                self.assertIn(name, printed)

    def test_help_says_what_each_subcommand_does(self):
        # A name alone does not tell a reader which of "download video" and
        # "download audio" they want.
        _, out, err = self._run_main(["--help"])
        printed = out + err
        self.assertIn("probe", printed)
        self.assertIn("下载", printed)

    def test_no_arguments_prints_usage_and_exits_nonzero(self):
        code, out, err = self._run_main([])
        self.assertNotEqual(code, 0)
        self.assertIn("probe", out + err)

    def test_an_unknown_subcommand_is_refused_with_usage(self):
        code, out, err = self._run_main(["frobnicate"])
        self.assertNotEqual(code, 0)
        self.assertIn("probe", out + err)

    def test_each_subcommand_has_its_own_help(self):
        for name in ("probe", "video", "audio", "transcript"):
            with self.subTest(name=name):
                code, out, err = self._run_main([name, "--help"])
                self.assertEqual(code, 0)
                printed = out + err
                self.assertIn(name, printed)
                self.assertIn("usage", printed.lower())

    def test_the_help_names_the_other_subcommands(self):
        # A reader who lands on one subcommand's help should see the rest, so
        # they do not have to guess that the other three exist.
        _, out, err = self._run_main(["probe", "--help"])
        printed = out + err
        for name in ("probe", "video", "audio", "transcript"):
            with self.subTest(name=name):
                self.assertIn(name, printed)


class ProbeOutput(unittest.TestCase):
    """`videokit probe` printed so a person can read it.

    Every expected value below is read off `dump-multipart.json` and
    `list-subs-multipart.txt`, which are real recorded yt-dlp output, so they
    are independent of how the report is built. The measurements: 30 parts,
    3437.528 seconds for part 1, 5761.010 for part 30, 21 parts with an
    `ai-zh` track and 9 without, them being 1, 2, 6, 7, 8, 9, 13, 23 and 28.
    """

    # The live-recorded sentence replaces the fixture's copy, whose tier name
    # is stored as six U+FFFD and so cannot carry the real Chinese name.
    PLAIN_SUBS = "\n".join(
        line for line in MULTI_SUBS.splitlines() if "Format(s)" not in line
    )

    def setUp(self):
        self.original = videokit._run

        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(self.PLAIN_SUBS + PREMIUM_LINE, "")

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", self.original)

    def _probe(self, *extra: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = videokit.main(["probe", "BV1Y5QgB5Eas", *extra])
        self.assertEqual(code, 0)
        return out.getvalue()

    def _part_lines(self, printed: str) -> list[str]:
        """The printed lines that describe one part, in order.

        Anchored on the leading indent and a duration, which is the shape of a
        part row. The summary line begins at the margin and carries no
        duration column, so it cannot be mistaken for one.
        """
        return [line for line in printed.splitlines()
                if re.match(r"^\s{2,}(\d+)\s+\d+(?:分|小时)", line)]

    def test_the_part_count_is_reported(self):
        printed = self._probe()
        self.assertIn("30", printed)
        self.assertEqual(len(self._part_lines(printed)), 30)

    def test_each_part_carries_its_measured_duration(self):
        printed = self._probe()
        lines = self._part_lines(printed)
        # 3437.528 s and 5761.010 s, as a reader would say them.
        self.assertIn("57分18秒", lines[0])
        self.assertIn("1小时36分1秒", lines[-1])

    def test_the_total_running_time_is_reported(self):
        printed = self._probe()
        # 132076.843 seconds over the 30 parts.
        self.assertIn("36", printed)
        self.assertIn("小时", printed)

    def test_each_part_carries_its_subtitle_status(self):
        printed = self._probe()
        lines = self._part_lines(printed)
        self.assertEqual(sum("ai-zh" in line for line in lines), 21)
        self.assertEqual(sum("无字幕" in line for line in lines), 9)

    def test_the_parts_without_subtitles_are_the_measured_ones(self):
        printed = self._probe()
        lines = self._part_lines(printed)
        without = [
            int(re.match(r"^\s*(\d+)", line).group(1))
            for line in lines
            if "无字幕" in line
        ]
        self.assertEqual(tuple(without), PARTS_WITHOUT_SUBTITLES)

    def test_the_quality_tiers_are_listed_with_their_measured_numbers(self):
        printed = self._probe()
        # Names, ids, bitrates and sizes all read off the fixture.
        self.assertIn("720P 准高清", printed)
        self.assertIn("720p", printed)
        self.assertIn("1080P 高清", printed)
        self.assertIn("213.9", printed)   # 720p hevc tbr, 213.874 kbps
        self.assertIn("820.8", printed)   # 1080p avc1 tbr, 820.842 kbps
        self.assertIn("336.4", printed)   # 1080p avc1, 352708419 bytes as MiB

    def test_all_four_selectable_qualities_appear(self):
        printed = self._probe()
        for name in ("360p", "480p", "720p", "1080p"):
            with self.subTest(name=name):
                self.assertIn(name, printed)

    def test_the_premium_tier_is_named_as_unavailable(self):
        printed = self._probe()
        self.assertIn("1080P 高码率", printed)
        self.assertIn("未拿到", printed)

    def test_the_premium_note_says_which_tier_was_actually_reached(self):
        # The ticket's point: a user who asked for 1080p and received the
        # normal tier must not believe they got the high-bitrate one.
        printed = self._probe()
        self.assertIn("普通档", printed)
        self.assertIn("1080P 高清", printed)


class TranscriptProgress(unittest.TestCase):
    """`videokit transcript` shows progress, and its summary covers every status.

    A full run of the first real target is about 48 minutes, of which speech
    recognition is about 39. Silence for that long reads as a hang, so a part
    that goes to the engine is named when it starts and timed when it ends.

    The summary is asserted for the statuses present rather than for a fixed
    list. Tickets #18 and #19 add `failed` and `skipped` records to this same
    data structure, so a tally that named only today's values would silently
    drop theirs.
    """

    def setUp(self):
        self.calls: list[list[str]] = []
        self.original = videokit._run
        self.original_engine = videokit.load_engine
        self.engine = _Engine({"text": ASR_TEXT, "timestamp": ASR_TIMESTAMPS})
        self.loads: list[str] = []

        def fake_run(args, cookies):
            self.calls.append(args)
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            if "--list-subs" in args:
                return videokit.RunOutput(SUBS_TABLE, "")
            part = int(args[args.index("--playlist-items") + 1])
            if "--audio-format" in args:
                return videokit.RunOutput(str(self.audio / f"{part:02d}.wav"), "")
            if part in PARTS_WITH_SUBTITLES:
                return videokit.RunOutput(SUB_PRINT_PART, "")
            return videokit.RunOutput(SUB_PRINT_NO_TRACK, "")

        videokit._run = fake_run
        videokit.load_engine = lambda device="cpu": self.loads.append(device) or self.engine
        self.addCleanup(setattr, videokit, "_run", self.original)
        self.addCleanup(setattr, videokit, "load_engine", self.original_engine)
        self.tmp = Path(tempfile.mkdtemp(prefix="videokit-test-"))
        self.addCleanup(self._cleanup)
        self.out = self.tmp / "transcripts"
        self.audio = self.tmp / "audio"

    def _cleanup(self):
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        self.tmp.rmdir()

    def _run_main(self, *extra: str):
        # Both streams into one buffer, in write order. Progress goes to stderr
        # and the report to stdout, so asserting on stdout alone would miss the
        # progress and asserting on them separately would lose their order.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = videokit.main(
                ["transcript", "BV1Y5QgB5Eas", "--out", str(self.out),
                 "--audio-out", str(self.audio), *extra]
            )
        return code, out.getvalue() + err.getvalue(), err.getvalue()

    def test_the_plan_is_printed_before_the_waiting_starts(self):
        # The estimate has to come before the run, not after, or the reader has
        # already spent the time not knowing.
        _, printed, _ = self._run_main("--parts", "1,3")
        self.assertIn("语音识别", printed)

    def test_each_part_sent_to_the_engine_is_named(self):
        _, printed, _ = self._run_main("--parts", "1,6")
        for index in (1, 6):
            with self.subTest(index=index):
                self.assertRegex(printed, rf"第\s*{index}\s*讲")

    def test_a_part_that_needs_no_engine_is_not_announced_as_transcribing(self):
        # Part 3 has a platform track, so it must not be reported as going
        # through speech recognition. A line there would misstate the work.
        _, printed, _ = self._run_main("--parts", "3")
        self.assertNotIn("语音识别开始", printed)
        self.assertEqual(self.loads, [])

    def test_progress_appears_before_the_end_summary(self):
        # Order is the point. A line printed only at the end is not progress,
        # and one buffer in write order is what tells the two apart.
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            videokit.main(
                ["transcript", "BV1Y5QgB5Eas", "--out", str(self.out),
                 "--audio-out", str(self.audio), "--parts", "1"]
            )
        text = printed.getvalue()
        self.assertIn("语音识别开始", text)
        self.assertIn("delivered", text)
        self.assertLess(text.index("语音识别开始"), text.index("delivered"))

    def test_the_report_stays_on_stdout_with_progress_on_stderr(self):
        # So a caller can redirect the delivered report to a file and still see
        # the running commentary on the terminal.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            videokit.main(
                ["transcript", "BV1Y5QgB5Eas", "--out", str(self.out),
                 "--audio-out", str(self.audio), "--parts", "1"]
            )
        self.assertIn("delivered", out.getvalue())
        self.assertNotIn("语音识别开始", out.getvalue())
        self.assertIn("语音识别开始", err.getvalue())

    def test_the_end_summary_is_printed(self):
        _, printed, _ = self._run_main("--parts", "1,3")
        self.assertIn("delivered", printed)
        self.assertIn("platform", printed)
        self.assertIn("ASR", printed)

    def test_the_summary_carries_a_status_tally(self):
        _, printed, _ = self._run_main("--parts", "1,3")
        self.assertIn("状态", printed)
        self.assertIn("ok", printed)


class StatusTally(unittest.TestCase):
    """The summary counts whatever statuses the records actually carry.

    Tickets #18 and #19 extend this data structure with `failed` and
    `skipped`. The tally is written against the values found on the records,
    sorted, so their records appear without this file naming them.
    """

    def _record(self, index: int, status: str):
        return videokit.PartTranscript(
            bvid="BV1Y5QgB5Eas", index=index, title=f"{index:02d}-t",
            duration_sec=1.0, source=None, language=None, segments=(),
            status=status,
        )

    def test_every_status_present_is_counted(self):
        tally = videokit.status_tally([
            self._record(1, "ok"),
            self._record(2, "ok"),
            self._record(3, "failed"),
        ])
        self.assertIn("ok", tally)
        self.assertIn("2", tally)
        self.assertIn("failed", tally)

    def test_a_status_this_module_does_not_know_is_still_counted(self):
        # Nothing here names `skipped`; the point is that a value added by
        # another ticket shows up without an edit in this file.
        tally = videokit.status_tally([
            self._record(1, "ok"),
            self._record(2, "skipped"),
        ])
        self.assertIn("skipped", tally)
        self.assertIn("1", tally)

    def test_an_empty_batch_tallies_nothing_rather_than_raising(self):
        self.assertEqual(videokit.status_tally([]), "")


class ConsoleEncoding(unittest.TestCase):
    """The CLI's own output survives a console that cannot render it.

    The console on this machine is cp936 and `sys.stdout.encoding` is gbk, so
    writing to it goes through `encodings.gbk` in strict mode. Measured, that
    raises UnicodeEncodeError for characters outside the code page: U+20000,
    an emoji, U+2022 and U+FFFD all fail, while ordinary Chinese and the
    box-drawing characters used by the report do not.

    A part title comes from the platform, so the CLI cannot assume it encodes.
    The measured titles of the first real target all do, which is one video and
    not a guarantee. Crashing on an unprintable title would hand the user a
    traceback instead of the report they asked for, which is the failure the
    ticket's wording exists to avoid.
    """

    class _Cp936Pipe(io.StringIO):
        """A stdout that behaves like the measured cp936 pipe.

        `io.StringIO` accepts every character, so a report could be built and
        printed here without ever touching the code page that fails in
        practice. This encoding step is what the real stream does.
        """

        encoding = "gbk"

        def write(self, text):
            text.encode("cp936")  # strict, as the real stream is
            return super().write(text)

    def setUp(self):
        self.original = videokit._run

        def fake_run(args, cookies):
            if "--dump-single-json" in args:
                return videokit.RunOutput(MULTI_DUMP, "")
            return videokit.RunOutput(SUBS_TABLE + PREMIUM_LINE, "")

        videokit._run = fake_run
        self.addCleanup(setattr, videokit, "_run", self.original)

    def test_a_title_the_console_cannot_render_does_not_crash_probe(self):
        # U+20000 and an emoji, both measured to fail on cp936.
        video = videokit.Video(
            id="BV1Y5QgB5Eas",
            title="测试\U00020000标题",
            parts=(
                videokit.Part(
                    1, "BV1Y5QgB5Eas_p1", "01-\U0001f3ac讲", 3437.528, ("ai-zh",)
                ),
            ),
            formats=(),
        )
        stream = self._Cp936Pipe()
        original_probe = videokit.probe
        videokit.probe = lambda url, cookies=None: video
        self.addCleanup(setattr, videokit, "probe", original_probe)

        code = 0
        try:
            with contextlib.redirect_stdout(stream):
                code = videokit.main(["probe", "BV1Y5QgB5Eas"])
        except UnicodeEncodeError as exc:
            self.fail(f"probe crashed on an unprintable title: {exc}")
        self.assertEqual(code, 0)
        printed = stream.getvalue()
        self.assertTrue(printed.strip())

    def test_the_unprintable_character_is_replaced_not_dropped_silently(self):
        # The rest of the report has to survive. A title that renders as
        # question marks is honest about the console's limit; a title whose
        # remaining characters vanished would look like a different video.
        video = videokit.Video(
            id="BV1Y5QgB5Eas",
            title="测试\U00020000标题",
            parts=(),
            formats=(),
        )
        stream = self._Cp936Pipe()
        original_probe = videokit.probe
        videokit.probe = lambda url, cookies=None: video
        self.addCleanup(setattr, videokit, "probe", original_probe)

        with contextlib.redirect_stdout(stream):
            videokit.main(["probe", "BV1Y5QgB5Eas"])
        printed = stream.getvalue()
        self.assertIn("测试", printed)
        self.assertIn("标题", printed)
        self.assertIn("BV1Y5QgB5Eas", printed)

    def test_the_help_text_survives_the_measured_console(self):
        stream = self._Cp936Pipe()
        with contextlib.redirect_stdout(stream):
            code = videokit.main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("probe", stream.getvalue())

    def test_the_failure_path_also_survives(self):
        # A reference the platform rejects carries a message the user must
        # read, so the error path needs the same treatment as the report.
        def failing(args, cookies):
            raise videokit.VideoKitError("失败\U00020000原因")

        videokit._run = failing
        stream = self._Cp936Pipe()
        err = self._Cp936Pipe()
        code = 0
        try:
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(err):
                code = videokit.main(["probe", "BV1Y5QgB5Eas"])
        except UnicodeEncodeError as exc:
            self.fail(f"the error path crashed on an unprintable message: {exc}")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
