"""Run one Investigation from the terminal; pauses for Approval are answered here:

uv run bug-investigator "текст Bug Report"
uv run bug-investigator --issue https://github.com/<owner>/<repo>/issues/12
uv run bug-investigator --file bug.txt
cat bug.txt | uv run bug-investigator
"""

import argparse
import os
import sys
import uuid
from collections.abc import Callable

from dotenv import load_dotenv
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers.langchain import wait_for_all_tracers
from langgraph.types import Command
from pydantic import ValidationError

from bug_investigator import workspace
from bug_investigator.config import Settings
from bug_investigator.github import GitHub
from bug_investigator.graph import build_graph, recursion_limit
from bug_investigator.model_stages import ModelStages, make_chat_model
from bug_investigator.stages import GitHubError
from bug_investigator.state import Approval, BugReport, InvestigationState

_STATUS = {
    "prepare": "Подготовка: рабочая копия {workdir}, ветка {branch}",
    "baseline": "Baseline: {baseline}",
    "analyst": "Analyst: Verdict {verdict}",
    "planner": "Planner: Plan готов (попытка {attempts})",
    "coder": "Coder: Fix готов",
    "executor": "Executor: сборка с тестами — код {build_exit}",
    "critic": "Critic: {critic}",
    "issue_comment": "Комментарий в issue: {comment}",
    "not_a_bug_approval": "Approval «Не баг»: {not_a_bug_approval}",
    "plan_approval": "Plan Approval: {plan_approval}",
    "fix_approval": "Fix Approval: {fix_approval}",
    "publish": "Публикация: {publish}",
    "cleanup": "{cleanup}",
}

_DECISIONS = {"approve": "одобрено", "reject": "отклонено", "revise": "на доработку"}


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

    try:
        bug_report = _read_bug_report(args, settings)
    except (ValueError, OSError, GitHubError) as e:
        print(f"Bug Report не получен: {e}", file=sys.stderr)
        return 2
    if not bug_report["text"]:
        print("Пустой Bug Report.", file=sys.stderr)
        return 2
    ask = _answers(args)

    graph = build_graph(
        ModelStages(settings, make_chat_model(settings)), max_attempts=settings.max_attempts
    )
    config: RunnableConfig = {
        "run_name": "Investigation",
        "recursion_limit": recursion_limit(settings.max_attempts),
        "metadata": {"provider": settings.provider, "model": settings.model},
        "configurable": {"thread_id": str(uuid.uuid4())},
    }
    initial: InvestigationState = {"bug_report": bug_report}
    final: dict = dict(initial)
    step: InvestigationState | Command = initial
    finished: set[str] = set()
    try:
        while True:
            for update in graph.stream(step, config, stream_mode="updates"):
                for node, delta in update.items():
                    if node == "__interrupt__":
                        continue
                    final |= delta or {}
                    finished.add(node)
                    print(f"→ {_status(node, final)}", flush=True)
            interrupts = graph.get_state(config).interrupts
            if not interrupts:
                break
            _print_pause(interrupts[0].value)
            step = Command(resume=_approval(ask))
    finally:
        if "cleanup" not in finished and (workdir := final.get("workdir")):
            # The run broke off (an error, Ctrl-C at a pause) before the graph's cleanup.
            workspace.remove(workdir)
            print(f"Рабочая копия {workdir} удалена.", file=sys.stderr)
        wait_for_all_tracers()

    _print_result(final)
    return 0 if final.get("outcome") in ("pr_created", "not_a_bug") else 1


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="bug-investigator", description=__doc__)
    parser.add_argument("bug_report", nargs="?", help="текст Bug Report")
    parser.add_argument("--file", help="файл с текстом Bug Report")
    parser.add_argument("--issue", help="ссылка на issue Target Repository")
    return parser.parse_args(argv)


