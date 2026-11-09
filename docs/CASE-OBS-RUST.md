# Case: obs-rust/obs-studio queue stall (2026-11-09)

## What happened

From about 07:35Z, obs-rust/obs-studio had 100–160 Actions runs queued and
0–2 in progress. Nothing merged between 13:27Z and the end of the day; the
merge-queue head sat in `AWAITING_CHECKS`. By 15:15Z the backlog had drained.

Causes, from the pool coordinators' board reports:

1. **Runner starvation, not attestation.** Concurrency was capped at about 1
   job (an org Actions limit). Even trivial `claim` jobs waited.
2. **Claim-bot flood.** `claim-bot.yaml` starts a runner for *every* issue
   or PR comment and filters afterwards. At 10:00Z, 55% of queued runs were
   claim-bot runs triggered by worker progress comments.
3. **Multi-PR groups re-ran everything.** The ruleset groups up to 5 PRs
   (`ALLGREEN`). prooft 0.1.0 credited only a group's *first* entry; every
   later entry sits on an earlier entry's squash commit, so it ran every job
   even when its PR head was attested.

## Was holding the queue correct?

Yes. Nothing merged without checks, and nothing was skipped without proof.
The queue failed closed. The cost came from spending scarce runners on work
that was either useless (claim-bot no-ops) or already proven (later group
entries).

## What prooft now does

- `Repo.queue` credits **every entry of a linear merge group**. A later entry
  inherits a lane's proof only when the lane's declared inputs at the queue
  commit hash to the attested key, *and* no earlier entry in the group
  touched a trust surface. Tree-identity credit stays first-entry only.
- `FakeGitHub.merge_group([...])` simulates ALLGREEN groups.

## Recommendations for obs-studio (need maintainer sign-off, workflow edits)

- Give `claim-bot.yaml` a job-level `if:` that matches the claim command in
  `github.event.comment.body` before a runner is allocated.
- Declare `inputs:` for every lane in `ci-attestations.yml`. Without inputs, a
  lane is tree-bound and no grouped entry beyond the first can reuse it.
- Pools: post at most one comment per issue (each comment costs a runner).
