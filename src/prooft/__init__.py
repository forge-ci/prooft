"""prooft: proof marks for client builds.

Run a lane locally, stamp the commit with ``Ci-Attestation`` trailers, and let
CI and the merge queue skip the jobs those trailers prove. See README.md.
"""

from __future__ import annotations

from prooft.attestation import TRAILER_KEY, Attestation, Trailers
from prooft.ledger import DirLedger, GitHubArtifacts, Ledger, Proof
from prooft.policy import POLICY_FILE, Gate, Policy, PolicyError
from prooft.repo import LaneFailed, LaneRun, Repo, open  # noqa: A004 - fluent `prooft.open(...)`
from prooft.verify import JobDecision, Plan, State, Verdict, decide, verify

__version__ = "0.2.0"

__all__ = [
    "POLICY_FILE",
    "TRAILER_KEY",
    "Attestation",
    "DirLedger",
    "Gate",
    "GitHubArtifacts",
    "JobDecision",
    "LaneFailed",
    "LaneRun",
    "Ledger",
    "Plan",
    "Policy",
    "PolicyError",
    "Proof",
    "Repo",
    "State",
    "Trailers",
    "Verdict",
    "__version__",
    "decide",
    "open",
    "verify",
]
