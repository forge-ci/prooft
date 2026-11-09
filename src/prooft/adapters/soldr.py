"""soldr adapter: one ``soldr lint`` run -> the Rust gates it proved.

``SOLDR_PORCELAIN=1 soldr lint rust`` prints one JSON summary line with a
``steps`` list (soldr#3958):

    {"exit_code": 0, "steps": [{"name": "fmt", "ok": true, ...},
                               {"name": "clippy", "ok": true, ...},
                               {"name": "clippy --target x86_64-pc-windows-msvc", "ok": true, ...},
                               {"name": "dylint", "ok": true, ...}], ...}

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
import re

_STEP = re.compile(r"^(?P<check>fmt|clippy|dylint)(?:\s+--target\s+(?P<target>[A-Za-z0-9_.-]+))?$")


@dataclass(frozen=True)
class Step:
    name: str
    ok: bool


def parse_report(output: str) -> tuple[Step, ...]:
    """Steps from the last JSON object line of soldr's porcelain stdout."""
    for raw_line in reversed(output.splitlines()):
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            report: object = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(report, dict) or "steps" not in report:
            continue
        raw_steps: object = report["steps"]  # pyright: ignore[reportUnknownVariableType]
        if not isinstance(raw_steps, list):
            return ()
        steps: list[Step] = []
        for raw in raw_steps:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(raw, dict) and isinstance(raw.get("name"), str):  # pyright: ignore[reportUnknownMemberType]
                steps.append(Step(str(raw["name"]), raw.get("ok") is True))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
        return tuple(steps)
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
