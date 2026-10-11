"""VideoKit: probe and download Bilibili videos.

Entry points, one per input shape:

    probe(url)                  -> Video            what exists at this URL
    part_subtitles(url, part)   -> list[Segment]    that part's subtitle track
    transcribe(wav)             -> list[Segment]    that part's audio as speech
    download_video(...)         -> list[Path]       the video files
    download_audio(...)         -> list[Path]       the audio files, for ASR

Why probing is its own call: deciding what to download requires knowing what
is there first. Splitting it lets a caller ask "how many parts, which have
subtitles, what qualities" without touching the network for file data.

Why audio has its own call: transcript-only work needs 40 MB per part instead
of 130 MB or 340 MB. Same part, an order of magnitude apart.

Why subtitles are their own call: a part that has a track needs no ASR at all,
so the decision of which to run is made per part from this result. Measured on
the first real target, 21 of 30 parts have one, and whether a part has one is
uncorrelated with its length. So this is asked per part, never per video.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable

# yt-dlp writes its subtitle table to stdout, not to JSON. This is a known
# Bilibili extractor limitation (yt-dlp issue #17527): `subtitles` comes back
# as an empty object even for parts that have them, while `--list-subs`
# reports them correctly. So probing uses two calls, not one.
_SUB_HEADER = re.compile(r"^\[info\] Available subtitles for (\S+):\s*$")
_SUB_ROW = re.compile(
    r"^\s*(\S+)\s+(json3|json|srt|vtt|xml|ass|srv1|srv2|srv3|ttml|dfxp)\s*$"
)

# The danmaku track is attached unconditionally by the extractor and is a
# bullet-comment XML feed, not a subtitle. A part carrying only danmaku has
# no subtitles at all.
_NOT_A_SUBTITLE = {"danmaku"}

# yt-dlp's own words when it found no session. Printed to stderr, exit code 0.
# Measured across six inputs in recon.md: present whenever the credentials are
# missing or rejected, absent when they are good and the video simply has no
# subtitles. It is the only signal that separates those two, so the login
# judgment hangs on this string rather than on the exit code.
_LOGIN_WARNING = "Subtitles are only available when logged in"

# The Chrome profile that carries the Bilibili login. Named here because
# `.gitignore` must cover it as well as the cookie file: it holds the same
# credential, so ignoring only the file would leave the login committable.
PROFILE_DIRNAME = "browser-profile"


@dataclass(frozen=True)
class Part:
    """One 分P of a Bilibili video."""

    index: int
    id: str
    title: str
    duration_sec: float
    subtitle_langs: tuple[str, ...]

    @property
    def has_subtitles(self) -> bool:
        return bool(self.subtitle_langs)


@dataclass(frozen=True)
class Segment:
    """One timed line of transcript.

    `text` holds speech only. Timestamps are separate fields because the two
    transcript sources disagree on granularity: platform subtitles are cut per
    sentence, ASR can cut per word. Merged into one string, no downstream
    consumer could treat them alike.

    A note on punctuation. Bilibili's `ai-zh` tracks carry none. Measured over
    all 21 recorded tracks, 454245 text characters, the count of CJK sentence
    punctuation is zero, as is the count of ASCII `, ? ! ;`. The text is one
    continuous run. This is an accepted tradeoff, not a defect, and not
    something this module repairs.

    The two transcript sources therefore disagree on punctuation, and a single
    course mixes both styles: platform text reads as one unbroken run, ASR text
    carries marks because `ct-punc` puts them there. Repairing the platform
    side would mean running punctuation restoration over it, which recomputes
    timestamps that are already correct. Accepted for this round, recorded in
    the README. No Simplified to Traditional conversion is applied either; see
    `_asr_record` for why the correct package name matters if that changes.
    """

    start_sec: float
    end_sec: float
    text: str


@dataclass(frozen=True)
class Video:
    """What probe found at a URL."""

    id: str
    title: str
    parts: tuple[Part, ...]
    formats: tuple[dict, ...] = field(default_factory=tuple)
    # The tiers yt-dlp reported as withheld. Carried on the probe result rather
    # than fetched again by the caller, because the JSON format table cannot
    # answer this and the sentence is only in the text of a call `probe`
    # already makes. See `missing_tiers`.
    missing_tiers: tuple[str, ...] = field(default_factory=tuple)


def login_needed(subs: dict[str, tuple[str, ...]], stderr: str) -> bool:
    """Whether this run failed for want of a login.

    Two signals, both required, because neither alone is sufficient. The exit
    code is useless here: all six measured inputs exited 0, including the ones
    with no credentials at all.

    Any real track anywhere in the video proves the credentials work, so the
    empty check is over the whole video rather than per part. A course whose p1
    has no subtitles and whose p3 has them is a valid login, not a failure.

    Never reported as "expired". Expired and never-logged-in measure the same,
    so the caller is told what to do instead of what went wrong.
    """
    if any(subs.values()):
        return False
    return _LOGIN_WARNING in stderr


class VideoKitError(RuntimeError):
    """Raised when yt-dlp fails in a way the caller must handle."""


@lru_cache(maxsize=1)
def _ytdlp_command() -> tuple[str, ...]:
    """Locate yt-dlp, preferring one on PATH and falling back to `uv tool run`.

    yt-dlp is not a Python library here, it is an external program, so it may
    be installed globally, available only through uv, or absent entirely. The
    fallback matters because requiring a global install would be an extra
    setup step for a tool that already depends on uv for its environment.
    """
    found = shutil.which("yt-dlp")
    if found:
        return (found,)
    if shutil.which("uv"):
        return ("uv", "tool", "run", "--from", "yt-dlp", "yt-dlp")
    raise VideoKitError(
        "yt-dlp not found. Install it with `uv tool install yt-dlp` "
        "or `pip install yt-dlp`, or install uv so the fallback can run it."
    )


@dataclass(frozen=True)
class RunOutput:
    """Both streams of one successful yt-dlp run.

    stderr is carried even on success because the login warning is written
    there while the exit code stays 0. Dropping it makes a logged-out empty
    subtitle table identical to one for a video that has no subtitles.
    """

    stdout: str
    stderr: str


def _run(args: list[str], cookies: Path | None) -> RunOutput:
    """Run yt-dlp and return both streams, or raise with stderr attached.

    One place resolves credentials, so every caller gets the same behavior
    whether it passes a path, passes None, or omits the argument. A caller that
    names a file always wins; otherwise the stored login is used if there is
    one.

    Both streams are returned rather than stdout alone. yt-dlp writes the login
    warning to stderr while exiting 0, so stdout-only cannot tell a logged-out
    run from a video that has no subtitles.

    Encoding is left to the platform on purpose. yt-dlp writes its output in
    the console's code page (cp936 on this machine), not UTF-8, so forcing
    UTF-8 turns every Chinese title into replacement characters. Those titles
    end up in filenames, so a wrong guess here produces paths that cannot be
    opened.
    """
    cmd = list(_ytdlp_command())
    cookies = cookies if cookies is not None else default_cookies()
    if cookies is not None:
        cmd += ["--cookies", str(cookies)]
    cmd += args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise VideoKitError(f"could not execute {cmd[0]!r}: {exc}") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-8:])
        raise VideoKitError(f"yt-dlp failed (exit {proc.returncode}):\n{tail}")
    return RunOutput(stdout=proc.stdout or "", stderr=proc.stderr or "")


def netscape_text(cookies: list[dict]) -> str:
    """Serialize CDP cookies into the Netscape file yt-dlp reads.

    Only Bilibili domains are kept. A browser profile holds cookies for every
    site the user visited, and handing those to yt-dlp would spread credentials
    for unrelated services into a file that exists to hold one site's login.

    The output matches what yt-dlp's own exporter writes, because yt-dlp is the
    reader. Column order is domain, include-subdomains, path, secure, expiry,
    name, value.
    """
    lines = ["# Netscape HTTP Cookie File", ""]
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if "bilibili" not in domain:
            continue
        expires = int(cookie.get("expires") or 0)
        if expires < 0:
            expires = 0  # Netscape writes 0 for a session cookie.
        lines.append(
            "\t".join(
                [
                    domain,
                    # Include-subdomains is not its own flag, it is the leading
                    # dot. A host-only cookie such as www.bilibili.com must not
                    # claim subdomain coverage.
                    "TRUE" if domain.startswith(".") else "FALSE",
                    cookie.get("path", "/"),
                    "TRUE" if cookie.get("secure") else "FALSE",
                    str(expires),
                    cookie["name"],
                    cookie["value"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def default_data_dir() -> Path:
    """Where credentials live, deliberately outside the repository.

    A path inside the working tree leaves the credential one `git add -A` away
    from a commit, and relies on every future contributor remembering. Outside
    the tree that mistake is not available.

    `VIDEOKIT_DATA_DIR` overrides it, which is also how tests stay hermetic.
    """
    override = os.environ.get("VIDEOKIT_DATA_DIR")
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "VideoKit"


def credentials_path() -> Path:
    """The credential file the three entry points look for by default."""
    return default_data_dir() / "cookies.txt"


def save_cookies(cookies: list[dict], path: Path) -> Path:
    """Write cookies to `path`, readable only by the current account.

    The directory is restricted as well as the file. A credential file sitting
    in an open directory is one `type` away from disclosure, and on Windows the
    file would inherit that access back regardless of its own ACL.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _restrict_to_current_account(path.parent, inheritable=True)
    path.write_text(netscape_text(cookies), encoding="utf-8")
    _restrict_to_current_account(path, inheritable=False)
    return path


def _restrict_to_current_account(path: Path, inheritable: bool) -> None:
    """Drop inherited access and grant only the current account.

    Three icacls steps, each measured on Windows 11. `/reset` first, because a
    directory that already carries explicit entries such as `NT AUTHORITY` and
    `BUILTIN\\Administrators` keeps them through `/inheritance:r` alone.
    `/inheritance:r` then cuts the parent's grants, or they would return as
    inherited entries on the next write. `/grant:r` installs the one account.

    `inheritable` picks the grant form. They are not interchangeable: the
    object-inherit flags are valid on a directory and rejected on a file.

    Expected failure is a filesystem without Windows ACLs. There the parent
    directory's permissions are the only protection left, and that is what the
    caller gets rather than a crash on the credential write path.
    """
    if os.name != "nt":
        path.chmod(0o700 if inheritable else 0o600)
        return
    account = subprocess.run(["whoami"], capture_output=True, text=True).stdout.strip()
    grant = f"{account}:(OI)(CI)F" if inheritable else f"{account}:(F)"
    for args in (
        ["icacls", str(path), "/reset"],
        ["icacls", str(path), "/inheritance:r"],
        ["icacls", str(path), "/grant:r", grant],
    ):
        subprocess.run(args, capture_output=True)


def credentials_usable(path: Path) -> bool:
    """Whether `path` holds a credential file that carries a Bilibili login.

    A file with cookies but no `SESSDATA` is not a login. Chrome writes cookies
    for an anonymous profile too, so "the file exists and has rows" would report
    a session that is not there and send the caller down a path that silently
    returns no subtitle tracks.
    """
    path = Path(path)
    if not path.is_file():
        return False
    for row in rows_of(path):
        columns = row.split("\t")
        if len(columns) >= 6 and columns[5] == "SESSDATA":
            return True
    return False


def rows_of(path: Path) -> list[str]:
    """The data rows of a Netscape cookie file, comments and blanks dropped."""
    return [
        line
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if line and not line.startswith("#")
    ]


