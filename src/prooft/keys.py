"""Input keys: what a lane actually read, so a proof can outlive a rebase.

A lane may declare its inputs in the policy:

    lanes:
      lint: {inputs: ["src/**", "pyproject.toml", "uv.lock"]}

The lane key is ``sha256`` over a version header, the lane name, its
patterns, and ``path blob-sha`` for every file at the commit that matches a
pattern. Two commits with different trees but identical inputs share a key,
so the merge queue (new parents, maybe a moved ``main``) can still credit a
lane whose inputs did not change.

The patterns come from the *base* policy, and the key is recomputed from the
commit being judged, never read from the trailer alone. A lane that reads a
file its patterns miss would produce a false skip: declare generously.
"""

from __future__ import annotations

from collections.abc import Iterable
import functools
import hashlib
import re

from prooft.git import Git

KEY_VERSION = "prooft-lane-key-v1"


@functools.lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    """Glob to regex. ``**`` spans directories, ``*`` and ``?`` do not."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        out.append("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, patterns: Iterable[str]) -> bool:
    return any(_compile(p).match(path) for p in patterns)


def lane_key(git: Git, rev: str, lane: str, patterns: tuple[str, ...]) -> str:
    files = git.ls_tree(rev)
    lines = [f"{path} {blob}" for path, blob in sorted(files.items()) if matches(path, patterns)]
    payload = "\n".join([KEY_VERSION, lane, *patterns, "", *lines])
    return hashlib.sha256(payload.encode()).hexdigest()
