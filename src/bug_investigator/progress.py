"""What a person sees of each graph node: its name, what it is doing while it runs and
the short status it ends with. Shared by the CLI and the Chainlit UI."""

STEP_NAMES = {
    "prepare": "Подготовка",
    "baseline": "Baseline",
    "analyst": "Analyst",
    "not_a_bug_approval": "Approval «Не баг»",
    "issue_comment": "Комментарий в issue",
    "planner": "Planner",
    "plan_approval": "Plan Approval",
    "coder": "Coder",
    "executor": "Executor",
    "critic": "Critic",
    "fix_approval": "Fix Approval",
    "publish": "Публикация",
    "cleanup": "Завершение",
}

RUNNING = {
    "prepare": "Клонирует Target Repository во временную папку…",
    "baseline": "Собирает исходный код с тестами…",
    "analyst": "Analyst ищет проблемное место…",
    "not_a_bug_approval": "Ждёт решения человека…",
    "issue_comment": "Оставляет комментарий в issue…",
    "planner": "Planner составляет Plan…",
    "plan_approval": "Ждёт решения человека…",
    "coder": "Coder вносит Fix…",
    "executor": "Собирает Fix с тестами…",
    "critic": "Critic проверяет Fix…",
    "fix_approval": "Ждёт решения человека…",
    "publish": "Коммит, push и создание Fix PR…",
    "cleanup": "Удаляет рабочую копию…",
}

_STATUS = {
    "prepare": "Подготовка: рабочая копия {workdir}, ветка {branch}",
    "baseline": "Baseline: {baseline}",
    "analyst": "Analyst: Verdict {verdict}",
    "planner": "Planner: Plan готов (попытка {attempts})",
    "coder": "Coder: Fix готов",
    "executor": "Executor: сборка с тестами — код {build_exit}",
    "critic": "Critic: {critic}",
    "issue_comment": "Комментарий в issue: {comment}",
    "not_a_bug_approval": "Approval «Не баг»: {not_a_bug_approval}",
    "plan_approval": "Plan Approval: {plan_approval}",
    "fix_approval": "Fix Approval: {fix_approval}",
    "publish": "Публикация: {publish}",
    "cleanup": "{cleanup}",
}

_DECISIONS = {"approve": "одобрено", "reject": "отклонено", "revise": "на доработку"}


def node_status(node: str, state: dict) -> str:
    """One line on how the node ended, given the state right after it."""
    build = state.get("build_result") or {}
    if state.get("outcome") == "gave_up":
        critic = "агент сдался"
    else:
        critic = "Fix принят" if state.get("verification_passed") else "Fix отклонён"
    line = _STATUS.get(node, node).format(
        workdir=state.get("workdir"),
        branch=state.get("branch"),
        verdict=state.get("verdict"),
        attempts=state.get("attempts"),
        build_exit=build.get("exit_code"),
        critic=critic,
        baseline=_baseline(state),
        comment=_comment(state),
        not_a_bug_approval=_decision(state, "not_a_bug_approval"),
        plan_approval=_decision(state, "plan_approval"),
        fix_approval=_decision(state, "fix_approval"),
        publish=state.get("pr_url") or "Fix PR не создан",
        cleanup="Рабочая копия удалена" if state.get("workdir") else "Рабочей копии нет",
    )
    if state.get("outcome") == "gave_up" and node != "cleanup":
        line = f"{node}: агент сдался"
    return line


def _decision(state: dict, field: str) -> str:
    approval = state.get(field) or {}
    text = _DECISIONS.get(approval.get("decision", ""), "?")
    return f"{text} ({approval['comment']})" if approval.get("comment") else text


def _comment(state: dict) -> str:
    if "issue_number" not in state.get("bug_report", {}):
        return "нет — Bug Report не из issue"
    failed = f"Комментарий в issue #{state['bug_report']['issue_number']} не оставлен"
    return "не оставлен" if failed in state.get("outcome_reason", "") else "оставлен"


def _baseline(state: dict) -> str:
    baseline = state.get("baseline") or {}
    if baseline.get("exit_code") == 0:
        return "зелёный"
    failed = ", ".join(baseline.get("failed_tests") or []) or "имена тестов не найдены"
    issues = len(state.get("related_issues") or [])
    return f"красный ({failed}); открытых issue для Analyst: {issues}"
