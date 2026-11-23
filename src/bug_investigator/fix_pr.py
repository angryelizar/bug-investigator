"""Title and description of the Fix PR. Built from a template, without the model:
everything it says is already in the state.
"""

from bug_investigator.executor import TIMEOUT_EXIT_CODE
from bug_investigator.state import InvestigationState

_MAX_TITLE_CHARS = 72


def title(state: InvestigationState) -> str:
    report = state["bug_report"]
    lines = report["text"].strip().splitlines()
    text = report.get("title") or (lines[0] if lines else "Bug Report")
    if len(text) > _MAX_TITLE_CHARS:
        text = text[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
    return f"Fix: {text}"


def body(state: InvestigationState) -> str:
    report = state["bug_report"]
    if (number := report.get("issue_number")) is not None:
        source = f"Fixes #{number}"
    else:
        quoted = "\n".join(f"> {line}" for line in report["text"].strip().splitlines())
        source = f"## Bug Report\n\n{quoted}"
    return "\n\n".join(
        [
            source,
            f"## Причина\n\n{state.get('analysis_summary', '')}",
            f"## Изменение\n\n{state.get('fix_explanation', '')}",
            f"## Verification\n\n{_verification(state)}",
            "---\n_Создано агентом Bug Investigator после Fix Approval человека. "
            "Агент PR не мержит._",
        ]
    )


def _verification(state: InvestigationState) -> str:
    if state.get("reproduced"):
        kind = "**Reproduced Fix**: Reproduction падает на исходном коде и проходит вместе с Fix."
    else:
        kind = (
            "**Unreproduced Fix**: теста, который падал бы из-за бага на исходном коде, "
            "нет. Известно "
            "лишь, что Fix собирается и не ломает существующие тесты."
        )
    build = state.get("build_result")
    lines = [
        f"- Сборка с тестами (`docker build`) с Fix: код возврата {build['exit_code']}."
        if build
        else "- Сборки с Fix не было."
    ]
    if reproduction := state.get("reproduction_build"):
        line = (
            "- Сборка исходного кода только с тестами из Fix, без Fix: "
            f"код возврата {reproduction['exit_code']}"
        )
        if reproduction["exit_code"] == TIMEOUT_EXIT_CODE:
            line += " (прервана по таймауту)"
        elif state.get("reproduced"):
            line += "; упали " + ", ".join(f"`{t}`" for t in reproduction["failed_tests"])
        lines.append(line + ".")
    return f"{kind}\n\n" + "\n".join(lines)