def default_cookies() -> Path | None:
    """The stored credential, or None when there is no usable login yet.

    None is not an error. The three entry points then call yt-dlp without
    credentials, which is what an anonymous probe does anyway.
    """
    candidate = credentials_path()
    return candidate if credentials_usable(candidate) else None


class LoginError(VideoKitError):
    """Raised when the guided login cannot produce a credential file.

    Carries the failing step in its message. The four ways this fails are
    indistinguishable from outside, so the message is the only thing that tells
    a caller where to look.
    """


class LoginRequired(VideoKitError):
    """Raised when a run needed a session and did not have a working one.

    Distinct from `LoginError`, which is about the guided login failing. This
    one says the scrape came back with nothing because there was no login, and
    it carries the command that fixes it.
    """


def cdp_cookies(ws) -> list[dict]:
    """Read every cookie through an already-attached CDP page socket.

    `Network.enable` must come first. Asking a socket that has not enabled the
    Network domain returns zero cookies and no error, which looks exactly like
    an expired login. The socket is a parameter so this is testable without a
    browser.
    """
    reply = _cdp_call(ws, 1, "Network.enable")
    if "error" in reply:
        raise LoginError(f"Network.enable rejected: {reply['error']}")
    reply = _cdp_call(ws, 2, "Network.getAllCookies")
    if "error" in reply:
        raise LoginError(f"Network.getAllCookies rejected: {reply['error']}")
    return reply.get("result", {}).get("cookies", [])


def _cdp_call(ws, msg_id: int, method: str) -> dict:
    """Send one CDP command and return its reply.

    Events arrive on the same socket and share the message shape, so frames are
    matched on the id rather than accepted in order.
    """
    ws.send(json.dumps({"id": msg_id, "method": method, "params": {}}))
    while True:
        reply = json.loads(ws.recv())
        if reply.get("id") == msg_id:
            return reply


def select_page_target(targets: list[dict]) -> dict:
    """Pick the Bilibili page target to attach to.

    A page target is required. Attaching at browser level returns zero cookies
    for the same session with no error, so this is not a preference.
    """
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        raise LoginError(
            f"no page target to attach to; Chrome reported {len(targets)} "
            "targets and none were pages"
        )
    return next((t for t in pages if "bilibili" in t.get("url", "")), pages[0])


def has_login(cookies: list[dict]) -> bool:
    """Whether these cookies hold a Bilibili session.

    Chrome reports cookies for an anonymous profile, so a non-empty list proves
    nothing. `SESSDATA` is the session cookie a login creates.
    """
    return any(
        c.get("name") == "SESSDATA" and "bilibili" in c.get("domain", "")
        for c in cookies
    )


def chrome_argv(chrome: Path, profile: Path, port: int, url: str) -> list[str]:
    """The startup command for the browser the user scans into.

    `--remote-allow-origins=*` is mandatory, not defensive. Chrome 111 and later
    rejects a CDP WebSocket that carries an Origin header, and the failure is a
    403 at the handshake, which looks like a network fault rather than a flag
    decision.

    `--user-data-dir` points at a fixed profile so the login survives the run.
    A fresh directory each time would cost a QR scan on every invocation, which
    is the thing this ticket exists to avoid.
    """
    return [
        str(chrome),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]


