"""End-to-end PR / push / merge-queue simulations against a fake GitHub."""

from __future__ import annotations

import time

import pytest

from prooft import State
from prooft.repo import LaneFailed, Repo
from prooft.sim.github import FakeGitHub
from tests.conftest import POLICY, commit

OK = ["true"]


def attest_all(repo: Repo) -> None:
    repo.lane("lint").run(OK)
    repo.lane("tests").run(OK)


def test_unattested_pr_runs_everything(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    run = hub.open_pr(dev, "feature", title="add a").ci()
    assert run.skipped == ()
    assert set(run.ran) == {"ci.yml:lint", "ci.yml:test"}
    assert run.green


def test_fully_attested_pr_skips_everything(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    run = hub.open_pr(dev, "feature", title="add a").ci()
    assert set(run.skipped) == {"ci.yml:lint", "ci.yml:test"}
    assert run.ran == ()


def test_partial_attestation_skips_only_proven_jobs(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    dev.lane("lint").run(OK)
    run = hub.open_pr(dev, "feature", title="add a").ci()
    assert run.skipped == ("ci.yml:lint",)
    assert run.ran == ("ci.yml:test",)


def test_new_commit_after_attesting_invalidates_proof(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    pr = hub.open_pr(dev, "feature", title="add a")
    commit(dev, "b.txt", "2", "add b")  # new commit, no trailers
    run = hub.push(dev, pr)
    assert run.skipped == ()


def test_content_amend_is_wrong_tree(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    (dev.root / "a.txt").write_text("changed", encoding="utf-8")
    dev.git.run("commit", "-q", "-a", "--amend", "--no-edit")
    assert dev.verify().state("python/all/lint") is State.WRONG_TREE


def test_rebase_is_wrong_parents(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    other = hub.clone("other")
    commit(other, "z.txt", "z", "main moves")
    other.git.run("push", "-q", "origin", "HEAD:main")
    dev.git.run("fetch", "-q", "origin")
    dev.git.run("rebase", "-q", "origin/main")
    # main added a file, so the rebased tree differs as well
    assert dev.verify().state("python/all/lint") is State.WRONG_TREE


def test_same_tree_on_other_parents_is_wrong_parents(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    head = dev.commit()
    grafted = dev.git.run("commit-tree", head.tree, "-m", head.message).strip()  # same tree, no parents
    assert dev.verify(grafted).state("python/all/lint") is State.WRONG_PARENTS


def test_tampered_trailer_is_bad_stamp(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    dev.lane("lint").run(OK)
    msg = dev.commit().message.replace('"via":"run"', '"via":"reused"')
    dev.git.amend_message(msg)
    assert dev.verify().state("python/all/lint") is State.BAD_STAMP


def test_stale_attestation_runs_remotely(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    dev.lane("lint").record(at=int(time.time()) - 25 * 3600)
    run = hub.open_pr(dev, "feature", title="add a").ci()
    assert run.plan.verdict.state("python/all/lint") is State.STALE
    assert "ci.yml:lint" in run.ran


def test_failed_lane_attests_nothing(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    with pytest.raises(LaneFailed):
        dev.lane("lint").run(["false"])
    assert dev.verify().state("python/all/lint") is State.MISSING


def test_dirty_tree_refuses_to_attest(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    (dev.root / "a.txt").write_text("dirty", encoding="utf-8")
    with pytest.raises(RuntimeError, match="dirty"):
        dev.lane("lint").run(OK)


@pytest.mark.parametrize(("title", "fork"), [("[ci-full] add a", False), ("add a", True)])
def test_title_tag_and_fork_force_remote(hub: FakeGitHub, dev: Repo, title: str, fork: bool) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    run = hub.open_pr(dev, "feature", title=title, fork=fork).ci()
    assert run.skipped == ()


def test_pr_cannot_loosen_its_own_policy(hub: FakeGitHub, dev: Repo) -> None:
    """Editing ci-attestations.yml is a trust surface: the PR runs remotely."""
    commit(dev, "ci-attestations.yml", POLICY.replace("max-age-hours: 24", "max-age-hours: 9999"), "loosen")
    attest_all(dev)
    run = hub.open_pr(dev, "feature", title="loosen").ci()
    assert run.skipped == ()
    assert "trust surface" in run.plan.jobs[0].reason


def test_lying_attestation_is_caught_after_merge_not_silently(hub: FakeGitHub, dev: Repo) -> None:
    """The honesty contract: a forged pass skips the PR run, but main's push run still executes."""
    commit(dev, "BROKEN", "", "break it")
    attest_all(dev)  # 'true' always passes: a liar
    pr = hub.open_pr(dev, "feature", title="break")
    assert pr.ci().green  # skipped, so green
    queue = hub.merge_queue(pr)
    assert queue.green and pr.merged  # queue credits the identical tree
    post = hub.run_ci("push", queue.sha, base="main")  # a default-branch push without a PR proof
    assert not post.green  # ...but the policy's trailers are not on the queue commit: main runs, and catches it


def test_merge_queue_credits_identical_tree(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    pr = hub.open_pr(dev, "feature", title="add a")
    pr.ci()
    run = hub.merge_queue(pr)
    assert set(run.skipped) == {"ci.yml:lint", "ci.yml:test"}
    assert pr.merged


def test_merge_queue_reruns_when_main_moved(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    attest_all(dev)
    pr = hub.open_pr(dev, "feature", title="add a")
    other = hub.clone("other")
    commit(other, "z.txt", "z", "main moves")
    other.git.run("push", "-q", "origin", "HEAD:main")
    run = hub.merge_queue(pr)
    assert run.skipped == ()
    assert "differs" in run.plan.jobs[0].reason
    assert pr.merged  # still merges: the jobs ran and were green
