"""The pre-push hook: shadow warns, enforce refuses, and the strictest mode wins."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from prooft import hook
from prooft.repo import Repo
from tests.conftest import POLICY, commit

ENFORCE = POLICY.replace("version: 1\n", "version: 1\nmode: enforce\n")


def push_line(repo: Repo, remote_sha: str = hook.ZERO) -> list[str]:
    return [f"refs/heads/feature {repo.commit().sha} refs/heads/feature {remote_sha}\n"]


def test_shadow_warns_but_allows(dev: Repo) -> None:
    commit(dev, "a.txt", "1", "add a")
    result = hook.check_push(dev, push_line(dev))
    assert result.ok
    assert "warning" in result.lines[0]


def test_enforce_refuses_unattested(dev: Repo) -> None:
    commit(dev, "ci-attestations.yml", ENFORCE, "enforce")
    result = hook.check_push(dev, push_line(dev))
    assert not result.ok
    assert "refusing" in result.lines[0]


def test_enforce_allows_attested(dev: Repo) -> None:
    commit(dev, "ci-attestations.yml", ENFORCE, "enforce")
    dev.lane("lint").run(["true"])
    dev.lane("tests").run(["true"])
    assert hook.check_push(dev, push_line(dev)).ok


def test_cannot_downgrade_enforce_in_the_pushed_commit(dev: Repo) -> None:
    remote = commit(dev, "ci-attestations.yml", ENFORCE, "enforce")
    commit(dev, "ci-attestations.yml", POLICY, "sneak back to shadow")
    result = hook.check_push(dev, push_line(dev, remote_sha=remote))
    assert not result.ok


def test_deletions_and_tags_are_ignored(dev: Repo) -> None:
    lines = [
        f"(delete) {hook.ZERO} refs/heads/x {'a' * 40}\n",
        f"HEAD {dev.commit().sha} refs/tags/v1 {hook.ZERO}\n",
    ]
    assert hook.check_push(dev, lines) == hook.PushResult(True, ())


def test_install_refuses_custom_hook_without_force(dev: Repo) -> None:
    path = dev.root / ".git" / "hooks" / "pre-push"
    path.write_text("#!/bin/sh\necho mine\n")
    with pytest.raises(RuntimeError, match="custom hook"):
        hook.install(dev)
    hook.install(dev, force=True)
    assert hook.HEADER in path.read_text()
    assert (path.parent / "pre-push.pre-prooft").read_text() == "#!/bin/sh\necho mine\n"


def test_installed_hook_blocks_a_real_git_push(dev: Repo) -> None:
    commit(dev, "ci-attestations.yml", ENFORCE, "enforce")
    hook.install(dev)
    env = {**os.environ, "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"}
    refused = subprocess.run(
        ["git", "push", "-q", "origin", "HEAD:refs/heads/feature"],
        cwd=dev.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "refusing" in refused.stderr
    dev.lane("lint").run(["true"])
    dev.lane("tests").run(["true"])
    allowed = subprocess.run(
        ["git", "push", "-q", "origin", "HEAD:refs/heads/feature"],
        cwd=dev.root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert allowed.returncode == 0, allowed.stderr


def test_new_branch_inherits_enforce_from_the_default_branch(dev: Repo) -> None:
    commit(dev, "ci-attestations.yml", ENFORCE, "enforce on main")
    dev.git.run("push", "-q", "origin", "HEAD:main")
    dev.git.run("fetch", "-q", "origin")
    commit(dev, "ci-attestations.yml", POLICY, "new branch flips to shadow")
    assert not hook.check_push(dev, push_line(dev)).ok
