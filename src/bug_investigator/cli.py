"""Run one Investigation from the terminal, without pauses:

uv run bug-investigator "текст Bug Report"
uv run bug-investigator --file bug.txt
cat bug.txt | uv run bug-investigator
"""

import argparse
import os
import sys

from dotenv import load_dotenv
from langchain_core.tracers.langchain import wait_for_all_tracers
from pydantic import ValidationError

from bug_investigator.config import Settings
from bug_investigator.graph import build_graph
from bug_investigator.model_stages import ModelStages, make_chat_model
from bug_investigator.state import BugReport, InvestigationState

_STATUS = {
    "prepare": "Подготовка: рабочая копия {workdir}, ветка {branch}",
    "baseline": "Baseline: {baseline}",
    "analyst": "Analyst: Verdict {verdict}",
    "planner": "Planner: Plan готов (попытка {attempts})",
    "coder": "Coder: Fix готов",
    "executor": "Executor: сборка с тестами — код {build_exit}",
    "critic": "Critic: {critic}",
    "issue_comment": "Комментарий в issue: {comment}",
}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    load_dotenv()  # API keys and LANGSMITH_* go to os.environ, never into Settings
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as e:
        print(f"Ошибка конфигурации (переменные BI_*):\n{e}", file=sys.stderr)
        return 2
    if missing := settings.missing_secrets():
        print(f"Не заданы переменные окружения: {', '.join(missing)}", file=sys.stderr)
        return 2
    if os.environ.get("LANGSMITH_TRACING", "").lower() != "true" or not os.environ.get(
        "LANGSMITH_API_KEY"
    ):
        print(
            "Внимание: нужны LANGSMITH_TRACING=true и LANGSMITH_API_KEY, иначе трейса не будет.",
            file=sys.stderr,
        )

    text = _read_bug_report(args)
    if not text:
        print("Пустой Bug Report.", file=sys.stderr)
        return 2

    graph = build_graph(
        ModelStages(settings, make_chat_model(settings)), max_attempts=settings.max_attempts
    )
    initial: InvestigationState = {"bug_report": BugReport(text=text, source="chat")}
    final: dict = dict(initial)
    try:
        for update in graph.stream(
            initial,
            {
                "run_name": "Investigation",
                "recursion_limit": 4 * settings.max_attempts + 10,
                "metadata": {"provider": settings.provider, "model": settings.model},
            },
            stream_mode="updates",
        ):
            for node, delta in update.items():
                final |= delta or {}
                print(f"→ {_status(node, final)}", flush=True)
    finally:
        wait_for_all_tracers()

    _print_result(final)
    return 0 if final.get("outcome") == "fix_ready" else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bug-investigator", description=__doc__)
    parser.add_argument("bug_report", nargs="?", help="текст Bug Report")
    parser.add_argument("--file", help="файл с текстом Bug Report")
    return parser.parse_args(argv)


def _read_bug_report(args: argparse.Namespace) -> str:
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return f.read().strip()
    if args.bug_report:
        return args.bug_report.strip()
    return sys.stdin.read().strip()


def _status(node: str, state: dict) -> str:
    build = state.get("build_result") or {}
    critic = {"fix_ready": "Fix принят", "gave_up": "агент сдался"}.get(
        state.get("outcome", ""), "Fix отклонён"
    )
    line = _STATUS.get(node, node).format(
        workdir=state.get("workdir"),
        branch=state.get("branch"),
        verdict=state.get("verdict"),
        attempts=state.get("attempts"),
        build_exit=build.get("exit_code"),
        critic=critic,
        baseline=_baseline(state),
        comment=_comment(state),
    )
    if state.get("outcome") == "gave_up":
        line = f"{node}: агент сдался"
    return line


def _comment(state: dict) -> str:
    if "issue_number" not in state.get("bug_report", {}):
        return "нет — Bug Report не из issue"
    failed = f"Комментарий в issue #{state['bug_report']['issue_number']} не оставлен"
    return "не оставлен" if failed in state.get("outcome_reason", "") else "оставлен"


def _baseline(state: dict) -> str:
    baseline = state.get("baseline") or {}
    if baseline.get("exit_code") == 0:
        return "зелёный"
    failed = ", ".join(baseline.get("failed_tests") or []) or "имена тестов не найдены"
    issues = len(state.get("related_issues") or [])
    return f"красный ({failed}); открытых issue для Analyst: {issues}"


def _print_result(state: dict) -> None:
    print(f"\nИтог: {state.get('outcome')}")
    if state.get("outcome_reason"):
        print(state["outcome_reason"])
    if state.get("related_issue"):
        print(f"Связанное issue: #{state['related_issue']}")
    if state.get("analysis_summary"):
        print(f"\nAnalysis:\n{state['analysis_summary']}")
    if state.get("plan"):
        print(f"\nPlan:\n{state['plan']}")
    if state.get("fix_explanation"):
        print(f"\nFix:\n{state['fix_explanation']}")
    if state.get("fix_diff"):
        print(f"\n{state['fix_diff']}")
    if state.get("workdir"):
        print(f"Рабочая копия: {state['workdir']} (ветка {state.get('branch')})")


if __name__ == "__main__":
    sys.exit(main())
