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


if __name__ == "__main__":
    unittest.main(verbosity=2)
