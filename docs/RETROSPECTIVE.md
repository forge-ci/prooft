# ci.yml retrospective (from making prooft compliant)

Findings to upstream to [forge-ci/ci.yml](https://github.com/forge-ci/ci.yml)
once prooft reaches MVP. Tracked in
[forge-ci/prooft#1](https://github.com/forge-ci/prooft/issues/1).
Linter pin at the time: `24362f3e60248ebe72ffd261e5585640228cbc5d`.

## Policy gaps

1. **No pure-Python profile.** `KNOWN_PROFILES` has Rust profiles, `cpp`
   and `github-action`. A pure-Python package (no crate, a PyPI wheel)
   has to borrow `github-action`, which drops `PKG-*` checks it should
   keep. Proposal: `python-lib` / `python-cli`, dropping `RUST`, `BIN`,
   `CPP`, keeping `PKG` with Python-only meanings.
2. **GEN-004 hard-requires `pylint`.** basedpyright (or pyright) in
   `recommended`/`strict` mode is a stronger checker than pylint for
   typed code. Proposal: accept `pylint | basedpyright | pyright` as the
   third tool. prooft carries an `[[exceptions]]` entry until then.
3. **GATE-002/003 name one producer.** The `verify` job contract is
   written around `ci-lint local-gate verify`. A repo that produces
   GATE-010 trailers with another tool (prooft) has no sanctioned shape.
   Proposal: specify the *trailer* and the *decision*, and let any
   producer that passes a conformance fixture set satisfy GATE-003.
4. **Verifier provenance is unstated.** The policy says the *definition*
   is read from the PR base. It must also say the *verifier code and the
   workflow script that calls it* come from the base: otherwise a PR edits
   `ci/verify.py` (not a `.github/` path, so not a trust surface) to
   approve itself. prooft's `verify` job checks out the base SHA and only
   fetches the head as data.

## Schema friction

5. **`[cache.pr].budget` is documented optional but loads as required**
   (`CT-002 missing required key 'cache.pr.budget'`) even with
   `mode = "none"`, where the docs say it derives to `0`.
6. **Required tables for repos with nothing to say.** A repo with no
   cache and no Rust still must write `[cache]`, `[cache.pr]`,
   `[local]`, `[publish]`, `allow.setup-soldr` and
   `allow.platform-selector`. Proposal: profile-scoped defaults.
7. **GEN-022 forces a cache janitor even with zero cache families
   saving** (`save-cache: false` everywhere, `pr.mode = "none"`).
   Proposal: exempt repos whose static cache budget is 0.
8. **GEN-024 misreads the `toJSON(needs)` aggregator.** `CI OK` consumes
   every upstream job's `result` via `toJSON(needs)`, but GEN-024 flags
   each edge as ordering-only. Proposal: treat a `toJSON(needs)`
   reference as consuming every `needs:` entry.

## What worked well

- GATE-010's trailer format was precise enough to implement byte-for-byte
  from the doc; a test checks prooft against `ci_lint.attestations`.
- The restricted-YAML rule made a zero-dependency parser practical.
- GEN-005 (no bash entry points) pushed the repo to one `ci.py`, which is
  also what the attestation lanes call.

## Found while dogfooding input keys

9. **Declared inputs drift from real inputs.** prooft's lint lane declared
   `src/**`, `tests/**`, ... but `ruff format --check .` also formats the
   Python blocks inside `*.md`. A README-only change would have kept a
   stale `python/all/fmt` proof valid. Caught by hand, not by a tool.
   Proposal for GATE-007/010: an input-key lane must be audited (strace or
   the tool's own file list, as obs-rust's `lane_proof.py` does) before its
   key may authorize a skip; until then prefer exclusion lists over
   inclusion globs.
