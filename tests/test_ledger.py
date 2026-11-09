"""Ledgers: hosted proofs for an input key (obs-studio's lane proofs, #699)."""

from __future__ import annotations

from pathlib import Path
import time

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


def artifact(name: str, *, expired: bool = False, fork: bool = False, created: str = "") -> dict[str, object]:
    stamp = created or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "name": name,
        "expired": expired,
        "created_at": stamp,
        "workflow_run": {"id": 7, "repository_id": 1, "head_repository_id": 2 if fork else 1, "head_branch": "b"},
    }


def test_github_artifacts_trust_rules() -> None:
    name = "lane-proof-v1-lint-k"
    listing: dict[str, object] = {"artifacts": [artifact(name, fork=True), artifact(name, expired=True)]}
    gh = GitHubArtifacts("o/r", name="lane-proof-v1-{lane}-{key}", fetch=lambda _url: listing)
    assert gh.lookup("lint", "k") is None
    listing["artifacts"] = [artifact(name), artifact("lane-proof-v1-lint-other")]
    proof = gh.lookup("lint", "k")
    assert proof is not None and proof.source == "run 7 on b"
    assert abs(proof.at - time.time()) < 120


def test_cli_plan_with_a_ledger_dir(keyed: Repo, tmp_path: Path) -> None:  # noqa: F811
    base = keyed.commit().sha
    commit(keyed, "src/a.py", "x = 1\n", "add a")
    DirLedger(tmp_path).record("lint", lint_key(keyed), source="main")
    assert main(["-C", str(keyed.root), "plan", "--base", base, "--ledger", str(tmp_path)]) == 0
