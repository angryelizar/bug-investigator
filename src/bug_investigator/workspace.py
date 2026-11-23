"""Working copy of the Target Repository: clone, branch, diff, reset. Git is called by
the graph, never by the model (ADR 0001).
"""

import hashlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from bug_investigator.stages import AgentGaveUp, Workspace
from bug_investigator.state import BugReport

_PREFIX = "bug-investigator-"
# Multi-module projects keep tests in `<module>/src/test`; `**/` also matches the root.
_TESTS = ":(glob)**/src/test/**"
_AUTHOR = "Bug Investigator"
_AUTHOR_EMAIL = "bug-investigator@users.noreply.github.com"


def prepare(repo: str, base_branch: str, bug_report: BugReport) -> Workspace:
    workdir = tempfile.mkdtemp(prefix=_PREFIX)
    branch = f"bug-investigator/{_short_id(bug_report)}"
    try:
        _git(None, "clone", "--depth", "1", "--branch", base_branch, repo, workdir)
        _git(workdir, "checkout", "-b", branch)
    except (RuntimeError, OSError) as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise AgentGaveUp(f"Не удалось подготовить рабочую копию: {e}") from e
    return Workspace(workdir=workdir, branch=branch)


def diff(workdir: str) -> str:
    """Everything changed against HEAD, new files included."""
    _git(workdir, "add", "--all")
    return _git(workdir, "diff", "--cached", "--no-color", "HEAD")


def reset(workdir: str) -> None:
    """Back to the cloned commit, so each Coder attempt starts from clean code."""
    _git(workdir, "reset", "--hard", "--quiet", "HEAD")
    _git(workdir, "clean", "-fd", "--quiet")


@contextmanager
def reproduction_copy(workdir: str) -> Iterator[str | None]:
    """A temporary copy of the original code plus the new and changed files under
    `src/test/**` from the Fix, without the test files it deletes (build A); None if
    the Fix adds or changes no tests.
    The working copy with the Fix is not touched; the copy is removed on exit.
    """
    _git(workdir, "add", "--all")
    # Without rename detection a moved test is a deletion plus an addition, so the old
    # path does not stay in the copy next to the new one.
    listed = _git(
        workdir, "diff", "--cached", "--name-status", "--no-renames", "-z", "HEAD", "--", _TESTS
    ).split("\0")
    changes = list(zip(listed[0::2], listed[1::2], strict=False))
    written = [path for status, path in changes if status in ("A", "M")]
    deleted = [path for status, path in changes if status == "D"]
    if not written:
        yield None
        return
    copy = tempfile.mkdtemp(prefix=f"{_PREFIX}reproduction-")
    try:
        _git(workdir, "worktree", "add", "--quiet", "--detach", copy, "HEAD")
        for path in written:
            target = Path(copy, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(workdir, path), target)
        for path in deleted:
            Path(copy, path).unlink(missing_ok=True)
        yield copy
    finally:
        shutil.rmtree(copy, ignore_errors=True)
        try:
            _git(workdir, "worktree", "prune")
        except (RuntimeError, OSError):
            pass  # a stale worktree entry in a working copy that is removed anyway


def commit_and_push(workdir: str, branch: str, message: str) -> str:
    """Commit the Fix and push its branch; returns the pushed branch name, suffixed
    (`-2`, `-3`...) if the name is already taken in the remote."""
    _git(workdir, "add", "--all")
    # An agent's commit must not hang on a signing agent or fail on a partial identity.
    options = ["-c", "commit.gpgsign=false"]
    if not (_config(workdir, "user.email") and _config(workdir, "user.name")):
        options += ["-c", f"user.name={_AUTHOR}", "-c", f"user.email={_AUTHOR_EMAIL}"]
    _git(workdir, *options, "commit", "--quiet", "-m", message)
    name = _free_branch_name(workdir, branch)
    if name != branch:
        _git(workdir, "branch", "-m", branch, name)
    _git(workdir, "push", "--quiet", "origin", f"HEAD:refs/heads/{name}")
    return name


def delete_remote_branch(workdir: str, branch: str) -> None:
    """Best effort: a branch without its PR should not push the next run to a suffix."""
    try:
        _git(workdir, "push", "--quiet", "origin", "--delete", branch)
    except (RuntimeError, OSError):
        pass


def remove(workdir: str) -> None:
    """Delete the working copy; only the temporary folders made by `prepare`."""
    path = Path(workdir)
    if path.parent == Path(tempfile.gettempdir()) and path.name.startswith(_PREFIX):
        shutil.rmtree(path, ignore_errors=True)


def _free_branch_name(workdir: str, branch: str) -> str:
    taken = {
        line.split("refs/heads/", 1)[1]
        for line in _git(workdir, "ls-remote", "--heads", "origin").splitlines()
        if "refs/heads/" in line
    }
    name, n = branch, 1
    while name in taken:
        n += 1
        name = f"{branch}-{n}"
    return name


def _config(workdir: str, key: str) -> str:
    result = subprocess.run(
        ["git", "config", key], cwd=workdir, capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _short_id(bug_report: BugReport) -> str:
    if "issue_number" in bug_report:
        return str(bug_report["issue_number"])
    return hashlib.sha1(bug_report["text"].encode()).hexdigest()[:7]


def _git(cwd: str | Path | None, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout
