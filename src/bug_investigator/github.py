"""GitHub calls made by the graph, never by the model (ADR 0001). They go through the
`gh` CLI, which keeps the token in its own keyring or `GH_TOKEN`: nothing secret
passes through this process's state or traces.
"""

import json
import re
import subprocess
from typing import Any

from bug_investigator.stages import GitHubError
from bug_investigator.state import BugReport, RelatedIssue

_GITHUB_URL = re.compile(r"github\.com[:/](?P<slug>[\w.-]+/[\w.-]+?)(?:\.git)?/?$")
_ISSUE_URL = re.compile(
    r"^https?://github\.com/(?P<slug>[\w.-]+/[\w.-]+)/issues/(?P<number>\d+)/?(?:[#?].*)?$"
)
_TIMEOUT_S = 30
_MAX_QUERY_CHARS = 256


def repo_slug(repo_url: str) -> str | None:
    """`owner/name` of a GitHub repository URL; None for anything else."""
    m = _GITHUB_URL.search(repo_url.strip())
    return m["slug"] if m else None


def issue_number(link: str, repo_url: str) -> int:
    """Number of an issue given by its link; only issues of the Target Repository are
    accepted, since its code is the one the agent builds and changes (ADR 0002)."""
    m = _ISSUE_URL.match(link.strip())
    if not m:
        raise ValueError(f"Не ссылка на issue GitHub: {link}")
    slug = repo_slug(repo_url)
    if not slug or m["slug"].lower() != slug.lower():
        raise ValueError(f"Issue не из Target Repository ({slug or repo_url}): {link}")
    return int(m["number"])


class GitHub:
    def __init__(self, repo_url: str):
        self.slug = repo_slug(repo_url)

    def search_open_issues(self, terms: list[str], limit: int) -> list[RelatedIssue]:
        """Open issues (not PRs) mentioning any of the terms, best matches first."""
        base = f"repo:{self._slug()} is:issue is:open "
        quoted = [f'"{t}"' for t in terms]
        # GitHub rejects longer queries with 422: drop the last terms until it fits.
        while len(quoted) > 1 and len(base + " OR ".join(quoted)) > _MAX_QUERY_CHARS:
            quoted.pop()
        query = base + " OR ".join(quoted)
        found = self._api(
            "-X", "GET", "search/issues", "-f", f"q={query}", "-f", f"per_page={limit}"
        )
        return [
            RelatedIssue(number=i["number"], title=i["title"], url=i["html_url"])
            for i in found.get("items", [])[:limit]
        ]

    def recent_open_issues(self, limit: int) -> list[RelatedIssue]:
        """Latest open issues; the endpoint also returns PRs, which are dropped."""
        items = self._api(
            "-X",
            "GET",
            f"repos/{self._slug()}/issues",
            "-f",
            "state=open",
            "-f",
            "sort=created",
            "-f",
            "direction=desc",
            "-f",
            f"per_page={min(limit * 2, 100)}",
        )
        issues = [i for i in items if "pull_request" not in i]
        return [
            RelatedIssue(number=i["number"], title=i["title"], url=i["html_url"])
            for i in issues[:limit]
        ]

    def bug_report(self, link: str) -> BugReport:
        """Bug Report from an issue of the Target Repository: title and body only,
        comments are not read."""
        number = issue_number(link, f"https://github.com/{self._slug()}")
        issue = self._api("-X", "GET", f"repos/{self._slug()}/issues/{number}")
        if "pull_request" in issue:
            raise GitHubError(f"#{number} — это Pull Request, а не issue")
        title = (issue.get("title") or "").strip()
        body = (issue.get("body") or "").strip()
        return BugReport(
            text=f"{title}\n\n{body}".strip(), source="issue", issue_number=number, title=title
        )

    def create_pull_request(self, *, head: str, base: str, title: str, body: str) -> str:
        """Opens a PR (never merges it); returns its URL."""
        pr = self._api(
            "-X",
            "POST",
            f"repos/{self._slug()}/pulls",
            "--input",
            "-",
            stdin=json.dumps({"head": head, "base": base, "title": title, "body": body}),
        )
        return pr["html_url"]

    def comment(self, number: int, body: str) -> None:
        self._api(
            "-X",
            "POST",
            f"repos/{self._slug()}/issues/{number}/comments",
            "--input",
            "-",
            stdin=json.dumps({"body": body}),
        )

    def _slug(self) -> str:
        if not self.slug:
            raise GitHubError("Target Repository не на GitHub")
        return self.slug

    def _api(self, *args: str, stdin: str | None = None) -> Any:
        try:
            result = subprocess.run(
                ["gh", "api", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as e:
            raise GitHubError("gh CLI не найден") from e
        except subprocess.TimeoutExpired as e:
            raise GitHubError(f"GitHub не ответил за {_TIMEOUT_S} с") from e
        if result.returncode != 0:
            raise GitHubError(f"gh api: {result.stderr.strip() or result.stdout.strip()}")
        try:
            return json.loads(result.stdout or "{}")
        except json.JSONDecodeError as e:
            raise GitHubError(f"gh api вернул не JSON: {result.stdout[:200]}") from e
