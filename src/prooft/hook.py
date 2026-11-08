"""The pre-push hook: catch an unattested head before it costs CI minutes.

    prooft hook install            write .git/hooks/pre-push (refuses to clobber a custom hook)
    prooft check-push              what the hook runs; reads git's pre-push lines on stdin

The policy's ``mode`` decides what an unattested head does:

- ``shadow`` (default): print one warning per head, push anyway. CI runs it.
- ``enforce``: refuse the push.

The strictest mode wins between the pushed commit's policy and the remote
tip's, so a branch cannot escape ``enforce`` by flipping its own policy to
``shadow`` in the commit it pushes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import stat

from prooft.git import GitError
from prooft.policy import Policy, PolicyError
from prooft.repo import Repo
from prooft.verify import State, verify

HEADER = "# installed by: prooft"
ZERO = "0" * 40

HOOK = f"""#!/bin/sh
{HEADER}
# Re-run `prooft hook install` to refresh; `--force` replaces a custom hook.
exec prooft -C "$(git rev-parse --show-toplevel)" check-push "$@"
"""


@dataclass(frozen=True)
class PushResult:
    ok: bool
    lines: tuple[str, ...]


def install(repo: Repo, *, force: bool = False) -> Path:
    hooks = Path(repo.git.run("rev-parse", "--git-path", "hooks").strip())
    if not hooks.is_absolute():
        hooks = repo.root / hooks
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-push"
    if path.exists() and HEADER not in path.read_text(encoding="utf-8", errors="replace"):
        if not force:
            raise RuntimeError(f"{path} is a custom hook; re-run with --force (it is saved as pre-push.pre-prooft)")
        path.replace(hooks / "pre-push.pre-prooft")
    path.write_text(HOOK, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _policy_at(repo: Repo, sha: str) -> Policy | None:
    try:
        return repo.policy(sha)
    except (PolicyError, GitError):
        return None


def check_push(repo: Repo, stdin_lines: Iterable[str]) -> PushResult:
    lines: list[str] = []
    ok = True
    for raw in stdin_lines:
        parts = raw.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == ZERO or not remote_ref.startswith("refs/heads/"):
            continue  # a deletion or a tag: nothing to attest
        policy = _policy_at(repo, local_sha)
        if policy is None:
            continue  # this commit has no prooft policy
        # a new branch has no remote tip: compare against the default branch instead
        remote = _policy_at(repo, remote_sha if remote_sha != ZERO else "refs/remotes/origin/HEAD")
        remote = remote or (_policy_at(repo, "refs/remotes/origin/main") if remote_sha == ZERO else None)
        enforce = policy.mode == "enforce" or (remote is not None and remote.mode == "enforce")
        verdict = verify(repo.commit(local_sha), policy)
        bad = [s for s in verdict.statuses if s.state is not State.VALID]
        if not bad:
            lines.append(f"prooft: {remote_ref} {local_sha[:12]} fully attested")
            continue
        summary = ", ".join(f"{s.gate}={s.state}" for s in bad)
        if enforce:
            ok = False
            lines.append(f"prooft: refusing {remote_ref} {local_sha[:12]} (mode=enforce): {summary}")
        else:
            lines.append(
                f"prooft: warning: {remote_ref} {local_sha[:12]} not fully attested, CI will run it: {summary}"
            )
    return PushResult(ok, tuple(lines))
