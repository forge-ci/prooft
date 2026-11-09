"""soldr adapter: one ``soldr lint`` run -> the Rust gates it proved.

``SOLDR_PORCELAIN=1 soldr lint rust`` prints one JSON summary line. On failure it
lists ``steps``; on success (schema 2) it drops them and the step names are read
from the ``log_path`` headers instead. A failing line looks like

    {"exit_code": 101, "steps": [{"name": "fmt", "ok": true, ...},
                               {"name": "clippy", "ok": true, ...},
                               {"name": "clippy --target x86_64-pc-windows-msvc", "ok": true, ...},
                               {"name": "dylint", "ok": false, ...}], ...}

Step names map to gate paths:

    fmt                      -> rust/all/fmt
    clippy                   -> rust/<host triple>/clippy
    clippy --target <T>      -> rust/<T>/clippy
    dylint                   -> rust/<host triple>/dylint
    dylint --target <T>      -> rust/<T>/dylint

Only steps with ``ok: true`` count, and only gates the policy declares are
stamped. Other steps (repo ``extra`` commands, unknown names) are ignored:
omission means CI runs them.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re

_STEP = re.compile(r"^(?P<check>fmt|clippy|dylint)(?:\s+--target\s+(?P<target>[A-Za-z0-9_.-]+))?$")


@dataclass(frozen=True)
class Step:
    name: str
    ok: bool


_LOG_STEP = re.compile(r"^# --- step (?P<name>.+?): .* ---$")


def _last_report(output: str) -> dict[str, object] | None:
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            report: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(report, dict):
            return report  # pyright: ignore[reportUnknownVariableType]
    return None


def _steps_from_log(path: str) -> tuple[Step, ...]:
    """Step names from the porcelain log's ``# --- step NAME: CMD ---`` headers (all passed)."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(Step(m.group("name"), True) for m in map(_LOG_STEP.match, text.splitlines()) if m)


def parse_report(output: str) -> tuple[Step, ...]:
    """Steps from the last JSON line of soldr's porcelain stdout.

    A failing run lists ``steps``. A successful schema-2 line drops them
    (soldr#4010) but keeps ``log_path``: every step in that log passed.
    """
    report = _last_report(output)
    if report is None:
        return ()
    raw_steps = report.get("steps")
    if isinstance(raw_steps, list):
        steps: list[Step] = []
        for raw in raw_steps:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(raw, dict) and isinstance(raw.get("name"), str):  # pyright: ignore[reportUnknownMemberType]
                steps.append(Step(str(raw["name"]), raw.get("ok") is True))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return tuple(steps)
    log_path = report.get("log_path")
    if report.get("exit_code") == 0 and report.get("status") == "ok" and isinstance(log_path, str):
        return _steps_from_log(log_path)
    return ()


def gate_for(step: str, host: str) -> str | None:
    match = _STEP.match(step.strip())
    if not match:
        return None
    check = match.group("check")
    if check == "fmt":
        return "rust/all/fmt"
    return f"rust/{match.group('target') or host}/{check}"


def proven_gates(output: str, host: str, declared: set[str] | frozenset[str]) -> tuple[str, ...]:
    """Declared gates whose soldr step passed, in report order."""
    out: list[str] = []
    for step in parse_report(output):
        gate = gate_for(step.name, host)
        if step.ok and gate is not None and gate in declared and gate not in out:
            out.append(gate)
    return tuple(out)
