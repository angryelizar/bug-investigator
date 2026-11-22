"""Stages backed by a chat model (Analyst, Planner, Coder, Critic) plus the code
stages (preparation, build). Between stages only short results travel; the
model's conversation with its tools stays inside one agent turn (ADR 0001).
"""

from typing import TypeVar

import groq
import openai
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field, ValidationError

from bug_investigator import executor, prompts, workspace
from bug_investigator.config import Settings
from bug_investigator.github import GitHub
from bug_investigator.graph import MAX_RELATED_ISSUES
from bug_investigator.stages import AgentGaveUp, Analysis, Critique, Fix, Workspace
from bug_investigator.state import Baseline, BuildResult, InvestigationState, RelatedIssue
from bug_investigator.tools import build_tools

T = TypeVar("T", bound=BaseModel)

# Raised by provider SDKs once their own retries with backoff are exhausted.
_PROVIDER_ERRORS = (groq.APIError, openai.APIError)
# Not transient: the provider refused the request itself (model name, parameters...).
_REQUEST_REJECTED = (groq.BadRequestError, openai.BadRequestError)
# The model answered, but not in the requested shape (e.g. Groq `tool_use_failed`).
_FORMAT_ERRORS = (
    groq.BadRequestError,
    openai.BadRequestError,
    OutputParserException,
    ValidationError,
)
_FORMAT_ATTEMPTS = 2
_FORMAT_REMINDER = (
    "Верни итог строго в виде JSON-объекта по заданной схеме, без пояснений вне JSON."
)

_MAX_DIFF_CHARS_FOR_CRITIC = 8_000


class Plan(BaseModel):
    cause: str = Field(description="Где и почему возникает баг: файл, метод, условие")
    reproduction: str = Field(
        description="Какой тест написать, чтобы он падал из-за бага; "
        "или почему тест написать нельзя"
    )
    change: str = Field(description="Что именно изменить в коде, по файлам")

    def render(self) -> str:
        return (
            f"Причина: {self.cause}\n\nReproduction: {self.reproduction}\n\n"
            f"Изменение: {self.change}"
        )


class CoderReport(BaseModel):
    explanation: str = Field(description="Что изменено и почему это исправляет баг")


def make_chat_model(settings: Settings) -> BaseChatModel:
    options: dict = {
        "model": settings.model,
        "max_retries": settings.max_retries,
        "timeout": settings.request_timeout_s,
    }
    if settings.temperature is not None:
        options["temperature"] = settings.temperature
    if settings.provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(**options)
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(**options)


