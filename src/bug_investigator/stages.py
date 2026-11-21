"""What each stage of the Investigation returns, and the contract the graph needs.

The graph owns the order of stages and the routing (ADR 0001); a `Stages`
implementation owns how a stage gets its answer (a model, git, docker...).
"""

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, Field

from bug_investigator.state import BuildResult, InvestigationState, Verdict


class AgentGaveUp(Exception):
    """A stage cannot produce a result (tool-call limit hit, retries exhausted...)."""


@dataclass(frozen=True)
class Workspace:
    workdir: str
    branch: str


class Analysis(BaseModel):
    verdict: Verdict = Field(description="Verdict по Bug Report")
    summary: str = Field(description="Где проблемное место и почему; или чего не хватает")


class Fix(BaseModel):
    diff: str
    explanation: str


class Critique(BaseModel):
    passed: bool = Field(description="true, если Fix можно показывать человеку")
    notes: str = Field(description="Что не так и что исправить; пусто, если всё хорошо")


class Stages(Protocol):
    def prepare(self, state: InvestigationState) -> Workspace: ...
    def analyst(self, state: InvestigationState) -> Analysis: ...
    def planner(self, state: InvestigationState) -> str: ...
    def coder(self, state: InvestigationState) -> Fix: ...
    def build(self, state: InvestigationState) -> BuildResult: ...
    def critic(self, state: InvestigationState) -> Critique: ...
