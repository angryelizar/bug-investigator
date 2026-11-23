"""The Investigation graph: fixed order of stages, routing in code (ADR 0001)."""

from collections.abc import Callable
from typing import Protocol

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from bug_investigator import fix_pr, issue_comments
from bug_investigator.stages import AgentGaveUp, GitHubError, PublishError, Stages
from bug_investigator.state import Approval, Baseline, InvestigationState, RelatedIssue

MAX_RELATED_ISSUES = 10
# GitHub search allows at most five AND/OR/NOT operators in one query.
MAX_SEARCH_TERMS = 6


class Node(Protocol):
    def __call__(self, state: InvestigationState) -> dict: ...


def recursion_limit(max_attempts: int) -> int:
    """Steps of one graph run (until the next pause): five per Planner → Critic attempt
    plus the stages around the loop, with headroom."""
    return 5 * max_attempts + 12


def build_graph(stages: Stages, *, max_attempts: int = 3) -> CompiledStateGraph:
    """The graph pauses (`interrupt`) at Plan Approval, Fix Approval and the "Не баг"
    Approval; resume it with `Command(resume=Approval)` on the same `thread_id`.
    Pauses live in memory, as long as the process runs.
    """

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
        if analysis.verdict in ("insufficient_data", "blocked"):
            update |= {"outcome": analysis.verdict, "outcome_reason": analysis.summary}
        return update

    def not_a_bug_approval(state: InvestigationState) -> dict:
        approval = _ask({"kind": "not_a_bug", "summary": state.get("analysis_summary", "")})
        update: dict = {"not_a_bug_approval": approval}
        if approval["decision"] == "approve":
            update |= {"outcome": "not_a_bug", "outcome_reason": state.get("analysis_summary")}
        return update | _rejected(approval, "Verdict «Не баг»")

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

    def plan_approval(state: InvestigationState) -> dict:
        attempt = state.get("attempts", 1)
        approval = _ask(
            {
                "kind": "plan",
                "plan": state.get("plan", ""),
                "attempt": attempt,
                "max_attempts": max_attempts,
                # A second and later attempt comes only from the Critic loop: a human's
                # revision resets the count. Show why the previous Fix was turned down.
                "rejected_fix_notes": state.get("critic_notes", "") if attempt > 1 else "",
            }
        )
        update: dict = {"plan_approval": approval}
        if approval["decision"] == "revise":
            # A human's revision starts a fresh Planner → Critic budget.
            update["attempts"] = 0
        return update | _rejected(approval, "Plan")

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
        passed = critique.passed and build_ok and has_fix
        update: dict = {"critic_notes": notes, "verification_passed": passed}
        if not passed and state.get("attempts", 0) >= max_attempts:
            update |= {
                "outcome": "gave_up",
                "outcome_reason": f"Fix не прошёл Verification за {max_attempts} попытки: {notes}",
            }
        return update

    def fix_approval(state: InvestigationState) -> dict:
        approval = _ask(
            {
                "kind": "fix",
                "diff": state.get("fix_diff", ""),
                "explanation": state.get("fix_explanation", ""),
                "reproduced": state.get("reproduced", False),
                "build_result": state.get("build_result"),
                "critic_notes": state.get("critic_notes", ""),
            }
        )
        update: dict = {"fix_approval": approval}
        if approval["decision"] == "revise":
            # Critic's remarks on an accepted Fix are not a rejection for Planner.
            update |= {"attempts": 0, "critic_notes": ""}
        return update | _rejected(approval, "Fix")

    def publish(state: InvestigationState) -> dict:
        try:
            url = stages.publish(state, fix_pr.title(state), fix_pr.body(state))
        except PublishError as e:
            return {
                "outcome": "gave_up",
                "outcome_reason": f"Fix одобрен, но Fix PR не создан: {e}",
            }
        return {"outcome": "pr_created", "pr_url": url}

    def cleanup(state: InvestigationState) -> dict:
        stages.cleanup(state)
        return {}

    g = StateGraph(InvestigationState)
    for name, node in [
        ("prepare", prepare),
        ("baseline", baseline),
        ("analyst", analyst),
        ("planner", planner),
        ("coder", coder),
        ("executor", executor),
        ("critic", critic),
    ]:
        g.add_node(name, _ending_on_give_up(node))
    for name, node in [
        ("not_a_bug_approval", not_a_bug_approval),
        ("plan_approval", plan_approval),
        ("fix_approval", fix_approval),
        ("publish", publish),
        ("issue_comment", issue_comment),
        ("cleanup", cleanup),
    ]:
        g.add_node(name, node)

    g.add_edge(START, "prepare")
    for name, next_name in [
        ("prepare", "baseline"),
        ("baseline", "analyst"),
        ("planner", "plan_approval"),
        ("coder", "executor"),
        ("executor", "critic"),
    ]:
        g.add_conditional_edges(name, _finished_or(next_name), [next_name, "cleanup"])
    g.add_conditional_edges(
        "analyst",
        _after_analysis,
        ["planner", "not_a_bug_approval", "issue_comment", "cleanup"],
    )
    # "Доработать" goes back one stage with the human's comment.
    g.add_conditional_edges(
        "not_a_bug_approval",
        _after_approval("not_a_bug_approval", approved="issue_comment", revise="analyst"),
        ["issue_comment", "analyst", "cleanup"],
    )
    g.add_conditional_edges(
        "plan_approval",
        _after_approval("plan_approval", approved="coder", revise="planner"),
        ["coder", "planner", "cleanup"],
    )
    # Self-correction loop: a rejected Fix goes back to Planner with critic_notes.
    g.add_conditional_edges("critic", _after_critic, ["fix_approval", "planner", "cleanup"])
    g.add_conditional_edges(
        "fix_approval",
        _after_approval("fix_approval", approved="publish", revise="planner"),
        ["publish", "planner", "cleanup"],
    )
    g.add_edge("publish", "cleanup")
    g.add_edge("issue_comment", "cleanup")
    g.add_edge("cleanup", END)
    return g.compile(checkpointer=InMemorySaver())