class ModelStages:
    def __init__(self, settings: Settings, model: BaseChatModel):
        self.settings = settings
        self.model = model
        self.github = GitHub(settings.target_repo)

    def prepare(self, state: InvestigationState) -> Workspace:
        return workspace.prepare(
            self.settings.target_repo, self.settings.target_branch, state["bug_report"]
        )

    def baseline(self, state: InvestigationState) -> Baseline:
        return executor.baseline(_workdir(state), self.settings.build_timeout_s)

    def search_issues(self, terms: list[str]) -> list[RelatedIssue]:
        return self.github.search_open_issues(terms, limit=MAX_RELATED_ISSUES)

    def comment_on_issue(self, number: int, body: str) -> None:
        self.github.comment(number, body)

    def analyst(self, state: InvestigationState) -> Analysis:
        return self._agent_turn(
            "Analyst",
            prompts.ANALYST,
            prompts.analyst_task(state),
            self._tools(state, can_write=False),
            Analysis,
        )

    def planner(self, state: InvestigationState) -> str:
        # A retry must plan against the original code, not the rejected Fix.
        workspace.reset(_workdir(state))
        plan = self._agent_turn(
            "Planner",
            prompts.PLANNER,
            prompts.planner_task(state),
            self._tools(state, can_write=False),
            Plan,
        )
        return plan.render()

    def coder(self, state: InvestigationState) -> Fix:
        workdir = _workdir(state)
        report = self._agent_turn(
            "Coder",
            prompts.CODER,
            prompts.coder_task(state),
            self._tools(state, can_write=True),
            CoderReport,
        )
        return Fix(diff=workspace.diff(workdir), explanation=report.explanation)

    def build(self, state: InvestigationState) -> BuildResult:
        return executor.docker_build(_workdir(state), self.settings.build_timeout_s)

    def critic(self, state: InvestigationState) -> Critique:
        task = prompts.critic_task(state, max_diff_chars=_MAX_DIFF_CHARS_FOR_CRITIC)
        return self._final_answer(
            "Critic", [SystemMessage(prompts.CRITIC), HumanMessage(task)], Critique
        )

    def _tools(self, state: InvestigationState, *, can_write: bool) -> list[BaseTool]:
        return build_tools(
            _workdir(state),
            can_write=can_write,
            max_output_chars=self.settings.max_output_chars,
        )

    def _agent_turn(
        self,
        stage: str,
        system_prompt: str,
        task: str,
        tools: list[BaseTool],
        schema: type[T],
    ) -> T:
        # The ReAct loop runs without `response_format`: langgraph would then force a
        # tool call for the final answer, which Groq rejects with `tool_use_failed`
        # whenever the model answers in plain text. The final answer is requested
        # separately below, through the provider's JSON-schema mode.
        agent = create_react_agent(self.model, tools, prompt=system_prompt, name=stage)
        try:
            result = agent.invoke(
                {"messages": [HumanMessage(task)]},
                {"recursion_limit": self.settings.recursion_limit, "run_name": stage},
            )
        except GraphRecursionError as e:
            raise AgentGaveUp(
                f"{stage} исчерпал лимит в {self.settings.max_tool_calls} "
                "вызовов инструментов, не придя к ответу"
            ) from e
        except _REQUEST_REJECTED as e:
            raise AgentGaveUp(_rejected(stage, e)) from e
        except _PROVIDER_ERRORS as e:
            raise AgentGaveUp(f"{stage}: модель недоступна после повторов ({e})") from e
        return self._final_answer(
            stage, [SystemMessage(system_prompt), *result["messages"]], schema
        )

    def _final_answer(self, stage: str, messages: list[BaseMessage], schema: type[T]) -> T:
        """Ask for the stage result as JSON matching `schema`; retry once on a bad shape."""
        structured = self.model.with_structured_output(schema, method="json_schema")
        last_error: Exception | None = None
        for _ in range(_FORMAT_ATTEMPTS):
            try:
                response = structured.invoke(messages, {"run_name": f"{stage} result"})
            except _FORMAT_ERRORS as e:
                if isinstance(e, _REQUEST_REJECTED) and not _is_format_failure(e):
                    raise AgentGaveUp(_rejected(stage, e)) from e
                last_error = e
                messages = [*messages, HumanMessage(_FORMAT_REMINDER)]
                continue
            except _PROVIDER_ERRORS as e:
                raise AgentGaveUp(f"{stage}: модель недоступна после повторов ({e})") from e
            if isinstance(response, schema):
                return response
            last_error = TypeError(f"ответ типа {type(response).__name__}")
            messages = [*messages, HumanMessage(_FORMAT_REMINDER)]
        raise AgentGaveUp(f"{stage} не вернул итог в нужном формате: {last_error}")


def _workdir(state: InvestigationState) -> str:
    workdir = state.get("workdir")
    if not workdir:
        raise RuntimeError("No working copy: the preparation stage has not run")
    return workdir


def _is_format_failure(error: Exception) -> bool:
    """A 400 caused by the answer's shape, not by the request (model, parameters)."""
    text = str(error)
    return "tool_use_failed" in text or "json_validate_failed" in text


def _rejected(stage: str, error: Exception) -> str:
    return (
        f"{stage}: провайдер отклонил запрос — проверь BI_MODEL и параметры модели "
        f"в конфиге ({error})"
    )
