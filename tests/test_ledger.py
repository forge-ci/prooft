"""Ledgers: hosted proofs for an input key (obs-studio's lane proofs, #699)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import time

import pytest

from prooft import State
from prooft.cli import main
from prooft.ledger import DirLedger, GitHubArtifacts
from prooft.repo import Repo
from prooft.sim.github import FakeGitHub
from tests.conftest import commit
from tests.test_keys import keyed  # noqa: F401  # pyright: ignore[reportUnusedImport] - fixture

LINT = "ci.yml:lint"


def lint_key(repo: Repo, rev: str = "HEAD") -> str:
    return repo.keys(repo.policy(rev), rev)["lint"]


def test_unattested_pr_is_credited_from_the_ledger(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    base = keyed.commit().sha
    commit(keyed, "src/a.py", "x = 1\n", "add a (nobody ran lint here)")
    ledger = DirLedger(tmp_path)
    assert keyed.plan(base, ledger=ledger).run == (LINT, "ci.yml:test")
    ledger.record("lint", lint_key(keyed), source="run 1 on main")
    plan = keyed.plan(base, ledger=ledger)
    assert plan.skipped == (LINT,)
    assert "ledger" in plan.verdict.statuses[0].detail or plan.verdict.state("python/all/lint") is State.VALID


def test_ledger_proof_for_other_inputs_does_not_count(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    base = keyed.commit().sha
    ledger = DirLedger(tmp_path)
    ledger.record("lint", lint_key(keyed), source="old inputs")
    commit(keyed, "src/a.py", "x = 1\n", "inputs changed")
    assert LINT in keyed.plan(base, ledger=ledger).run


def test_stale_ledger_proof_does_not_count(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    base = keyed.commit().sha
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    ledger = DirLedger(tmp_path)
    ledger.record("lint", lint_key(keyed), source="ancient", at=time.time() - 400 * 24 * 3600)
    assert LINT in keyed.plan(base, ledger=ledger).run


def test_untrusted_head_ignores_the_ledger(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    base = keyed.commit().sha
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    ledger = DirLedger(tmp_path)
    ledger.record("lint", lint_key(keyed), source="main")
    assert keyed.plan(base, title="[ci-full] x", ledger=ledger).skipped == ()


def test_queue_entry_credited_from_the_ledger(hub: FakeGitHub, keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    pr = hub.open_pr(keyed, "feature", title="add a")
    base = keyed.commit("origin/main").sha
    runner = hub.clone("q")
    runner.git.run("checkout", "-q", "--detach", base)
    runner.git.run("merge", "-q", "--squash", pr.head)
    runner.git.run("commit", "-q", "-m", "add a (#1)")
    q = runner.commit().sha
    ledger = DirLedger(tmp_path)
    ledger.record("lint", lint_key(runner, q), source="earlier queue run")
    assert runner.queue(pr.head, q, base, ledger=ledger).skipped == (LINT,)


NAME = "lane-proof-v1-lint-k"
NOW = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fake_api(runs: dict[int, dict[str, object]], *, expired: bool = False) -> Callable[[str], object]:
    """GitHub's artifacts listing plus one runs/<id> document per run."""
    arts = [{"name": NAME, "expired": expired, "created_at": NOW, "workflow_run": {"id": i}} for i in runs]

    def fetch(url: str) -> object:
        if "/artifacts?" in url:
            return {"artifacts": arts}
        return runs[int(url.rsplit("/", 1)[1])]

    return fetch


def run(*, path: str = ".github/workflows/ci.yml", event: str = "merge_group", repo: str = "o/r") -> dict[str, object]:
    return {"path": path, "event": event, "head_branch": "b", "head_repository": {"full_name": repo}}


@pytest.mark.parametrize(
    "bad",
    [
        run(path=".github/workflows/claim-bot.yaml"),  # any workflow could upload an artifact by that name
        run(event="issue_comment"),
        run(event="workflow_dispatch"),
        run(repo="evil/fork"),
    ],
)
def test_github_artifacts_reject_untrusted_runs(bad: dict[str, object]) -> None:
    gh = GitHubArtifacts("o/r", name="lane-proof-v1-{lane}-{key}", fetch=fake_api({1: bad}))
    assert gh.lookup("lint", "k") is None


def test_github_artifacts_accept_a_ci_run() -> None:
    gh = GitHubArtifacts("o/r", name="lane-proof-v1-{lane}-{key}", fetch=fake_api({1: run(repo="evil/fork"), 2: run()}))
    proof = gh.lookup("lint", "k")
    assert proof is not None and proof.source == "run 2 (merge_group, b)"
    assert abs(proof.at - time.time()) < 120
    expired = GitHubArtifacts("o/r", name="lane-proof-v1-{lane}-{key}", fetch=fake_api({2: run()}, expired=True))
    assert expired.lookup("lint", "k") is None


def test_cli_plan_with_a_ledger_dir(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    base = keyed.commit().sha
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    DirLedger(tmp_path).record("lint", lint_key(keyed), source="main")
    assert main(["-C", str(keyed.root), "plan", "--base", base, "--ledger", str(tmp_path)]) == 0


def test_external_lane_key_drives_the_ledger(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    """obs-studio keys lanes with its own lane_proof.py; prooft only judges trust and freshness."""
    base = keyed.commit().sha
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    DirLedger(tmp_path).record("lint", "obs-key", source="queue run")
    assert keyed.plan(base, ledger=DirLedger(tmp_path), keys={"lint": "obs-key"}).skipped == (LINT,)
    assert keyed.plan(base, ledger=DirLedger(tmp_path), keys={"lint": "other"}).skipped == ()
    args = ["-C", str(keyed.root), "plan", "--base", base, "--ledger", str(tmp_path), "--json"]
    assert main([*args, "--lane-key", "lint=obs-key"]) == 0
    with pytest.raises(SystemExit, match="LANE=KEY"):
        main([*args, "--lane-key", "nokey"])
