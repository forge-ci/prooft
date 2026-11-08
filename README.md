# prooft

**Proof marks for client builds.** A gun barrel gets a proof mark once it
survives a test firing. prooft does the same for commits: run your lint and
tests locally, stamp the passing commit, and CI and the merge queue skip the
jobs that stamp already proves.

```console
$ prooft run lint -- soldr lint          # runs it; on success stamps HEAD
attested lint on 3f2a9c1e0b7d
$ git push                               # CI reads the stamp and skips ci.yml:lint
```

No server, no signing keys, no artifacts. The proof lives in the commit
message as one `Ci-Attestation:` trailer per gate, the format defined by
[ci.yml GATE-010](https://github.com/forge-ci/ci.yml/blob/main/docs/ci-attestations.md).
prooft's trailers are byte-compatible with `ci-lint`'s.

## The rules

Six rules. Everything else is detail.

1. **A gate is a check on a platform**: `<ecosystem>/<platform>/<check>`,
   e.g. `rust/x86_64-unknown-linux-gnu/clippy`. A **lane** is the local
   command that proves one or more gates.
2. **A proof is bound to the exact tree and parents.** Change one byte, or
   rebase, and it no longer counts (`wrong-tree`, `wrong-parents`). Edit the
   JSON and the stamp breaks (`bad-stamp`).
3. **Omission means "not run".** A gate without a trailer runs in CI. There
   is no way to say "skipped".
4. **A job skips only when every gate it covers is proven and fresh**
   (default 24 h per lane), and the head is trusted.
5. **The base decides.** CI reads the policy from the PR base. A PR that
   touches the policy, workflows or `ci.toml`, a fork, or a title tag
   (`[ci-full]`) runs everything remotely.
6. **Releases trust nothing.** A release recomputes every gate.

Rule 2 makes forgery visible rather than impossible: a trailer is an
honesty contract, not a signature. A forged pass can skip a PR job, but
the push to `main` runs the jobs on the merged commit, so a lie turns into
a visible red `main`, never a silent green.

### The merge queue

A queue commit is brand new, so its own trailers can never match. prooft
credits the PR's proof when the queue commit is a single commit on the base
**and its tree is identical to the attested PR head**: the bytes the queue
would test are the bytes that were proven. If `main` moved, the trees differ
and the queue runs the jobs.

## The policy: `ci-attestations.yml`

```yaml
version: 1
lanes:
  lint: {max-age-hours: 24}          # optional; 24 is the default
gates:
  python/all/fmt:        {lane: lint}
  python/all/types:      {lane: lint}
  python/linux-x64/test: {lane: tests}
jobs:                                 # <workflow file>:<job id> -> gates
  ci.yml:lint:           [python/all/fmt, python/all/types]
  ci.yml:test-linux-x64: [python/linux-x64/test]
```

It is a restricted YAML subset (no anchors, aliases, tags or block scalars)
parsed by the standard library alone, so every host reads it the same way.
`fidelity: emulation` (Wine, Darling) may prove `unit`, never `test`.

## Command line

```text
prooft run LANE -- CMD...      run CMD on a clean HEAD; on success stamp LANE's gates
prooft verify [REV]            every gate's state on REV
prooft plan --base REV         which jobs a PR run may skip  (--github-output, --json)
prooft queue --base REV --pr-head REV [--queue-head REV]
prooft lanes                   lanes and the gates each proves
prooft hook install [--force]  pre-push hook: warn (shadow) or refuse (enforce) an unattested head
```

`run` refuses a dirty tree, and refuses if the command changes tracked
files or moves HEAD. Re-running a lane replaces only that lane's trailers.

### Shadow or enforce

Top-level `mode:` in the policy decides what the pre-push hook does with an
unattested head. `shadow` (the default) warns and pushes: CI simply runs
the jobs. `enforce` refuses the push. The strictest mode between the pushed
commit and the remote tip (or the default branch, for a new branch) wins,
so a branch cannot flip itself back to `shadow` to escape.

## Python API

```python
import prooft

repo = prooft.open(".")
repo.lane("lint").run(["soldr", "lint"])  # stamp HEAD
repo.verify().ok("python/all/fmt")  # True
plan = repo.plan(base="origin/main", title="Add x")
plan.skipped  # ("ci.yml:lint",)
plan.outputs()  # {"skip_lint": "true", ...}
repo.queue(pr_head="pr", queue_head="HEAD", base="origin/main").skipped
```

Lower level: `prooft.Attestation`, `prooft.Trailers`, `prooft.Policy`,
`prooft.verify(commit, policy)`, `prooft.decide(policy, verdict)`.

## Simulating GitHub

`prooft.sim.github.FakeGitHub` is a bare `origin` plus pull requests, CI
runs and a merge queue, all offline. `tests/test_simulation.py` drives it
through every rule above: unattested, partial, stale, tampered, rebased,
fork, title tag, policy edit, forged pass, queue credit and queue rerun.

## Development

```console
./install      # uv sync + dev tools
./test         # pytest
./lint         # ruff format --check (black style), ruff check (isort, ...), basedpyright
./lint --fix
```

## Status

Alpha (0.1). prooft's own PRs skip `fast` and `test-linux-x64` when their trailers prove them. Next: input-keyed lane proofs for queue groups whose tree
differs from the PR head, and a `ci-lint` adapter. See
[docs/ROADMAP.md](docs/ROADMAP.md).

License: BSD-3-Clause.
