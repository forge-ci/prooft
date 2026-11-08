#!/usr/bin/env python3
"""The `verify` job: decide which jobs this run may skip (stdlib only).

The job checks out the PR *base*, so this script, the prooft verifier under
``src/`` and ``ci-attestations.yml`` all come from the base: a PR cannot edit
what judges it. The head commit is fetched only as data, to read its tree,
parents and trailers.

Pushes to main, merge-queue runs without a base checkout, dispatches and
fork PRs skip nothing: the default-branch run is what keeps a forged
trailer from going unnoticed.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
JOBS = ("fast", "test-linux-x64")


def write_outputs(values: dict[str, str]) -> None:
    lines = "".join(f"{k}={v}\n" for k, v in values.items())
    print(lines, end="")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write(lines)


def run_all(reason: str) -> int:
    print(f"verify: nothing skips ({reason})")
    write_outputs({f"skip_{job}": "false" for job in JOBS})
    return 0


def main() -> int:
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    if os.environ.get("EVENT_NAME") != "pull_request" or not base or not head:
        return run_all("not a pull_request run")
    if os.environ.get("FORK") == "true":
        return run_all("fork PRs always run remotely")
    fetch = subprocess.run(["git", "fetch", "-q", "origin", head], cwd=ROOT, check=False)
    if fetch.returncode != 0:
        return run_all(f"could not fetch head {head[:12]}")
    src = str(ROOT / "src")
    boot = f"import sys; sys.path.insert(0, {src!r}); from prooft.cli import main; sys.exit(main(sys.argv[1:]))"
    args = ["-C", str(ROOT), "plan", "--base", base, "--head", head, "--title", os.environ.get("PR_TITLE", "")]
    proc = subprocess.run([sys.executable, "-I", "-c", boot, *args, "--github-output"], check=False)
    if proc.returncode != 0:
        return run_all(f"verifier exited {proc.returncode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
