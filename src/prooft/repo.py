"""The fluent entry point.

import prooft

repo = prooft.open(".")
repo.lane("rust").run(["soldr", "lint"])     # run, then stamp HEAD on success
repo.verify().ok("rust/all/fmt")             # -> True
repo.plan(base="origin/main").skipped        # -> ("ci.yml:fast",)
repo.queue(pr_head="pr-head", queue_head="HEAD", base="origin/main").skipped
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import time

from prooft.attestation import Attestation, Via, append
from prooft.git import Commit, Git
from prooft.policy import POLICY_FILE, Policy, PolicyError
from prooft.verify import Plan, Verdict, decide, verify

#: Paths whose change makes a head untrusted: a PR must not be able to edit
#: the rules (or the jobs) it is judged by and still skip them.
DEFAULT_SURFACES: tuple[str, ...] = (POLICY_FILE, ".github/", "ci.toml", "local-gate.toml")

#: Title tags that force every job to run remotely.
FORCE_TAGS: tuple[str, ...] = ("[ci-full]", "[ci-remote]", "[no-attest]")


class LaneFailed(RuntimeError):
    def __init__(self, lane: str, code: int) -> None:
        super().__init__(f"lane {lane!r} failed with exit code {code}; nothing was attested")
        self.lane: str = lane
        self.code: int = code


@dataclass(frozen=True)
class Repo:
    root: Path
    surfaces: tuple[str, ...] = DEFAULT_SURFACES
    git: Git = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "git", Git(self.root))

    # ── reading ──────────────────────────────────────────────────────────

    def commit(self, rev: str = "HEAD") -> Commit:
        return self.git.commit(rev)

    def policy(self, rev: str | None = None) -> Policy:
        """The policy in the working tree, or at ``rev`` (use the PR base in CI)."""
        if rev is None:
            return Policy.load(self.root / POLICY_FILE)
        text = self.git.show(rev, POLICY_FILE)
        if text is None:
            raise PolicyError([f"{POLICY_FILE} does not exist at {rev}"])
        return Policy.parse(text, source=f"{rev}:{POLICY_FILE}")

    def verify(self, rev: str = "HEAD", *, policy: Policy | None = None, now: float | None = None) -> Verdict:
        return verify(self.commit(rev), policy or self.policy(), now=now)

    # ── writing ──────────────────────────────────────────────────────────

    def lane(self, name: str, *, policy: Policy | None = None) -> LaneRun:
        return LaneRun(self, name, policy or self.policy())

    # ── deciding ─────────────────────────────────────────────────────────

    def trust(self, base: str, head: str = "HEAD", *, title: str = "", fork: bool = False) -> tuple[bool, str]:
        """GATE-008-style head trust: may this head skip anything at all?"""
        if fork:
            return False, "fork PRs always run remotely"
        tag = next((t for t in FORCE_TAGS if t in title.lower()), None)
        if tag:
            return False, f"title tag {tag} forces remote execution"
        changed = self.git.run("diff", "--name-only", f"{base}...{head}").split()
        touched = sorted(
            p for p in changed if any(p == s or (s.endswith("/") and p.startswith(s)) for s in self.surfaces)
        )
        if touched:
            return False, f"head changes a trust surface: {', '.join(touched)}"
        return True, "trusted"

    def plan(
        self,
        base: str,
        head: str = "HEAD",
        *,
        title: str = "",
        fork: bool = False,
        now: float | None = None,
    ) -> Plan:
        """Which jobs a ``pull_request`` run on ``head`` may skip. Policy comes from ``base``."""
        policy = self.policy(base)
        trusted, why = self.trust(base, head, title=title, fork=fork)
        return decide(policy, verify(self.commit(head), policy, now=now), trusted=trusted, untrusted_reason=why)

    def queue(self, pr_head: str, queue_head: str, base: str, *, now: float | None = None) -> Plan:
        """Merge-queue credit: a single-PR group whose tree equals the PR head's.

        The queue commit is new (new parents), so its own trailers can never
        match. It inherits the PR head's verdict only when the queue head's
        sole parent is ``base`` and both trees are identical: the bytes the
        queue would test are exactly the bytes that were attested.
        """
        policy = self.policy(base)
        pr = self.commit(pr_head)
        q = self.commit(queue_head)
        base_sha = self.commit(base).sha
        verdict = verify(pr, policy, now=now)
        if q.parents != (base_sha,):
            return decide(
                policy, verdict, trusted=False, untrusted_reason="queue head is not a single commit on the base"
            )
        if q.tree != pr.tree:
            return decide(
                policy, verdict, trusted=False, untrusted_reason="queue tree differs from the attested PR tree"
            )
        trusted, why = self.trust(base_sha, pr.sha)
        return decide(policy, verdict, trusted=trusted, untrusted_reason=why)

    @staticmethod
    def release() -> str:
        """Releases never consume attestations: everything is recomputed."""
        return "release runs every gate; attestations are ignored"


@dataclass(frozen=True)
class LaneRun:
    repo: Repo
    name: str
    policy: Policy

    @property
    def gates(self) -> tuple[str, ...]:
        return tuple(g.path for g in self.policy.lane_gates(self.name))

    def run(self, command: Sequence[str], *, key: str = "") -> str:
        """Run ``command`` on a clean HEAD; on success stamp every gate of this lane.

        Refuses a dirty tree, and refuses if the command changed tracked files
        or moved HEAD (it would attest bytes nobody committed). Returns the
        new HEAD sha.
        """
        if not self.gates:
            raise ValueError(f"lane {self.name!r} proves no gate in {self.policy.source}")
        if not self.repo.git.is_clean():
            raise RuntimeError("working tree is dirty: commit first, prooft attests commits")
        before = self.repo.commit()
        started = time.monotonic()
        code = subprocess.run(list(command), cwd=self.repo.root, check=False).returncode
        secs = int(time.monotonic() - started)
        if code != 0:
            raise LaneFailed(self.name, code)
        after = self.repo.commit()
        if after.sha != before.sha or not self.repo.git.is_clean():
            raise RuntimeError(f"lane {self.name!r} changed HEAD or tracked files; nothing was attested")
        return self.record(secs=secs, key=key)

    def record(self, *, secs: int | None = None, key: str = "", via: Via = "run", at: int | None = None) -> str:
        """Stamp HEAD for this lane without running anything (the caller ran it)."""
        head = self.repo.commit()
        stamps = [
            Attestation.new(g, tree=head.tree, parents=head.parents, lane=self.name, key=key, via=via, secs=secs, at=at)
            for g in self.gates
        ]
        return self.repo.git.amend_message(append(head.message, stamps))


def open(path: str | Path = ".") -> Repo:  # noqa: A001 - fluent `prooft.open(...)`
    root = Git(Path(path)).run("rev-parse", "--show-toplevel").strip()
    return Repo(Path(root))
