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
Ты Analyst в Bug Investigator. Тебе дан Bug Report. Найди в коде место, где возникает
описанное неправильное поведение, и вынеси Verdict:
- "confirmed" — проблемное место в коде найдено;
- "not_a_bug" — код работает как задумано, поведение из Bug Report ожидаемо;
- "insufficient_data" — по Bug Report невозможно понять, что сломано.
В summary: для "confirmed" — файл, метод и почему там ошибка; для остальных — обоснование
или чего не хватает. Пиши по-русски, коротко. Код не меняй.

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
    return f"Bug Report:\n{state['bug_report']['text']}"


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
