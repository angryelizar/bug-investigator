"""Verification build: `docker build` of the working copy, whose Dockerfile compiles
and runs the tests (ADR 0002). One signal: exit code plus the tail of the log.
"""

import subprocess

from bug_investigator.stages import AgentGaveUp
from bug_investigator.state import BuildResult

TIMEOUT_EXIT_CODE = 124
_TAIL_LINES = 60
_TAIL_CHARS = 4_000


def docker_build(workdir: str, timeout_s: int) -> BuildResult:
    try:
        result = subprocess.run(
            ["docker", "build", "--progress=plain", "."],
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        log = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else e.stdout
        return BuildResult(
            exit_code=TIMEOUT_EXIT_CODE,
            log_tail=_tail(log or "") + f"\n[сборка прервана по таймауту {timeout_s} с]",
        )
    except FileNotFoundError as e:
        raise AgentGaveUp("Docker не найден: Verification невозможна") from e
    if result.returncode != 0 and "Cannot connect to the Docker daemon" in result.stdout:
        # Infrastructure, not the Fix: do not let Critic blame the code for it.
        raise AgentGaveUp("Docker-демон недоступен: Verification невозможна")
    return BuildResult(exit_code=result.returncode, log_tail=_tail(result.stdout))


def _tail(log: str) -> str:
    tail = "\n".join(log.splitlines()[-_TAIL_LINES:])
    return tail[-_TAIL_CHARS:]
