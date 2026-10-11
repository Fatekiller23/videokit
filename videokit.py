"""VideoKit: probe and download Bilibili videos.

Two entry points, one per input shape:

    probe(url)          -> Video                  what exists at this URL
    download_video(...) -> list[Path]             the video files
    download_audio(...) -> list[Path]             the audio files, for ASR

Why probing is its own call: deciding what to download requires knowing what
is there first. Splitting it lets a caller ask "how many parts, which have
subtitles, what qualities" without touching the network for file data.

Why audio has its own call: transcript-only work needs 40 MB per part instead
of 130 MB or 340 MB. Same part, an order of magnitude apart.
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
    profile = default_data_dir() / "browser-profile"
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
