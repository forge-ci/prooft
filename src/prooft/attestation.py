"""The attestation record: one gate passed on one exact tree.

An attestation travels as a commit-message trailer, never as a file:

    Ci-Attestation: {"at":1790917000,"gate":"rust/all/fmt",...,"stamp":"3b0e...","v":1}

The stamp is ``sha256(tree \\n parents \\n canonical-json-without-stamp)``
truncated to 32 hex digits. It is byte-compatible with ``ci_lint.attestations``
(zackees/ci.yml GATE-010), so a trailer written by either tool verifies in the
other. The stamp is tamper-evident, not a signature.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import time
from typing import Literal, TypeAlias

from prooft.platforms import host_label

TRAILER_KEY = "Ci-Attestation"
VERSION = 1
STAMP_HEX = 32

Via = Literal["run", "reused"]

_TRAILER = re.compile(rf"^{TRAILER_KEY}:\s*(?P<body>\{{.*\}})\s*$", re.MULTILINE)

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = "JsonScalar | list[JsonValue] | dict[str, JsonValue]"


@dataclass(frozen=True)
class Attestation:
    """Proof that ``gate`` passed on ``tree`` (with ``parents``)."""

    gate: str
    tree: str
    parents: tuple[str, ...]
    lane: str
    key: str = ""
    via: str = "run"
    secs: int | None = None
    host: str = ""
    at: int = 0
    stamp: str = ""

    @classmethod
    def new(
        cls,
        gate: str,
        *,
        tree: str,
        parents: tuple[str, ...] | list[str],
        lane: str,
        key: str = "",
        via: Via = "run",
        secs: int | None = None,
        at: int | None = None,
        host: str | None = None,
    ) -> Attestation:
        """Build and stamp a new attestation."""
        return cls(
            gate=gate,
            tree=tree,
            parents=tuple(parents),
            lane=lane,
            key=key,
            via=via,
            secs=secs,
            host=host_label() if host is None else host,
            at=int(time.time()) if at is None else at,
        ).sealed()

    def body(self) -> dict[str, JsonValue]:
        return {
            "v": VERSION,
            "gate": self.gate,
            "tree": self.tree,
            "parents": list(self.parents),
            "lane": self.lane,
            "key": self.key,
            "via": self.via,
            "secs": self.secs,
            "host": self.host,
            "at": self.at,
        }

    def compute_stamp(self) -> str:
        canonical = json.dumps(self.body(), sort_keys=True, separators=(",", ":"))
        payload = f"{self.tree}\n{','.join(self.parents)}\n{canonical}"
        return hashlib.sha256(payload.encode()).hexdigest()[:STAMP_HEX]

    def sealed(self) -> Attestation:
        """Return a copy with a freshly computed stamp."""
        return replace(self, stamp=self.compute_stamp())

    @property
    def intact(self) -> bool:
        """True when the stamp matches the record (nothing was edited)."""
        return bool(self.stamp) and self.stamp == self.compute_stamp()

    def to_json(self) -> str:
        body = self.body()
        body["stamp"] = self.stamp
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def to_trailer(self) -> str:
        return f"{TRAILER_KEY}: {self.to_json()}"

    @classmethod
    def from_json(cls, text: str) -> Attestation:
        raw: object = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("attestation is not a JSON object")
        return cls._from_raw(raw)  # pyright: ignore[reportUnknownArgumentType]

    @classmethod
    def _from_raw(cls, raw: dict[str, object]) -> Attestation:
        strings = ("gate", "tree", "lane", "key", "via", "host", "stamp")
        if raw.get("v") != VERSION or type(raw.get("v")) is not int:
            raise ValueError(f"unsupported version {raw.get('v')!r}")
        values: dict[str, str] = {}
        for name in strings:
            value = raw.get(name)
            if not isinstance(value, str):
                raise ValueError(f"field {name!r} must be a string")
            values[name] = value
        parents = raw.get("parents")
        if not isinstance(parents, list) or not all(isinstance(p, str) for p in parents):  # pyright: ignore[reportUnknownVariableType]
            raise ValueError("field 'parents' must be a list of strings")
        at = raw.get("at")
        if type(at) is not int:
            raise ValueError("field 'at' must be an integer")
        secs = raw.get("secs")
        if secs is not None and type(secs) is not int:
            raise ValueError("field 'secs' must be an integer or null")
        return cls(
            gate=values["gate"],
            tree=values["tree"],
            parents=tuple(str(p) for p in parents),  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
            lane=values["lane"],
            key=values["key"],
            via=values["via"],
            secs=secs,
            host=values["host"],
            at=at,
            stamp=values["stamp"],
        )


@dataclass(frozen=True)
class Trailers:
    """Every ``Ci-Attestation`` trailer in one commit message."""

    attestations: tuple[Attestation, ...]
    malformed: tuple[str, ...]

    @classmethod
    def parse(cls, message: str) -> Trailers:
        good: list[Attestation] = []
        bad: list[str] = []
        for match in _TRAILER.finditer(message):
            try:
                good.append(Attestation.from_json(match.group("body")))
            except (ValueError, json.JSONDecodeError) as exc:
                bad.append(f"malformed {TRAILER_KEY} trailer: {exc}")
        return cls(tuple(good), tuple(bad))


def strip(message: str) -> str:
    """Remove every attestation trailer, keeping the rest of the message."""
    kept = [line for line in message.rstrip("\n").splitlines() if not line.startswith(f"{TRAILER_KEY}:")]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept) + "\n"


def append(message: str, attestations: list[Attestation] | tuple[Attestation, ...]) -> str:
    """Replace the trailers of ``message`` for the given gates, keep the others.

    Trailers for gates not in ``attestations`` are preserved, so lanes can be
    attested one at a time.
    """
    replaced = {a.gate for a in attestations}
    existing = [a for a in Trailers.parse(message).attestations if a.gate not in replaced]
    body = strip(message).rstrip("\n")
    merged = sorted([*existing, *attestations], key=lambda a: a.gate)
    lines = [a.to_trailer() for a in merged]
    sep = "\n" if _ends_in_trailer_block(body) else "\n\n"
    return body + sep + "\n".join(lines) + "\n" if lines else body + "\n"


_TRAILER_LINE = re.compile(r"^[A-Za-z][A-Za-z0-9-]*: ")


def _ends_in_trailer_block(body: str) -> bool:
    paragraphs = body.split("\n\n")
    if len(paragraphs) < 2:
        return False
    return all(_TRAILER_LINE.match(line) for line in paragraphs[-1].splitlines())
