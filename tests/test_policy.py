from __future__ import annotations

import pytest

from prooft.miniyaml import MiniYamlError, parse
from prooft.policy import Policy, PolicyError

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
