"""The bare ``prooft`` runner: lint first, then tests, then push.

    prooft                  run the lint stage; stamp HEAD when the tree is clean
    prooft --test           lint, then the test stage
    prooft --push           lint (and --test), then push with a lease and open or reuse the PR
    prooft --no-lint ...    skip lint. Discouraged: nothing is attested for lint,
                            `prooft check-build` keeps refusing builds, and hosted CI runs it
    prooft check-build      exit 0 only when lint passed on the current working tree

Lanes come from ``ci-attestations.yml``: a lane with ``run:`` is runnable, and its
``stage`` (``lint`` or ``test``, inferred from its gates when absent) decides
the order. Lint always runs before tests: it fails in seconds on a warm cache,
while tests build and run the whole project. ``run: soldr`` is ``soldr lint`` in
porcelain mode, stamping each Rust gate whose step passed.

A lint pass is also recorded locally against the *working tree* (dirty or not),
so ``prooft check-build`` can tell a build wrapper or command scanner whether
the bytes about to be built were linted. A dirty tree is linted but not
attested: attestations stamp commits.

Exit codes: 0 ok, 1 a lane failed, 2 usage or policy error, 3 refused.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import TextIO

from prooft.git import GitError
from prooft.policy import Policy, PolicyError
from prooft.repo import LaneFailed, Repo

LINT_RECORD = "prooft/lint-passes"
_RECORD_KEEP = 200
NO_LINT_WARNING = (
    "prooft: --no-lint skips the lint stage. This is discouraged: lint is the cheapest failure, "
    "builds stay refused until it passes, and hosted CI runs it anyway."
)
BUILD_REFUSED = (
    "prooft: lint has not passed on this working tree, so building is refused.\n"
    "  Run `prooft` first (Rust: `soldr lint` through prooft), fix what it reports, then build.\n"
    "  `prooft --test` runs lint and then the tests. Skipping lint (`--no-lint`) is discouraged."
)


class Refused(RuntimeError):
    """The runner will not proceed (dirty tree for a push, protected branch, nothing to run)."""


@dataclass(frozen=True)
class Options:
    lint: bool = True
    test: bool = False
    push: bool = False
    pr: bool = True
    base: str | None = None
    remote: str = "origin"
    dry_run: bool = False


@dataclass(frozen=True)
class Step:
    stage: str  # lint | test | push | pr
    lane: str
    argv: tuple[str, ...]


# ── the local lint record ────────────────────────────────────────────────


def record_path(repo: Repo) -> Path:
    return repo.git.git_dir() / LINT_RECORD


def lint_passed(repo: Repo, tree: str | None = None) -> bool:
    """Did lint pass on ``tree`` (default: the current working tree)?"""
    path = record_path(repo)
    if not path.is_file():
        return False
    tree = tree or repo.git.worktree_tree()
    return any(line.split(" ", 1)[0] == tree for line in path.read_text(encoding="utf-8").splitlines())


def record_lint(repo: Repo, tree: str) -> None:
    path = record_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    lines.append(f"{tree} {int(time.time())}")
    path.write_text("\n".join(lines[-_RECORD_KEEP:]) + "\n", encoding="utf-8")


def check_build(repo: Repo) -> tuple[bool, str]:
    """``(ok, message)``: may the current working tree be built?"""
    if lint_passed(repo):
        return True, "prooft: lint passed on this working tree; building is allowed"
    return False, BUILD_REFUSED


# ── planning ─────────────────────────────────────────────────────────────


def default_base(repo: Repo, remote: str) -> str:
    try:
        ref = repo.git.run("symbolic-ref", "--short", f"refs/remotes/{remote}/HEAD").strip()
    except GitError:
        return "main"
    return ref.split("/", 1)[1] if "/" in ref else ref


def plan(policy: Policy, opts: Options, *, branch: str | None, base: str) -> list[Step]:
    """Every step in order. Pure, so tests pin it: lint, then test, then push, then pr."""
    steps: list[Step] = []
    if opts.lint:
        lint = policy.runnable("lint")
        if not lint:
            raise PolicyError(
                [f"{policy.source}: no lint lane declares `run:` (Rust: `run: soldr`); `--no-lint` skips lint"]
            )
        steps += [Step("lint", lane, policy.runs[lane]) for lane in lint]
    if opts.test:
        tests = policy.runnable("test")
        if not tests:
            raise PolicyError([f"{policy.source}: --test, but no test lane declares `run:`"])
        steps += [Step("test", lane, policy.runs[lane]) for lane in tests]
    if opts.push:
        if branch is None:
            raise Refused("HEAD is detached: check out a branch to push")
        if branch == base:
            raise Refused(f"refusing to push the base branch {base!r}: push a topic branch")
        ref = f"HEAD:refs/heads/{branch}"
        steps.append(Step("push", "", ("git", "push", "--force-with-lease", "-u", opts.remote, ref)))
        if opts.pr:
            steps.append(Step("pr", "", ("gh", "pr", "create", "--fill", "--base", base, "--head", branch)))
    return steps


# ── running ──────────────────────────────────────────────────────────────


def _is_soldr_lint(argv: Sequence[str]) -> bool:
    return tuple(argv[:2]) == ("soldr", "lint")


def _attested(repo: Repo, policy: Policy, lane: str) -> bool:
    valid = set(repo.verify(policy=policy).valid)
    return all(g.path in valid for g in policy.lane_gates(lane))


def _run_dirty(repo: Repo, step: Step) -> None:
    """Run a lane on a dirty tree: judged by exit code, nothing stamped."""
    before = repo.git.worktree_tree()
    env = {**os.environ, "SOLDR_PORCELAIN": "1"} if _is_soldr_lint(step.argv) else None
    code = subprocess.run(list(step.argv), cwd=repo.root, env=env, check=False).returncode
    if code != 0:
        raise LaneFailed(step.lane, code)
    if repo.git.worktree_tree() != before:
        raise Refused(f"lane {step.lane!r} changed files; review the changes and rerun")


def _run_lane(repo: Repo, policy: Policy, step: Step) -> str:
    """Run one lane; return what happened, for the log."""
    if not repo.git.is_clean():
        _run_dirty(repo, step)
        return f"{step.stage} {step.lane}: passed on the working tree (dirty: commit to attest)"
    if _attested(repo, policy, step.lane):
        return f"{step.stage} {step.lane}: already attested on HEAD"
    if _is_soldr_lint(step.argv):
        result = repo.soldr(step.argv)
        if not result.ok:
            raise LaneFailed(step.lane, result.exit_code)
        return f"{step.stage} {step.lane}: attested {', '.join(result.gates) or 'no declared gate'}"
    sha = repo.lane(step.lane, policy=policy).run(step.argv)
    return f"{step.stage} {step.lane}: attested on {sha[:12]}"


def _push(repo: Repo, step: Step) -> None:
    code = subprocess.run(list(step.argv), cwd=repo.root, check=False).returncode
    if code != 0:
        raise LaneFailed("push", code)


def _pr(repo: Repo, step: Step, branch: str) -> str:
    found = subprocess.run(
        ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
        cwd=repo.root,
        capture_output=True,
        text=True,
        check=False,
    )
    if found.returncode == 0 and found.stdout.strip():
        return f"pr: {found.stdout.strip()} (existing)"
    code = subprocess.run(list(step.argv), cwd=repo.root, check=False).returncode
    if code != 0:
        raise LaneFailed("pr", code)
    return "pr: opened"


def run(repo: Repo, opts: Options, *, out: TextIO | None = None) -> int:
    policy = repo.policy()
    branch = repo.git.branch()
    base = opts.base or default_base(repo, opts.remote)
    steps = plan(policy, opts, branch=branch, base=base)
    stream = out or sys.stdout

    def say(line: str) -> None:
        print(f"prooft: {line}", file=stream, flush=True)

    if not opts.lint:
        print(NO_LINT_WARNING, file=sys.stderr, flush=True)
    if opts.dry_run:
        for step in steps:
            say(f"would run {step.stage}{' ' + step.lane if step.lane else ''}: {' '.join(step.argv)}")
        return 0
    if opts.push and not repo.git.is_clean():
        raise Refused("working tree is dirty: commit first, prooft attests commits")
    tree = repo.git.worktree_tree()
    last_lint = max((i for i, s in enumerate(steps) if s.stage == "lint"), default=-1)
    for i, step in enumerate(steps):
        if step.stage == "push":
            _push(repo, step)
            say(f"pushed {branch}")
        elif step.stage == "pr":
            say(_pr(repo, step, str(branch)))
        else:
            say(_run_lane(repo, policy, step))
        if i == last_lint:
            record_lint(repo, tree)  # a failing lane raised before this: every lint lane passed
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="prooft", description="Lint first, then tests, then push.")
    p.add_argument("-C", dest="path", default=".", help="repository path (default: .)")
    lint = p.add_mutually_exclusive_group()
    lint.add_argument("--lint", dest="lint", action="store_true", default=True, help="run the lint stage (default)")
    lint.add_argument("--no-lint", dest="lint", action="store_false", help="skip lint (discouraged)")
    p.add_argument("--test", action="store_true", help="run the test stage after lint")
    p.add_argument("--push", action="store_true", help="push the branch with a lease and open or reuse its PR")
    p.add_argument("--no-pr", dest="pr", action="store_false", help="with --push: do not open a PR")
    p.add_argument("--base", help="PR base (default: the remote's HEAD branch)")
    p.add_argument("--remote", default="origin")
    p.add_argument("--dry-run", action="store_true", help="print the steps and run nothing")
    return p


def main(argv: Sequence[str], open_repo: Callable[[str], Repo]) -> int:
    ns = parser().parse_args(list(argv))
    opts = Options(
        lint=ns.lint,
        test=ns.test,
        push=ns.push,
        pr=ns.pr,
        base=ns.base,
        remote=ns.remote,
        dry_run=ns.dry_run,
    )
    try:
        return run(open_repo(str(ns.path)), opts)
    except Refused as exc:
        print(f"prooft: refused: {exc}", file=sys.stderr)
        return 3
