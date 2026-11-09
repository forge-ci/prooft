"""A simulated GitHub: a bare ``origin``, pull requests, CI runs and a merge queue.

It exists so the attestation rules can be exercised end to end, offline, in a
unit test: a developer pushes, CI plans from the PR *base* policy, runs only
the jobs it cannot skip, and the merge queue builds a fresh commit on the base
and decides again. Each remote job is simulated by running the lane command
that the policy says proves its gates.

    hub = FakeGitHub.create(tmp, jobs={"ci.yml:fast": ["python", "-c", "pass"]})
    dev = hub.clone("dev")
    ...commit, attest...
    pr = hub.open_pr(dev, "feature", title="Add x")
    run = pr.ci()                     # -> Run(skipped=("ci.yml:fast",), ran=(), green=True)
    merged = hub.merge_queue(pr)      # -> Run on the queue commit
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
import subprocess

from prooft.git import Git
from prooft.repo import Repo
from prooft.verify import Plan


@dataclass(frozen=True)
class Run:
    event: str
    sha: str
    plan: Plan
    ran: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def skipped(self) -> tuple[str, ...]:
        return self.plan.skipped

    @property
    def green(self) -> bool:
        return not self.failed


@dataclass
class PullRequest:
    hub: FakeGitHub
    number: int
    branch: str
    title: str
    fork: bool = False
    runs: list[Run] = field(default_factory=list[Run])
    merged: bool = False

    @property
    def head(self) -> str:
        return self.hub.origin.run("rev-parse", f"refs/heads/{self.branch}").strip()

    def ci(self) -> Run:
        """A ``pull_request`` run on the current head."""
        run = self.hub.run_ci("pull_request", self.head, base="main", title=self.title, fork=self.fork)
        self.runs.append(run)
        return run


@dataclass
class FakeGitHub:
    root: Path
    jobs: Mapping[str, Sequence[str]]
    prs: list[PullRequest] = field(default_factory=list[PullRequest])
    log: list[str] = field(default_factory=list[str])

    @classmethod
    def create(cls, root: Path, *, jobs: Mapping[str, Sequence[str]]) -> FakeGitHub:
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / "origin.git")], check=True)
        return cls(root, jobs)

    @property
    def origin(self) -> Git:
        return Git(self.root / "origin.git")

    def clone(self, name: str) -> Repo:
        path = self.root / name
        subprocess.run(
            ["git", "clone", "-q", str(self.root / "origin.git"), str(path)], check=True, capture_output=True
        )
        git = Git(path)
        git.run("config", "user.email", f"{name}@example.invalid")
        git.run("config", "user.name", name)
        git.run("config", "commit.gpgsign", "false")
        return Repo(path)

    def open_pr(self, repo: Repo, branch: str, *, title: str, fork: bool = False) -> PullRequest:
        repo.git.run("push", "-q", "--force", "origin", f"HEAD:refs/heads/{branch}")
        pr = PullRequest(self, len(self.prs) + 1, branch, title, fork)
        self.prs.append(pr)
        self.log.append(f"PR #{pr.number} opened: {title}")
        return pr

    def push(self, repo: Repo, pr: PullRequest) -> Run:
        """Force-push the developer's HEAD to the PR branch, then run CI."""
        repo.git.run("push", "-q", "--force", "origin", f"HEAD:refs/heads/{pr.branch}")
        return pr.ci()

    # ── CI ───────────────────────────────────────────────────────────────

    def _runner(self) -> Repo:
        path = self.root / "_runner"
        if not path.exists():
            subprocess.run(
                ["git", "clone", "-q", str(self.root / "origin.git"), str(path)], check=True, capture_output=True
            )
            for key, value in (("user.email", "ci@example.invalid"), ("user.name", "ci"), ("commit.gpgsign", "false")):
                Git(path).run("config", key, value)
        repo = Repo(path)
        repo.git.run(
            "fetch", "-q", "--force", "origin", "+refs/heads/*:refs/remotes/origin/*", "+refs/queue/*:refs/queue/*"
        )
        return repo

    def _execute(self, runner: Repo, sha: str, plan: Plan) -> tuple[tuple[str, ...], tuple[str, ...]]:
        runner.git.run("checkout", "-q", "--detach", sha)
        ran: list[str] = []
        failed: list[str] = []
        for job in plan.run:
            command = self.jobs.get(job)
            if command is None:
                continue
            ran.append(job)
            if subprocess.run(list(command), cwd=runner.root, check=False, capture_output=True).returncode != 0:
                failed.append(job)
        return tuple(ran), tuple(failed)

    def run_ci(self, event: str, sha: str, *, base: str, title: str = "", fork: bool = False) -> Run:
        runner = self._runner()
        plan = runner.plan(f"origin/{base}", sha, title=title, fork=fork)
        ran, failed = self._execute(runner, sha, plan)
        run = Run(event, sha, plan, ran, failed)
        self.log.append(f"{event} {sha[:8]}: skipped={list(run.skipped)} ran={list(ran)} failed={list(failed)}")
        return run

    # ── merge queue ──────────────────────────────────────────────────────

    def merge_queue(self, pr: PullRequest) -> Run:
        """Build the queue commit (PR tree on top of main), run CI, fast-forward main if green."""
        runner = self._runner()
        base = runner.commit("origin/main").sha
        head = pr.head
        runner.git.run("checkout", "-q", "--detach", base)
        try:
            runner.git.run("merge", "-q", "--squash", head)
        except Exception:
            runner.git.run("reset", "-q", "--hard", base)
            raise
        runner.git.run("commit", "-q", "--allow-empty", "-m", f"{pr.title} (#{pr.number})")
        queue_sha = runner.commit().sha
        plan = runner.queue(pr_head=head, queue_head=queue_sha, base=base)
        ran, failed = self._execute(runner, queue_sha, plan)
        run = Run("merge_group", queue_sha, plan, ran, failed)
        self.log.append(f"merge_group #{pr.number} {queue_sha[:8]}: skipped={list(run.skipped)} ran={list(ran)}")
        if run.green:
            runner.git.run("push", "-q", "origin", f"{queue_sha}:refs/heads/main")
            pr.merged = True
            self.log.append(f"PR #{pr.number} merged as {queue_sha[:8]}")
        pr.runs.append(run)
        return run

    def merge_group(self, prs: list[PullRequest]) -> list[Run]:
        """A multi-PR group (``grouping_strategy: ALLGREEN``): entry N sits on entries 1..N-1.

        Each entry gets its own ``merge_group`` run; main fast-forwards to the
        top only if every entry is green, as GitHub does.
        """
        runner = self._runner()
        base = runner.commit("origin/main").sha
        runner.git.run("checkout", "-q", "--detach", base)
        runs: list[Run] = []
        for pr in prs:
            runner.git.run("merge", "-q", "--squash", pr.head)
            runner.git.run("commit", "-q", "--allow-empty", "-m", f"{pr.title} (#{pr.number})")
            sha = runner.commit().sha
            plan = runner.queue(pr_head=pr.head, queue_head=sha, base=base)
            ran, failed = self._execute(runner, sha, plan)
            run = Run("merge_group", sha, plan, ran, failed)
            self.log.append(f"merge_group #{pr.number} {sha[:8]}: skipped={list(run.skipped)} ran={list(ran)}")
            pr.runs.append(run)
            runs.append(run)
        if all(r.green for r in runs):
            runner.git.run("push", "-q", "origin", f"{runs[-1].sha}:refs/heads/main")
            for pr in prs:
                pr.merged = True
        return runs
