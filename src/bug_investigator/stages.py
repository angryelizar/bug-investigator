"""What each stage of the Investigation returns, and the contract the graph needs.

The graph owns the order of stages and the routing (ADR 0001); a `Stages`
implementation owns how a stage gets its answer (a model, git, docker...).
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from bug_investigator.state import (
    Baseline,
    BuildResult,
    InvestigationState,
    RelatedIssue,
    Verdict,
)


class AgentGaveUp(Exception):
    """A stage cannot produce a result (tool-call limit hit, retries exhausted...)."""


class GitHubError(Exception):
    """A GitHub call failed; the Investigation goes on without its result."""


@dataclass(frozen=True)
class Workspace:
    workdir: str
    branch: str


class Analysis(BaseModel):
    verdict: Verdict = Field(description="Verdict по Bug Report")
    summary: str = Field(description="Где проблемное место и почему; или чего не хватает")
    # Required but nullable: strict JSON-schema modes reject optional fields.
    related_issue: int | None = Field(
        description="Для blocked: номер открытого issue из списка, описывающего причину "
        "падения Baseline; иначе null"
    )


class Fix(BaseModel):
    diff: str
    explanation: str


class Critique(BaseModel):
    passed: bool = Field(description="true, если Fix можно показывать человеку")
    notes: str = Field(description="Что не так и что исправить; пусто, если всё хорошо")


class Stages(Protocol):
    def prepare(self, state: InvestigationState) -> Workspace: ...
    def baseline(self, state: InvestigationState) -> Baseline: ...
    def search_issues(self, terms: list[str]) -> list[RelatedIssue]: ...
    def recent_open_issues(self, limit: int) -> list[RelatedIssue]: ...
    def comment_on_issue(self, number: int, body: str) -> None: ...
    def analyst(self, state: InvestigationState) -> Analysis: ...
    def planner(self, state: InvestigationState) -> str: ...
    def coder(self, state: InvestigationState) -> Fix: ...
    def build(self, state: InvestigationState) -> BuildResult: ...
    def critic(self, state: InvestigationState) -> Critique: ...
