"""Comments the graph leaves in the Bug Report's issue. Built from templates, without
the model: the Verdict and its reasons are already in the state.
"""

from bug_investigator.state import InvestigationState

_SIGNATURE = "_Комментарий оставлен агентом Bug Investigator._"


def insufficient_data(state: InvestigationState) -> str:
    return (
        "**Bug Investigator: Verdict «Недостаточно данных»**\n\n"
        "По описанию не удалось найти место в коде, которое объясняет проблему. "
        "Чего не хватает:\n\n"
        f"{state.get('analysis_summary', '')}\n\n"
        f"{_SIGNATURE}"
    )


def not_a_bug(state: InvestigationState) -> str:
    return (
        "**Bug Investigator: Verdict «Не баг»**\n\n"
        "Код работает как задумано, описанное поведение ожидаемо:\n\n"
        f"{state.get('analysis_summary', '')}\n\n"
        "Verdict подтверждён человеком.\n\n"
        f"{_SIGNATURE}"
    )


def blocked(state: InvestigationState) -> str:
    baseline = state.get("baseline")
    failed = baseline["failed_tests"] if baseline else []
    tests = "\n".join(f"- `{t}`" for t in failed) or "- (имена тестов не удалось прочитать из лога)"
    parts = [
        "**Bug Investigator: Verdict «Заблокировано»**",
        "Сборка с тестами исходного кода (Baseline) красная, и упавшие тесты не связаны "
        "с этим issue. Пока они падают, исправление нельзя проверить.",
        f"Упавшие тесты:\n{tests}",
    ]
    if related := _related(state):
        parts.append(f"Похоже, это падение уже описано в #{related['number']}: {related['title']}")
    parts += [state.get("analysis_summary", ""), _SIGNATURE]
    return "\n\n".join(p for p in parts if p)


def _related(state: InvestigationState):
    number = state.get("related_issue")
    return next((i for i in state.get("related_issues", []) if i["number"] == number), None)