def _ask(pause: dict) -> Approval:
    answer = interrupt(pause)
    decision = answer.get("decision") if isinstance(answer, dict) else None
    if decision not in ("approve", "reject", "revise"):
        raise ValueError(f"Approval: ожидался approve/reject/revise, получено {answer!r}")
    return Approval(decision=decision, comment=str(answer.get("comment") or "").strip())


def _rejected(approval: Approval, what: str) -> dict:
    if approval["decision"] != "reject":
        return {}
    reason = f"{what}: отклонено человеком"
    if approval["comment"]:
        reason += f" — {approval['comment']}"
    return {"outcome": "rejected", "outcome_reason": reason}


def _after_approval(
    field: str, *, approved: str, revise: str
) -> Callable[[InvestigationState], str]:
    def route(state: InvestigationState) -> str:
        decision = state.get(field, {}).get("decision")  # pyright: ignore[reportAttributeAccessIssue]
        return {"approve": approved, "revise": revise}.get(decision or "", "cleanup")

    return route


def _after_critic(state: InvestigationState) -> str:
    if "outcome" in state:
        return "cleanup"
    return "fix_approval" if state.get("verification_passed") else "planner"


_COMMENTS: dict[str, Callable[[InvestigationState], str]] = {
    "insufficient_data": issue_comments.insufficient_data,
    "blocked": issue_comments.blocked,
    "not_a_bug": issue_comments.not_a_bug,
}


def _after_analysis(state: InvestigationState) -> str:
    if state.get("outcome") == "gave_up":
        return "cleanup"
    verdict = state.get("verdict")
    if verdict == "not_a_bug":
        return "not_a_bug_approval"
    if verdict in _COMMENTS:
        return "issue_comment"
    return "planner"


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
    return lambda state: "cleanup" if "outcome" in state else next_node
