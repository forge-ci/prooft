# Roadmap to MVP

MVP means a downstream repo can replace its ad-hoc attestation scripts with
`pip install prooft` plus three workflow lines, and the merge queue stops
rebuilding PRs that were already proven.

## Done (0.1)

- [x] `Attestation` record + `Ci-Attestation` trailer, byte-compatible with `ci_lint` (GATE-010)
- [x] `ci-attestations.yml` policy, zero-dependency restricted YAML
- [x] `verify` → per-gate states; `decide` → per-job skip/run
- [x] Head trust: forks, title tags, trust-surface edits run remotely (GATE-008 subset)
- [x] Merge-queue credit for a single-PR group with an identical tree
- [x] `prooft run LANE -- CMD` stamps HEAD; refuses dirty trees and self-modifying lanes
- [x] `FakeGitHub` simulator + scenario tests for every rule
- [x] ruff (black-style format, isort) + basedpyright (recommended) clean

## Next (0.2: dogfood + ci.yml compliance)

1. [x] **ci.yml compliance.** (PR #2: precheck 0 violations.) `ci.toml`, `ci-pre.yml`/`ci.yml` workflows, `ci-ok`
   aggregator; `./ci.py precheck` green. Blockers found so far, filed as
   ci.yml retrospective items (see `docs/RETROSPECTIVE.md`):
   - no pure-Python profile (`github-action` is the nearest stand-in);
   - GEN-004 hard-requires `pylint`, rejecting basedpyright as the checker;
   - GEN-005 forbids bash entry points (fine: `ci.py` replaced them).
2. [x] **Dogfood.** (PR #3: hosted run skipped `fast` + `test-linux-x64`.) prooft's own PRs carry prooft trailers; the hosted `verify`
   job runs `prooft plan --github-output` and the lint/test jobs skip.
3. [x] **`prooft hook install`**: pre-push hook that warns (shadow) or refuses
   (enforce) an unattested head.
4. [x] **`mode: shadow | enforce`** in the policy, strictest-of (pushed, remote, default branch).

## Then (0.3: merge-queue speed)

5. [x] **Input-keyed lane proofs** (obs-rust `lane_proof.py`, #699): key =
   sha256 over the lane's argv, runner image and `(path, blob)` of its
   declared inputs, read from the base. A queue group whose tree differs
   from the PR head still skips a lane whose *inputs* are unchanged.
6. **Ledger backends** for input-keyed proofs: Actions artifacts (name =
   `lane-proof-v1-<lane>-<key>`), and a local file ledger for the simulator.
7. **Multi-PR queue groups**: credit each lane whose inputs match a proven
   key, not just whole trees.

## Then (0.4: client builds)

8. **soldr adapter**: `prooft run rust -- soldr lint` maps soldr's JSON
   summary to per-target gates (`rust/<triple>/clippy`, `.../dylint`).
9. **`prooft emit --json`**: a standalone attestation bundle (for tools that
   cannot amend commits, e.g. a remote build farm).
10. **Isolation proof (GATE-009)**: accept a bosn run record
    (`workspace`, `sha`, `dirty`) as evidence the runner saw this tree.

## Later

- Signed attestations (sigstore / SSH signing) as an optional upgrade over
  the honesty contract.
- `ci-lint` delegating its attestation module to prooft.
- `prooft explain JOB`: why a job did or did not skip, in one paragraph.
