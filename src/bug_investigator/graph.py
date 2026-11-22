"""The Investigation graph: fixed order of stages, routing in code (ADR 0001)."""

from collections.abc import Callable
from itertools import pairwise
from typing import Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from bug_investigator import issue_comments
from bug_investigator.stages import AgentGaveUp, GitHubError, Stages
from bug_investigator.state import Baseline, InvestigationState, RelatedIssue

MAX_RELATED_ISSUES = 10
# GitHub search allows at most five AND/OR/NOT operators in one query.
MAX_SEARCH_TERMS = 6


class Node(Protocol):
    def __call__(self, state: InvestigationState) -> dict: ...


def build_graph(stages: Stages, *, max_attempts: int = 3) -> CompiledStateGraph:
    def prepare(state: InvestigationState) -> dict:
        ws = stages.prepare(state)
        return {"workdir": ws.workdir, "branch": ws.branch}

    def baseline(state: InvestigationState) -> dict:
        result = stages.baseline(state)
        update: dict = {"baseline": result}
        if result["exit_code"] != 0:
            # Found by the graph, not the model, so that Analyst can name the issue
            # behind a "blocked" Verdict without spending tool calls on GitHub.
            own = state["bug_report"].get("issue_number")
            update["related_issues"] = _related_issues(stages, result, exclude=own)
        return update

    def analyst(state: InvestigationState) -> dict:
        analysis = stages.analyst(state)
        baseline = state.get("baseline")
        if analysis.verdict == "blocked" and (not baseline or baseline["exit_code"] == 0):
            # Never tell the reporter the build is red when it is not.
            raise AgentGaveUp(
                f"Analyst вынес «Заблокировано» при зелёном Baseline: {analysis.summary}"
            )
        update: dict = {"verdict": analysis.verdict, "analysis_summary": analysis.summary}
        found = {i["number"] for i in state.get("related_issues", [])}
        if analysis.verdict == "blocked" and analysis.related_issue in found:
            # Only an issue the graph actually found: the model may invent a number.
            update["related_issue"] = analysis.related_issue
        if analysis.verdict != "confirmed":
            # The "Не баг" Approval and its comment come in ticket 04.
            update |= {"outcome": analysis.verdict, "outcome_reason": analysis.summary}
        return update

    def issue_comment(state: InvestigationState) -> dict:
        number = state["bug_report"].get("issue_number")
        if number is None:
            return {}
        body = _COMMENTS[state.get("verdict", "")](state)
        try:
            stages.comment_on_issue(number, body)
        except GitHubError as e:
            return {
                "outcome_reason": f"{state.get('outcome_reason', '')}\n\n"
                f"Комментарий в issue #{number} не оставлен: {e}".strip()
            }
        return {}

    def planner(state: InvestigationState) -> dict:
        return {"plan": stages.planner(state), "attempts": state.get("attempts", 0) + 1}

    def coder(state: InvestigationState) -> dict:
        fix = stages.coder(state)
        return {"fix_diff": fix.diff, "fix_explanation": fix.explanation}

    def executor(state: InvestigationState) -> dict:
        return {"build_result": stages.build(state)}

    def critic(state: InvestigationState) -> dict:
        critique = stages.critic(state)
        build = state.get("build_result") or {"exit_code": -1, "log_tail": "сборки не было"}
        build_ok = build["exit_code"] == 0
        notes = critique.notes
        has_fix = bool((state.get("fix_diff") or "").strip())
        if not has_fix:
            notes = f"{notes}\n\nFix пустой: Coder не изменил ни одного файла.".strip()
        if not build_ok:
            notes = (
                f"{notes}\n\nСборка с тестами упала (код {build['exit_code']}), "
                f"хвост лога:\n{build['log_tail']}"
            ).strip()
        if critique.passed and build_ok and has_fix:
            return {"critic_notes": notes, "outcome": "fix_ready"}
        if state.get("attempts", 0) >= max_attempts:
            return {
                "critic_notes": notes,
                "outcome": "gave_up",
                "outcome_reason": f"Fix не прошёл Verification за {max_attempts} попытки: {notes}",
            }
        return {"critic_notes": notes}

    pipeline: list[tuple[str, Node]] = [
        ("prepare", prepare),
        ("baseline", baseline),
        ("analyst", analyst),
        ("planner", planner),
        ("coder", coder),
        ("executor", executor),
        ("critic", critic),
    ]

    g = StateGraph(InvestigationState)
    for name, node in pipeline:
        g.add_node(name, _ending_on_give_up(node))
    g.add_node("issue_comment", issue_comment)
    g.add_edge(START, pipeline[0][0])
    for (name, _), (next_name, _) in pairwise(pipeline):
        if name != "analyst":
            g.add_conditional_edges(name, _finished_or(next_name), [next_name, END])
    g.add_conditional_edges("analyst", _after_analysis, ["planner", "issue_comment", END])
    g.add_edge("issue_comment", END)
    # Self-correction loop: a rejected Fix goes back to Planner with critic_notes.
    g.add_conditional_edges("critic", _finished_or("planner"), ["planner", END])
    return g.compile()


_COMMENTS: dict[str, Callable[[InvestigationState], str]] = {
    "insufficient_data": issue_comments.insufficient_data,
    "blocked": issue_comments.blocked,
}


def _after_analysis(state: InvestigationState) -> str:
    if state.get("verdict") in _COMMENTS:
        return "issue_comment"
    return _finished_or("planner")(state)


def _related_issues(
    stages: Stages, baseline: Baseline, *, exclude: int | None
) -> list[RelatedIssue]:
    """Issues found by the failed tests' names first, then the latest open ones: issues
    are written in business terms and rarely name a test, so Analyst matches by meaning.
    """
    found: list[RelatedIssue] = []
    if terms := _search_terms(baseline):
        try:
            found = [i for i in stages.search_issues(terms) if i["number"] != exclude]
            found = found[:MAX_RELATED_ISSUES]
        except GitHubError:
            pass
    if len(found) < MAX_RELATED_ISSUES:
        try:
            latest = stages.recent_open_issues(MAX_RELATED_ISSUES + len(found) + 1)
        except GitHubError:
            latest = []
        numbers = {i["number"] for i in found} | {exclude}
        found += [i for i in latest if i["number"] not in numbers]
    return found[:MAX_RELATED_ISSUES]


def _search_terms(baseline: Baseline) -> list[str]:
    tests = [t.rsplit(".", 1) for t in baseline["failed_tests"]]
    methods = [t[-1] for t in tests]
    classes = [t[0] for t in tests if len(t) == 2]
    terms = list(dict.fromkeys([*methods, *baseline["exceptions"], *classes]))
    return terms[:MAX_SEARCH_TERMS]


def _ending_on_give_up(node: Node) -> Node:
    def run(state: InvestigationState) -> dict:
        try:
            return node(state)
        except AgentGaveUp as e:
            return {"outcome": "gave_up", "outcome_reason": str(e)}

    return run


def _finished_or(next_node: str) -> Callable[[InvestigationState], str]:
    return lambda state: END if "outcome" in state else next_node
