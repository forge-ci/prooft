"""The bare ``prooft`` runner: lint first, then tests, then push; and ``check-build``."""

from __future__ import annotations

import io
from pathlib import Path
import sys

import pytest

from prooft import cli, runner
from prooft.policy import Policy, PolicyError
from prooft.repo import LaneFailed, Repo
from tests.conftest import commit

# Each lane appends its name to .git/order and fails when a file named <LANE>BAD exists.
CHECK = """\
import pathlib, subprocess, sys
lane = sys.argv[1]
git = subprocess.run(["git", "rev-parse", "--absolute-git-dir"], capture_output=True, text=True)
gitdir = pathlib.Path(git.stdout.strip())
with open(gitdir / "order", "a") as fh:
    fh.write(lane + "\\n")
sys.exit(pathlib.Path(lane.upper() + "BAD").exists())
"""

# `tests` is declared first on purpose: the runner must still lint first.
POLICY = f"""\
version: 1
lanes:
  tests:
    run: [{sys.executable}, check.py, tests]
  lint:
    run: [{sys.executable}, check.py, lint]
gates:
  python/all/lint: {{lane: lint}}
  python/linux-x64/test: {{lane: tests}}
jobs:
  ci.yml:lint: [python/all/lint]
  ci.yml:test: [python/linux-x64/test]
"""


@pytest.fixture
def repo(dev: Repo) -> Repo:
    (dev.root / "check.py").write_text(CHECK, encoding="utf-8")
    commit(dev, "ci-attestations.yml", POLICY, "runner policy")
    return dev


def order(repo: Repo) -> list[str]:
    path = repo.git.git_dir() / "order"
    return path.read_text(encoding="utf-8").split() if path.is_file() else []


def go(repo: Repo, opts: runner.Options | None = None) -> int:
    return runner.run(repo, opts or runner.Options(), out=io.StringIO())


# ── policy ───────────────────────────────────────────────────────────────


def test_stage_is_inferred_from_gate_checks_and_can_be_declared() -> None:
    policy = Policy.parse(
        """\
version: 1
lanes:
  rust: {run: soldr}
  unit:
    run: [soldr, cargo, test]
  slowlint:
    run: [./lint]
    stage: test
gates:
  rust/all/fmt: {lane: rust}
  rust/x86_64-unknown-linux-gnu/clippy: {lane: rust}
  rust/x86_64-unknown-linux-gnu/test: {lane: unit}
  general/linux-x64/lint: {lane: slowlint}
"""
    )
    assert policy.runs["rust"] == ("soldr", "lint")
    assert policy.runnable("lint") == ("rust",)
    assert policy.runnable("test") == ("unit", "slowlint")


@pytest.mark.parametrize(
    ("lanes", "needle"),
    [
        ("  lint:\n    run: [x]\n    stage: build", "stage must be one of"),
        ("  lint: {run: 7}", "run must be `soldr`"),
        ("  orphan:\n    run: [x]", "proves no gate"),
    ],
)
def test_bad_run_and_stage_are_policy_errors(lanes: str, needle: str) -> None:
    text = f"version: 1\nlanes:\n{lanes}\ngates:\n  python/all/lint: {{lane: lint}}\n"
    with pytest.raises(PolicyError, match=needle):
        Policy.parse(text)


def test_plan_puts_lint_before_test_before_push_before_pr(repo: Repo) -> None:
    opts = runner.Options(test=True, push=True)
    steps = runner.plan(repo.policy(), opts, branch="feature", base="main")
    assert [(s.stage, s.lane) for s in steps] == [("lint", "lint"), ("test", "tests"), ("push", ""), ("pr", "")]
    assert steps[2].argv == ("git", "push", "--force-with-lease", "-u", "origin", "HEAD:refs/heads/feature")
    assert steps[3].argv[-4:] == ("--base", "main", "--head", "feature")


def test_plan_without_a_lint_lane_says_how_to_declare_one(dev: Repo) -> None:
    with pytest.raises(PolicyError, match="run: soldr"):
        runner.plan(dev.policy(), runner.Options(), branch="feature", base="main")


@pytest.mark.parametrize(("branch", "needle"), [(None, "detached"), ("main", "base branch")])
def test_plan_refuses_to_push_a_detached_head_or_the_base(repo: Repo, branch: str | None, needle: str) -> None:
    with pytest.raises(runner.Refused, match=needle):
        runner.plan(repo.policy(), runner.Options(push=True), branch=branch, base="main")


# ── running ──────────────────────────────────────────────────────────────


def test_bare_runner_lints_attests_and_allows_the_build(repo: Repo) -> None:
    assert runner.check_build(repo)[0] is False
    assert go(repo) == 0
    assert order(repo) == ["lint"]
    verdict = repo.verify()
    assert verdict.ok("python/all/lint")
    assert not verdict.ok("python/linux-x64/test")
    assert runner.check_build(repo)[0] is True


def test_test_flag_runs_lint_first_and_attests_both(repo: Repo) -> None:
    assert go(repo, runner.Options(test=True)) == 0
    assert order(repo) == ["lint", "tests"]
    assert set(repo.verify().valid) == {"python/all/lint", "python/linux-x64/test"}


