"""emit / apply: carry proofs from a machine that cannot amend the commit (a build farm)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prooft import State
from prooft.cli import main
from prooft.repo import Repo
from prooft.sim.github import FakeGitHub
from tests.conftest import commit


def farm_of(hub: FakeGitHub, dev: Repo, name: str = "farm") -> Repo:
    """A second clone checked out at dev's commit, as a build farm would be."""
    dev.git.run("push", "-q", "origin", "HEAD:refs/heads/feature")
    farm = hub.clone(name)
    farm.git.run("checkout", "-q", "--detach", dev.commit().sha)
    return farm


def test_emit_lists_only_valid_attestations(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    dev.lane("lint").run(["true"])
    bundle = dev.emit()
    assert bundle["sha"] == dev.commit().sha
    entries = bundle["attestations"]
    assert isinstance(entries, list)
    assert [e["gate"] for e in entries] == ["python/all/lint"]  # pyright: ignore[reportUnknownVariableType]


def test_farm_bundle_applies_to_the_same_commit(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    farm = farm_of(hub, dev)
    farm.lane("lint").run(["true"])
    farm.lane("tests").run(["true"])
    gates = dev.apply(farm.emit())
    assert set(gates) == {"python/all/lint", "python/linux-x64/test"}
    assert dev.verify().ok("python/all/lint", "python/linux-x64/test")


def test_bundle_for_another_tree_is_rejected(hub: FakeGitHub, dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    farm = farm_of(hub, dev)
    farm.lane("lint").run(["true"])
    bundle = farm.emit()
    commit(dev, "b.txt", "2", "dev moved on")
    assert dev.apply(bundle) == ()
    assert dev.verify().state("python/all/lint") is State.MISSING


def test_bad_bundle_is_an_error(dev: Repo) -> None:
    with pytest.raises(ValueError, match="not a prooft bundle"):
        dev.apply({"v": 2})


def test_cli_emit_then_apply(hub: FakeGitHub, dev: Repo, tmp_path: Path) -> None:
    commit(dev, "a.txt", "1", "add a")
    farm = farm_of(hub, dev)
    farm.lane("lint").run(["true"])
    out = tmp_path / "bundle.json"
    assert main(["-C", str(farm.root), "emit", "-o", str(out)]) == 0
    assert json.loads(out.read_text())["v"] == 1
    assert main(["-C", str(dev.root), "apply", str(out)]) == 0
    assert dev.verify().ok("python/all/lint")
