"""Builds of the working copy: `docker build`, whose Dockerfile compiles and runs the
tests (ADR 0002). One signal: exit code plus the tail of the log; for the Baseline,
also the failed tests read from the log.
"""

import re
import subprocess
from dataclasses import dataclass, field

from bug_investigator.stages import AgentGaveUp
from bug_investigator.state import Baseline, BuildResult

TIMEOUT_EXIT_CODE = 124
_TAIL_LINES = 60
_TAIL_CHARS = 4_000

# `[ERROR] pkg.FooTest.method -- Time elapsed: 0.01 s <<< FAILURE!` (Surefire 3), with the
# `#14 17.19 ` prefix that `docker build --progress=plain` adds to each line.
_FAILED_TEST = re.compile(
    r"\[ERROR\]\s+(?:[\w$]+\.)*(?P<cls>[\w$]+)\.(?P<method>[\w$]+)"
    # Parameterized tests: `method(int)[2]`.
    r"(?:\([^)]*\))?(?:\[[^\]]*\])?\s+--\s+Time elapsed:"
    r".*<<<\s+(?:FAILURE|ERROR)!"
)
_EXCEPTION = re.compile(r"^(?:[\w$]+\.)*(?P<name>[\w$]+(?:Exception|Error))(?::|\s*$)")
_DOCKER_PREFIX = re.compile(r"^(?:#\d+\s+)?\d+\.\d+\s?")
# They only say "the expected value differs": no clue to search issues by.
_ASSERTION_ERRORS = {"AssertionFailedError", "AssertionError", "ComparisonFailure"}
_NO_DOCKERFILE = ("failed to read dockerfile", "open Dockerfile: no such file")


@dataclass
class Failures:
    tests: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)


def docker_build(workdir: str, timeout_s: int) -> BuildResult:
    exit_code, log = _run(workdir, timeout_s)
    return BuildResult(exit_code=exit_code, log_tail=_tail(log))


def baseline(workdir: str, timeout_s: int) -> Baseline:
    """Build the original code. A Baseline that did not run ends the Investigation:
    without it no Fix can be verified, so there is no point asking the model anything.
    """
    exit_code, log = _run(workdir, timeout_s)
    if exit_code == TIMEOUT_EXIT_CODE:
        raise AgentGaveUp(f"Baseline не завершился за {timeout_s} с: Verification невозможна")
    if exit_code != 0 and any(marker in log for marker in _NO_DOCKERFILE):
        raise AgentGaveUp("В Target Repository нет Dockerfile: Verification невозможна")
    failures = parse_failures(log)
    return Baseline(
        exit_code=exit_code,
        log_tail=_tail(log),
        failed_tests=failures.tests,
        exceptions=failures.exceptions,
    )


def parse_failures(log: str) -> Failures:
    """Failed tests (`TestClass.method`) and the exceptions they threw, in log order."""
    failures = Failures()
    expect_exception = False
    for raw in log.splitlines():
        line = _DOCKER_PREFIX.sub("", raw).strip()
        if m := _FAILED_TEST.search(line):
            _add(failures.tests, f"{m['cls']}.{m['method']}")
            expect_exception = True
            continue
        if expect_exception and line:
            expect_exception = False
            if (m := _EXCEPTION.match(line)) and m["name"] not in _ASSERTION_ERRORS:
                _add(failures.exceptions, m["name"])
    return failures


def _add(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _run(workdir: str, timeout_s: int) -> tuple[int, str]:
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
        return (
            TIMEOUT_EXIT_CODE,
            (log or "") + f"\n[сборка прервана по таймауту {timeout_s} с]",
        )
    except FileNotFoundError as e:
        raise AgentGaveUp("Docker не найден: Verification невозможна") from e
    if result.returncode != 0 and "Cannot connect to the Docker daemon" in result.stdout:
        # Infrastructure, not the code: do not let the model blame the code for it.
        raise AgentGaveUp("Docker-демон недоступен: Verification невозможна")
    return result.returncode, result.stdout


def _tail(log: str) -> str:
    tail = "\n".join(log.splitlines()[-_TAIL_LINES:])
    return tail[-_TAIL_CHARS:]
