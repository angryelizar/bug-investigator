"""One Investigation driven pause by pause: the graph runs until the next Approval or
its end, reporting each node as it starts and finishes. Used by the CLI and the UI."""

import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from bug_investigator import workspace
from bug_investigator.config import Settings
from bug_investigator.graph import build_graph, recursion_limit
from bug_investigator.model_stages import ModelStages, make_chat_model
from bug_investigator.progress import node_status
from bug_investigator.state import Approval, BugReport, InvestigationState


@dataclass(frozen=True)
class NodeStarted:
    node: str


@dataclass(frozen=True)
class NodeFinished:
    node: str
    status: str  # `progress.node_status` right after the node


Event = NodeStarted | NodeFinished


class InvestigationStopped(Exception):
    """The person stopped the Investigation; raised at the next node boundary."""


class Investigation:
    def __init__(self, settings: Settings, bug_report: BugReport):
        self._graph = build_graph(
            ModelStages(settings, make_chat_model(settings)), max_attempts=settings.max_attempts
        )
        thread_id = str(uuid.uuid4())
        self._run_name = run_name(bug_report)
        self._config: RunnableConfig = {
            "recursion_limit": recursion_limit(settings.max_attempts),
            # LangSmith groups traces with the same `thread_id` into one thread: every
            # resume after a pause is a trace of its own.
            "metadata": {
                "provider": settings.provider,
                "model": settings.model,
                "thread_id": thread_id,
                "source": bug_report["source"],
            }
            | ({"issue_number": n} if (n := bug_report.get("issue_number")) else {}),
            "configurable": {"thread_id": thread_id},
        }
        self._pause_kind: str | None = None  # of the pending pause
        self._initial: InvestigationState = {"bug_report": bug_report}
        self._stopped = threading.Event()
        self._cleaned = False
        self.state: dict = dict(self._initial)  # merged node updates so far
        self.finished = False
        self.running = False  # `run` is in progress, possibly in another thread

    def run(self, on_event: Callable[[Event], None], answer: Approval | None = None) -> dict | None:
        """Run until the next pause and return its content (`kind` and what to show),
        or until the end and return None. `answer` resumes the pending pause.
        If it raises, call `abandon`."""
        step = self._initial if answer is None else Command(resume=answer)
        name = self._run_name
        if answer is not None and self._pause_kind:
            name += f" · после {_PAUSE_NAMES.get(self._pause_kind, self._pause_kind)}"
        config: RunnableConfig = {**self._config, "run_name": name}
        self.running = True
        try:
            stream = self._graph.stream(step, config, stream_mode=["tasks", "updates"])
            for mode, chunk in cast(Iterator[tuple[str, dict]], stream):
                # Once the outcome is known (e.g. the Fix PR is created), only cleanup is
                # left: let it run rather than lose the result.
                if self._stopped.is_set() and "outcome" not in self.state:
                    raise InvestigationStopped
                if mode == "tasks":
                    if "input" in chunk:  # a task's start; its result carries "result"
                        on_event(NodeStarted(chunk["name"]))
                else:
                    for node, delta in chunk.items():
                        if node == "__interrupt__":
                            continue
                        self.state |= delta or {}
                        self._cleaned |= node == "cleanup"
                        on_event(NodeFinished(node, node_status(node, self.state)))
            interrupts = self._graph.get_state(self._config).interrupts
        finally:
            self.running = False
        # Checked after `running` is off: a stop that comes later finds it off and
        # abandons the paused Investigation itself.
        if interrupts and self._stopped.is_set():
            raise InvestigationStopped
        if interrupts:
            self._pause_kind = interrupts[0].value.get("kind")
            return interrupts[0].value
        self.finished = True
        return None

    def stop(self) -> None:
        """Ask a running `run` (in another thread) to break off at the next node."""
        self._stopped.set()

    def abandon(self) -> str | None:
        """Remove the working copy if the graph's own cleanup has not: the run broke off
        or was left at a pause. Returns the removed path. Never call it while `run` is
        in progress."""
        if self._cleaned or not (workdir := self.state.get("workdir")):
            return None
        workspace.remove(workdir)
        self._cleaned = True
        return workdir


_PAUSE_NAMES = {"plan": "Plan Approval", "fix": "Fix Approval", "not_a_bug": "Approval «Не баг»"}
_MAX_NAME_CHARS = 60


def run_name(bug_report: BugReport) -> str:
    """Trace name in LangSmith: the issue number and title, or the Bug Report's first
    line, so that Investigations can be told apart in the list of traces."""
    if number := bug_report.get("issue_number"):
        title = bug_report.get("title") or ""
        return _shorten(f"Investigation #{number}: {title}".rstrip(": "))
    lines = [line.strip() for line in bug_report["text"].splitlines()]
    first_line = next((line for line in lines if line), "")
    return _shorten(f"Investigation: {first_line}".rstrip(": "))


def _shorten(name: str) -> str:
    return name if len(name) <= _MAX_NAME_CHARS else name[: _MAX_NAME_CHARS - 1].rstrip() + "…"