def _read_bug_report(args: argparse.Namespace, settings: Settings) -> BugReport:
    if args.issue:
        return GitHub(settings.target_repo).bug_report(args.issue)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            text = f.read()
    elif args.bug_report:
        text = args.bug_report
    else:
        text = sys.stdin.read()
    return BugReport(text=text.strip(), source="chat")


def _answers(args: argparse.Namespace) -> Callable[[str], str]:
    """Where pause answers come from: the terminal, even if stdin held the Bug Report.
    Without a terminal every pause is answered with EOF, i.e. rejected."""
    if args.issue or args.file or args.bug_report or sys.stdin.isatty():
        return input
    tty = None

    def ask(prompt: str) -> str:
        nonlocal tty
        if tty is None:
            try:
                tty = open("/dev/tty", encoding="utf-8")  # open for the whole run
            except OSError as e:
                raise EOFError from e
        print(prompt, end="", flush=True)
        line = tty.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")

    return ask


def _approval(ask: Callable[[str], str]) -> Approval:
    while True:
        try:
            choice = ask("\n[1] Одобрить  [2] Отклонить  [3] Доработать > ").strip()
        except EOFError:
            print("\nОтвета нет — отклонено.")
            return Approval(decision="reject", comment="")
        if choice in ("1", "2", "3"):
            break
    if choice == "1":
        return Approval(decision="approve", comment="")
    if choice == "2":
        return Approval(decision="reject", comment="")
    comment = ""
    while not comment:
        try:
            comment = ask("Комментарий для доработки > ").strip()
        except EOFError:
            print("\nКомментария нет — отклонено.")
            return Approval(decision="reject", comment="")
    return Approval(decision="revise", comment=comment)


def _print_pause(pause: dict) -> None:
    kind = pause.get("kind")
    if kind == "not_a_bug":
        print(f"\n== Approval: Verdict «Не баг» ==\n{pause['summary']}")
    elif kind == "plan":
        print(f"\n== Plan Approval ==\n{pause['plan']}")
    elif kind == "fix":
        build = pause.get("build_result") or {}
        fix_kind = "Reproduced Fix" if pause.get("reproduced") else "Unreproduced Fix"
        print(f"\n== Fix Approval ==\n{pause['explanation']}\n")
        print(f"{fix_kind}; сборка с тестами — код {build.get('exit_code')}")
        if pause.get("critic_notes"):
            print(f"Critic: {pause['critic_notes']}")
        print(f"\n{pause['diff']}")


def _status(node: str, state: dict) -> str:
    build = state.get("build_result") or {}
    if state.get("outcome") == "gave_up":
        critic = "агент сдался"
    else:
        critic = "Fix принят" if state.get("verification_passed") else "Fix отклонён"
    line = _STATUS.get(node, node).format(
        workdir=state.get("workdir"),
        branch=state.get("branch"),
        verdict=state.get("verdict"),
        attempts=state.get("attempts"),
        build_exit=build.get("exit_code"),
        critic=critic,
        baseline=_baseline(state),
        comment=_comment(state),
        not_a_bug_approval=_decision(state, "not_a_bug_approval"),
        plan_approval=_decision(state, "plan_approval"),
        fix_approval=_decision(state, "fix_approval"),
        publish=state.get("pr_url") or "Fix PR не создан",
        cleanup="Рабочая копия удалена" if state.get("workdir") else "Рабочей копии нет",
    )
    if state.get("outcome") == "gave_up" and node != "cleanup":
        line = f"{node}: агент сдался"
    return line


def _decision(state: dict, field: str) -> str:
    approval = state.get(field) or {}
    text = _DECISIONS.get(approval.get("decision", ""), "?")
    return f"{text} ({approval['comment']})" if approval.get("comment") else text


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
    if state.get("fix_diff") and not state.get("pr_url"):
        print(f"\n{state['fix_diff']}")
    if state.get("pr_url"):
        print(f"\nFix PR: {state['pr_url']}")


if __name__ == "__main__":
    sys.exit(main())
