"""Regenerate the login-signal fixtures from real yt-dlp runs.

Not part of the suite, and it needs the network. Run it when the site or
yt-dlp changes format and the recorded fixtures need refreshing.

    uv run --python 3.12 python test/_record_login_signals.py

It writes three files into test/fixtures/, captured as separate streams:

  list-subs-logged-out.txt    a run with no usable credential; the warning
                              itself is in the companion file below
  list-subs-login-warning.txt stderr of that run, one line
  list-subs-valid-no-subs.txt a valid login on a part that has no subtitles

Those three are the measured evidence behind `videokit.login_needed`. The two
empty subtitle tables are byte-different but structurally identical, so the
warning line is the only thing separating them.
"""

import subprocess
import sys
from pathlib import Path

CMD = ["uv", "tool", "run", "--from", "yt-dlp", "yt-dlp"]
URL = "https://www.bilibili.com/video/BV1Y5QgB5Eas"
GOOD = Path.home() / "AppData/Local/VideoKit/cookies.txt"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# (label, cookies, --playlist-items, stdout fixture name, stderr fixture name)
CASES = [
    ("logged out, p3", None, "3", "list-subs-logged-out.txt", "list-subs-login-warning.txt"),
    ("valid login, p1", GOOD, "1", "list-subs-valid-no-subs.txt", None),
]


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    if not GOOD.is_file():
        print(f"no credential at {GOOD}; run `videokit.py login` first")
        return 1
    for label, cookies, items, out_name, err_name in CASES:
        args = list(CMD)
        if cookies:
            args += ["--cookies", str(cookies)]
        args += ["--skip-download", "--list-subs", "--playlist-items", items, URL]
        p = subprocess.run(args, capture_output=True, text=True, errors="replace")
        (FIXTURES / out_name).write_text(p.stdout or "", encoding="utf-8")
        if err_name:
            (FIXTURES / err_name).write_text(p.stderr or "", encoding="utf-8")
        warned = "Subtitles are only available when logged in" in (p.stderr or "")
        print(f"{label:<20} exit={p.returncode} stdout={len(p.stdout or ''):>6} "
              f"stderr={len(p.stderr or ''):>5} warned={warned}")
        print(f"    -> {out_name}" + (f" + {err_name}" if err_name else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
