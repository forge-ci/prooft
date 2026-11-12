"""The policy: which gates exist, which lane proves each, which jobs they cover.

Read from ``ci-attestations.yml`` (the GATE-010 definition file):

    version: 1
    lanes:                                  # optional (prooft extension)
      rust: {max-age-hours: 24}
    gates:
      rust/all/fmt: {lane: rust}
      rust/x86_64-unknown-linux-gnu/test: {lane: tests}
    jobs:
      ci.yml:fast: [rust/all/fmt, rust/x86_64-unknown-linux-gnu/test]

CI must read the policy from the PR *base*, never the head, so a PR cannot
loosen the rules it is judged by.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re

from prooft.miniyaml import MiniYamlError, parse

POLICY_FILE = "ci-attestations.yml"
DEFAULT_MAX_AGE_HOURS = 24.0
ECOSYSTEMS = ("rust", "python", "cpp", "general", "data", "swift")
FIDELITIES = ("native", "vm", "emulation")
MODES = ("shadow", "enforce")  # shadow: an unattested push warns; enforce: the pre-push hook refuses it

_GATE = re.compile(r"^(?P<eco>[a-z]+)/(?P<platform>[A-Za-z0-9_.-]+)/(?P<check>[a-z0-9][a-z0-9-]*)$")
_JOB = re.compile(r"^[^/:]+:[^:]+$")


class PolicyError(ValueError):
    """The policy file is invalid. ``problems`` lists every issue found."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems: list[str] = problems


@dataclass(frozen=True)
class Gate:
    path: str
    lane: str
    fidelity: str = "native"

    @property
    def ecosystem(self) -> str:
        return self.path.split("/", 1)[0]

    @property
    def check(self) -> str:
        return self.path.rsplit("/", 1)[1]


@dataclass(frozen=True)
class Policy:
    gates: dict[str, Gate]
    jobs: dict[str, tuple[str, ...]]
    max_age_hours: dict[str, float] = field(default_factory=dict[str, float])
    source: str = POLICY_FILE
    mode: str = "shadow"
    #: lane -> input globs. A lane with inputs gets an input key (see keys.py).
    inputs: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])

    # ── loading ──────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        path = Path(path)
        if path.is_dir():
            path = path / POLICY_FILE
        return cls.parse(path.read_text(encoding="utf-8"), source=str(path))

    @classmethod
    def parse(cls, text: str, *, source: str = POLICY_FILE) -> Policy:  # noqa: C901
        try:
            raw = parse(text)
        except MiniYamlError as exc:
            raise PolicyError([f"{source}: {exc}"]) from exc
        problems: list[str] = []
        if not isinstance(raw, dict):
            raise PolicyError([f"{source}: top level must be a mapping"])
        doc: dict[str, object] = raw  # pyright: ignore[reportUnknownVariableType]
        unknown = set(doc) - {"version", "mode", "lanes", "gates", "jobs"}
        if unknown:
            problems.append(f"unknown top-level keys: {', '.join(sorted(unknown))}")
        if doc.get("version") != 1:
            problems.append("version must be 1")

        mode = doc.get("mode", "shadow")
        if mode not in MODES:
            problems.append(f"mode must be one of {', '.join(MODES)}")
        mode = str(mode)
        max_age, inputs = _lanes(doc.get("lanes"), problems)

        gates: dict[str, Gate] = {}
        for gate_path, spec in _mapping(doc.get("gates"), "gates", problems).items():
            error = validate_gate_path(gate_path)
            if error:
                problems.append(error)
                continue
            spec_map = _mapping(spec, f"gates.{gate_path}", problems)
            lane = spec_map.get("lane")
            fidelity = spec_map.get("fidelity", "native")
            if not isinstance(lane, str) or not lane:
                problems.append(f"gate {gate_path}: 'lane' is required")
                continue
            if fidelity not in FIDELITIES:
                problems.append(f"gate {gate_path}: fidelity must be one of {', '.join(FIDELITIES)}")
                continue
            if fidelity == "emulation" and gate_path.endswith("/test"):
                problems.append(f"gate {gate_path}: an emulation lane cannot prove a 'test' check (GATE-011)")
                continue
            gates[gate_path] = Gate(gate_path, lane, str(fidelity))

        jobs: dict[str, tuple[str, ...]] = {}
        for job, listed in _mapping(doc.get("jobs"), "jobs", problems).items():
            if not _JOB.match(job):
                problems.append(f"job {job!r} must be '<workflow file>:<job id>'")
                continue
            if not isinstance(listed, list) or not listed:
                problems.append(f"job {job}: must list at least one gate")
                continue
            names = tuple(str(g) for g in listed)  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
            for name in names:
                if name not in gates:
                    problems.append(f"job {job}: gate {name} is not declared under 'gates'")
            jobs[job] = names
        if problems:
            raise PolicyError([f"{source}: {p}" for p in problems])
        return cls(gates, jobs, max_age, source, mode, inputs)

    # ── queries ──────────────────────────────────────────────────────────

    def lane_gates(self, lane: str) -> tuple[Gate, ...]:
        return tuple(g for g in self.gates.values() if g.lane == lane)

    @property
    def lanes(self) -> tuple[str, ...]:
        return tuple(sorted({g.lane for g in self.gates.values()}))

    def max_age_seconds(self, lane: str) -> float:
        return self.max_age_hours.get(lane, DEFAULT_MAX_AGE_HOURS) * 3600


def validate_gate_path(path: str) -> str | None:
    match = _GATE.match(path)
    if not match:
        return f"gate {path!r} must be '<ecosystem>/<platform>/<check>'"
    if match.group("eco") not in ECOSYSTEMS:
        return f"gate {path}: ecosystem must be one of {', '.join(ECOSYSTEMS)}"
    return None


def _mapping(value: object, where: str, problems: list[str]) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        problems.append(f"{where} must be a mapping")
        return {}
    return value  # pyright: ignore[reportUnknownVariableType]


def _lanes(value: object, problems: list[str]) -> tuple[dict[str, float], dict[str, tuple[str, ...]]]:
    max_age: dict[str, float] = {}
    inputs: dict[str, tuple[str, ...]] = {}
    for lane, spec in _mapping(value, "lanes", problems).items():
        spec_map = _mapping(spec, f"lanes.{lane}", problems)
        unknown = set(spec_map) - {"max-age-hours", "inputs"}
        if unknown:
            problems.append(f"lanes.{lane}: unknown keys {', '.join(sorted(unknown))}")
        hours = spec_map.get("max-age-hours", DEFAULT_MAX_AGE_HOURS)
        if isinstance(hours, bool) or not isinstance(hours, int | float) or hours <= 0:
            problems.append(f"lanes.{lane}.max-age-hours must be a positive number")
        else:
            max_age[lane] = float(hours)
        globs = spec_map.get("inputs")
        if globs is None:
            continue
        if not isinstance(globs, list) or not globs or not all(isinstance(g, str) and g for g in globs):  # pyright: ignore[reportUnknownVariableType]
            problems.append(f"lanes.{lane}.inputs must be a non-empty list of glob strings")
        else:
            inputs[lane] = tuple(str(g) for g in globs)  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    return max_age, inputs
