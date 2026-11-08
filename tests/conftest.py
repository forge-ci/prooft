"""Shared fixtures: a simulated GitHub with prooft's own style of policy."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from prooft.repo import Repo
from prooft.sim.github import FakeGitHub

POLICY = """\
version: 1
lanes:
  lint: {max-age-hours: 24}
gates:
  python/all/lint: {lane: lint}
  python/linux-x64/test: {lane: tests}
jobs:
  ci.yml:lint: [python/all/lint]
  ci.yml:test: [python/linux-x64/test]
"""

# Remote jobs: each "fails" when the tree contains a file named BROKEN.
CHECK = [sys.executable, "-c", "import pathlib,sys; sys.exit(pathlib.Path('BROKEN').exists())"]


def commit(repo: Repo, path: str, text: str, message: str) -> str:
    (repo.root / path).parent.mkdir(parents=True, exist_ok=True)
    (repo.root / path).write_text(text, encoding="utf-8")
    repo.git.run("add", "-A")
    repo.git.run("commit", "-q", "-m", message)
    return repo.commit().sha


@pytest.fixture
def hub(tmp_path: Path) -> FakeGitHub:
    hub = FakeGitHub.create(tmp_path / "gh", jobs={"ci.yml:lint": CHECK, "ci.yml:test": CHECK})
    seed = hub.clone("seed")
    commit(seed, "ci-attestations.yml", POLICY, "init")
    seed.git.run("push", "-q", "origin", "HEAD:main")
    return hub


@pytest.fixture
def dev(hub: FakeGitHub) -> Repo:
    repo = hub.clone("dev")
    repo.git.run("checkout", "-q", "-b", "feature")
    return repo
