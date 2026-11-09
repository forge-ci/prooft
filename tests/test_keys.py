"""Input-keyed proofs: survive a rebase or a moved main when the lane's inputs are unchanged."""

from __future__ import annotations

import pytest

from prooft import State
from prooft.keys import matches
from prooft.policy import Policy, PolicyError
from prooft.repo import Repo
from prooft.sim.github import FakeGitHub, PullRequest
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


def attested_pr(hub: FakeGitHub, name: str, path: str, *, lanes: tuple[str, ...] = ("lint",)) -> PullRequest:
    """A PR from its own clone of main, attested on the given lanes."""
    author = hub.clone(name)
    commit(author, path, f"{name}\n", f"{name} touches {path}")
    for lane in lanes:
        author.lane(lane).run(["true"])
    return hub.open_pr(author, name, title=name)


def test_multi_pr_group_credits_later_entries_with_unchanged_inputs(hub: FakeGitHub, keyed: Repo) -> None:
    """obs-studio queues groups of up to 5 (ALLGREEN): entry 2 sits on entry 1's squash commit."""
    del keyed
    docs = attested_pr(hub, "docs-pr", "docs/a.md")
    code = attested_pr(hub, "code-pr", "src/b.py")
    first, second = hub.merge_group([docs, code])
    assert first.skipped == ("ci.yml:lint",)
    assert second.skipped == ("ci.yml:lint",)  # docs-pr changed no lint input
    assert docs.merged and code.merged


def test_multi_pr_group_reruns_when_an_earlier_entry_changed_the_inputs(hub: FakeGitHub, keyed: Repo) -> None:
    del keyed
    one = attested_pr(hub, "one", "src/one.py")
    two = attested_pr(hub, "two", "src/two.py")
    _, second = hub.merge_group([one, two])
    assert "ci.yml:lint" in second.ran  # entry 2's inputs include entry 1's file


def test_multi_pr_group_reruns_after_an_earlier_entry_edits_a_trust_surface(hub: FakeGitHub, keyed: Repo) -> None:
    del keyed
    ci = attested_pr(hub, "ci-edit", ".github/workflows/x.yml")
    code = attested_pr(hub, "code", "src/c.py")
    _, second = hub.merge_group([ci, code])
    assert "ci.yml:lint" in second.ran
