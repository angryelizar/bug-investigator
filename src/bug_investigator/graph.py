"""The Investigation graph: fixed order of stages, routing in code (ADR 0001)."""

from collections.abc import Callable
from itertools import pairwise
from typing import Protocol

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from bug_investigator.stages import AgentGaveUp, Stages
from bug_investigator.state import InvestigationState


class Node(Protocol):
    def __call__(self, state: InvestigationState) -> dict: ...


def build_graph(stages: Stages, *, max_attempts: int = 3) -> CompiledStateGraph:
    def prepare(state: InvestigationState) -> dict:
        ws = stages.prepare(state)
        return {"workdir": ws.workdir, "branch": ws.branch}

    def analyst(state: InvestigationState) -> dict:
        analysis = stages.analyst(state)
        update: dict = {"verdict": analysis.verdict, "analysis_summary": analysis.summary}
        if analysis.verdict != "confirmed":
            # Ticket 02 only plans confirmed bugs; issue comments and the
            # "Не баг" Approval come in tickets 03 and 04.
            update |= {"outcome": analysis.verdict, "outcome_reason": analysis.summary}
        return update

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
        ("analyst", analyst),
        ("planner", planner),
        ("coder", coder),
        ("executor", executor),
        ("critic", critic),
    ]

    g = StateGraph(InvestigationState)
    for name, node in pipeline:
        g.add_node(name, _ending_on_give_up(node))
    g.add_edge(START, pipeline[0][0])
    for (name, _), (next_name, _) in pairwise(pipeline):
        g.add_conditional_edges(name, _finished_or(next_name), [next_name, END])
    # Self-correction loop: a rejected Fix goes back to Planner with critic_notes.
    g.add_conditional_edges("critic", _finished_or("planner"), ["planner", END])
    return g.compile()


def _ending_on_give_up(node: Node) -> Node:
    def run(state: InvestigationState) -> dict:
        try:
            return node(state)
        except AgentGaveUp as e:
            return {"outcome": "gave_up", "outcome_reason": str(e)}

    return run


def _finished_or(next_node: str) -> Callable[[InvestigationState], str]:
    return lambda state: END if "outcome" in state else next_node
