"""The few git plumbing calls prooft needs. Nothing else shells out to git."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess


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

    def run(self, *args: str, input_text: str | None = None) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), *args],
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
