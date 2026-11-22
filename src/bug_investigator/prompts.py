"""System prompts and task texts for each stage. Each task carries only the short
results of earlier stages, never their tool conversations.
"""

from bug_investigator.state import InvestigationState

_TOOLS_NOTE = """\
Инструменты работают внутри рабочей копии Target Repository, пути — относительные.
- `find_files` — поиск файлов по части имени или glob.
- `grep` — поиск по содержимому; `definitions_only=true` оставляет только объявления.
- `list_directory`, `read_file` — обзор и чтение.
Ответы длиннее лимита обрезаются: ищи точечно, не читай всё подряд.
Число вызовов инструментов ограничено — начинай с поиска по именам из Bug Report."""

ANALYST = f"""\
Ты Analyst в Bug Investigator. Тебе даны Bug Report и Baseline — результат сборки
с тестами исходного кода. Найди в коде место, где возникает описанное неправильное
поведение, и вынеси Verdict:
- "confirmed" — только если найдено место в коде, которое объясняет конкретное
  наблюдаемое поведение из Bug Report (ошибку, неверный результат, упавший тест).
  Подозрительный или «некрасивый» код, не объясняющий симптом, — не повод для "confirmed".
- "not_a_bug" — симптом в Bug Report описан, но код работает как задумано и это поведение
  ожидаемо.
- "insufficient_data" — в Bug Report нет симптома (ошибки, неверного результата, упавшего
  теста) или по нему нельзя понять, что сломано. Мнение или пожелание без симптома
  («версия старая», «код медленный») — это "insufficient_data", а не "not_a_bug".
- "blocked" — только если в Bug Report есть симптом, Baseline красный и упавшие тесты
  с этим симптомом не связаны: пока они падают, Fix нельзя проверить. Без симптома —
  "insufficient_data", даже при красном Baseline. Если упавшие тесты и есть симптом из Bug Report —
  это не "blocked", а обычный разбор.
Настройки сборки (версии, плагины, параметры компилятора) проверяй с учётом унаследованных:
parent POM, BOM, свойства (`<properties>`), конфигурация плагинов по умолчанию.
В summary: для "confirmed" — файл, метод и почему там ошибка; для "insufficient_data" —
перечень того, чего не хватает (стек-трейс, шаги, ожидаемый и фактический результат);
для остальных — обоснование. related_issue — номер issue из списка открытых issue,
если для "blocked" одно из них описывает причину падения Baseline; иначе null.
Пиши по-русски, коротко. Код не меняй.

{_TOOLS_NOTE}"""

PLANNER = f"""\
Ты Planner в Bug Investigator. Тебе даны Bug Report и итог Analysis. Составь Plan:
причина (файл, метод, условие), Reproduction (тест под src/test, который падает из-за бага
и проходит после исправления; если тест написать нельзя — объясни почему) и минимальное
изменение кода по файлам. Не трогай то, что не относится к багу. Если прошлый Fix
отклонён — учти замечания Critic. Пиши по-русски.

{_TOOLS_NOTE}"""

CODER = f"""\
Ты Coder в Bug Investigator. Реализуй Plan в рабочей копии.
- `write_file` перезаписывает файл целиком: сначала прочитай файл через `read_file`,
  затем запиши полное новое содержимое, не теряя остальной код.
- Меняй только то, что нужно для исправления. Не меняй Dockerfile и сборку.
- Если Plan предусматривает Reproduction — добавь или измени тест под src/test.
В explanation опиши по-русски, что изменено и почему это исправляет баг.

{_TOOLS_NOTE}"""

CRITIC = """\
Ты Critic в Bug Investigator. Тебе даны Bug Report, Plan, diff Fix и результат сборки
с тестами. Реши, можно ли показывать Fix человеку:
- объясняет ли diff поведение из Bug Report и устраняет ли его причину;
- не задевает ли он лишнего (посторонние правки, удалённый код, ослабленные тесты);
- если сборка упала — Fix не проходит.
passed=true только если всё в порядке. В notes — конкретно, что исправить. По-русски."""


def analyst_task(state: InvestigationState) -> str:
    parts = [f"Bug Report:\n{state['bug_report']['text']}", _baseline_text(state)]
    if issues := state.get("related_issues"):
        listed = "\n".join(f"#{i['number']}: {i['title']}" for i in issues)
        parts.append(
            "Открытые issue: сначала найденные по именам упавших тестов и исключений, "
            "затем последние открытые. Issue пишут словами пользователя, а не именами "
            f"тестов, — сопоставляй с упавшими тестами по смыслу:\n{listed}"
        )
    return "\n\n".join(parts)


def _baseline_text(state: InvestigationState) -> str:
    baseline = state.get("baseline")
    if not baseline:
        return "Baseline: нет данных."
    if baseline["exit_code"] == 0:
        return (
            "Baseline зелёный: исходный код собирается, все тесты проходят. "
            "Verdict blocked невозможен."
        )
    lines = [f"Baseline красный (код возврата {baseline['exit_code']})."]
    if baseline["failed_tests"]:
        lines.append("Упавшие тесты:\n" + "\n".join(f"- {t}" for t in baseline["failed_tests"]))
    if baseline["exceptions"]:
        lines.append("Исключения: " + ", ".join(baseline["exceptions"]))
    if not baseline["failed_tests"]:
        lines.append(f"Имена упавших тестов не найдены, хвост лога:\n{baseline['log_tail']}")
    return "\n".join(lines)


def planner_task(state: InvestigationState) -> str:
    parts = [
        f"Bug Report:\n{state['bug_report']['text']}",
        f"Итог Analysis:\n{state.get('analysis_summary', '')}",
    ]
    if notes := state.get("critic_notes"):
        parts += [
            f"Прошлый Plan:\n{state.get('plan', '')}",
            f"Замечания Critic к прошлому Fix:\n{notes}",
        ]
    return "\n\n".join(parts)


def coder_task(state: InvestigationState) -> str:
    return f"Bug Report:\n{state['bug_report']['text']}\n\nPlan:\n{state.get('plan', '')}"


def critic_task(state: InvestigationState, *, max_diff_chars: int) -> str:
    diff = state.get("fix_diff") or "(пустой diff: Coder ничего не изменил)"
    if len(diff) > max_diff_chars:
        diff = diff[:max_diff_chars] + f"\n… [diff обрезан, всего {len(diff)} символов]"
    build = state.get("build_result")
    build_text = (
        f"код возврата {build['exit_code']}, хвост лога:\n{build['log_tail']}"
        if build
        else "сборки не было"
    )
    return (
        f"Bug Report:\n{state['bug_report']['text']}\n\n"
        f"Plan:\n{state.get('plan', '')}\n\n"
        f"Объяснение Coder:\n{state.get('fix_explanation', '')}\n\n"
        f"Diff:\n{diff}\n\n"
        f"Сборка с тестами: {build_text}"
    )
