"""The fluent entry point.

import prooft

repo = prooft.open(".")
repo.lane("rust").run(["soldr", "lint"])     # run, then stamp HEAD on success
repo.verify().ok("rust/all/fmt")             # -> True
repo.plan(base="origin/main").skipped        # -> ("ci.yml:fast",)
repo.queue(pr_head="pr-head", queue_head="HEAD", base="origin/main").skipped
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from prooft.adapters import soldr as soldr_adapter
from prooft.attestation import Attestation, Trailers, Via, append
from prooft.git import Commit, Git
from prooft.keys import lane_key
from prooft.ledger import Ledger, credit
from prooft.platforms import host_triple
from prooft.policy import POLICY_FILE, Policy, PolicyError
from prooft.verify import Plan, State, Verdict, decide, verify

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

    def keys(self, policy: Policy, rev: str = "HEAD") -> dict[str, str]:
        """Input key per lane that declares inputs, computed from ``rev``'s files."""
        return {lane: lane_key(self.git, rev, lane, globs) for lane, globs in policy.inputs.items()}

    def verify(self, rev: str = "HEAD", *, policy: Policy | None = None, now: float | None = None) -> Verdict:
        policy = policy or self.policy()
        return verify(self.commit(rev), policy, now=now, keys=self.keys(policy, rev))

    # ── writing ──────────────────────────────────────────────────────────

    def lane(self, name: str, *, policy: Policy | None = None) -> LaneRun:
        return LaneRun(self, name, policy or self.policy())

    def stamp(
        self,
        gates: Iterable[str],
        *,
        policy: Policy | None = None,
        secs: int | None = None,
        key: str = "",
        via: Via = "run",
        at: int | None = None,
    ) -> str:
        """Stamp HEAD for exactly ``gates``, each with its policy lane and input key. Returns the new HEAD."""
        policy = policy or self.policy()
        head = self.commit()
        stamps: list[Attestation] = []
        for path in dict.fromkeys(gates):
            gate = policy.gates.get(path)
            if gate is None:
                raise ValueError(f"{path} is not a gate in {policy.source}")
            globs = policy.inputs.get(gate.lane)
            lane_k = key or (lane_key(self.git, head.sha, gate.lane, globs) if globs else "")
            stamps.append(
                Attestation.new(
                    path, tree=head.tree, parents=head.parents, lane=gate.lane, key=lane_k, via=via, secs=secs, at=at
                )
            )
        if not stamps:
            return head.sha
        return self.git.amend_message(append(head.message, stamps))

    def soldr(
        self, command: Sequence[str] = ("soldr", "lint", "rust", "--check"), *, host: str | None = None
    ) -> SoldrRun:
        """Run ``soldr lint`` in porcelain mode, then stamp the declared Rust gates whose steps passed.

        Like ``LaneRun.run`` it refuses a dirty tree and a run that changes
        tracked files. A failing step stamps nothing for itself; the steps
        that passed before it are still stamped.
        """
        policy = self.policy()
        triple = host or host_triple()
        if triple is None:
            raise RuntimeError("unknown host triple: pass host=...")
        if not self.git.is_clean():
            raise RuntimeError("working tree is dirty: commit first, prooft attests commits")
        before = self.commit().sha
        env = {**os.environ, "SOLDR_PORCELAIN": "1", "CI": "true"}
        started = time.monotonic()
        proc = subprocess.run(list(command), cwd=self.root, env=env, capture_output=True, text=True, check=False)
        secs = int(time.monotonic() - started)
        sys.stderr.write(proc.stderr)
        if self.commit().sha != before or not self.git.is_clean():
            raise RuntimeError("soldr changed HEAD or tracked files; nothing was attested")
        gates = soldr_adapter.proven_gates(proc.stdout, triple, frozenset(policy.gates))
        sha = self.stamp(gates, policy=policy, secs=secs) if gates else before
        return SoldrRun(sha=sha, gates=gates, exit_code=proc.returncode)

    def emit(self, rev: str = "HEAD", *, policy: Policy | None = None) -> dict[str, object]:
        """A JSON-ready bundle of ``rev``'s *valid* attestations, for tools that cannot amend commits."""
        policy = policy or self.policy()
        commit = self.commit(rev)
        valid = set(self.verify(rev, policy=policy).valid)
        return {
            "v": 1,
            "sha": commit.sha,
            "tree": commit.tree,
            "parents": list(commit.parents),
            "attestations": [
                json.loads(a.to_json()) for a in Trailers.parse(commit.message).attestations if a.gate in valid
            ],
        }

    def apply(self, bundle: Mapping[str, object], *, policy: Policy | None = None) -> tuple[str, ...]:
        """Stamp HEAD with every attestation in ``bundle`` that is valid *for HEAD*; return those gates.

        A bundle produced elsewhere (a build farm, another clone) is judged
        exactly as if its trailers were on HEAD already: an entry for another
        tree only counts when its lane's declared inputs are unchanged.
        """
        policy = policy or self.policy()
        raw = bundle.get("attestations")
        if bundle.get("v") != 1 or not isinstance(raw, list):
            raise ValueError("not a prooft bundle (expected v=1 and an 'attestations' list)")
        incoming = [Attestation.from_json(json.dumps(item)) for item in raw]  # pyright: ignore[reportUnknownVariableType]
        head = self.commit()
        candidate = Commit(head.sha, head.tree, head.parents, "\n".join(a.to_trailer() for a in incoming))
        verdict = verify(candidate, policy, keys=self.keys(policy, head.sha))
        accepted = [a for a in incoming if verdict.state(a.gate) is State.VALID]
        if accepted:
            self.git.amend_message(append(head.message, accepted))
        return tuple(a.gate for a in accepted)

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
        ledger: Ledger | None = None,
        keys: Mapping[str, str] | None = None,
    ) -> Plan:
        """Which jobs a ``pull_request`` run on ``head`` may skip. Policy comes from ``base``.

        ``keys`` overrides the input key of named lanes (a repository that
        keys its lanes its own way, e.g. obs-studio's ``lane_proof.py key``).
        """
        policy = self.policy(base)
        trusted, why = self.trust(base, head, title=title, fork=fork)
        keys = {**self.keys(policy, head), **(keys or {})}
        verdict = self._credit(verify(self.commit(head), policy, now=now, keys=keys), policy, keys, ledger, now)
        return decide(policy, verdict, trusted=trusted, untrusted_reason=why)

    @staticmethod
    def _credit(
        verdict: Verdict, policy: Policy, keys: Mapping[str, str], ledger: Ledger | None, now: float | None
    ) -> Verdict:
        return verdict if ledger is None else credit(verdict, policy, keys, ledger, now=now)

    def queue(
        self,
        pr_head: str,
        queue_head: str,
        base: str,
        *,
        now: float | None = None,
        ledger: Ledger | None = None,
        keys: Mapping[str, str] | None = None,
    ) -> Plan:
        """Merge-queue credit for one entry of a merge group.

        The queue commit is new (new parents), so its own trailers never match.
        It inherits the PR head's proofs in two ways:

        - **same tree**: the queue would test exactly the attested bytes
          (first entry only);
        - **same inputs**: ``main`` moved, or earlier group entries sit
          underneath, but a lane whose declared inputs hash to the attested
          key at the *queue* commit still counts.

        Later entries of a multi-PR group (``max_entries_to_merge > 1``) are
        credited only when the entries beneath them touch no trust surface.
        """
        policy = self.policy(base)
        pr = self.commit(pr_head)
        q = self.commit(queue_head)
        base_sha = self.commit(base).sha
        depth = self._queue_depth(q, base_sha)
        if depth is None:
            verdict = verify(pr, policy, now=now)
            reason = "queue head is not a linear stack of group entries on the base"
            return decide(policy, verdict, trusted=False, untrusted_reason=reason)
        trusted, why = self.trust(base_sha, pr.sha)
        if trusted and depth > 1:
            below, why_below = self.trust(base_sha, q.parents[0])
            if not below:
                trusted, why = False, f"an earlier group entry: {why_below}"
        keys = {**self.keys(policy, q.sha), **(keys or {})}
        if depth == 1 and q.tree == pr.tree:
            verdict = verify(pr, policy, now=now, keys=keys)
        else:
            as_queued = Commit(q.sha, q.tree, q.parents, pr.message)
            verdict = verify(as_queued, policy, now=now, keys=keys)
        verdict = self._credit(verdict, policy, keys, ledger, now)
        return decide(policy, verdict, trusted=trusted, untrusted_reason=why)

    def _queue_depth(self, q: Commit, base_sha: str, limit: int = 32) -> int | None:
        """Group entries from ``base`` up to ``q`` (1 = first entry); None if not a linear stack."""
        commit, depth = q, 0
        while depth < limit:
            if len(commit.parents) != 1:
                return None
            depth += 1
            if commit.parents[0] == base_sha:
                return depth
            commit = self.commit(commit.parents[0])
        return None

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
        """Stamp HEAD for every gate of this lane without running anything (the caller ran it)."""
        return self.repo.stamp(self.gates, policy=self.policy, secs=secs, key=key, via=via, at=at)


def open(path: str | Path = ".") -> Repo:  # noqa: A001 - fluent `prooft.open(...)`
    root = Git(Path(path)).run("rev-parse", "--show-toplevel").strip()
    return Repo(Path(root))


@dataclass(frozen=True)
class SoldrRun:
    sha: str
    gates: tuple[str, ...]
    exit_code: int

    @property
    def ok(self) -> bool:
        return self.exit_code == 0
