from __future__ import annotations

from pathlib import Path

import pytest

from prooft.miniyaml import MiniYamlError, parse
from prooft.policy import Policy, PolicyError
from prooft.repo import Repo
from tests.conftest import commit

GOOD = """\
version: 1   # comment
lanes:
  rust: {max-age-hours: 12}
gates:
  rust/all/fmt: {lane: rust}
  rust/x86_64-pc-windows-msvc/unit: {lane: wine, fidelity: emulation}
jobs:
  ci.yml:fast:
    - rust/all/fmt
"""


def test_parse_good_policy() -> None:
    policy = Policy.parse(GOOD)
    assert policy.lanes == ("rust", "wine")
    assert policy.jobs == {"ci.yml:fast": ("rust/all/fmt",)}
    assert policy.max_age_seconds("rust") == 12 * 3600
    assert policy.max_age_seconds("wine") == 24 * 3600


def test_load_data_and_swift_policy(tmp_path: Path) -> None:
    text = """\
version: 1
gates:
  data/all/flatpak-manifest: {lane: data}
  data/all/qt-xml: {lane: data}
  data/all/compatibility: {lane: data}
  data/all/services-schema: {lane: data}
  swift/all/fmt: {lane: swift}
jobs:
  ci.yml:data: [data/all/flatpak-manifest, data/all/qt-xml, data/all/compatibility, data/all/services-schema]
  ci.yml:swift: [swift/all/fmt]
"""
    (tmp_path / "ci-attestations.yml").write_text(text, encoding="utf-8")
    policy = Policy.load(tmp_path)
    assert policy.lanes == ("data", "swift")
    assert policy.jobs["ci.yml:data"] == tuple(gate.path for gate in policy.lane_gates("data"))
    assert policy.jobs["ci.yml:swift"] == ("swift/all/fmt",)
    for lane in policy.lanes:
        assert policy.max_age_seconds(lane) == 24 * 3600
        assert all(gate.ecosystem == lane and gate.fidelity == "native" for gate in policy.lane_gates(lane))


@pytest.mark.parametrize("gate", ["data/all/qt-xml", "swift/all/fmt", "cpp/all/fmt", "general/all/lint"])
def test_ecosystem_lane_attests_and_skips_job(dev: Repo, gate: str) -> None:
    text = f"version: 1\ngates:\n  {gate}: {{lane: checks}}\njobs:\n  ci.yml:checks: [{gate}]\n"
    base = commit(dev, "ci-attestations.yml", text, "declare checks")
    commit(dev, "README.txt", "check this commit\n", "change inputs")
    dev.lane("checks").run(["git", "--version"])
    assert dev.verify().ok(gate)
    assert dev.plan(base).skipped == ("ci.yml:checks",)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ("version: 2\n", "version must be 1"),
        ("version: 1\ngates:\n  bad: {lane: x}\n", "must be '<ecosystem>/<platform>/<check>'"),
        ("version: 1\ngates:\n  go/all/fmt: {lane: x}\n", "ecosystem must be one of"),
        ("version: 1\ngates:\n  rust/all/fmt: {}\n", "'lane' is required"),
        ("version: 1\ngates:\n  rust/all/test: {lane: w, fidelity: emulation}\n", "cannot prove a 'test'"),
        (
            "version: 1\ngates:\n  rust/all/fmt: {lane: r}\njobs:\n  fast: [rust/all/fmt]\n",
            "'<workflow file>:<job id>'",
        ),
        ("version: 1\njobs:\n  ci.yml:fast: [rust/all/fmt]\n", "is not declared"),
        ("version: 1\nextra: 1\n", "unknown top-level keys"),
    ],
)
def test_policy_errors(text: str, needle: str) -> None:
    with pytest.raises(PolicyError, match=needle.replace("[", r"\[").replace("(", r"\(")):
        Policy.parse(text)


@pytest.mark.parametrize("text", ["a: &x 1\n", "a: *x\n", "a: !!str 1\n", "a: |\n  x\n", "---\na: 1\n", "<<: {a: 1}\n"])
def test_miniyaml_rejects_unsafe_yaml(text: str) -> None:
    with pytest.raises(MiniYamlError):
        parse(text)


def test_miniyaml_scalars_and_quotes() -> None:
    assert parse("a: 'x # y'\nb: true\nc: 1.5\nd: ~\ne: [1, two]\n") == {
        "a": "x # y",
        "b": True,
        "c": 1.5,
        "d": None,
        "e": [1, "two"],
    }
