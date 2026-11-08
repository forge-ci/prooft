#!/usr/bin/env python3
"""`CI OK`: the one required check (stdlib only).

Green when every job in ``needs`` succeeded, except that a job may be
``skipped`` when -- and only when -- ``verify`` emitted ``skip_<job>=true``
for it (the base verifier proved every gate the job covers). Any other skip,
failure or cancellation is red.
"""

from __future__ import annotations

import json
import os
import sys

SKIPPABLE = ("fast", "test-linux-x64")


def judge(needs: dict[str, dict[str, object]]) -> list[str]:
    problems: list[str] = []
    verify = needs.get("verify", {})
    raw_outputs = verify.get("outputs")
    outputs: dict[str, object] = raw_outputs if isinstance(raw_outputs, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    for job, info in sorted(needs.items()):
        result = info.get("result")
        if result == "success":
            print(f"ok       {job}")
        elif result == "skipped" and job in SKIPPABLE and outputs.get(f"skip_{job}") == "true":
            print(f"attested {job} (skipped: every gate proven by a Ci-Attestation trailer)")
        else:
            problems.append(f"{job}: {result}")
    return problems


def main() -> int:
    raw = os.environ.get("CI_OK_NEEDS_JSON", "")
    if not raw:
        print("ci_ok.py: CI_OK_NEEDS_JSON is not set", file=sys.stderr)
        return 2
    problems = judge(json.loads(raw))
    for problem in problems:
        print(f"FAIL     {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