def _chrome_executable(explicit: Path | None = None) -> Path:
    """Locate Chrome, or say which names were tried."""
    if explicit is not None:
        if Path(explicit).is_file():
            return Path(explicit)
        raise LoginError(f"chrome.exe not found at {explicit}")
    found = shutil.which("chrome")
    if found:
        return Path(found)
    candidates = [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LoginError(
        "chrome.exe not found. Pass its path, or install Chrome. Looked on "
        f"PATH and at {', '.join(str(c) for c in candidates)}"
    )


def _debug_port() -> int:
    return int(os.environ.get("VIDEOKIT_DEBUG_PORT", "9223"))


def _fetch_targets(port: int) -> list[dict]:
    """Ask the browser what targets it has.

    An unreachable port is the browser still starting, so it is reported as
    such rather than as a connection error the caller has to interpret.
    """
    url = f"http://127.0.0.1:{port}/json/list"
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LoginError(f"could not reach the browser at {url}: {exc}") from exc


def _launch_browser(argv: list[str]) -> subprocess.Popen:
    try:
        return subprocess.Popen(argv)
    except OSError as exc:
        raise LoginError(f"could not start {argv[0]!r}: {exc}") from exc


def _stop_browser(proc: subprocess.Popen) -> None:
    """Close the browser we opened.

    The profile on disk keeps the login, so closing here costs the user
    nothing and leaves no stray window behind.
    """
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()


def _connect(url: str):
    import websocket  # type: ignore[import-untyped]

    return websocket.create_connection(url, timeout=30)


def _await_session(port: int, timeout_sec: float) -> list[dict]:
    """Wait for the user to finish scanning, then return the session cookies.

    Raises with the failing step named. The three failures here look identical
    from the outside, and one of them, an anonymous profile, produces a healthy
    cookie list with no session in it.
    """
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            page = select_page_target(_fetch_targets(port))
        except LoginError as exc:
            if time.monotonic() >= deadline:
                raise LoginError(f"could not attach to the Bilibili page: {exc}") from exc
            time.sleep(1)
            continue

        ws = _connect(page["webSocketDebuggerUrl"])
        try:
            cookies = cdp_cookies(ws)
        finally:
            ws.close()

        if has_login(cookies):
            return cookies
        if time.monotonic() >= deadline:
            raise LoginError(
                "no Bilibili login was detected. Scan the QR code in the "
                "browser window to sign in, then run this again."
            )
        time.sleep(1)


def login(chrome: Path | None = None, timeout_sec: float = 180.0) -> Path:
    """Walk the user through one QR scan and store the resulting credential.

    Opens a browser on the dedicated profile, waits for a session, writes the
    Netscape file yt-dlp reads, and closes the browser. The profile stays on
    disk, so this is a one-time cost per machine rather than per run.
    """
    profile = default_data_dir() / PROFILE_DIRNAME
    profile.mkdir(parents=True, exist_ok=True)
    port = _debug_port()
    argv = chrome_argv(
        _chrome_executable(chrome), profile, port, "https://www.bilibili.com/"
    )
    proc = _launch_browser(argv)
    try:
        cookies = _await_session(port, timeout_sec)
    finally:
        _stop_browser(proc)
    return save_cookies(cookies, credentials_path())


def reuse_or_login() -> Path:
    """The stored login, opening the browser only when there is not one."""
    return default_cookies() or login()


def parse_subtitle_table(text: str) -> dict[str, tuple[str, ...]]:
    """Parse `--list-subs` output into {part id: (language, ...)}.

    Part ids appear in two shapes: `BV1Y5QgB5Eas_p3` for a multi-part video
    and `BV1GJ411x7h7` with no suffix for a single one. Both are handled by
    matching whatever identifier the header carries.
    """
    found: dict[str, set[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        header = _SUB_HEADER.match(line)
        if header:
            current = header.group(1)
            found.setdefault(current, set())
            continue
        if current is None:
            continue
        row = _SUB_ROW.match(line)
        if row:
            found[current].add(row.group(1))
            continue
        if line.strip() == "" and found.get(current):
            current = None
        elif line.startswith("["):
            current = None
    return {k: tuple(sorted(v - _NOT_A_SUBTITLE)) for k, v in found.items()}


def login_required_message() -> str:
    """What the user is told when a scrape came back empty for want of a login.

    Names what to do rather than what went wrong. Expired, revoked and
    never-logged-in are externally identical, measured in recon.md, so
    asserting a cause would be a guess presented as a finding. The raw yt-dlp
    warning is left out on purpose: it reads as a bug report, and the ticket's
    point is that the user gets a next step instead.
    """
    return (
        "Bilibili returned no subtitle tracks and reported that subtitles "
        "need a login. A credential that was never obtained and one that has "
        "stopped working look identical from the outside, so nothing is "
        "claimed about which one this is.\n"
        "Run this to obtain a credential:\n"
        "    uv run --python 3.12 python videokit.py login"
    )


_TIMESTAMP = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> "
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)


def _seconds(hours: str, minutes: str, seconds: str, millis: str) -> float:
    """One timestamp field, as seconds from the start of the track."""
    whole = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    return whole + int(millis) / 1000


def parse_srt(text: str) -> list[Segment]:
    """Parse an SRT track into segments ordered by start time.

    The shape is strictly four lines per block, `index / timestamp / text /
    blank`, `4N+1` lines in total, ending in a blank line. Measured over all 21
    recorded tracks, 40283 segments, this holds with zero exceptions: no
    multi-line text, no empty text, no leading or trailing whitespace, and the
    separator is always `" --> "`.

    Two properties of the real data drive the implementation.

    The index line is not parsed. Ten segments consist of digits alone, so
    "an integer line" cannot be told from "an index line" by inspection. The
    timestamp line is the anchor instead, and it is unambiguous because `-->`
    never appears in the text.

    Blank lines only delimit blocks and carry no content, so they are skipped
    rather than used to split. That also tolerates a track that ends without a
    final blank line, which a byte-range cut can produce.

    Ordered by start time as read, which matches the source. The tracks are
    strictly increasing in start time with no overlaps, so no sorting is
    applied and none is needed.

    An empty input yields an empty list. That is a different outcome from a
    part that has no subtitle track, which `part_subtitles` reports by raising,
    not by returning `[]`.
    """
    lines = text.split("\n")
    segments: list[Segment] = []
    for position, line in enumerate(lines):
        stamp = _TIMESTAMP.match(line.rstrip("\r"))
        if not stamp:
            continue
        if position + 1 >= len(lines):
            continue  # A timestamp with no line after it is not a segment.
        body = lines[position + 1].rstrip("\r")
        if not body.strip():
            continue
        start = _seconds(*stamp.groups()[:4])
        end = _seconds(*stamp.groups()[4:])
        segments.append(Segment(start_sec=start, end_sec=end, text=body))
    return segments


class SubtitleUnavailable(VideoKitError):
    """Raised when a part has no subtitle track to transcribe.

    Deliberately not "return an empty list". The caller's two possible
    responses are worlds apart: an empty transcript gets written to disk and
    looks finished, while a missing track means this part needs ASR instead.
    Raising is what keeps those apart.
    """


# Why the track text is read from stdout rather than from a written file.
# yt-dlp writes subtitles to disk under a name this module cannot predict:
# `-o` placeholders do not govern it and `--print after_move:filepath` stays
# empty for subtitles, both measured. The `requested_subtitles` field instead
# carries the track text itself inside the JSON dump, and for part 12 that text
# is byte-identical to the file yt-dlp writes, sha256
# b13447822d78f2a02fe39101a66c5b17283602fdeca82c96725e0557f224c24c, 123777
# bytes. Reading stdout also sidesteps filename encoding entirely.
#
# The payload is pure ASCII because the decoder runs with ensure_ascii, so
# Chinese text travels escaped. That matters here: `_run` decodes the
# subprocess with the console code page on purpose, and escaped payloads are
# immune to it.
_SUBTITLE_PRINT_FIELD = "%(requested_subtitles)j"


def part_subtitles(
    url: str,
    part: int,
    language: str = "ai-zh",
    cookies: Path | None = None,
) -> list[Segment]:
    """Fetch one part's platform subtitle track and parse it into segments.

    `language` is the track language. `ai-zh` is the machine-generated Chinese
    track, which is the only one these videos carry.

    Raises `SubtitleUnavailable` when the part has no track, which is not the
    same as a track that parses to nothing. See that exception.

    No punctuation is added. Measured across all 21 recorded tracks, the CJK
    punctuation count is zero. The returned text is one continuous run, and
    that is the source data, not a parsing artifact.
    """
    run = _run(
        [
            "--skip-download",
            "--write-subs",
            "--sub-langs", language,
            "--sub-format", "srt",
            "--playlist-items", str(part),
            "--print", _SUBTITLE_PRINT_FIELD,
            url,
        ],
        cookies,
    )
    track = _requested_track(run.stdout, language)
    if track is None:
        if _LOGIN_WARNING in run.stderr:
            raise LoginRequired(login_required_message())
        raise SubtitleUnavailable(
            f"part {part} has no {language!r} subtitle track"
        )
    segments = parse_srt(track)
    if not segments:
        # A track that carries no timed block is not a transcript either.
        raise SubtitleUnavailable(
            f"part {part} has a {language!r} track with no timed segments"
        )
    return segments


def _requested_track(payload: str, language: str) -> str | None:
    """The SRT text for `language` inside a `requested_subtitles` payload.

    Absent means there is no transcribable track, and three shapes all mean
    that. A part with no subtitles at all prints `NA`, measured on part 1 and
    on part 6. A language the video does not carry prints `NA` too, measured by
    asking part 12 for `zh-CN`. And a danmaku-only payload is JSON whose only
    key is `danmaku`, which is a bullet-comment XML feed rather than a
    subtitle.
    """
    try:
        found = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(found, dict):
        return None
    track = found.get(language)
    if not isinstance(track, dict):
        return None
    data = track.get("data")
    return data if isinstance(data, str) and data.strip() else None


def probe(url: str, cookies: Path | None = None) -> Video:
    """Report what exists at `url` without downloading file data.

    Two yt-dlp calls: JSON for structure and formats, `--list-subs` for
    subtitles, because the JSON path does not expose subtitles for Bilibili.

    Raises `LoginRequired` when the subtitle call came back with nothing and
    yt-dlp said a login was missing. Refusing here is the point: the caller
    would otherwise get 30 parts that all report no subtitles, send every one
    of them to speech recognition, and never learn why.
    """
    raw = _run(
        ["--dump-single-json", "--skip-download", url],
        cookies,
    ).stdout
    data = json.loads(raw)
    subs_run = _run(["--list-subs", "--skip-download", url], cookies)
    subs = parse_subtitle_table(subs_run.stdout)
    if login_needed(subs, subs_run.stderr):
        raise LoginRequired(login_required_message())

    entries = data.get("entries") or [data]
    parts = []
    for entry in entries:
        pid = entry.get("id", "")
        parts.append(
            Part(
                index=entry.get("playlist_index") or 1,
                id=pid,
                title=entry.get("title", ""),
                duration_sec=float(entry.get("duration") or 0.0),
                subtitle_langs=subs.get(pid, ()),
            )
        )

    return Video(
        id=data.get("id", ""),
        title=data.get("title", ""),
        parts=tuple(sorted(parts, key=lambda p: p.index)),
        formats=tuple(entries[0].get("formats") or ()),
        # Both streams, because the sentence's stream is not fixed. Measured
        # today it arrives on stdout with a working credential; the recorded
        # login warning shows the sibling signal arriving on stderr.
        missing_tiers=missing_tiers(subs_run.stdout, subs_run.stderr),
    )


@dataclass(frozen=True)
class PartTranscript:
    """One part's transcript, whichever source produced it.

    Both sources land here so the delivered text does not depend on which one
    was used. `source` is the only field that names the difference, and **it
    never reaches the delivered file.** That separation is deliberate and it is
    not a contradiction of the accepted decision "use the subtitles as they
    are, no source marking and no quality grading": that decision is about the
    file the reader opens. The program still needs to know, because a part
    taken from a platform track and a part filled in by ASR carry different
    timestamp granularity and different punctuation.

    `status` is `"ok"`, `"skipped"` or `"failed"`, and a failed record always
    carries a `failure_reason`. Ticket #17 had a third value, `pending`, for a
    part whose track was missing and whose audio had not been fetched yet. It is
    gone now that both outcomes are decided in one pass: a part either produced
    a transcript or it failed, and a value nothing can produce is vocabulary that
    only invites a caller to handle a case that cannot happen.

    `skipped` is ticket #19's value, for a part whose delivered file was already
    on disk and complete. It is separate from `ok` because the summary and the
    caller both need to tell "this run made it" from "it was already there", and
    counting both as delivered would report a second pass that produced two
    parts as a pass that produced thirty.

    `status` and `failure_reason` stay separate because the reason decides
    whether a rerun can help. `no_audio` cannot be fixed by retrying;
    `asr_error` can. Merged into one field, a caller cannot tell them apart.
    """

    bvid: str
    index: int
    title: str
    duration_sec: float
    source: str | None
    language: str | None
    segments: tuple[Segment, ...]
    status: str
    failure_reason: str | None = None


def _needs_separator(left: str, right: str) -> bool:
    """Whether a half-width space belongs between two segments.

    Measured over all 21 recorded tracks, 40283 segments and 40262 adjacent
    boundaries. Exactly 55 boundaries carry an ASCII alphanumeric character on
    both sides, and gluing those welds two words together. The measured
    example is p03 segments 2012 and 2013, `very good` followed by `Good`
    becoming `very goodGood`. Nine more of the 55 are like p18 segments 366
    and 367, where `60616263646566676869` runs into `70707172737475767778` as
    one 39-digit run.

    The other 40207 boundaries take no separator. 38994 of them sit between
    two Chinese characters, so `" ".join` would insert 38994 spaces the source
    does not have and break the text into pieces. A Chinese character on
    either side therefore means no separator, and so does a punctuation mark:
    315 segments end in `%`, and `农业占比7%` followed by `然后呢这个第二就是
    第一产` reads correctly as one run.

    The rule is measured, not chosen. Changing it means re-measuring these
    four numbers.
    """
    if not left or not right:
        return False
    before, after = left[-1], right[0]
    return before.isascii() and before.isalnum() and after.isascii() and after.isalnum()


def transcript_text(segments: Iterable[Segment]) -> str:
    """Join timed segments into the continuous prose that gets delivered.

    No timestamp enters the result; the separators carry no information beyond
    the one case `_needs_separator` describes.

    Two properties of the source text show up here and are not defects.

    There is no sentence punctuation anywhere in it. Measured over all 21
    recorded tracks, 454245 text characters, the count of CJK sentence
    punctuation is zero, as is the count of ASCII `, ? ! ;`. The delivered
    text is one unbroken run, and paragraph boundaries would need a strategy
    this ticket does not add.

    Speaker repetition is preserved. 17 pairs of adjacent segments repeat
    verbatim, for instance p12 segments 418 and 419, both `也不可能啊`. They
    are two separate stretches of speech; the delivered text reads
    `...政治阻力极大啊也不可能啊也不可能啊`. Removing one would drop about a
    second of real content, so nothing is deduplicated.
    """
    pieces: list[str] = []
    previous: Segment | None = None
    for current in segments:
        if previous is not None and _needs_separator(previous.text, current.text):
            pieces.append(" ")
        pieces.append(current.text)
        previous = current
    return "".join(pieces)


def delivered_name(index: int, title: str) -> str:
    """The name a delivered artifact carries, from the two fields that set it.

    Takes the index and title rather than a record because the skip pre-check
    has to name an artifact before any transcript record exists for it. One
    function builds the name for both paths, so the file the pre-check probes
    and the file `write_transcript` produces cannot disagree.

    The index prefix is what makes a directory listing read in course order,
    because every title begins with the course name rather than with its own
    number.

    A title is external data, so characters that cannot appear in a Windows
    file name are replaced. No real title needs this: measured over the 30
    titles of the first target, zero carry one. It is a guard at a boundary,
    not a repair.
    """
    unsafe = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    safe = unsafe.sub("_", title).strip().rstrip(".")
    return f"{index:02d}-{safe}.txt"


def transcript_filename(record: PartTranscript) -> str:
    """The delivered file's name for one record."""
    return delivered_name(record.index, record.title)


def transcript_path(out_dir: Path, index: int, title: str) -> Path:
    """Where this part's delivered artifact belongs, without writing it."""
    return Path(out_dir) / delivered_name(index, title)


def write_transcript(record: PartTranscript, out_dir: Path) -> Path:
    """Write one delivered transcript and return its path.

    Plain text, UTF-8, no timestamps, no source marker, no quality grade. The
    name carries the part index and title.

    A failed record has no transcript to write. Writing an empty file for one
    would look finished, which is the outcome `SubtitleUnavailable` exists to
    prevent, so callers must not ask.

    **The payload is built in full, written to a neighbouring temporary file,
    then swapped into place.** `Path.write_text` opens the target with mode `w`,
    which truncates it before writing anything, so an interruption in between
    leaves a short file under the delivered name. Measured on this machine, a
    finished 1500 byte artifact became zero bytes when the rewrite failed at
    the encode step, and the same thing happens to a process killed mid-write.
    After this change an interruption leaves either the old artifact untouched
    or no file at all, never a plausible fragment. That matters more since the
    skip check reads the artifact to decide whether to trust it: a truncated
    file is exactly the shape the check has to judge, and this keeps the module
    from being the thing that creates one.

    The temporary file is a sibling, not a file in the system temp directory.
    `os.replace` is only atomic within one filesystem, and a temp directory is
    often on another, where the call degrades to a copy and the atomicity goes
    away.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_path(out_dir, record.index, record.title)
    payload = transcript_text(record.segments)
    # Encoded before the temporary file exists, so a payload that cannot be
    # encoded fails with the previous artifact still in place.
    data = payload.encode("utf-8")
    staging = path.with_name(f".{path.name}.partial")
    try:
        staging.write_bytes(data)
        os.replace(staging, path)
    finally:
        # A crash cannot reach this, but a failed `os.replace` can, and a
        # leftover working file would sit in the output directory looking like
        # an artifact to anything that globs it.
        staging.unlink(missing_ok=True)
    return path


# The lowest density a finished transcript can have and still be called
# finished. Measured over the 30 delivered artifacts of the first real course,
# the range is 4.239 characters per second of probed part duration on part 30
# to 5.418 on part 23, a spread of 1.28 times across parts of one course, so
# the floor is deliberately not set at the observed low end. A floor above a
# real part's density would re-transcribe a finished part, which costs minutes;
# the floor below accepts a file that is only grossly short, which is what the
# atomic write in `write_transcript` exists to make impossible in the first
# place. 3.0 carries no false rejection on any of the 30 measured artifacts and
# rejects the measured half file at 2.701.
_MIN_CHARS_PER_SEC = 3.0


def _skipped_record(part: Part, bvid: str) -> PartTranscript:
    """The record for a part whose artifact was already on disk.

    Carries no segments. It could read them back, but nothing consumes them
    here: the delivered file is the artifact, the run does not rewrite it, and
    reading a 50 KB file per skipped part to fill a field nobody asks for is
    work without a reader. `status` is what the summary and the caller go on.

    `skipped` joins the spec's `ok` / `failed`. A skipped part is `ok` in the
    sense that its transcript exists, and it is deliberately not recorded as
    `ok` because that would count it among the parts this run produced. The
    ticket asks for exactly that separation.
    """
    return PartTranscript(
        bvid=bvid,
        index=part.index,
        title=part.title,
        duration_sec=part.duration_sec,
        source=None,
        language=None,
        segments=(),
        status="skipped",
    )


def transcript_complete(path: Path, duration_sec: float) -> bool:
    """Whether the artifact at `path` is a finished transcript for this part.

    Existence alone cannot answer this. The delivered file is continuous UTF-8
    text with no trailing newline and no end marker, so a file left by an
    interrupted write is a clean prefix of the real thing and looks like a
    transcript from the outside. Treating one as finished loses the content
    that never got written and never says so, which is the failure this
    function exists to prevent.

    The file must decode as strict UTF-8. Every artifact is UTF-8 Chinese, so
    most of its bytes are the leading or continuation byte of a three byte
    character. Cutting the stream at an arbitrary offset therefore usually
    splits one, and measured over 30 real artifacts at 11 cut points, 223 of
    330 arbitrary byte cuts raise UnicodeDecodeError.

    The remaining 107 cuts land on a character boundary and decode cleanly, so
    decoding is not enough on its own. Measured on part 23 cut at byte 25591,
    the half file is 8537 clean characters and reads as a transcript. The
    second check is therefore length against the probed duration: a finished
    artifact carries at least `_MIN_CHARS_PER_SEC` characters per second of its
    part, and an interrupted one carries a fraction of that.
    """
    path = Path(path)
    if not path.is_file():
        return False
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return False
    if not text.strip():
        return False
    return len(text) >= _MIN_CHARS_PER_SEC * duration_sec


def _failed_record(bvid: str, part: Part, reason: str) -> PartTranscript:
    """The record for a part that produced no transcript.

    `status = "failed"` and `failure_reason` are separate fields because the
    reason decides whether a rerun is worth anything. **`no_audio` cannot be
    fixed by retrying; `asr_error` can.** A part whose audio does not exist on
    the platform will still not have it on the tenth attempt, so a caller that
    retries one spends an hour of recognition to fail again. A part whose
    engine raised may well succeed on the next run, so a caller that gives up
    on one loses a transcript it could have had. Merged into a single field, no
    caller can tell which of the two it is holding, and the two call for
    opposite actions.

    Nothing is written for a failed part. An empty file reads as a finished
    transcript, so `transcripts` guards the write on `status == "ok"`.

    A failed record carries no `source`: no path produced the text, and naming
    one would claim a provenance that does not exist.
    """
    return PartTranscript(
        bvid=bvid,
        index=part.index,
        title=part.title,
        duration_sec=part.duration_sec,
        source=None,
        language=None,
        segments=(),
        status="failed",
        failure_reason=reason,
    )


# What the ASR path reports as the language of a transcript it produced. The
# engine is `paraformer-zh`, so the answer is Chinese regardless of what the
# platform track was called. Measured on the 21 recorded tracks, the platform
# side is always `ai-zh`, and the spec allows `"zh"` as the ASR value.
ASR_LANGUAGE = "zh"

# The name `download_audio` writes, so a part's audio can be found again
# without a second network call. `%(playlist_index)02d` is what puts the part
# number in the name, and the extension is `wav` after the transcoding step.
AUDIO_TEMPLATE = "%(playlist_index)02d.%(ext)s"


def audio_path(audio_dir: Path, index: int) -> Path:
    """Where `download_audio` puts one part's WAV."""
    return Path(audio_dir) / f"{index:02d}.wav"


def part_audio(
    video: Video,
    part: Part,
    audio_dir: Path,
    cookies: Path | None = None,
) -> Path | None:
    """The 16 kHz mono WAV for one part, downloading it only when absent.

    Already on disk, it is returned without a network call. The nine parts of
    the first target that need ASR were downloaded once and total 1.1 GB, so
    re-fetching them on a rerun would cost that download again for nothing.

    None means yt-dlp reported no file, which is what `_asr_record` turns into
    `failure_reason = "no_audio"`. This function does not decide that outcome;
    it only reports it.
    """
    existing = audio_path(audio_dir, part.index)
    if existing.is_file():
        return existing
    written = download_audio(
        video_url(video.id), audio_dir, parts=[part.index], cookies=cookies
    )
    if not written:
        return None
    return written[0]


def _asr_record(
    video: Video,
    part: Part,
    audio_dir: Path,
    engine,
    cookies: Path | None,
    device: str,
) -> PartTranscript:
    """Complete one part from its audio, in the same shape the platform path uses.

    The only field that differs between the two sources is `source`. Everything
    else is filled the same way, which is what lets a deliverable be produced
    without asking where it came from.

    `LoginRequired` is not caught here and must not be. It travels up with its
    next-step message, because transcribing a whole course in response to a
    stale credential would cost about 39 minutes and produce a full set of
    transcripts from the wrong cause.

    Two accepted tradeoffs land in this record rather than in the delivered
    file.

    The ASR path carries punctuation and the platform path does not, so one
    course mixes two styles. Measured over the 21 recorded tracks, 454245 text
    characters, the CJK punctuation count is zero; `ct-punc` puts marks in the
    ASR text. Repairing the platform side would mean running punctuation
    restoration over it too, and that recomputes the timestamps that are already
    correct. Not done in this round.

    No Simplified to Traditional conversion is applied. Both sides come back in
    Simplified and are delivered that way. If that ever changes, the correct
    package is `opencc`; two similarly named packages mistranslate, turning
    「头发」 into 「頭發」 because they convert character by character instead of
    by phrase.
    """
    wav = part_audio(video, part, audio_dir, cookies)
    if wav is None:
        # The platform reported no audio file for this part, so there is
        # nothing to transcribe and no transcript to deliver. The part is
        # recorded `failed` with `failure_reason = "no_audio"`, the value that
        # tells a caller not to retry: the audio does not exist to be fetched,
        # so a second attempt fails the same way at the same point. The batch
        # carries on, because one part with no audio must not end the run.
        return _failed_record(video.id, part, "no_audio")
    if engine is None:
        # One load per batch is the point of the `engine` argument. A caller
        # walking 30 parts passes one in; this branch is for a caller that
        # transcribes a single part.
        engine = load_engine(device)
    segments = transcribe(wav, engine=engine, device=device)
    return PartTranscript(
        bvid=video.id,
        index=part.index,
        title=part.title,
        duration_sec=part.duration_sec,
        source="asr",
        language=ASR_LANGUAGE,
        segments=tuple(segments),
        status="ok",
    )


def video_url(reference: str) -> str:
    """Turn a bare BV number into the URL yt-dlp takes.

    `BV1Y5QgB5Eas` is what a user has in hand and `Video.id` holds exactly
    that, while the extractor needs a URL in the argument position. Handing it
    the bare identifier fails with `'BV1Y5QgB5Eas' is not a valid URL`,
    measured against the real program. Both shapes are accepted so neither has
    to be converted by hand.
    """
    reference = reference.strip()
    if reference.startswith(("http://", "https://")):
        return reference
    return f"https://www.bilibili.com/video/{reference}"


def _preferred_language(part: Part) -> str:
    """The track to take for a part that has more than one."""
    for preferred in ("ai-zh", "zh-CN", "zh"):
        if preferred in part.subtitle_langs:
            return preferred
    return part.subtitle_langs[0]


def part_transcript(
    video: Video,
    part: Part,
    cookies: Path | None = None,
    audio_dir: Path | None = None,
    engine=None,
    device: str = "cpu",
) -> PartTranscript:
    """Judge one part and produce its transcript record, from either source.

    The judgment is per part, never per video. Measured on the first real
    target, 21 of 30 parts carry a track and 9 do not, and the state does not
    follow part length, so a whole-video answer would be wrong for one group
    or the other.

    **The two sources meet here and nowhere downstream.** A part with a track
    is assembled from it with `source = "platform"`; a part without one is
    transcribed from its own audio with `source = "asr"`. Every other field is
    filled identically, so a caller that has a `PartTranscript` cannot tell
    which path produced it, and `write_transcript` does not ask. This is the
    join the ticket is about, and it is why a missing part is completed
    without a second command.

    A credential fault is not either source and is not swallowed. Once the
    login is stale every part reads as trackless, so sending all 30 to the ASR
    path would cost about 39 minutes in response to a problem that
    re-exporting the credential fixes. Ticket #13 raises `LoginRequired` for
    that, and it is deliberately not caught here, so it travels up with its
    next-step message. `SubtitleUnavailable` is the only outcome that means
    "this part needs ASR".

    `engine` is passed in for the same reason `transcribe` takes one: a load
    costs 29 to 50 seconds, so a batch loads once. `audio_dir` is where the
    part's WAV lives, or is put.
    """
    if not part.has_subtitles:
        return _asr_record(
            video, part, audio_dir or default_audio_dir(), engine, cookies, device
        )
    language = _preferred_language(part)
    try:
        segments = part_subtitles(video_url(video.id), part.index, language, cookies)
    except SubtitleUnavailable:
        # The probe table and the fetch can disagree, for instance if the
        # track was withdrawn between the two calls. Either way this part
        # still needs a transcript from somewhere, and ASR is that somewhere.
        return _asr_record(
            video, part, audio_dir or default_audio_dir(), engine, cookies, device
        )
    return PartTranscript(
        bvid=video.id,
        index=part.index,
        title=part.title,
        duration_sec=part.duration_sec,
        source="platform",
        language=language,
        segments=tuple(segments),
        status="ok",
    )


def default_audio_dir() -> Path:
    """Where a part's audio goes when the caller names no directory.

    Kept apart from the delivered transcripts, because a WAV is a working
    file: 100 MB per part against a few hundred KB of text.
    """
    return Path.cwd() / "audio"


class _DeferredEngine:
    """Loads the engine on first use, and only if something needs it.

    Built for `transcripts` so that a batch which never hits a part without a
    track does not pay 29 to 50 seconds for a model it will not use, while a
    batch that does hit one still loads it exactly once. `transcribe` receives
    this object as if it were the engine, and the first call to `generate`
    triggers the load.

    Device verification is the caller's, not this class's, so a silently
    downgraded GPU request still fails before any audio is read.
    """

    def __init__(self, device: str = "cpu", loader=None):
        self._device = device
        self._loader = loader or load_engine
        self._engine = None
        self.loads = 0

    def _resolve(self):
        if self._engine is None:
            self._engine = self._loader(self._device)
            self.loads += 1
        return self._engine

    def generate(self, **kwargs):
        return self._resolve().generate(**kwargs)

    # `verify_device` reads this, so it is forwarded to the real engine rather
    # than answered here. Reading it before the engine is loaded is what would
    # defeat the point, so the load happens first.
    @property
    def model(self):
        return self._resolve().model


def transcripts(
    url: str,
    out_dir: Path,
    parts: list[int] | None = None,
    cookies: Path | None = None,
    audio_dir: Path | None = None,
    engine=None,
    device: str = "cpu",
) -> tuple[PartTranscript, ...]:
    """Judge every part of `url` and deliver one transcript per part.

    Both sources are covered in one pass. A part with a track is delivered
    from that track; a part without one is transcribed from its own audio and
    delivered the same way, with no second command and no separate flag.

    `url` accepts a bare BV number as well as a link, the same as the CLI. The
    documented call is `transcripts("BV1Y5QgB5Eas", ...)`, and handing a bare
    identifier straight to yt-dlp fails with `'BV1Y5QgB5Eas' is not a valid
    URL`, measured against the real program.

    Returns one record per part, in course order, after writing a file for
    each. A failed part produces a record with no file and a `failure_reason`
    naming why, which `transcript_report` prints once the batch is done.

    Processing does not stop at a part without subtitles, and it does not stop
    at a part that fails. One part with no audio, or one part whose ASR raised,
    is that part's problem; the remaining parts are still worth their
    transcript, and a 30-part batch that abandoned everything on one fault
    would waste the other 29 parts of compute. A part already delivered is not
    touched by a later failure: each part is written as it completes and
    nothing rewinds.

    Two faults are deliberately not treated as failed parts, because they are
    not conditions of a part. A credential fault makes every part look
    trackless, and a missing ASR install makes every part fail at the engine.
    Both travel up with the message naming the one action that fixes them,
    rather than being recorded thirty times as thirty failing parts.

    **The engine is loaded once for the whole batch, not once per part.** The
    measured cost is 29 to 50 seconds per load against about 200 seconds per
    47 minute part, so a per-part load would add roughly 20 minutes to this
    batch. `_DeferredEngine` defers the load until a part actually needs it,
    and a caller that already holds an engine passes it in as `engine`.
    """
    video = probe(video_url(url), cookies)
    selected = [
        part for part in video.parts if parts is None or part.index in parts
    ]
    holder = engine if engine is not None else _DeferredEngine(device)
    records: list[PartTranscript] = []
    for part in selected:
        # The pre-check. A finished artifact means no work at all for this
        # part: no subtitle fetch, no audio download, no model call. This is
        # what makes a second run over a nearly complete course fast, and it
        # is the only persistence the spec allows.
        #
        # It reads the artifact rather than trusting that the name exists,
        # because an interrupted write leaves a clean prefix of a real
        # transcript under the real name. See `transcript_complete`.
        #
        # It sits ahead of the `try` below on purpose. A part that is already
        # delivered cannot fail, and skipping it keeps the failure path from
        # having to reason about a part it did no work for.
        existing = transcript_path(out_dir, part.index, part.title)
        if transcript_complete(existing, part.duration_sec):
            records.append(_skipped_record(part, video.id))
            continue
        try:
            record = part_transcript(
                video,
                part,
                cookies=cookies,
                audio_dir=audio_dir or default_audio_dir(),
                engine=holder,
                device=device,
            )
        except (LoginRequired, TranscriptionUnavailable):
            # Neither is a condition of this part, so neither is a failed part.
            # A stale credential makes every part look trackless, and a missing
            # ASR install makes every part fail at the engine. Recording either
            # one thirty times would report a single machine-level fault as
            # thirty per-part faults and drop the one action that fixes it,
            # which is the install or the re-login its own message names.
            raise
        except Exception as exc:  # noqa: BLE001
            # Every other exception is this part's own, and the class is not
            # the information the caller acts on. The engine stack raises what
            # it raises: a torch allocation fault, a decoder refusing a
            # damaged WAV, or this module's own refusal to pair a mismatched
            # ASR result. All of them mean the same thing operationally, which
            # is that this part produced no transcript and a rerun may well
            # succeed, so all of them are `asr_error`. A 30-part batch must not
            # end because one part did, and an interrupt still must, so
            # KeyboardInterrupt is left alone by catching `Exception`.
            record = _failed_record(video.id, part, "asr_error")
            print(
                f"part {part.index} failed (asr_error): "
                f"{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
        if record.status == "ok":
            # A record that is not `ok` has no transcript to write, and an
            # empty file for one would read as finished. This is also what
            # keeps a failure from touching a part already delivered: each part
            # is written as it completes and nothing rewinds, so a failure on
            # part 20 leaves parts 1 to 19 on disk exactly as they were.
            write_transcript(record, out_dir)
        records.append(record)
    return tuple(records)


# The sentence yt-dlp prints when the platform withheld a tier it knows about.
# Measured on the first real target, it reads
#
#   [BiliBili] Format(s) 1080P 高码率 are missing; you have to become a premium
#   member to download them.
#
# The format table cannot substitute for this. Measured on the same video with
# a working credential and with none, the two `formats` tables are identical
# row for row, 15 rows each with the same `format_id`, `quality`, `format` and
# `tbr`, and the highest `quality` is 80 in both. So nothing in the JSON says
# whether the high-bitrate tier was withheld, and a caller reading only
# `formats` reports the normal 1080P as if it were the premium one.
_MISSING_TIERS = re.compile(r"Format\(s\) (.+?) are missing")

# What marks a tier as the membership one. `1080P 高码率` is the measured name,
# and the marker is the distinguishing word rather than the whole name: the
# normal tier on the same page is `1080P 高清`, which differs only in that word.
# Keying on the full name would silently stop matching if the platform reworded
# the rest, and a missed warning is the failure this exists to prevent. It does
# not match `1080P 高清` or `720P 准高清`, so the two do not collide.
_PREMIUM_MARKER = "高码率"


def missing_tiers(text: str, *more: str) -> tuple[str, ...]:
    """The tiers yt-dlp reported as withheld, in the order it named them.

    Accepts any number of text blocks so a caller can pass every stream it
    captured without deciding which one holds the sentence. The measured
    location is stdout and it repeats once per part, so the result is
    deduplicated; a 30-part course prints the same sentence 30 times.

    An empty result means either that nothing was withheld or that yt-dlp said
    nothing, and the two are not distinguished here. Reporting "no tier is
    missing" is the honest reading of silence.
    """
    found: list[str] = []
    for block in (text, *more):
        for line in block.splitlines():
            match = _MISSING_TIERS.search(line)
            if not match:
                continue
            for tier in match.group(1).split(","):
                tier = tier.strip()
                if tier and tier not in found:
                    found.append(tier)
    return tuple(found)


def premium_unavailable(tiers: Iterable[str]) -> bool:
    """Whether the withheld tiers include the membership high-bitrate one.

    Kept apart from `missing_tiers` because the two answer different questions
    and only this one changes what the user is told. A caller that asks for
    1080p receives the normal tier and would otherwise believe it got the
    better one, which is the confusion this exists to remove.
    """
    return any(_PREMIUM_MARKER in tier for tier in tiers)


# The qualities `_format_selector` accepts, with the format id it names for
# each. One table serves the report, the validation and the selector, so the
# names a caller is offered are exactly the names that work.
_QUALITIES: dict[str, str] = {
    "360p": "30011",   # hevc, ~35 MB/part
    "480p": "30033",   # hevc,  ~52 MB/part
    "720p": "30066",   # hevc, ~131 MB/part
    "1080p": "30080",  # avc1, ~336 MB/part
}

# What a download gets when the caller names no quality. 720p is the measured
# middle: 87.6 MiB per part against 336.4 for 1080p, and the video is lecture
# material where the text on screen is what matters.
_DEFAULT_QUALITY = "720p"


def _format_selector(quality: str) -> str:
    """Map a quality name to a yt-dlp format expression.

    Bilibili serves video and audio as separate streams and does not offer a
    muxed one, so both selectors name a video stream plus an audio stream.
    """
    if quality not in _QUALITIES:
        raise ValueError(
            f"unknown quality {quality!r}; expected one of {sorted(_QUALITIES)}"
        )
    return f"{_QUALITIES[quality]}+{_AUDIO_FORMAT_ID}"  # 30280 = highest-bitrate audio


# The highest-bitrate audio stream, which is what every selector pairs with.
_AUDIO_FORMAT_ID = "30280"


def _download(
    url: str,
    parts: list[int] | None,
    selector: str,
    out_dir: Path,
    cookies: Path | None,
    template: str,
    extra: list[str],
) -> list[Path]:
    """Download, returning the paths yt-dlp reports it wrote.

    The paths come from yt-dlp's own `after_move:filepath` output rather than
    from globbing the output directory. Globbing cannot work here: the
    template contains `%(...)s` placeholders, and post-processing (merging,
    audio extraction) can change the extension, so the file on disk is not
    predictable from the template alone.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    args: list[str] = []
    if parts:
        args += ["--playlist-items", ",".join(str(p) for p in parts)]
    args += [
        "-f", selector,
        "-o", str(out_dir / template),
        "--print", "after_move:filepath",
        url,
    ]
    stdout = _run(args + extra, cookies).stdout
    return [Path(line) for line in stdout.splitlines() if line.strip()]


def download_video(
    url: str,
    out_dir: Path,
    parts: list[int] | None = None,
    quality: str = "720p",
    cookies: Path | None = None,
) -> list[Path]:
    """Download video files, merging Bilibili's separate video and audio streams.

    `parts` selects 分P by index; None means every part.
    """
    return _download(
        url=url,
        parts=parts,
        selector=_format_selector(quality),
        out_dir=out_dir,
        cookies=cookies,
        template="%(playlist_index)02d-%(title).80s.%(ext)s",
        extra=["--merge-output-format", "mp4"],
    )


def download_audio(
    url: str,
    out_dir: Path,
    parts: list[int] | None = None,
    cookies: Path | None = None,
) -> list[Path]:
    """Download audio only, as 16 kHz mono WAV ready for ASR.

    Resampling happens here rather than in the transcriber so the file on disk
    is the file the model wants.
    """
    return _download(
        url=url,
        parts=parts,
        selector="30280",
        out_dir=out_dir,
        cookies=cookies,
        template=AUDIO_TEMPLATE,
        extra=[
            "-x",
            "--audio-format", "wav",
            "--postprocessor-args", "-ar 16000 -ac 1",
        ],
    )


# What each failure reason means for a rerun. `no_audio` cannot be fixed by
# retrying: the platform has no audio for that part, so the tenth attempt fails
# at the same place as the first. `asr_error` can: the engine loaded and
# worked on other parts, so a transient fault, a damaged file fetched once, or
# a model that ran out of memory may well succeed next time.
#
# The reason string alone does not say this, and the person reading the summary
# has just waited 40 minutes. Naming the consequence beside the reason is the
# whole point of keeping `status` and `failure_reason` apart, so the summary
# states it rather than making the reader look it up.
_RETRYABLE = {"asr_error": True, "no_audio": False}


def transcript_report(records: Iterable[PartTranscript], out_dir: Path) -> str:
    """The line printed after a batch, naming what was produced and from where.

    The source split is the point of the first two lines. A part filled by ASR
    and a part taken from a platform track produce files that read differently,
    so a reader who saw only "30 of 30" would not know how much of the course
    was machine-transcribed.

    The failed line names each part and its reason, and says which of the two
    reasons a rerun would fix. That is the distinction `failure_reason` exists
    for. A caller who cannot tell `no_audio` from `asr_error` either reruns a
    part that can never succeed or abandons one that would succeed on the next
    attempt.

    A part that failed is not counted as delivered. Its number is in the
    denominator because it was asked for, and absent from the numerator because
    no file was written for it.

    Skipped parts are counted on their own line and named for the same reason.
    They were asked for, so they stay in the denominator, and they were not
    produced by this run, so they stay out of the numerator. A second pass over
    a course that is already 28 parts done would otherwise read as "2 of 30",
    which is the opposite of what happened.
    """
    records = list(records)
    delivered = [r for r in records if r.status == "ok"]
    platform = [r for r in delivered if r.source == "platform"]
    asr = [r for r in delivered if r.source == "asr"]
    skipped = [r for r in records if r.status == "skipped"]
    failed = [r for r in records if r.status == "failed"]
    lines = [
        f"delivered {len(delivered)} of {len(records)} parts to {out_dir}",
    ]
    if delivered:
        lines.append(
            f"{len(platform)} from platform subtitles, "
            f"{len(asr)} from ASR"
            + (f" (parts {', '.join(str(r.index) for r in asr)})" if asr else "")
        )
    if skipped:
        lines.append(
            f"skipped {len(skipped)} parts already delivered "
            f"(parts {', '.join(str(r.index) for r in skipped)})"
        )
    if failed:
        lines.append(
            "failed: "
            + ", ".join(
                f"{r.index} ({r.failure_reason}, {_retry_word(r.failure_reason)})"
                for r in failed
            )
        )
    return "\n".join(lines)


def _retry_word(reason: str | None) -> str:
    """How a rerun would treat one failure reason.

    An unknown reason is reported as retryable rather than silently as not.
    Guessing "not retryable" would tell a caller to abandon a part that might
    be recoverable, and this vocabulary is only ever written by this module.
    """
    return "retryable" if _RETRYABLE.get(reason, True) else "not retryable"


class AsrError(VideoKitError):
    """Raised when speech recognition cannot produce a trustworthy transcript.

    Covers both a missing engine and a result this module refuses to pair. The
    second case matters more: a mismatched result is not a crash to retry, it
    is a transcript that would be silently wrong if it were returned.
    """


class TranscriptionUnavailable(AsrError):
    """Raised when the ASR stack is not installed on this machine.

    Separate from the other failures because the caller's response is a
    different one. Nothing about the audio or the network is wrong here, so
    retrying is pointless and the fix is an install.
    """


# The three names in the message below are all required, and the third is the
# one that is easy to miss. funasr and torch alone install cleanly and load a
# model successfully, then raise ImportError the moment audio is fed in.
# Measured on this machine, funasr's own message for that case names both
# backends:
#
#   ImportError: torchaudio is not installed and neither is the
#   kaldi-native-fbank fallback backend.
#
# Either backend satisfies it. This package takes kaldi-native-fbank because it
# does not pin a torch version the way torchaudio does.
_MISSING_DEPS = (
    "the ASR stack is not installed. Transcription needs three packages, "
    "declared as this package's `asr` extra:\n"
    "    uv sync --extra asr\n"
    "funasr is the engine, torch is its tensor backend, and "
    "kaldi-native-fbank extracts the features. All three are requirements. "
    "Without the third, funasr loads a model and then raises ImportError when "
    "the first audio arrives."
)


@dataclass(frozen=True)
class RecognisedUnit:
    """One thing the engine timed: a Han character or a run of ASCII.

    `start` and `end` are offsets into the transcript text, so a caller can
    find what sits between two of them.
    """

    start: int
    end: int
    text: str


# One Han character, or one whole run of ASCII letters and digits, is one
# recognised unit. Measured on a real 3160.2 second run: 15883 Han characters
# plus 25 ASCII runs is 15908, exactly the number of millisecond pairs the
# engine returned, while the text has 17123 characters. The difference is
# punctuation, which consumes no time at all.
#
# The ASCII run is one unit rather than one per letter, and that is not an
# approximation. Measured on the same run, `GDP` appears 23 times, `WTO` 9
# times, and pairing per character instead of per run overruns by exactly the
# 61 extra letters those runs contain.
_UNIT_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]")

# Where a segment may end. The four CJK marks are the obvious ones. `.` and `,`
# are here because ct-punc writes western marks after ASCII words, and leaving
# them out changes the measured segment count on a full run from 1152 to 1148.
# The rest cost nothing to accept and cover marks other runs may produce.
_CUT_MARKS = "，。？！、；：,.;:!?"


def recognised_units(text: str) -> list[RecognisedUnit]:
    """Split transcript text into the units the engine timed.

    See `_UNIT_RE` for why a character and an ASCII run count alike.
    """
    return [
        RecognisedUnit(start=m.start(), end=m.end(), text=m.group())
        for m in _UNIT_RE.finditer(text)
    ]


def asr_segments(text: str, timestamps: list[list[int]]) -> list[Segment]:
    """Pair engine text with engine timestamps, cutting at punctuation.

    `timestamps` is the `timestamp` field of a result, a list of
    `[start_ms, end_ms]` pairs. The cut is made by scanning the text for
    punctuation rather than by reading the result's `sentence_info`, and that
    is deliberate. On a full-length run, FunASR 1.4.16 groups sentences against
    text it has split differently, so `sentence_info` text drifts away from the
    top-level `text` starting at character 318. Its millisecond bounds are
    correct, its text is not, so this module rebuilds the grouping instead.
    Measured on the saved run, the two agree on every one of the 1152
    boundaries, so nothing is lost by not reading it.

    Fillers are not removed. `啊`, `呢`, `就是` and `这个` stay exactly where the
    engine put them. Measured on the saved run, `text.count("啊")` is 277 and
    `text.count("这个")` is 424. This is a decision rather than an oversight:
    the transcript is meant to read as the speaker talked, and the same course
    carries platform subtitles that keep fillers too.

    Raises `AsrError` when the two counts disagree, and that check is the point
    of the function rather than a courtesy. Any pairing made after a mismatch
    is a guess that still produces plausible-looking output, which is the
    failure mode worth refusing.
    """
    units = recognised_units(text)
    if not units:
        raise AsrError("the engine returned no transcript text")
    if len(units) != len(timestamps):
        raise AsrError(
            f"the engine returned {len(units)} recognised units but "
            f"{len(timestamps)} timestamps; no pairing can be trusted"
        )

    segments: list[Segment] = []
    unit_cursor = 0  # First unit of the segment being built.
    char_cursor = 0  # First character of the segment being built.
    for i, unit in enumerate(units):
        following = units[i + 1].start if i + 1 < len(units) else len(text)
        cut = next(
            (
                unit.end + offset
                for offset, char in enumerate(text[unit.end : following])
                if char in _CUT_MARKS
            ),
            None,
        )
        if cut is None:
            continue
        segments.append(
            Segment(
                start_sec=timestamps[unit_cursor][0] / 1000,
                end_sec=timestamps[i][1] / 1000,
                text=text[char_cursor : cut + 1].strip(),
            )
        )
        unit_cursor = i + 1
        char_cursor = cut + 1

    # Speech after the last mark has no mark of its own to end on, so it takes
    # the end of the audio. Both the saved full-length run and a 60 second
    # slice end this way.
    tail = text[char_cursor:].strip()
    if unit_cursor < len(units) and tail:
        segments.append(
            Segment(
                start_sec=timestamps[unit_cursor][0] / 1000,
                end_sec=timestamps[-1][1] / 1000,
                text=tail,
            )
        )
    return segments


def detect_device(model) -> str:
    """Where the model's parameters actually live, as a string.

    Read from the parameters because the obvious attributes lie. Measured on
    this machine, `model.device` raises AttributeError, and
    `getattr(model.model, "device", None)` returns None even when the model is
    on the CPU, which reads as "no information" rather than as "the CPU".
    """
    try:
        parameters = model.model.parameters()
    except AttributeError as exc:
        raise AsrError(
            "cannot read a device from this engine; it exposes no "
            f"model.model.parameters() ({exc})"
        ) from exc
    try:
        return str(next(parameters).device)
    except StopIteration as exc:
        raise AsrError(
            "cannot read a device from this engine; its model has no parameters"
        ) from exc


def verify_device(model, requested: str) -> str:
    """Confirm the requested device is the one in use, and report it back.

    FunASR downgrades silently. Measured on this machine, passing
    `device="cuda:0"` where `torch.cuda.is_available()` is False loads the
    model on the CPU and returns a perfectly good transcript, so nothing
    downstream can tell the two apart. Checking here is the only place the
    difference is visible, and a caller who asked for a GPU wants to know it
    did not get one.

    Returns the actual device so a caller can record it.
    """
    actual = detect_device(model)
    if requested.startswith("cuda") and not actual.startswith("cuda"):
        raise AsrError(
            f"requested device {requested} but the model is running on "
            f"{actual}. FunASR does not report this itself."
        )
    return actual


def engine_kwargs(device: str = "cpu") -> dict:
    """The engine configuration, as the keyword arguments `AutoModel` takes.

    `punc_model` is a requirement and not a refinement. Measured with it
    disabled, the output is 1511 spaces among 3023 characters with no
    punctuation of any kind, and there is then nothing in the text to cut a
    segment on. It also reassembles the per-unit markers into continuous text.

    CPU is the default because it is enough. Measured at 13.4 to 15.3 times
    real time on this machine, so a 47 minute part takes under 4 minutes.
    """
    return {
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",  # Splits long audio into utterance chunks.
        "punc_model": "ct-punc",
        "device": device,
        "disable_update": True,  # The models are cached; skip the online check.
        "disable_pbar": True,    # A progress bar writes escape codes into logs.
    }


def load_engine(device: str = "cpu"):
    """Build the ASR engine, or explain what is missing.

    Imported here rather than at module scope so that probing, downloading and
    subtitle retrieval keep working on a machine that has none of the ASR
    stack. It is a 2 GB dependency and most callers never transcribe anything.
    """
    try:
        from funasr import AutoModel
    except ImportError as exc:
        raise TranscriptionUnavailable(_MISSING_DEPS) from exc
    model = AutoModel(**engine_kwargs(device))
    verify_device(model, device)
    return model


def transcribe(
    wav: Path,
    engine=None,
    device: str = "cpu",
) -> list[Segment]:
    """Turn one 16 kHz mono WAV into that part's timed segments.

    One part per call, and the audio is expected to be the file
    `download_audio` already produced. The model is 2 GB and lives behind the
    `engine` argument, so a caller working through 30 parts loads it once and
    passes it in rather than paying 29 to 50 seconds per part.

    Device verification happens before the engine is handed any audio, so a
    silently downgraded GPU request fails at startup rather than after minutes
    of work.
    """
    if engine is None:
        engine = load_engine(device)
    verify_device(engine, device)
    try:
        result = engine.generate(input=str(wav), batch_size_s=300)[0]
    except ImportError as exc:
        # The fbank backend is imported lazily, at feature extraction rather
        # than at model load, so a missing one surfaces here on the first file
        # instead of at `load_engine`. Only that case is translated; any other
        # import failure is a different problem and keeps its own traceback.
        if "fbank" not in str(exc):
            raise
        raise TranscriptionUnavailable(_MISSING_DEPS) from exc
    return asr_segments(result.get("text", ""), result.get("timestamp") or [])


# --- The command line entry point (ticket #20) ----------------------------
#
# One entry point exposes all four capabilities so a caller does not have to
# import the module to use them. The help text is part of the deliverable: the
# ticket asks that the argument names and the help be usable by someone who has
# not read the source, so what each subcommand does is stated rather than
# implied by its name. "video" and "audio" differ only in what comes back, and
# a reader who cannot tell them apart picks the wrong one and downloads an hour
# of video to transcribe.

_USAGE = """usage: videokit <子命令> <BV号或链接> [选项]

子命令
  probe       探测。报告有多少讲、每讲多长、哪些有字幕、有哪些清晰度档位。
              只是问，不下载任何文件数据。
  video       下载视频。按清晰度下选定讲次，分开的音视频流自动合并成一个文件。
  audio       只下音频。直接落成 16 kHz 单声道 WAV，就是语音识别要的格式，
              同一讲的文件从上百 MB 降到几十 MB。
  transcript  转文稿。有字幕的讲次取平台字幕，无字幕的取音频跑本地语音识别，
              两者都写成交付文稿，一条命令跑完。
  login       登录一次。拉起浏览器扫码，凭据落盘，之后不必再登。

选项
  --parts 3,4        只处理这几讲。省略表示全部讲次。
  --out 目录         产物写到这里。
  --audio-out 目录   transcript 用的音频落点。
  --quality 720p     视频清晰度，仅 video 用。可选 360p / 480p / 720p / 1080p。
  -h, --help         显示帮助。

例子
  videokit probe BV1Y5QgB5Eas
  videokit video BV1Y5QgB5Eas --quality 720p --parts 3,4 --out videos
  videokit audio BV1Y5QgB5Eas --out audio
  videokit transcript BV1Y5QgB5Eas --out transcripts
"""

# One entry per subcommand: the usage line a reader can copy, then what the
# command does in one sentence. Kept as data so the per-command help and the
# list in `_USAGE` cannot drift apart, and so a new subcommand cannot be added
# without appearing in the help.
_SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "probe": (
        "videokit probe <BV号或链接> [--parts 3,4]",
        "探测。报告有多少讲、每讲多长、哪些有字幕、有哪些清晰度档位，"
        "并说明会员高码率档是否拿到。不下载任何文件数据。",
    ),
    "video": (
        "videokit video <BV号或链接> [--quality 720p] [--parts 3,4] [--out 目录]",
        "下载视频。分开的音视频流自动合并成一个文件。清晰度可选 "
        "360p / 480p / 720p / 1080p，默认 720p。",
    ),
    "audio": (
        "videokit audio <BV号或链接> [--parts 3,4] [--out 目录]",
        "只下音频，落成 16 kHz 单声道 WAV，语音识别直接用这个文件。",
    ),
    "transcript": (
        "videokit transcript <BV号或链接> [--parts 3,4] [--out 目录] "
        "[--audio-out 目录]",
        "逐讲判定并产出文稿。有字幕的取平台字幕，无字幕的取音频跑本地语音识别，"
        "两者都写成交付文稿，不需要另跑一条命令。",
    ),
    "login": (
        "videokit login",
        "登录一次。拉起浏览器窗口，扫码后把凭据落盘，"
        "探测、下载、转文稿之后自动使用它。",
    ),
}

_OPTIONS_HELP = """选项
  --parts 3,4        只处理这几讲。省略表示全部讲次。
  --out 目录         产物写到这里。
  --audio-out 目录   transcript 用的音频落点。
  --quality 720p     视频清晰度，仅 video 用。可选 360p / 480p / 720p / 1080p。
  -h, --help         显示帮助。
"""

# What a caller may type to ask for help, and what the module answers to.
_HELP_FLAGS = ("-h", "--help")


def _wants_help(args: list[str]) -> bool:
    return any(a in _HELP_FLAGS for a in args)


def _references(args: list[str]) -> list[str]:
    """The positional arguments, which is where the BV number or link lives."""
    options_with_values = {"--parts", "--out", "--quality", "--audio-out"}
    found: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg in options_with_values:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        found.append(arg)
    return found


def _print_help(name: str | None = None) -> int:
    """Print the general help, or one subcommand's.

    The general form lists every subcommand, and a subcommand's own help names
    the others as well, so a reader who arrives at one of them can still find
    the rest without guessing that they exist.
    """
    if name is None:
        print(_USAGE, end="")
        return 0
    usage, description = _SUBCOMMANDS[name]
    print(f"usage: {usage}\n")
    print(description + "\n")
    print(_OPTIONS_HELP, end="")
    print("其他子命令")
    for other in _SUBCOMMANDS:
        if other != name:
            print(f"  {other}")
    return 0


def _usage_error(text: str) -> int:
    """Refuse a command line, naming what was wrong, and exit nonzero.

    The general usage is printed beside the complaint, so the reader does not
    have to run a second command to learn the syntax.
    """
    print(text, file=sys.stderr)
    print(file=sys.stderr)
    print(_USAGE, end="", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    """The command line entry point. Returns a process exit code.

    Five subcommands, dispatched by name. `--help` is answered before any
    argument is validated, so it works with no video reference, which is how a
    reader discovers the syntax in the first place.

    Both output streams are wrapped for the run so an unprintable character
    cannot turn a report into a traceback. See `_ConsoleSafe`. The real streams
    are restored on the way out, including on an exception, so importing this
    module does not leave them altered for the caller.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    real_out, real_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _safe_console(real_out), _safe_console(real_err)
    try:
        return _dispatch(argv)
    finally:
        sys.stdout, sys.stderr = real_out, real_err


def _dispatch(argv: list[str]) -> int:
    """Route one parsed command line to its subcommand."""
    if not argv:
        return _usage_error("missing subcommand")

    name, rest = argv[0], argv[1:]
    if name in _HELP_FLAGS or name == "help":
        return _print_help()
    if name not in _SUBCOMMANDS:
        return _usage_error(f"unknown subcommand: {name}")
    if _wants_help(rest):
        return _print_help(name)
    if name == "probe":
        return _probe_command(rest)
    if name == "video":
        return _video_command(rest)
    if name == "audio":
        return _audio_command(rest)
    if name == "transcript":
        return _transcript_command(rest)
    return _login_command(rest)


def _login_command(rest: list[str]) -> int:
    """`videokit login`. Opens the browser and stores the credential."""
    try:
        path = login()
    except LoginError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    print(f"credential written to {path}")
    return 0


def _duration_text(seconds: float) -> str:
    """A duration as a person would say it, with the seconds field kept.

    Seconds are the smallest unit the probe data carries, so nothing finer is
    invented. A part is minutes long and a course is hours long, so hours
    appear only when there are any; a reader comparing part lengths is not
    helped by a leading `0小时`.
    """
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    return f"{minutes}分{secs}秒"


def _mebibytes(bytes_: float) -> str:
    """A size as MiB, one decimal, which is the unit the download sizes read in."""
    return f"{bytes_ / 1048576:.1f} MiB"


def _quality_report(video: Video) -> list[str]:
    """The quality tiers on offer, with the numbers a choice needs.

    The numbers come from the recorded format table rather than from a
    constant, so a tier the platform stops offering shows as absent here
    instead of as a stale line. A row whose `filesize_approx` is missing is
    reported without a size rather than with a guess.

    Driven by `_QUALITIES`, so the tiers listed are exactly the ones a caller
    can pass to `--quality`. The platform's own format table lists each tier
    three times, once per codec, and choosing a quality is not choosing a codec.
    """
    by_id = {f.get("format_id"): f for f in video.formats}
    lines = ["清晰度档位"]
    for name, format_id in _QUALITIES.items():
        row = by_id.get(format_id)
        if row is None:
            lines.append(f"  {name:6} 本站未提供（格式 {format_id} 不存在）")
            continue
        platform_name = row.get("format") or format_id
        pieces = [f"  {name:6} {platform_name}"]
        tbr = row.get("tbr")
        if tbr:
            pieces.append(f"{tbr:.1f} kbps")
        size = row.get("filesize_approx") or row.get("filesize")
        if size:
            pieces.append(_mebibytes(size))
        lines.append("  ".join(pieces))
    return lines


def probe_report(video: Video) -> str:
    """The probe result as text a person reads.

    Four things the caller asked for: how many parts, how long each one is,
    which qualities exist, and which parts have subtitles. The last is the one
    that decides how much computing the rest of the work costs, because a part
    with a track needs no speech recognition at all.

    The withheld-tier note is the part that has to be unmissable. A user who
    asks for 1080p and is handed the normal tier would otherwise believe they
    got the high-bitrate stream, and nothing in the format table says
    otherwise: measured on this video, the table is identical logged in and
    logged out.
    """
    parts = video.parts
    with_subs = [p for p in parts if p.has_subtitles]
    without = [p for p in parts if not p.has_subtitles]
    total = sum(p.duration_sec for p in parts)

    lines: list[str] = [
        f"{video.title}  ({video.id})",
        f"{len(parts)} 讲，合计 {_duration_text(total)}",
        "",
        "讲次",
    ]
    for part in parts:
        duration = _duration_text(part.duration_sec)
        if part.has_subtitles:
            status = "字幕 " + ", ".join(part.subtitle_langs)
        else:
            status = "无字幕，需语音识别"
        lines.append(f"  {part.index:2}  {duration:>12}  {status}")

    lines.append("")
    lines.append(
        f"字幕小计  {len(with_subs)} 讲有字幕，{len(without)} 讲无字幕"
        + (
            f"（{', '.join(str(p.index) for p in without)}）"
            if without
            else ""
        )
    )
    lines.append("")
    lines.extend(_quality_report(video))
    lines.append("")
    lines.extend(_premium_note(video))
    return "\n".join(lines)


def _premium_note(video: Video) -> list[str]:
    """What to say about the membership high-bitrate tier.

    Three outcomes, each said plainly. When the tier is withheld the note names
    the normal tier the user will actually receive, because "高码率 unavailable"
    alone leaves them unsure what they are getting instead. When the tier is
    not reported missing, nothing is promised either: the sentence yt-dlp
    prints is the only evidence there is, and its absence is not proof the tier
    is available.
    """
    if premium_unavailable(video.missing_tiers):
        withheld = ", ".join(video.missing_tiers)
        return [
            f"会员档位  未拿到 {withheld}。",
            "          会员高码率档需要大会员，当前凭据下只能取到普通档",
            "          1080P 高清。选了 1080p 拿到的是普通档，不是高码率。",
        ]
    if video.missing_tiers:
        withheld = ", ".join(video.missing_tiers)
        return [f"档位提示  yt-dlp 报告缺档：{withheld}。"]
    return [
        "会员档位  未报告缺会员高清档。",
        "          这一项只能看 yt-dlp 的提示，它没报不等于档位一定拿得到。",
    ]


def _probe_command(rest: list[str]) -> int:
    """`videokit probe <BV号或链接>`. Reports what is there, downloads nothing."""
    references = _references(rest)
    if not references:
        return _usage_error("probe needs a BV号 or 链接")
    print("探测中，这一步要跑两次 yt-dlp，约 40 到 90 秒。", flush=True)
    try:
        video = probe(video_url(references[0]))
    except VideoKitError as exc:
        print(f"probe failed: {exc}", file=sys.stderr)
        return 1
    print(probe_report(video))
    return 0


def _selected_parts(video: Video, rest: list[str]) -> list[int]:
    """Which part indexes to act on, in course order.

    `--parts` is parsed here and validated against the probe result, so a part
    number that does not exist is refused rather than silently producing
    nothing. Omitting the option means every part, which is the whole-course
    case the ticket asks for.
    """
    selection = _option(rest, "--parts")
    if not selection:
        return [part.index for part in video.parts]
    wanted: list[int] = []
    for piece in selection.split(","):
        piece = piece.strip()
        if not piece.isdigit():
            raise ValueError(f"--parts 只收讲次序号，收到 {piece!r}")
        wanted.append(int(piece))
    known = {part.index for part in video.parts}
    unknown = [p for p in wanted if p not in known]
    if unknown:
        raise ValueError(
            f"这个视频没有第 {', '.join(str(p) for p in unknown)} 讲，"
            f"可选范围是 1 到 {max(known) if known else 0}"
        )
    return [p for p in wanted if p in known]


def _download_command(kind: str, rest: list[str], quality: str | None) -> int:
    """Download video or audio, reporting one line per part as it lands.

    Per part rather than one call for the whole selection, and that is a
    deliberate cost. `_run` captures the subprocess streams, so a single call
    over 30 parts would show the user nothing at all for the half hour it ran:
    yt-dlp's own progress output would go into a pipe nobody reads. Calling
    yt-dlp once per part costs the extra extraction, measured at about 2.6
    seconds per part on this machine, and buys a real completion line per part.

    No percentage is printed. The only figure the library reports is which
    files yt-dlp finished writing, so the count of finished parts is the only
    progress that is measurable, and claiming more would be inventing numbers.
    """
    references = _references(rest)
    if not references:
        return _usage_error(f"{kind} needs a BV号 or 链接")

    out_dir = Path(_option(rest, "--out") or Path.cwd() / kind)
    print(f"探测中，先确认要下哪些讲。", flush=True)
    try:
        video = probe(video_url(references[0]))
    except VideoKitError as exc:
        print(f"{kind} failed: {exc}", file=sys.stderr)
        return 1

    try:
        selected = _selected_parts(video, rest)
    except ValueError as exc:
        return _usage_error(str(exc))
    if not selected:
        print("没有选中任何讲，什么都没做。", file=sys.stderr)
        return 1

    if quality is not None and quality not in _QUALITIES:
        # Refused before anything is fetched, and the valid names are listed so
        # the reader does not have to run a second command to find them.
        print(
            f"未知清晰度 {quality!r}，可选 "
            f"{' / '.join(sorted(_QUALITIES))}。",
            file=sys.stderr,
        )
        return 1

    by_index = {part.index: part for part in video.parts}
    print(
        f"共 {len(selected)} 讲，"
        f"下载到 {out_dir}。每讲下完打印一行。",
        flush=True,
    )
    total_written = 0
    for position, index in enumerate(selected, start=1):
        part = by_index[index]
        label = f"[{position}/{len(selected)}]"
        print(f"{label} 第 {index} 讲 {_duration_text(part.duration_sec)} 开始", flush=True)
        try:
            if kind == "video":
                written = download_video(
                    video_url(video.id), out_dir, parts=[index], quality=quality
                )
            else:
                written = download_audio(
                    video_url(video.id), out_dir, parts=[index]
                )
        except VideoKitError as exc:
            # One part failing does not end the run. The remaining parts are
            # still worth their file, and the failure is named here rather than
            # left to a count at the end.
            print(f"{label} 第 {index} 讲失败：{exc}", file=sys.stderr, flush=True)
            continue
        if written:
            total_written += len(written)
            for path in written:
                print(f"{label} 第 {index} 讲完成 {path}", flush=True)
        else:
            # yt-dlp exited 0 and reported no file. Saying nothing would read as
            # success, which is the shape of failure worth naming.
            print(
                f"{label} 第 {index} 讲：yt-dlp 没有报告写出文件。",
                file=sys.stderr,
                flush=True,
            )
    print(f"完成，共写出 {total_written} 个文件到 {out_dir}")
    return 0


def _video_command(rest: list[str]) -> int:
    """`videokit video <BV号或链接>`. Downloads video, merging both streams."""
    if _option(rest, "--quality") is None and "--quality" in rest:
        return _usage_error("--quality 需要一个值")
    return _download_command(
        "video", rest, _option(rest, "--quality") or _DEFAULT_QUALITY
    )


def _audio_command(rest: list[str]) -> int:
    """`videokit audio <BV号或链接>`. Downloads audio as 16 kHz mono WAV."""
    return _download_command("audio", rest, None)


# The audio file name carries this module's own part numbering, so the part
# being transcribed can be recovered from the file the engine was handed.
# `audio_path` writes `NN.wav` and `AUDIO_TEMPLATE` is `%(playlist_index)02d`,
# so the stem is the part index, which is the same fact read backwards.
_AUDIO_STEM = re.compile(r"^(\d+)$")


def _part_of(audio) -> int | None:
    """The part index a WAV path belongs to, or None when it does not say."""
    if audio is None:
        return None
    match = _AUDIO_STEM.match(Path(str(audio)).stem)
    return int(match.group(1)) if match else None


class _ProgressEngine:
    """Reports each part as the engine starts and finishes it.

    Progress reaches the user through the engine rather than through a
    parameter on `transcripts`, and that is deliberate. `transcripts` owns
    per-part judgment, failure recording and the skip check, so the CLI must
    call it rather than re-implement its loop. Adding a progress parameter to
    it would change an interface that tickets #18 and #19 are editing at the
    same time. Wrapping the engine observes a boundary that already exists, and
    it is the expensive one: a 47-minute part takes about 3.5 minutes here.

    Which part is being transcribed comes from the WAV path the engine was
    handed. That is a fact about the call rather than a guess, and it is the
    only part-identifying information this boundary carries.

    The load stays deferred, so a batch whose every part has a platform track
    loads nothing and prints nothing.
    """

    def __init__(self, device: str = "cpu", loader=None, out=None):
        self._device = device
        self._loader = loader or load_engine
        self._engine = None
        self._out = out if out is not None else sys.stdout
        self._started: float | None = None
        self.loads = 0

    def _say(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def _resolve(self):
        if self._engine is None:
            self._say("首次需要语音识别，正在加载模型，实测约 30 到 70 秒。")
            began = time.monotonic()
            self._engine = self._loader(self._device)
            self.loads += 1
            self._say(f"模型加载完成，用时 {time.monotonic() - began:.1f} 秒。")
        return self._engine

    def generate(self, **kwargs):
        index = _part_of(kwargs.get("input"))
        if index is not None:
            self._say(f"第 {index} 讲 语音识别开始")
            self._started = time.monotonic()
        try:
            return self._resolve().generate(**kwargs)
        finally:
            if index is not None and self._started is not None:
                self._say(
                    f"第 {index} 讲 语音识别完成，用时 "
                    f"{time.monotonic() - self._started:.1f} 秒"
                )
                self._started = None

    @property
    def model(self):
        return self._resolve().model


def status_tally(records: Iterable[PartTranscript]) -> str:
    """One line counting the records by status.

    Written against the values found on the records rather than against a fixed
    list of the ones this ticket knows. Tickets #18 and #19 give this structure
    the statuses `failed` and `skipped`, so a tally that named only `ok` would
    report their results as nothing at all. Sorted, so the line is stable.
    """
    counts: dict[str, int] = {}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    if not counts:
        return ""
    return "状态：" + "，".join(
        f"{status} {counts[status]}" for status in sorted(counts)
    )


def _transcript_command(rest: list[str]) -> int:
    """`videokit transcript <BV号或链接> [--out 目录] [--parts 3,4] [--audio-out 目录]`.

    One command for both sources. A part without a subtitle track is
    transcribed from its own audio as part of this run, so the caller does not
    have to notice which parts were missing and invoke something else.

    Progress goes to stderr through `_ProgressEngine`, so a caller that pipes
    stdout to a file still gets the delivered report there and the running
    commentary on the terminal. A full run is about 48 minutes, and silence for
    that long reads as a hang.
    """
    references = _references(rest)
    if not references:
        return _usage_error("transcript needs a BV号 or 链接")
    reference = references[0]
    out_dir = Path(_option(rest, "--out") or Path.cwd() / "transcripts")
    audio_dir = Path(_option(rest, "--audio-out") or default_audio_dir())
    selection = _option(rest, "--parts")
    if selection is None:
        parts = None
    else:
        try:
            parts = _parse_parts(selection)
        except ValueError as exc:
            return _usage_error(str(exc))

    engine = _ProgressEngine(out=sys.stderr)
    print(
        "探测中，先确定哪些讲要跑语音识别。这一步要跑两次 yt-dlp。",
        file=sys.stderr, flush=True,
    )
    try:
        records = transcripts(
            video_url(reference), out_dir, parts,
            audio_dir=audio_dir, engine=engine,
        )
    except VideoKitError as exc:
        print(f"transcript failed: {exc}", file=sys.stderr)
        return 1

    report = transcript_report(records, out_dir)
    tally = status_tally(records)
    print(report)
    if tally:
        print(tally)
    return 0


def _parse_parts(selection: str) -> list[int]:
    """Parse a `--parts 3,4` value, or explain what is wrong with it."""
    wanted: list[int] = []
    for piece in selection.split(","):
        piece = piece.strip()
        if not piece.isdigit():
            raise ValueError(f"--parts 只收讲次序号，收到 {piece!r}")
        wanted.append(int(piece))
    return wanted


class _ConsoleSafe:
    """A stdout or stderr wrapper that cannot raise on an unprintable character.

    The console on this machine is cp936 with `errors="strict"`, so writing a
    character outside that code page raises UnicodeEncodeError. Measured, the
    failures are U+20000, an emoji, U+2022 and U+FFFD, while ordinary Chinese
    and the box-drawing characters this report uses are fine.

    Part titles come from the platform, so the CLI cannot assume they encode.
    A title with one character outside the code page would otherwise turn a
    finished report into a traceback, which is exactly the "raw low-level error
    instead of a usable message" outcome the spec's user story 14 rules out.

    Only this module's own output is affected. `sys.stdout` is not reconfigured
    and the environment is untouched, because `_run`'s decision to let the
    platform pick the subprocess encoding is deliberate and a test locks it;
    setting `PYTHONIOENCODING` here would break every Chinese title in a
    filename, measured and recorded in the README.

    Each write is attempted as the console wants it, and retried with
    replacement on failure. Substituting at the character level keeps the rest
    of the text intact, which matters: a title that prints as `测试??标题`
    still identifies the video, while a write that failed whole would lose the
    line.
    """

    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        if not isinstance(text, str):  # pragma: no cover - streams take text
            return self._stream.write(text)
        try:
            text.encode(getattr(self._stream, "encoding", None) or "utf-8")
        except (UnicodeEncodeError, LookupError):
            text = self._replace(text)
        return self._stream.write(text)

    def _replace(self, text: str) -> str:
        """`text` with every character the console cannot encode replaced."""
        encoding = getattr(self._stream, "encoding", None) or "utf-8"
        try:
            return text.encode(encoding, "replace").decode(encoding, "replace")
        except LookupError:  # pragma: no cover - an unknown code page name
            return text.encode("ascii", "replace").decode("ascii")

    def __getattr__(self, name):
        # flush, isatty, fileno and the rest belong to the real stream.
        return getattr(self._stream, name)


def _safe_console(stream):
    """`stream` in a form that cannot raise on an unprintable character.

    A stream with no `encoding`, such as the `io.StringIO` a test substitutes,
    is returned unchanged: it has no code page to fail against.
    """
    if getattr(stream, "encoding", None) is None:
        return stream
    if isinstance(stream, _ConsoleSafe):
        return stream
    return _ConsoleSafe(stream)


# The characters this module's own report is built from. Ordinary Chinese is in
# cp936; a part title's rarer characters may not be, which is why the wrapper
# above exists rather than a rule about which characters are allowed.


def _option(args: list[str], name: str) -> str | None:
    """The value of `name`, or None when it is absent."""
    if name not in args:
        return None
    position = args.index(name)
    if position + 1 >= len(args):
        return None
    return args[position + 1]


if __name__ == "__main__":
    sys.exit(main())
