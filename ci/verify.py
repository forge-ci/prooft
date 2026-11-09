#!/usr/bin/env python3
"""The `verify` job: decide which jobs this run may skip (stdlib only).

The job checks out the *base* (the PR base, or the merge group's base), so
this script, the prooft verifier under ``src/`` and ``ci-attestations.yml``
all come from the base: a change cannot edit what judges it. Heads are
fetched only as data.

- ``pull_request``: ``prooft plan`` on the PR head.
- ``merge_group``: ``prooft queue`` -- the queue commit inherits the PR
  head's proofs when it is one commit on the base and its tree, or a lane's
  declared inputs, match. The PR head is found from the queue ref
  (``gh-readonly-queue/<base>/pr-<N>-<sha>``) via the REST API.
- anything else (main pushes, dispatches) and forks skip nothing: the
  default-branch run is what keeps a forged trailer from going unnoticed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
JOBS = ("fast", "test-linux-x64")
QUEUE_PR = re.compile(r"/pr-(?P<number>\d+)-[0-9a-f]{40}$")


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


def queue_pr_number(ref: str) -> int | None:
    match = QUEUE_PR.search(ref)
    return int(match.group("number")) if match else None


def pr_head_sha(repository: str, number: int, token: str) -> str:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/pulls/{number}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        sha: object = json.load(response)["head"]["sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"PR #{number} has no head sha")
    return sha


def prooft(*args: str) -> int:
    """Run the *base* checkout's prooft, isolated from the environment."""
    src = str(ROOT / "src")
    boot = f"import sys; sys.path.insert(0, {src!r}); from prooft.cli import main; sys.exit(main(sys.argv[1:]))"
    return subprocess.run([sys.executable, "-I", "-c", boot, "-C", str(ROOT), *args], check=False).returncode


def fetch(*shas: str) -> bool:
    return subprocess.run(["git", "fetch", "-q", "origin", *shas], cwd=ROOT, check=False).returncode == 0


def pull_request(base: str, head: str) -> int:
    if os.environ.get("FORK") == "true":
        return run_all("fork PRs always run remotely")
    if not fetch(head):
        return run_all(f"could not fetch head {head[:12]}")
    title = os.environ.get("PR_TITLE", "")
    code = prooft("plan", "--base", base, "--head", head, "--title", title, "--github-output")
    return 0 if code == 0 else run_all(f"verifier exited {code}")


def merge_group(base: str, queue_head: str) -> int:
    number = queue_pr_number(os.environ.get("QUEUE_REF", ""))
    if number is None:
        return run_all("queue ref does not name a single PR")
    try:
        pr_head = pr_head_sha(os.environ["REPOSITORY"], number, os.environ["GITHUB_TOKEN"])
    except (KeyError, OSError, ValueError) as exc:
        return run_all(f"could not resolve PR #{number}'s head: {exc}")
    if not fetch(pr_head, queue_head):
        return run_all("could not fetch the PR and queue heads")
    print(f"verify: merge group for PR #{number}, PR head {pr_head[:12]}, queue head {queue_head[:12]}")
    code = prooft("queue", "--base", base, "--pr-head", pr_head, "--queue-head", queue_head, "--github-output")
    return 0 if code == 0 else run_all(f"verifier exited {code}")


def main() -> int:
    event = os.environ.get("EVENT_NAME", "")
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    if not base or not head:
        return run_all(f"{event or 'this event'} has no base to judge against")
    if event == "pull_request":
        return pull_request(base, head)
    if event == "merge_group":
        return merge_group(base, head)
    return run_all(f"{event} runs everything")


if __name__ == "__main__":
    raise SystemExit(main())
