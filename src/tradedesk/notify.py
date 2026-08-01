"""Local-only alerts. Terminal bell and macOS notification centre. Nothing leaves the machine.

Deliberately has no HTTP client, no webhook, no token. A trading tool that phones home is
a tool that leaks your positions, and the failure mode is silent.
"""

from __future__ import annotations

import shutil
import subprocess
import sys


def bell() -> None:
    """Terminal bell. Works everywhere, costs nothing."""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


def macos(title: str, message: str, *, subtitle: str = "") -> bool:
    """macOS notification via osascript. Returns whether it was delivered.

    Quotes are stripped rather than escaped: this text can contain a symbol or a price
    but never needs quoting, and building an AppleScript string from unescaped input is
    a command-injection shape worth simply not having.
    """
    if sys.platform != "darwin" or not shutil.which("osascript"):
        return False
    clean = lambda s: s.replace('"', "").replace("\\", "")  # noqa: E731
    script = f'display notification "{clean(message)}" with title "{clean(title)}"'
    if subtitle:
        script += f' subtitle "{clean(subtitle)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=5,
                       capture_output=True)
        return True
    except Exception:
        return False


def alert(title: str, message: str, *, subtitle: str = "", sound: bool = True) -> None:
    """Local alert: notification centre if available, terminal bell as the fallback."""
    if sound:
        bell()
    macos(title, message, subtitle=subtitle)
