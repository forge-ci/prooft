#!/usr/bin/env python3
"""prooft's gate runner: ``./ci.py <gate> [...]`` (stdlib only).

    ./ci.py --list              show gates
    ./ci.py fmt                 ruff format --check (black-compatible style)
    ./ci.py lint                ruff check (pycodestyle, pyflakes, isort, bugbear, ...)
    ./ci.py types               basedpyright
    ./ci.py test [pytest args]  pytest
    ./ci.py fix                 ruff format + ruff check --fix
    ./ci.py precheck            ci-lint precheck at ci.toml's linter pin
    ./ci.py all                 fmt, lint, types, test (continues past failures)
    ./ci.py attest LANE         run LANE's gates, then stamp HEAD with prooft

Lanes (see ci-attestations.yml): ``lint`` = fmt + lint + types, ``tests`` = test.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
UV = ["uv", "run", "--frozen"]
LINTER_REPO = "https://github.com/forge-ci/ci.yml"


def sh(cmd: Sequence[str]) -> int:
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(list(cmd), cwd=ROOT, check=False).returncode


def gate_fmt(_: list[str]) -> int:
    return sh([*UV, "ruff", "format", "--check", "."])


def gate_lint(_: list[str]) -> int:
    return sh([*UV, "ruff", "check", "."])


def gate_types(_: list[str]) -> int:
    return sh([*UV, "basedpyright"])


def gate_test(extra: list[str]) -> int:
    return sh([*UV, "pytest", *extra])


def gate_fix(_: list[str]) -> int:
    return sh([*UV, "ruff", "format", "."]) or sh([*UV, "ruff", "check", "--fix", "."])


def _linter_checkout() -> Path:
    """Clone/fetch forge-ci/ci.yml at ci.toml's pinned SHA into .ci-lint/ (gitignored)."""
    match = re.search(r'^linter\s*=\s*"[^@"]+@([0-9a-f]{40})"', (ROOT / "ci.toml").read_text(), re.MULTILINE)
    if not match:
        raise SystemExit('ci.toml has no linter = "<repo>@<sha>" pin')
    sha = match.group(1)
    dest = ROOT / ".ci-lint"
    if not dest.exists():
        subprocess.run(["git", "clone", "-q", LINTER_REPO, str(dest)], check=True)
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    if head.stdout.strip() != sha:
        subprocess.run(["git", "-C", str(dest), "fetch", "-q", "origin", sha], check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", sha], check=True)
    return dest


def gate_precheck(_: list[str]) -> int:
    checkout = _linter_checkout()
    env = {**os.environ, "PYTHONPATH": str(checkout)}
    cmd = [
        "uv",
        "run",
        "--no-project",
        "--with",
        "pyyaml",
        "python3",
        "-m",
        "ci_lint",
        "precheck",
        "--repo",
        ".",
        "--local",
    ]
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=ROOT, env=env, check=False).returncode


LANES: dict[str, tuple[str, ...]] = {"lint": ("fmt", "lint", "types"), "tests": ("test",)}


def gate_attest(extra: list[str]) -> int:
    if len(extra) != 1 or extra[0] not in LANES:
        print(f"usage: ./ci.py attest {{{'|'.join(LANES)}}}", file=sys.stderr)
        return 2
    lane = extra[0]
    inner = [sys.executable, str(Path(__file__).resolve()), *LANES[lane]]
    return sh([*UV, "prooft", "run", lane, "--", *inner])


def gate_all(_: list[str]) -> int:
    failed = [name for name in ("fmt", "lint", "types", "test") if GATES[name]([])]
    print("all gates passed" if not failed else f"failed: {', '.join(failed)}")
    return 1 if failed else 0


GATES: dict[str, Callable[[list[str]], int]] = {
    "fmt": gate_fmt,
    "lint": gate_lint,
    "types": gate_types,
    "test": gate_test,
    "fix": gate_fix,
    "precheck": gate_precheck,
    "attest": gate_attest,
    "all": gate_all,
}


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--list":
        print("\n".join(GATES))
        return 0
    # several plain gates in one call: ./ci.py fmt lint types
    if all(a in GATES and a not in ("test", "attest") for a in argv) and len(argv) > 1:
        return max(GATES[a]([]) for a in argv)
    gate = GATES.get(argv[0])
    if gate is None:
        print(f"unknown gate {argv[0]!r}; try --list", file=sys.stderr)
        return 2
    return gate(argv[1:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
