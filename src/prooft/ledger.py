"""Ledgers: hosted proofs that a lane's exact inputs passed.

A trailer proves a lane on the commit that carries it. A *ledger* proves it
for an input key, wherever that key turns up again: a merge-queue entry, a
rebased head, a PR whose author never ran the lane. Both answer one question,
"did these exact lane inputs pass?", so prooft reads both.

    ledger = DirLedger(".prooft/ledger")            # tests, build farms, caches
    ledger = GitHubArtifacts("obs-rust/obs-studio", token)  # obs-studio #699
    plan = prooft.open().queue(pr, q, base, ledger=ledger)

Only lanes that declare ``inputs`` have a key, so only they can use a ledger.
A ledger entry counts only within the lane's ``max-age-hours``.
"""

from __future__ import annotations

import calendar
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import time
from typing import Protocol, cast
import urllib.parse
import urllib.request

from prooft.policy import Policy
from prooft.verify import GateStatus, State, Verdict, fresh

DEFAULT_NAME = "prooft-v1-{lane}-{key}"
"""Ledger entry name; obs-studio's hosted proofs use ``lane-proof-v1-{lane}-{key}``."""


@dataclass(frozen=True)
class Proof:
    lane: str
    key: str
    at: float
    source: str


class Ledger(Protocol):
    def lookup(self, lane: str, key: str) -> Proof | None:
        """The newest trusted proof for ``(lane, key)``, or None."""
        ...


@dataclass(frozen=True)
class DirLedger:
    """One JSON file per proof in a directory."""

    root: Path
    name: str = DEFAULT_NAME

    def _path(self, lane: str, key: str) -> Path:
        return Path(self.root) / (self.name.format(lane=lane, key=key) + ".json")

    def record(self, lane: str, key: str, *, source: str, at: float | None = None) -> Proof:
        proof = Proof(lane, key, time.time() if at is None else at, source)
        path = self._path(lane, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"lane": lane, "key": key, "at": proof.at, "source": source}) + "\n")
        return proof

    def lookup(self, lane: str, key: str) -> Proof | None:
        try:
            raw = _obj(json.loads(self._path(lane, key).read_text()))
        except (OSError, ValueError):
            return None
        at = raw.get("at")
        if raw.get("lane") != lane or raw.get("key") != key or not isinstance(at, (int, float)):
            return None
        return Proof(lane, key, float(at), str(raw.get("source", "")))


def _obj(value: object) -> dict[str, object]:
    """``value`` as a JSON object (empty if it is not one)."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


Fetch = Callable[[str], object]


def _github_fetch(token: str) -> Fetch:
    def fetch(url: str) -> object:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    return fetch


_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _epoch(stamp: object) -> float | None:
    if not isinstance(stamp, str) or not _ISO.match(stamp):
        return None
    return float(calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")))


@dataclass(frozen=True)
class GitHubArtifacts:
    """Artifacts named after the key, uploaded by a passing run (obs-studio #699).

    A proof counts only if the artifact is unexpired and the run that uploaded
    it is ``workflow`` (the CI workflow, not any workflow a comment or a
    dispatch can start), on a ``pull_request`` or ``merge_group`` event, from
    a branch of *this* repository (a fork's run executes the fork's workflow,
    so it proves nothing).
    """

    repository: str
    token: str = ""
    name: str = DEFAULT_NAME
    workflow: str = ".github/workflows/ci.yml"
    events: tuple[str, ...] = ("pull_request", "merge_group")
    fetch: Fetch | None = None

    def lookup(self, lane: str, key: str) -> Proof | None:
        fetch = self.fetch or _github_fetch(self.token)
        api = f"https://api.github.com/repos/{self.repository}/actions"
        name = self.name.format(lane=lane, key=key)
        query = urllib.parse.urlencode({"name": name, "per_page": 20})
        best: Proof | None = None
        try:
            for art in map(_obj, _list(_obj(fetch(f"{api}/artifacts?{query}")).get("artifacts"))):
                at = _epoch(art.get("created_at"))
                run_id = _obj(art.get("workflow_run")).get("id")
                if art.get("name") != name or art.get("expired") is not False or at is None or run_id is None:
                    continue
                if best is not None and at <= best.at:
                    continue
                run = _obj(fetch(f"{api}/runs/{run_id}"))
                if self.trusted(run):
                    best = Proof(lane, key, at, f"run {run_id} ({run.get('event')}, {run.get('head_branch')})")
        except (OSError, ValueError):
            return None
        return best

    def trusted(self, run: dict[str, object]) -> bool:
        return (
            run.get("path") == self.workflow
            and run.get("event") in self.events
            and _obj(run.get("head_repository")).get("full_name") == self.repository
        )


def credit(
    verdict: Verdict, policy: Policy, keys: Mapping[str, str], ledger: Ledger, *, now: float | None = None
) -> Verdict:
    """Mark gates VALID when the ledger proves their lane's current input key.

    Only gates a trailer did not already prove, and never gates with a
    hard error (bad stamp, undeclared, duplicate, lane mismatch): those
    point at tampering, not at a missing proof.
    """
    current = time.time() if now is None else now
    replaceable = {State.MISSING, State.WRONG_TREE, State.WRONG_PARENTS, State.STALE}
    hits: dict[str, Proof | None] = {}
    statuses: list[GateStatus] = []
    for status in verdict.statuses:
        gate = policy.gates.get(status.gate)
        key = keys.get(gate.lane, "") if gate else ""
        if gate is None or status.state not in replaceable or not key:
            statuses.append(status)
            continue
        if gate.lane not in hits:
            hits[gate.lane] = ledger.lookup(gate.lane, key)
        proof = hits[gate.lane]
        if proof is None or not fresh(proof.at, current, policy.max_age_seconds(gate.lane)):
            statuses.append(status)
            continue
        statuses.append(replace(status, state=State.VALID, detail=f"ledger: lane {gate.lane} passed ({proof.source})"))
    return replace(verdict, statuses=tuple(statuses))
