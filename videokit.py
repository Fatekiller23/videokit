"""VideoKit: probe and download Bilibili videos.

Entry points, one per input shape:

    probe(url)                  -> Video            what exists at this URL
    part_subtitles(url, part)   -> list[Segment]    that part's subtitle track
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


def _run(args: list[str], cookies: Path | None) -> str:
    """Run yt-dlp and return stdout, or raise with its stderr attached.

    One place resolves credentials, so every caller gets the same behavior
    whether it passes a path, passes None, or omits the argument. A caller that
    names a file always wins; otherwise the stored login is used if there is
    one.

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
    return proc.stdout


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
    payload = _run(
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
    track = _requested_track(payload, language)
    if track is None:
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
    """
    raw = _run(
        ["--dump-single-json", "--skip-download", url],
        cookies,
    )
    data = json.loads(raw)
    subs = parse_subtitle_table(
        _run(["--list-subs", "--skip-download", url], cookies)
    )

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

    `status` has a third value beyond the spec's `ok` / `failed`. A part with
    no subtitle track is neither: its transcript does not exist yet, and the
    ASR path is expected to produce it. Recording that as `ok` would claim a
    transcript that is not there, and recording it as `failed` with
    `failure_reason = "no_audio"` would blame the audio, which is present.
    `pending` is what is left, and the nine such parts of a 30-part course are
    the reason the value exists.

    `status` and `failure_reason` stay separate because the reason decides
    whether a rerun can help. `no_audio` cannot be fixed by retrying;
    `asr_error` can. Merged into one field, a caller cannot tell them apart.
    This ticket produces no `failed` record. Ticket #18 owns that path.
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


def transcript_filename(record: PartTranscript) -> str:
    """The delivered file's name, ordered by part.

    The index prefix is what makes a directory listing read in course order,
    because every title begins with the course name rather than with its own
    number.

    A title is external data, so characters that cannot appear in a Windows
    file name are replaced. No real title needs this: measured over the 30
    titles of the first target, zero carry one. It is a guard at a boundary,
    not a repair.
    """
    unsafe = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    title = unsafe.sub("_", record.title).strip().rstrip(".")
    return f"{record.index:02d}-{title}.txt"


def write_transcript(record: PartTranscript, out_dir: Path) -> Path:
    """Write one delivered transcript and return its path.

    Plain text, UTF-8, no timestamps, no source marker, no quality grade. The
    name carries the part index and title.

    A `pending` record has no transcript to write. Writing an empty file for
    one would look finished, which is the outcome `SubtitleUnavailable` exists
    to prevent, so callers must not ask.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / transcript_filename(record)
    path.write_text(transcript_text(record.segments), encoding="utf-8")
    return path


def _pending_record(bvid: str, part: Part) -> PartTranscript:
    """The record for a part whose subtitles are missing, so ASR owes it one."""
    return PartTranscript(
        bvid=bvid,
        index=part.index,
        title=part.title,
        duration_sec=part.duration_sec,
        source=None,
        language=None,
        segments=(),
        status="pending",
    )


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
) -> PartTranscript:
    """Judge one part and produce its transcript record.

    The judgment is per part, never per video. Measured on the first real
    target, 21 of 30 parts carry a track and 9 do not, and the state does not
    follow part length, so a whole-video answer would be wrong for one group
    or the other.

    A part with no track returns a `pending` record rather than raising. An
    exception here would abort the batch at part 1 of this course, and the
    nine parts without a track are spread through the middle of it.

    A credential fault is not the same thing and is not swallowed. Once the
    login is stale every part reads as trackless, so treating that as
    `pending` would hand the whole course to the ASR path in response to a
    problem that re-exporting the credential fixes. Ticket #13 raises
    `LoginRequired` for that, and it is deliberately not caught here, so it
    travels up with its next-step message. `SubtitleUnavailable` is the only
    outcome that means "this part needs ASR".
    """
    if not part.has_subtitles:
        return _pending_record(video.id, part)
    language = _preferred_language(part)
    try:
        segments = part_subtitles(video.id, part.index, language, cookies)
    except SubtitleUnavailable:
        # The probe table and the fetch can disagree, for instance if the
        # track was withdrawn between the two calls. Either way this part
        # still needs a transcript from somewhere, so it is pending.
        return _pending_record(video.id, part)
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


