"""Stages backed by a chat model (Analyst, Planner, Coder, Critic) plus the code
stages (preparation, build). Between stages only short results travel; the
model's conversation with its tools stays inside one agent turn (ADR 0001).
"""

from typing import TypeVar

import groq
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.errors import GraphRecursionError
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, Field

from bug_investigator import executor, prompts, workspace
from bug_investigator.config import Settings
from bug_investigator.stages import AgentGaveUp, Analysis, Critique, Fix, Workspace
from bug_investigator.state import BuildResult, InvestigationState
from bug_investigator.tools import build_tools

T = TypeVar("T", bound=BaseModel)

# Raised by provider SDKs once their own retries with backoff are exhausted.
_PROVIDER_ERRORS = (groq.APIError, openai.APIError)

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
    if settings.provider == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=settings.model,
            temperature=settings.temperature,
            max_retries=settings.max_retries,
            timeout=settings.request_timeout_s,
        )
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=settings.model,
        temperature=settings.temperature,
        max_retries=settings.max_retries,
        timeout=settings.request_timeout_s,
    )


class ModelStages:
    def __init__(self, settings: Settings, model: BaseChatModel):
        self.settings = settings
        self.model = model

    def prepare(self, state: InvestigationState) -> Workspace:
        return workspace.prepare(
            self.settings.target_repo, self.settings.target_branch, state["bug_report"]
        )

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
        structured = self.model.with_structured_output(Critique)
        try:
            result = structured.invoke(
                [SystemMessage(prompts.CRITIC), HumanMessage(task)],
                {"run_name": "Critic"},
            )
        except _PROVIDER_ERRORS as e:
            raise AgentGaveUp(f"Critic: модель недоступна после повторов ({e})") from e
        if not isinstance(result, Critique):
            raise AgentGaveUp("Critic не вернул оценку в нужном формате")
        return result

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
        agent = create_react_agent(
            self.model, tools, prompt=system_prompt, response_format=schema, name=stage
        )
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
        except _PROVIDER_ERRORS as e:
            raise AgentGaveUp(f"{stage}: модель недоступна после повторов ({e})") from e
        response = result.get("structured_response")
        if not isinstance(response, schema):
            raise AgentGaveUp(f"{stage} не вернул ответ в нужном формате")
        return response


def _workdir(state: InvestigationState) -> str:
    workdir = state.get("workdir")
    if not workdir:
        raise RuntimeError("No working copy: the preparation stage has not run")
    return workdir
