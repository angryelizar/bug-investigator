"""State of one Investigation, as it flows between graph nodes.

Only short stage results live here, never the model's tool chatter (ADR 0001),
and never secrets: the whole state goes to LangSmith traces.
"""

from typing import Literal, NotRequired, TypedDict

Verdict = Literal["confirmed", "not_a_bug", "insufficient_data", "blocked"]

Outcome = Literal[
    "fix_ready",  # Fix passed Verification; Approval and publication come in ticket 04
    "pr_created",
    "rejected",
    "gave_up",
    "not_a_bug",
    "insufficient_data",
    "blocked",
]


class BugReport(TypedDict):
    text: str
    source: Literal["chat", "issue"]
    issue_number: NotRequired[int]


class BuildResult(TypedDict):
    exit_code: int
    log_tail: str


class Baseline(BuildResult):
    """The build of the original code, before any change by the agent."""

    failed_tests: list[str]  # `TestClass.method`
    exceptions: list[str]  # simple names of exceptions the failed tests threw


class RelatedIssue(TypedDict):
    number: int
    title: str
    url: str


class InvestigationState(TypedDict):
    bug_report: BugReport
    workdir: NotRequired[str]
    branch: NotRequired[str]
    baseline: NotRequired[Baseline]
    related_issues: NotRequired[list[RelatedIssue]]  # open issues found for a red Baseline
    verdict: NotRequired[Verdict]
    analysis_summary: NotRequired[str]
    related_issue: NotRequired[int]  # the issue Analyst named as the cause of "blocked"
    plan: NotRequired[str]
    plan_approval: NotRequired[str]
    fix_diff: NotRequired[str]
    fix_explanation: NotRequired[str]
    reproduced: NotRequired[bool]
    build_result: NotRequired[BuildResult]
    critic_notes: NotRequired[str]
    attempts: NotRequired[int]
    fix_approval: NotRequired[str]
    outcome: NotRequired[Outcome]
    outcome_reason: NotRequired[str]
    pr_url: NotRequired[str]
