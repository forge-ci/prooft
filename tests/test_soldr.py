"""soldr adapter: porcelain steps -> per-target Rust gates."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from prooft import State
from prooft.adapters.soldr import gate_for, parse_report, proven_gates
from prooft.repo import Repo
from tests.conftest import commit

HOST = "x86_64-unknown-linux-gnu"
WIN = "x86_64-pc-windows-msvc"

RUST_POLICY = f"""\
version: 1
gates:
  rust/all/fmt: {{lane: rust}}
  rust/{HOST}/clippy: {{lane: rust}}
  rust/{WIN}/clippy: {{lane: rust}}
  rust/{HOST}/dylint: {{lane: rust}}
jobs:
  ci.yml:fast: [rust/all/fmt, rust/{HOST}/clippy, rust/{HOST}/dylint]
  ci.yml:cross: [rust/{WIN}/clippy]
"""


def report(*steps: tuple[str, bool]) -> str:
    body = {"exit_code": 0 if all(ok for _, ok in steps) else 1, "steps": [{"name": n, "ok": ok} for n, ok in steps]}
    return "noise\n" + json.dumps(body) + "\n"


@pytest.mark.parametrize(
    ("step", "gate"),
    [
        ("fmt", "rust/all/fmt"),
        ("clippy", f"rust/{HOST}/clippy"),
        (f"clippy --target {WIN}", f"rust/{WIN}/clippy"),
        ("dylint", f"rust/{HOST}/dylint"),
        ("ruff check .", None),
    ],
)
def test_gate_for(step: str, gate: str | None) -> None:
    assert gate_for(step, HOST) == gate


def test_parse_report_takes_the_last_json_line() -> None:
    steps = parse_report(report(("fmt", True), ("clippy", False)))
    assert [(s.name, s.ok) for s in steps] == [("fmt", True), ("clippy", False)]
    assert parse_report("no json here\n") == ()


def test_only_passed_and_declared_gates_count() -> None:
    out = report(("fmt", True), ("clippy", True), (f"clippy --target {WIN}", False), ("ruff check .", True))
    declared = frozenset({"rust/all/fmt", f"rust/{HOST}/clippy", f"rust/{WIN}/clippy"})
    assert proven_gates(out, HOST, declared) == ("rust/all/fmt", f"rust/{HOST}/clippy")


def fake_soldr(tmp: Path, output: str, code: int = 0) -> list[str]:
    script = tmp / "fake_soldr.py"
    script.write_text(f"import sys\nsys.stdout.write({output!r})\nsys.exit({code})\n")
    return [sys.executable, str(script)]


def test_repo_soldr_stamps_per_target_gates(dev: Repo, tmp_path: Path) -> None:
    commit(dev, "ci-attestations.yml", RUST_POLICY, "rust policy")
    out = report(("fmt", True), ("clippy", True), (f"clippy --target {WIN}", True), ("dylint", True))
    result = dev.soldr(fake_soldr(tmp_path, out), host=HOST)
    assert result.ok
    assert len(result.gates) == 4
    plan_verdict = dev.verify()
    assert plan_verdict.ok("rust/all/fmt", f"rust/{HOST}/clippy", f"rust/{WIN}/clippy", f"rust/{HOST}/dylint")


def test_repo_soldr_failure_stamps_only_what_passed(dev: Repo, tmp_path: Path) -> None:
    commit(dev, "ci-attestations.yml", RUST_POLICY, "rust policy")
    out = report(("fmt", True), ("clippy", False))
    result = dev.soldr(fake_soldr(tmp_path, out, code=101), host=HOST)
    assert not result.ok
    assert result.gates == ("rust/all/fmt",)
    assert dev.verify().state(f"rust/{HOST}/clippy") is State.MISSING


def test_schema2_success_line_reads_steps_from_the_log(tmp_path: Path) -> None:
    """soldr#4010 drops `steps` from a success line; the log headers still name every step."""
    log = tmp_path / "lint.log"
    log.write_text(
        "noise\n# --- step fmt: soldr cargo fmt --all -- --check ---\nok\n"
        f"# --- step clippy --target {WIN}: soldr cargo clippy --target {WIN} ---\n"
    )
    line = json.dumps({"exit_code": 0, "status": "ok", "schema_version": 2, "log_path": str(log)})
    assert [(s.name, s.ok) for s in parse_report(line)] == [("fmt", True), (f"clippy --target {WIN}", True)]
    failed = json.dumps({"exit_code": 1, "status": "error", "log_path": str(log)})
    assert parse_report(failed) == ()
