"""The workflow's own scripts: CI OK's skip crediting and verify's base-only judging."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

from prooft.repo import Repo
from prooft.sim.github import FakeGitHub
from tests.conftest import commit

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "ci" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ci_ok = load("ci_ok")


def needs(fast: str, skip_fast: str = "false") -> dict[str, dict[str, object]]:
    return {
        "verify": {"result": "success", "outputs": {"skip_fast": skip_fast}},
        "fast": {"result": fast, "outputs": {}},
    }


def test_ci_ok_green_when_all_succeed() -> None:
    assert ci_ok.judge(needs("success")) == []


def test_ci_ok_credits_only_verified_skips() -> None:
    assert ci_ok.judge(needs("skipped", skip_fast="true")) == []
    assert ci_ok.judge(needs("skipped", skip_fast="false")) == ["fast: skipped"]


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_ci_ok_red_on_failure(result: str) -> None:
    assert ci_ok.judge(needs(result)) == [f"fast: {result}"]


def run_verify(base: Repo, env: dict[str, str], tmp: Path) -> dict[str, str]:
    """Run ci/verify.py from a copy placed in the *base* checkout, as the workflow does."""
    (base.root / "ci").mkdir(exist_ok=True)
    shutil.copy(ROOT / "ci" / "verify.py", base.root / "ci" / "verify.py")
    shutil.copytree(ROOT / "src", base.root / "src", dirs_exist_ok=True)
    out = tmp / "github_output"
    out.write_text("")
    full = {**os.environ, **env, "GITHUB_OUTPUT": str(out)}
    subprocess.run([sys.executable, str(base.root / "ci" / "verify.py")], env=full, check=True, capture_output=True)
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_verify_skips_attested_head_judged_by_base(hub: FakeGitHub, dev: Repo, tmp_path: Path) -> None:
    # the conftest policy uses ci.yml:lint / ci.yml:test; verify.py only reports its own jobs
    commit(dev, "a.txt", "1", "add a")
    dev.lane("lint").run(["true"])
    dev.lane("tests").run(["true"])
    dev.git.run("push", "-q", "origin", "HEAD:refs/heads/feature")
    base = hub.clone("base")
    env = {"EVENT_NAME": "pull_request", "BASE_SHA": base.commit().sha, "HEAD_SHA": dev.commit().sha}
    outputs = run_verify(base, env, tmp_path)
    assert outputs == {"skip_lint": "true", "skip_test": "true"}


def test_verify_push_event_skips_nothing(hub: FakeGitHub, tmp_path: Path) -> None:
    base = hub.clone("base")
    outputs = run_verify(base, {"EVENT_NAME": "push", "BASE_SHA": "", "HEAD_SHA": base.commit().sha}, tmp_path)
    assert outputs == {"skip_fast": "false", "skip_test-linux-x64": "false"}


verify_script = load("verify")


@pytest.mark.parametrize(
    ("ref", "number"),
    [
        (f"refs/heads/gh-readonly-queue/main/pr-12-{'a' * 40}", 12),
        ("refs/heads/gh-readonly-queue/main/pr-x-abc", None),
        ("refs/heads/main", None),
    ],
)
def test_queue_pr_number(ref: str, number: int | None) -> None:
    assert verify_script.queue_pr_number(ref) == number


def test_verify_merge_group_without_a_pr_skips_nothing(hub: FakeGitHub, tmp_path: Path) -> None:
    base = hub.clone("base")
    env = {"EVENT_NAME": "merge_group", "BASE_SHA": base.commit().sha, "HEAD_SHA": base.commit().sha, "QUEUE_REF": "x"}
    assert run_verify(base, env, tmp_path) == {"skip_fast": "false", "skip_test-linux-x64": "false"}
