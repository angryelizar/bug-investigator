"""Run one Investigation from the terminal; pauses for Approval are answered here:

uv run bug-investigator "текст Bug Report"
uv run bug-investigator --issue https://github.com/<owner>/<repo>/issues/12
uv run bug-investigator --file bug.txt
cat bug.txt | uv run bug-investigator
"""

import argparse
import os
import sys
from collections.abc import Callable

from dotenv import load_dotenv
from langchain_core.tracers.langchain import wait_for_all_tracers
from pydantic import ValidationError

from bug_investigator.config import Settings
from bug_investigator.github import GitHub
from bug_investigator.investigation import Event, Investigation, NodeFinished
from bug_investigator.stages import GitHubError
from bug_investigator.state import Approval, BugReport


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

    investigation = Investigation(settings, bug_report)
    answer: Approval | None = None
    try:
        while (pause := investigation.run(_print_event, answer)) is not None:
            _print_pause(pause)
            answer = _approval(ask)
    finally:
        # The run broke off (an error, Ctrl-C at a pause) before the graph's cleanup.
        if not investigation.finished and (workdir := investigation.abandon()):
            print(f"Рабочая копия {workdir} удалена.", file=sys.stderr)
        wait_for_all_tracers()

    final = investigation.state
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
        print(f"\n== Plan Approval — попытка {pause['attempt']} из {pause['max_attempts']} ==")
        if pause.get("rejected_fix_notes"):
            print(f"Прошлый Fix не прошёл Verification:\n{pause['rejected_fix_notes']}\n")
        print(pause["plan"])
    elif kind == "fix":
        build = pause.get("build_result") or {}
        fix_kind = "Reproduced Fix" if pause.get("reproduced") else "Unreproduced Fix"
        print(f"\n== Fix Approval ==\n{pause['explanation']}\n")
        print(f"{fix_kind}; сборка с тестами — код {build.get('exit_code')}")
        if reproduction := pause.get("reproduction_build"):
            print(f"Только тесты из Fix на исходном коде — код {reproduction['exit_code']}")
        if pause.get("critic_notes"):
            print(f"Critic: {pause['critic_notes']}")
        print(f"\n{pause['diff']}")


def _print_event(event: Event) -> None:
    if isinstance(event, NodeFinished):
        print(f"→ {event.status}", flush=True)


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