def test_a_lint_failure_stops_before_the_tests(repo: Repo) -> None:
    commit(repo, "LINTBAD", "", "break lint")
    with pytest.raises(LaneFailed):
        go(repo, runner.Options(test=True))
    assert order(repo) == ["lint"]
    assert runner.check_build(repo) == (False, runner.BUILD_REFUSED)


def test_an_attested_lane_is_not_rerun(repo: Repo) -> None:
    go(repo)
    go(repo)
    assert order(repo) == ["lint"]


def test_a_dirty_tree_is_linted_but_not_attested_and_edits_revoke_the_build(repo: Repo) -> None:
    commit(repo, "src.py", "x = 0\n", "tracked source")
    (repo.root / "src.py").write_text("x = 1\n", encoding="utf-8")
    head = repo.commit().sha
    assert go(repo) == 0
    assert repo.commit().sha == head  # nothing stamped
    assert runner.check_build(repo)[0] is True
    (repo.root / "src.py").write_text("x = 2\n", encoding="utf-8")
    ok, message = runner.check_build(repo)
    assert not ok
    assert "Run `prooft` first" in message


def test_no_lint_warns_and_builds_stay_refused(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert go(repo, runner.Options(lint=False, test=True)) == 0
    assert order(repo) == ["tests"]
    assert "discouraged" in capsys.readouterr().err
    assert runner.check_build(repo)[0] is False


def test_push_lints_then_pushes_the_branch(repo: Repo) -> None:
    assert go(repo, runner.Options(push=True, pr=False, base="main")) == 0
    remote = repo.git.run("ls-remote", "origin", "refs/heads/feature").split()
    assert remote[0] == repo.commit().sha
    assert repo.verify().ok("python/all/lint")


def test_push_refuses_a_dirty_tree(repo: Repo) -> None:
    (repo.root / "check.py").write_text("changed\n", encoding="utf-8")
    with pytest.raises(runner.Refused, match="dirty"):
        go(repo, runner.Options(push=True, pr=False, base="main"))
    assert order(repo) == []


def test_dry_run_prints_the_order_and_runs_nothing(repo: Repo) -> None:
    out = io.StringIO()
    runner.run(repo, runner.Options(test=True, dry_run=True), out=out)
    lines = out.getvalue().splitlines()
    assert [line.split()[3] for line in lines] == ["lint", "test"]
    assert order(repo) == []


# ── command line ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("argv", "is_runner"),
    [
        ([], True),
        (["--test"], True),
        (["-C", "x", "--push"], True),
        (["-C", "x"], True),
        (["verify"], False),
        (["-C", "x", "lanes"], False),
        (["--help"], False),
    ],
)
def test_cli_routes_flag_only_invocations_to_the_runner(argv: list[str], is_runner: bool) -> None:
    assert cli._is_runner(argv) is is_runner  # pyright: ignore[reportPrivateUsage]


def test_cli_check_build_exits_3_with_instructions(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["-C", str(repo.root), "check-build"]) == 3
    assert "Run `prooft` first" in capsys.readouterr().err
    assert cli.main(["-C", str(repo.root)]) == 0
    assert cli.main(["-C", str(repo.root), "check-build"]) == 0


def test_cli_lane_failure_exits_1(repo: Repo) -> None:
    commit(repo, "LINTBAD", "", "break lint")
    assert cli.main(["-C", str(repo.root), "--test"]) == 1
    assert order(repo) == ["lint"]


def test_worktree_tree_matches_head_tree_when_clean(repo: Repo) -> None:
    assert repo.git.worktree_tree() == repo.commit().tree
    (repo.root / "new.txt").write_text("n\n", encoding="utf-8")
    assert repo.git.worktree_tree() != repo.commit().tree
    assert Path(repo.root / "new.txt").exists()
    assert "new.txt" not in repo.git.run("diff", "--cached", "--name-only")


# ── a lane that is itself an attester (ci-lint local-gate run) ──────────


AMEND = """\
import subprocess, sys
msg = subprocess.run(["git", "log", "-1", "--format=%B"], capture_output=True, text=True).stdout
if sys.argv[1] == "message":
    subprocess.run(["git", "commit", "-q", "--amend", "-m", msg.rstrip() + "\\n\\nLocal-Gate: tree=x"], check=True)
else:
    open("extra.txt", "w").write("x")
    subprocess.run(["git", "add", "extra.txt"], check=True)
    subprocess.run(["git", "commit", "-q", "-m", "sneaky"], check=True)
"""


def _amend_policy(repo: Repo, mode: str) -> None:
    (repo.root / "amend.py").write_text(AMEND, encoding="utf-8")
    lane = f"  lint:\n    run: [{sys.executable}, amend.py, {mode}]\n"
    policy = f"version: 1\nlanes:\n{lane}gates:\n  python/all/lint: {{lane: lint}}\n"
    commit(repo, "ci-attestations.yml", policy, "amending lane")


def test_a_lane_may_amend_only_the_message_and_its_trailer_survives(repo: Repo) -> None:
    _amend_policy(repo, "message")
    assert go(repo) == 0
    message = repo.commit().message
    assert "Local-Gate: tree=x" in message
    assert repo.verify().ok("python/all/lint")


def test_a_lane_that_commits_new_content_is_refused(repo: Repo) -> None:
    _amend_policy(repo, "content")
    with pytest.raises(RuntimeError, match="changed HEAD"):
        go(repo)
