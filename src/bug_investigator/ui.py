"""Chainlit UI: a Bug Report in, a step per graph node, Approval with buttons at pauses,
the Investigation's outcome at the end.

uv run chainlit run src/bug_investigator/ui.py
"""

import asyncio
import re

import chainlit as cl
from chainlit.utils import utc_now
from dotenv import load_dotenv
from pydantic import ValidationError

from bug_investigator.config import Settings
from bug_investigator.github import GitHub
from bug_investigator.investigation import Event, Investigation, NodeStarted
from bug_investigator.progress import RUNNING, STEP_NAMES
from bug_investigator.stages import GitHubError
from bug_investigator.state import Approval, BugReport

load_dotenv()  # API keys and LANGSMITH_* go to os.environ, never into Settings

# A pause lives as long as the process (InMemorySaver); a day is plenty to decide.
ANSWER_TIMEOUT_S = 24 * 60 * 60

_ISSUE_LINK = re.compile(r"https?://\S+/issues/\d+\S*")

_OUTCOMES = {
    "pr_created": "Fix PR создан",
    "rejected": "Отклонено человеком",
    "gave_up": "Агент сдался",
    "not_a_bug": "Не баг",
    "insufficient_data": "Недостаточно данных",
    "blocked": "Заблокировано",
}

_ACTIONS = [
    ("approve", "Одобрить"),
    ("reject", "Отклонить"),
    ("revise", "Доработать"),
]


