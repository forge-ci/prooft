"""Judge a commit's attestations against a policy, then decide which jobs skip.

The rules, in order (the first that fails names the state):

1. ``undeclared``   the gate is not in the policy.
2. ``duplicate``    the gate is attested more than once.
3. ``bad-stamp``    the record was edited after it was stamped.
4. ``lane-mismatch`` the policy says another lane proves this gate.
5. ``wrong-tree``   it proves another tree, and the lane's declared inputs changed too.
6. ``wrong-parents`` it proves another history, and the lane's inputs changed too.
7. ``stale``        older than the lane's ``max-age-hours``, or from the future.
8. ``valid``        otherwise.

A declared gate with no trailer is ``missing``: omission means "not run", and
CI runs it. A job skips only when the head is trusted and every gate it lists
is ``valid``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import math
import time

from prooft.attestation import Attestation, Trailers
from prooft.git import Commit
from prooft.policy import Gate, Policy

CLOCK_SKEW_SECONDS = 60.0


class State(str, Enum):
    VALID = "valid"
    MISSING = "missing"
    UNDECLARED = "undeclared"
    DUPLICATE = "duplicate"
    BAD_STAMP = "bad-stamp"
    WRONG_TREE = "wrong-tree"
    WRONG_PARENTS = "wrong-parents"
    LANE_MISMATCH = "lane-mismatch"
    STALE = "stale"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class GateStatus:
    gate: str
    state: State
    detail: str

    @property
    def ok(self) -> bool:
        return self.state is State.VALID


@dataclass(frozen=True)
class Verdict:
    """Every declared gate's state on one commit."""

    sha: str
    statuses: tuple[GateStatus, ...]
    malformed: tuple[str, ...]

    def state(self, gate: str) -> State:
        for status in self.statuses:
            if status.gate == gate:
                return status.state
        return State.MISSING

    def ok(self, *gates: str) -> bool:
        return all(self.state(g) is State.VALID for g in gates)

    @property
    def valid(self) -> tuple[str, ...]:
        return tuple(s.gate for s in self.statuses if s.ok)

    @property
    def errors(self) -> tuple[GateStatus, ...]:
        return tuple(s for s in self.statuses if s.state not in (State.VALID, State.MISSING))


def fresh(at: float, now: float, max_age: float, skew: float = CLOCK_SKEW_SECONDS) -> bool:
    """Keep the original execution time; never refresh it on reuse."""
    if not all(math.isfinite(v) for v in (at, now, max_age, skew)):
        return False
    return at > 0 and max_age > 0 and -skew <= now - at <= max_age


def _judge(
    att: Attestation, declared: Gate, commit: Commit, policy: Policy, *, now: float | None, keys: Mapping[str, str]
) -> GateStatus:
    """Rules 3-8 for one declared, first-seen attestation (``now=None`` skips the age check)."""
    if not att.intact:
        return GateStatus(att.gate, State.BAD_STAMP, "stamp does not match the record")
    if att.lane != declared.lane:
        return GateStatus(att.gate, State.LANE_MISMATCH, f"proved by lane {att.lane!r}, policy says {declared.lane!r}")
    same_inputs = bool(att.key) and keys.get(att.lane) == att.key
    if att.tree != commit.tree and not same_inputs:
        return GateStatus(att.gate, State.WRONG_TREE, f"proves tree {att.tree[:12]}, commit has {commit.tree[:12]}")
    if att.parents != commit.parents and not same_inputs:
        return GateStatus(att.gate, State.WRONG_PARENTS, "proved on other parents (rebased or cherry-picked)")
    if now is not None and not fresh(att.at, now, policy.max_age_seconds(declared.lane)):
        return GateStatus(att.gate, State.STALE, "expired, or timestamped in the future")
    how = "same tree" if att.tree == commit.tree and att.parents == commit.parents else "inputs unchanged"
    return GateStatus(att.gate, State.VALID, f"{att.via} by lane {att.lane} on {att.host or 'unknown host'} ({how})")


def verify(
    commit: Commit,
    policy: Policy,
    *,
    now: float | None = None,
    check_age: bool = True,
    keys: Mapping[str, str] | None = None,
) -> Verdict:
    """Judge ``commit``'s trailers. ``keys`` maps lane -> input key *computed at this commit*;
    an attestation whose key matches is valid even on another tree (rule 4/5 relaxation)."""
    current = (time.time() if now is None else now) if check_age else None
    lane_keys: Mapping[str, str] = keys or {}
    trailers = Trailers.parse(commit.message)
    seen: set[str] = set()
    statuses: list[GateStatus] = []
    for att in trailers.attestations:
        declared = policy.gates.get(att.gate)
        if declared is None:
            statuses.append(GateStatus(att.gate, State.UNDECLARED, "not a gate in the policy"))
        elif att.gate in seen:
            statuses.append(GateStatus(att.gate, State.DUPLICATE, "attested more than once"))
        else:
            seen.add(att.gate)
            statuses.append(_judge(att, declared, commit, policy, now=current, keys=lane_keys))
    statuses.extend(GateStatus(g, State.MISSING, "not attested: CI runs it") for g in policy.gates if g not in seen)
    return Verdict(commit.sha, tuple(statuses), trailers.malformed)


@dataclass(frozen=True)
class JobDecision:
    job: str
    skip: bool
    reason: str

    @property
    def output_name(self) -> str:
        """``ci.yml:fast`` -> ``skip_fast`` (a ``GITHUB_OUTPUT`` key)."""
        return "skip_" + self.job.split(":", 1)[1]


@dataclass(frozen=True)
class Plan:
    """What CI should do for one head."""

    verdict: Verdict
    jobs: tuple[JobDecision, ...]

    @property
    def skipped(self) -> tuple[str, ...]:
        return tuple(j.job for j in self.jobs if j.skip)

    @property
    def run(self) -> tuple[str, ...]:
        return tuple(j.job for j in self.jobs if not j.skip)

    def skips(self, job: str) -> bool:
        return job in self.skipped

    def outputs(self) -> dict[str, str]:
        return {j.output_name: "true" if j.skip else "false" for j in self.jobs}


def decide(policy: Policy, verdict: Verdict, *, trusted: bool = True, untrusted_reason: str = "") -> Plan:
    decisions: list[JobDecision] = []
    for job, gates in policy.jobs.items():
        if not trusted:
            decisions.append(JobDecision(job, False, untrusted_reason or "head is not trusted"))
            continue
        lacking = [g for g in gates if verdict.state(g) is not State.VALID]
        if lacking:
            states = ", ".join(f"{g}={verdict.state(g)}" for g in lacking)
            decisions.append(JobDecision(job, False, f"not attested: {states}"))
        else:
            decisions.append(JobDecision(job, True, f"all {len(gates)} gate(s) attested"))
    return Plan(verdict, tuple(decisions))
