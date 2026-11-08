"""Host facade: the only place prooft asks which OS/CPU it runs on (ci.yml LAYOUT-001)."""

from __future__ import annotations

import platform


def host_label() -> str:
    """``linux-x86_64``, ``darwin-arm64``, ``windows-amd64``."""
    return f"{platform.system().lower()}-{platform.machine().lower()}"
