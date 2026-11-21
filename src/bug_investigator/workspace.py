"""Working copy of the Target Repository: clone, branch, diff, reset. Git is called by
the graph, never by the model (ADR 0001).
"""

import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from bug_investigator.stages import AgentGaveUp, Workspace
from bug_investigator.state import BugReport


def prepare(repo: str, base_branch: str, bug_report: BugReport) -> Workspace:
    workdir = tempfile.mkdtemp(prefix="bug-investigator-")
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


def _short_id(bug_report: BugReport) -> str:
    if "issue_number" in bug_report:
        return str(bug_report["issue_number"])
    return hashlib.sha1(bug_report["text"].encode()).hexdigest()[:7]


def _git(cwd: str | Path | None, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout
