"""Host facade: the only place prooft asks which OS/CPU it runs on (ci.yml LAYOUT-001)."""

from __future__ import annotations

import platform


def host_label() -> str:
    """``linux-x86_64``, ``darwin-arm64``, ``windows-amd64``."""
    return f"{platform.system().lower()}-{platform.machine().lower()}"


_TRIPLES = {
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("windows", "amd64"): "x86_64-pc-windows-msvc",
    ("windows", "arm64"): "aarch64-pc-windows-msvc",
}


def host_triple() -> str | None:
    """The Rust host triple for this machine, or None when unknown."""
    return _TRIPLES.get((platform.system().lower(), platform.machine().lower()))