def transcripts(
    url: str,
    out_dir: Path,
    parts: list[int] | None = None,
    cookies: Path | None = None,
) -> tuple[PartTranscript, ...]:
    """Judge every part of `url` and deliver a transcript for each one that has one.

    Returns one record per part, in course order, whether or not a file was
    written. Callers get the `pending` parts too, because "which parts still
    need ASR" is the question the next ticket asks.

    Processing does not stop at a part without subtitles. It does stop on a
    credential fault, which is not a per-part condition: it makes every part
    look trackless, so continuing would run the ASR path over the whole course
    and produce a full set of transcripts from the wrong cause.
    """
    video = probe(url, cookies)
    selected = [
        part for part in video.parts if parts is None or part.index in parts
    ]
    records: list[PartTranscript] = []
    for part in selected:
        record = part_transcript(video, part, cookies)
        if record.status == "ok":
            write_transcript(record, out_dir)
        records.append(record)
    return tuple(records)


def _format_selector(quality: str) -> str:
    """Map a quality name to a yt-dlp format expression.

    Bilibili serves video and audio as separate streams and does not offer a
    muxed one, so both selectors name a video stream plus an audio stream.
    """
    video = {
        "1080p": "30080",   # avc1, ~336 MB/part
        "720p": "30066",    # hevc, ~131 MB/part
        "480p": "30033",    # hevc,  ~52 MB/part
        "360p": "30011",    # hevc,  ~35 MB/part
    }
    if quality not in video:
        raise ValueError(
            f"unknown quality {quality!r}; expected one of {sorted(video)}"
        )
    return f"{video[quality]}+30280"  # 30280 = highest-bitrate audio


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
    stdout = _run(args + extra, cookies)
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
        template="%(playlist_index)02d.%(ext)s",
        extra=[
            "-x",
            "--audio-format", "wav",
            "--postprocessor-args", "-ar 16000 -ac 1",
        ],
    )


def video_url(reference: str) -> str:
    """Turn a bare BV number into the URL yt-dlp takes.

    `BV1Y5QgB5Eas` is what a user has in hand, a URL is what the extractor
    needs. Both are accepted so neither has to be converted by hand.
    """
    reference = reference.strip()
    if reference.startswith(("http://", "https://")):
        return reference
    return f"https://www.bilibili.com/video/{reference}"


def transcript_report(records: Iterable[PartTranscript], out_dir: Path) -> str:
    """The line printed after a batch, naming what was produced and what was not.

    The `pending` count is the point of the line. Without it a short run looks
    exactly like a complete one.
    """
    records = list(records)
    delivered = [r for r in records if r.status == "ok"]
    pending = [r for r in records if r.status == "pending"]
    failed = [r for r in records if r.status == "failed"]
    lines = [
        f"delivered {len(delivered)} of {len(records)} parts to {out_dir}",
    ]
    if pending:
        lines.append(
            f"{len(pending)} parts await ASR "
            f"(no subtitle track): {', '.join(str(r.index) for r in pending)}"
        )
    if failed:
        lines.append(
            "failed: "
            + ", ".join(f"{r.index} ({r.failure_reason})" for r in failed)
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """The credential command. Returns a process exit code.

    Scope is deliberately two subcommands. `login` exists because the scan has
    to be reachable without importing the module. `transcript` exists because
    the ticket asks for one command that turns a BV number into delivered
    transcripts. The general CLI that exposes probe and both downloads belongs
    to ticket #20.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["transcript"]:
        return _transcript_command(argv[1:])
    if argv[:1] != ["login"]:
        print(
            "usage: videokit login\n"
            "       videokit transcript <BV号或链接> [--out 目录] [--parts 3,4]\n\n"
            "Log in to Bilibili once. Opens a browser window, and after the QR "
            "code is scanned it stores a credential file that probe and the "
            "downloads then use automatically.\n\n"
            "transcript judges every part of a video and writes one plain-text "
            "transcript per part that has subtitles.",
            file=sys.stderr,
        )
        return 2
    try:
        path = login()
    except LoginError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return 1
    print(f"credential written to {path}")
    return 0


def _transcript_command(rest: list[str]) -> int:
    """`videokit transcript <BV号或链接> [--out 目录] [--parts 3,4]`."""
    reference = next((a for a in rest if not a.startswith("-")), None)
    if reference is None:
        print(
            "usage: videokit transcript <BV号或链接> [--out 目录] [--parts 3,4]",
            file=sys.stderr,
        )
        return 2
    out_dir = Path(_option(rest, "--out") or Path.cwd() / "transcripts")
    selection = _option(rest, "--parts")
    parts = [int(p) for p in selection.split(",")] if selection else None
    try:
        records = transcripts(video_url(reference), out_dir, parts)
    except VideoKitError as exc:
        print(f"transcript failed: {exc}", file=sys.stderr)
        return 1
    print(transcript_report(records, out_dir))
    return 0


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