@cl.on_chat_start
async def start() -> None:
    settings = await _settings()
    if settings is None:
        return
    await cl.Message(
        content="Опишите Bug Report — что работает неправильно, желательно со стек-трейсом, — "
        f"или вставьте ссылку на issue из {settings.target_repo}."
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    if cl.user_session.get("investigation"):
        await cl.Message(
            content="Investigation уже идёт: дождитесь итога или остановите её кнопкой Stop."
        ).send()
        return
    settings = await _settings()
    if settings is None:
        return
    try:
        bug_report = await cl.make_async(_bug_report)(message.content, settings)
    except (ValueError, GitHubError) as e:
        await cl.Message(content=f"Bug Report не получен: {e}").send()
        return
    if not bug_report["text"]:
        await cl.Message(content="Пустой Bug Report.").send()
        return
    if bug_report["source"] == "issue":
        await cl.Message(
            content=f"Bug Report из issue #{bug_report.get('issue_number')}: "
            f"**{bug_report.get('title') or 'без заголовка'}**"
        ).send()

    investigation = Investigation(settings, bug_report)
    cl.user_session.set("investigation", investigation)
    steps: dict[str, cl.Step] = {}
    answer: Approval | None = None
    try:
        while (pause := await _run(investigation, steps, answer)) is not None:
            answer = await _approval(pause)
    except Exception as e:
        await cl.Message(content=f"Investigation прервана ошибкой: {e}").send()
        return
    finally:
        # Also on a stop, which cancels this coroutine.
        _break_off(investigation)
        if cl.user_session.get("investigation") is investigation:
            cl.user_session.set("investigation", None)
        await _close_steps(steps)
    await cl.Message(content=_result(investigation.state)).send()


@cl.on_stop
@cl.on_chat_end
async def stop() -> None:
    if investigation := cl.user_session.get("investigation"):
        _break_off(investigation)


def _break_off(investigation: Investigation) -> None:
    """A running graph stops at the next node and removes the working copy itself
    (see `_run`); one left at a pause has nobody else to do it."""
    if investigation.finished:
        return
    investigation.stop()
    if not investigation.running:
        investigation.abandon()


async def _run(
    investigation: Investigation, steps: dict[str, cl.Step], answer: Approval | None
) -> dict | None:
    """Run the graph to the next pause in a worker thread, mirroring nodes as steps."""

    # Steps sent from the worker thread do not see Chainlit's step stack: without this
    # parent they would render after the messages of this run, not in order with them.
    parent = cl.context.current_step
    parent_id = parent.id if parent else None

    def on_event(event: Event) -> None:
        cl.run_sync(_show(event, steps, parent_id))

    def work() -> dict | None:
        try:
            return investigation.run(on_event, answer)
        except BaseException:
            investigation.abandon()
            raise

    # Shielded: a stop cancels this coroutine, but the thread runs on until the graph
    # notices `stop()` at the next node; its InvestigationStopped is then read here
    # so that asyncio does not log it as never retrieved.
    worker = asyncio.ensure_future(cl.make_async(work)())
    worker.add_done_callback(lambda f: f.cancelled() or f.exception())
    return await asyncio.shield(worker)


async def _show(event: Event, steps: dict[str, cl.Step], parent_id: str | None) -> None:
    if isinstance(event, NodeStarted):
        if event.node in steps:  # an Approval resumed after its pause
            return
        step = cl.Step(
            name=STEP_NAMES.get(event.node, event.node),
            type="tool",
            parent_id=parent_id,
            show_input=False,
        )
        step.start = utc_now()
        step.output = RUNNING.get(event.node, "…")
        steps[event.node] = step
        await step.send()
        return
    step = steps.pop(event.node, None)
    if step is None:
        return
    step.output = event.status
    step.end = utc_now()
    await step.update()


async def _close_steps(steps: dict[str, cl.Step]) -> None:
    """Steps left open by a stop or an error, e.g. an Approval still waiting."""
    for step in list(steps.values()):
        step.output = f"{step.output} — прервано"
        step.end = utc_now()
        await step.update()
    steps.clear()


async def _approval(pause: dict) -> Approval:
    await cl.Message(content=_pause(pause)).send()
    choice = await cl.AskActionMessage(
        content="Ваше решение?",
        actions=[
            cl.Action(name=decision, payload={"decision": decision}, label=label)
            for decision, label in _ACTIONS
        ],
        timeout=ANSWER_TIMEOUT_S,
    ).send()
    if choice is None:
        await cl.Message(content="Ответа нет — отклонено.").send()
        return Approval(decision="reject", comment="")
    decision = choice["payload"]["decision"]
    if decision != "revise":
        return Approval(decision=decision, comment="")
    while True:
        reply = await cl.AskUserMessage(
            content="Что доработать? Напишите комментарий следующим сообщением.",
            timeout=ANSWER_TIMEOUT_S,
        ).send()
        if reply is None:
            await cl.Message(content="Комментария нет — отклонено.").send()
            return Approval(decision="reject", comment="")
        if comment := reply.get("output", "").strip():
            return Approval(decision="revise", comment=comment)


async def _settings() -> Settings | None:
    try:
        settings = Settings()  # pyright: ignore[reportCallIssue]
    except ValidationError as e:
        await cl.Message(content=f"Ошибка конфигурации (переменные BI_*):\n```\n{e}\n```").send()
        return None
    if missing := settings.missing_secrets():
        await cl.Message(content=f"Не заданы переменные окружения: {', '.join(missing)}").send()
        return None
    return settings


def _bug_report(text: str, settings: Settings) -> BugReport:
    """A message that is only an issue link reads the issue; anything else is the text."""
    text = text.strip()
    if _ISSUE_LINK.fullmatch(text):
        return GitHub(settings.target_repo).bug_report(text)
    return BugReport(text=text, source="chat")


def _pause(pause: dict) -> str:
    kind = pause.get("kind")
    if kind == "not_a_bug":
        return (
            "### Approval: Verdict «Не баг»\n\n"
            f"{pause['summary']}\n\n"
            "Одобрить — согласиться (если Bug Report из issue, в нём появится комментарий). "
            "Доработать — Analyst пересмотрит Verdict с вашим комментарием."
        )
    if kind == "plan":
        return f"### Plan Approval\n\n{pause['plan']}"
    build = pause.get("build_result") or {}
    fix_kind = "Reproduced Fix" if pause.get("reproduced") else "Unreproduced Fix"
    parts = [
        "### Fix Approval",
        pause.get("explanation", ""),
        f"**{fix_kind}**; сборка с тестами — код {build.get('exit_code')}",
    ]
    if pause.get("critic_notes"):
        parts.append(f"**Critic:** {pause['critic_notes']}")
    parts.append(f"```diff\n{pause.get('diff', '')}\n```")
    parts.append("Одобрить — создать Fix PR. Доработать — новый Plan с вашим комментарием.")
    return "\n\n".join(parts)


def _result(state: dict) -> str:
    outcome = state.get("outcome", "")
    parts = [f"## Итог: {_OUTCOMES.get(outcome, outcome or 'неизвестен')}"]
    if state.get("pr_url"):
        parts.append(f"Fix PR: {state['pr_url']}")
    if state.get("outcome_reason"):
        parts.append(state["outcome_reason"])
    if number := state.get("related_issue"):
        related = {i["number"]: i for i in state.get("related_issues") or []}
        link = related[number]["url"] if number in related else f"#{number}"
        parts.append(f"Связанное issue: {link}")
    return "\n\n".join(parts)
