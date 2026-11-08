# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnnecessaryTypeIgnoreComment=false
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from prooft.attestation import Attestation, Trailers, append, strip

TREE = "a" * 40
PARENTS = ("b" * 40,)


def make(gate: str = "rust/all/fmt", **kw: object) -> Attestation:
    return Attestation.new(gate, tree=TREE, parents=PARENTS, lane="rust", at=1_790_917_000, host="linux-x86_64", **kw)  # pyright: ignore[reportArgumentType]


def test_roundtrip_through_trailer() -> None:
    a = make(secs=12, key="k")
    parsed = Trailers.parse("subject\n\n" + a.to_trailer() + "\n")
    assert parsed.attestations == (a,)
    assert parsed.attestations[0].intact


def test_editing_the_json_breaks_the_stamp() -> None:
    a = make()
    tampered = a.to_trailer().replace('"secs":null', '"secs":1')
    (b,) = Trailers.parse(tampered).attestations
    assert not b.intact


def test_malformed_trailer_is_reported_not_raised() -> None:
    parsed = Trailers.parse('Ci-Attestation: {"v":2}\n')
    assert parsed.attestations == ()
    assert "unsupported version" in parsed.malformed[0]


def test_append_replaces_same_gate_and_keeps_others() -> None:
    first = append("subject\n", [make("rust/all/fmt"), make("rust/all/clippy")])
    second = append(first, [make("rust/all/fmt", secs=5)])
    gates = [(a.gate, a.secs) for a in Trailers.parse(second).attestations]
    assert gates == [("rust/all/clippy", None), ("rust/all/fmt", 5)]
    assert strip(second) == "subject\n"


def test_append_joins_an_existing_trailer_block() -> None:
    msg = append("subject\n\nbody\n\nSigned-off-by: x <x@y>\n", [make()])
    assert "Signed-off-by: x <x@y>\nCi-Attestation:" in msg


CI_LINT = Path(__file__).resolve().parents[2] / "ci.yml"


@pytest.mark.skipif(not (CI_LINT / "ci_lint" / "attestations.py").exists(), reason="no sibling ci.yml checkout")
def test_stamp_is_byte_compatible_with_ci_lint() -> None:
    sys.path.insert(0, str(CI_LINT))
    try:
        from ci_lint import attestations as ref  # pyright: ignore[reportMissingImports]  # noqa: PLC0415
    finally:
        sys.path.pop(0)
    theirs = ref.GateAttestation(
        "rust/all/fmt", TREE, PARENTS, "rust", "k", "run", 3, "linux-x86_64", 1_790_917_000
    ).stamped()  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    ours = make(secs=3, key="k")
    assert ours.to_trailer() == theirs.trailer()  # pyright: ignore[reportUnknownMemberType]
