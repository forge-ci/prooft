"""The few git plumbing calls prooft needs. Nothing else shells out to git."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import tempfile


class GitError(RuntimeError):
    """A git command failed."""


@dataclass(frozen=True)
class Commit:
    sha: str
    tree: str
    parents: tuple[str, ...]
    message: str


@dataclass(frozen=True)
class Git:
    root: Path

    def run(self, *args: str, input_text: str | None = None, env: dict[str, str] | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout

    def commit(self, rev: str = "HEAD") -> Commit:
        sha = self.run("rev-parse", "--verify", f"{rev}^{{commit}}").strip()
        tree = self.run("rev-parse", f"{sha}^{{tree}}").strip()
        parents = tuple(self.run("log", "-1", "--format=%P", sha).split())
        message = self.run("log", "-1", "--format=%B", sha)
        return Commit(sha, tree, parents, message)

    def show(self, rev: str, path: str) -> str | None:
        """File contents at ``rev``, or None when absent."""
        try:
            return self.run("show", f"{rev}:{path}")
        except GitError:
            return None

    def is_clean(self) -> bool:
        return not self.run("status", "--porcelain", "--untracked-files=no").strip()

    def amend_message(self, message: str) -> str:
        """Rewrite HEAD's message only. The tree, and so every stamp, is unchanged."""
        self.run("commit", "--amend", "--no-verify", "--allow-empty", "-F", "-", input_text=message)
        return self.run("rev-parse", "HEAD").strip()

    def ls_tree(self, rev: str) -> dict[str, str]:
        """``{path: blob sha}`` for every file at ``rev``."""
        out: dict[str, str] = {}
        for line in self.run("ls-tree", "-r", "-z", rev).split("\0"):
            if not line:
                continue
            meta, path = line.split("\t", 1)
            out[path] = meta.split()[2]
        return out

    def worktree_tree(self) -> str:
        """The tree the working directory would commit as (``git add -A``), without touching the index.

        Built in a throwaway index so a dirty tree has a stable identity: the
        runner's lint record is keyed on it, and a build check compares it.
        """
        with tempfile.TemporaryDirectory(prefix="prooft-index-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": os.path.join(tmp, "index")}
            if self._has_head():
                self.run("read-tree", "HEAD", env=env)
            self.run("add", "-A", env=env)
            return self.run("write-tree", env=env).strip()

    def _has_head(self) -> bool:
        try:
            self.run("rev-parse", "--verify", "-q", "HEAD^{commit}")
        except GitError:
            return False
        return True

    def git_dir(self) -> Path:
        return Path(self.run("rev-parse", "--absolute-git-dir").strip())

    def branch(self) -> str | None:
        """The checked-out branch, or None when HEAD is detached."""
        name = self.run("rev-parse", "--abbrev-ref", "HEAD").strip()
        return None if name == "HEAD" else name
