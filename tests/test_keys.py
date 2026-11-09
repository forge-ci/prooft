"""Input-keyed proofs: survive a rebase or a moved main when the lane's inputs are unchanged."""

from __future__ import annotations

import pytest

from prooft import State
from prooft.keys import matches
from prooft.policy import Policy, PolicyError
from prooft.repo import Repo
from prooft.sim.github import FakeGitHub
from tests.conftest import commit

KEYED = """\
version: 1
lanes:
  lint:
    inputs: ["src/**", pyproject.toml]
gates:
  python/all/lint: {lane: lint}
  python/linux-x64/test: {lane: tests}
jobs:
  ci.yml:lint: [python/all/lint]
  ci.yml:test: [python/linux-x64/test]
"""


@pytest.mark.parametrize(
    ("path", "pattern", "hit"),
    [
        ("src/a.py", "src/**", True),
        ("src/x/y/a.py", "src/**", True),
        ("srcx/a.py", "src/**", False),
        ("a.py", "*.py", True),
        ("d/a.py", "*.py", False),
        ("d/a.py", "**/*.py", True),
        ("a.py", "**/*.py", True),
        ("pyproject.toml", "pyproject.toml", True),
        ("a.txt", "a.?xt", True),
    ],
)
def test_glob(path: str, pattern: str, hit: bool) -> None:
    assert matches(path, [pattern]) is hit


def test_policy_parses_inputs() -> None:
    assert Policy.parse(KEYED).inputs == {"lint": ("src/**", "pyproject.toml")}
    with pytest.raises(PolicyError, match="inputs must be"):
        Policy.parse("version: 1\nlanes:\n  lint:\n    inputs: []\n")


@pytest.fixture
def keyed(hub: FakeGitHub, dev: Repo) -> Repo:
    """Put the keyed policy on main, rebase dev onto it."""
    seed = hub.clone("keyed-seed")
    commit(seed, "ci-attestations.yml", KEYED, "keyed policy")
    seed.git.run("push", "-q", "origin", "HEAD:main")
    dev.git.run("fetch", "-q", "origin")
    dev.git.run("reset", "-q", "--hard", "origin/main")
    return dev


def move_main(hub: FakeGitHub, path: str) -> None:
    other = hub.clone(f"mover-{path.replace('/', '-')}")
    commit(other, path, "moved", f"main touches {path}")
    other.git.run("push", "-q", "origin", "HEAD:main")


def test_stamp_carries_the_input_key(keyed: Repo) -> None:
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    keyed.lane("lint").run(["true"])
    (status,) = [s for s in keyed.verify().statuses if s.gate == "python/all/lint"]
    assert status.state is State.VALID
    assert "same tree" in status.detail


def test_rebase_over_unrelated_change_keeps_a_keyed_proof(hub: FakeGitHub, keyed: Repo) -> None:
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    keyed.lane("lint").run(["true"])
    keyed.lane("tests").run(["true"])
    move_main(hub, "docs/notes.md")
    keyed.git.run("fetch", "-q", "origin")
    keyed.git.run("rebase", "-q", "origin/main")
    verdict = keyed.verify()
    assert verdict.state("python/all/lint") is State.VALID  # keyed lane: inputs unchanged
    assert verdict.state("python/linux-x64/test") is State.WRONG_TREE  # unkeyed lane: tree-bound


def test_rebase_over_an_input_change_invalidates(hub: FakeGitHub, keyed: Repo) -> None:
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    keyed.lane("lint").run(["true"])
    move_main(hub, "src/b.py")
    keyed.git.run("fetch", "-q", "origin")
    keyed.git.run("rebase", "-q", "origin/main")
    assert keyed.verify().state("python/all/lint") is State.WRONG_TREE


def test_queue_credits_keyed_lane_after_main_moved(hub: FakeGitHub, keyed: Repo) -> None:
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    keyed.lane("lint").run(["true"])
    keyed.lane("tests").run(["true"])
    pr = hub.open_pr(keyed, "feature", title="add a")
    move_main(hub, "docs/notes.md")
    run = hub.merge_queue(pr)
    assert run.skipped == ("ci.yml:lint",)  # inputs unchanged: still proven
    assert run.ran == ("ci.yml:test",)  # tree-bound: reruns on the new tree
    assert pr.merged


def test_queue_reruns_keyed_lane_when_main_changed_its_inputs(hub: FakeGitHub, keyed: Repo) -> None:
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    keyed.lane("lint").run(["true"])
    pr = hub.open_pr(keyed, "feature", title="add a")
    move_main(hub, "src/b.py")
    run = hub.merge_queue(pr)
    assert "ci.yml:lint" in run.ran
