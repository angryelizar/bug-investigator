"""Stand configuration. Secrets (API keys) are never fields here: the provider SDKs
and LangSmith read them from the environment themselves, so they cannot leak into
the Investigation state or traces.
"""

import os
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["groq", "openai"]

_KEY_VARS: dict[Provider, str] = {"groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BI_", env_file=".env", extra="ignore")

    # Target Repository: only repositories from the stand config are run (ADR 0002).
    target_repo: str
    target_branch: str = "main"

    provider: Provider = "groq"
    model: str
    temperature: float = 0.0

    # Limits
    max_tool_calls: int = 15  # per agent turn, enforced through recursion_limit
    max_attempts: int = 3  # Planner → Critic loop
    max_output_chars: int = 6_000  # per tool result
    max_retries: int = 4  # transient provider failures, with growing backoff
    request_timeout_s: float = 60.0
    build_timeout_s: int = 300

    def missing_secrets(self) -> list[str]:
        required = [_KEY_VARS[self.provider]]
        return [name for name in required if not os.environ.get(name)]

    @property
    def recursion_limit(self) -> int:
        # create_react_agent: one model step plus one tool step per call, plus the
        # first model step and the structured-response step.
        return 2 * self.max_tool_calls + 2
