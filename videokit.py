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


def main(argv: list[str] | None = None) -> int:
    """The credential command. Returns a process exit code.

    Scope is deliberately one subcommand. The general CLI that exposes probe
    and both downloads belongs to ticket #20; this exists because the scan has
    to be reachable without importing the module.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] != ["login"]:
        print(
            "usage: videokit login\n\n"
            "Log in to Bilibili once. Opens a browser window, and after the QR "
            "code is scanned it stores a credential file that probe and the "
            "downloads then use automatically.",
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


if __name__ == "__main__":
    sys.exit(main())
