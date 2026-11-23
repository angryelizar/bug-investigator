"""System prompts and task texts for each stage. Each task carries only the short
results of earlier stages, never their tool conversations.
"""

from bug_investigator.executor import TIMEOUT_EXIT_CODE
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
причина (файл, метод, условие), Reproduction и минимальное изменение кода по файлам.
Не трогай то, что не относится к багу.
Reproduction — тест под src/test, который падает из-за бага и проходит после исправления.
Его прогоняют на исходном коде без исправления, поэтому:
- он должен компилироваться на исходном коде: только существующие классы, методы и
  сигнатуры, без API, которое появится в исправлении. Иначе он упадёт на компиляции,
  а не из-за бага, и ничего не докажет;
- он проверяет именно поведение из Bug Report (ту ошибку или тот неверный результат),
  а не что-то рядом;
- это новый тест или новый тестовый метод; существующие проверки не удалять и не ослаблять.
Если тест написать нельзя — объясни почему. Если прошлый Fix
отклонён — учти замечания Critic; если человек вернул Plan или Fix с комментарием —
следуй ему. Пиши по-русски.

{_TOOLS_NOTE}"""

CODER = f"""\
Ты Coder в Bug Investigator. Реализуй Plan в рабочей копии.
- `write_file` перезаписывает файл целиком: сначала прочитай файл через `read_file`,
  затем запиши полное новое содержимое, не теряя остальной код.
- Меняй только то, что нужно для исправления. Не меняй Dockerfile и сборку.
- Если Plan предусматривает Reproduction — добавь под src/test новый тест или новый
  тестовый метод. Он прогоняется на исходном коде без твоего исправления, поэтому
  используй только API, которое уже есть в исходном коде, и проверяй именно поведение
  из Bug Report.
- Не удаляй и не ослабляй существующие тесты и проверки (assert, ожидаемые значения,
  ожидаемые исключения), даже если они мешают сборке.
В explanation опиши по-русски, что изменено и почему это исправляет баг.

{_TOOLS_NOTE}"""

CRITIC = """\
Ты Critic в Bug Investigator. Тебе даны Bug Report, Plan, diff Fix и результат сборки
с тестами. Реши, можно ли показывать Fix человеку:
- объясняет ли diff поведение из Bug Report и устраняет ли его причину;
- не задевает ли он лишнего (посторонние правки, удалённый код);
- не удалены и не ослаблены ли существующие тесты и проверки (удалённые тестовые методы,
  assert, изменённые ожидаемые значения, @Disabled) — если да, Fix не проходит;
- если есть Reproduction — проверяет ли она именно поведение из Bug Report;
- если сборка упала — Fix не проходит.
Unreproduced Fix (тесты из Fix не падают на исходном коде или их нет) сам по себе не повод
отклонять: человек увидит эту пометку.
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
    if (approval := state.get("not_a_bug_approval")) and approval["decision"] == "revise":
        parts += [
            f"Прошлый итог Analysis (Verdict not_a_bug):\n{state.get('analysis_summary', '')}",
            "Человек не согласен с Verdict «Не баг» и просит доработать. Его комментарий "
            f"важнее прошлого итога:\n{approval['comment']}",
        ]
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
    # A human's revision resets the attempts: only the Plan right after it gets the
    # comment, later Critic retries build on that Plan.
    revisions = (
        [
            f"Комментарий человека к {what}:\n{approval['comment']}"
            for what, approval in [
                ("прошлому Plan", state.get("plan_approval")),
                ("прошлому Fix", state.get("fix_approval")),
            ]
            if approval and approval["decision"] == "revise"
        ]
        if state.get("attempts", 0) == 0
        else []
    )
    notes = state.get("critic_notes")
    if notes or revisions:
        parts.append(f"Прошлый Plan:\n{state.get('plan', '')}")
    if notes:
        parts.append(f"Замечания Critic к прошлому Fix:\n{notes}")
    if revisions:
        parts += revisions
        parts.append("Комментарий человека важнее прошлого Plan: учти его в новом Plan.")
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
        f"Сборка с тестами: {build_text}\n\n"
        f"{_reproduction_text(state)}"
    )


def _reproduction_text(state: InvestigationState) -> str:
    reproduction = state.get("reproduction_build")
    build = state.get("build_result")
    if not build or build["exit_code"] != 0:
        return "Reproduction не проверялась: сборка с Fix не прошла."
    if not reproduction:
        return "Unreproduced Fix: тестов в Fix нет, на исходном коде проверять нечего."
    if state.get("reproduced"):
        failed = ", ".join(reproduction["failed_tests"])
        return f"Reproduced Fix: на исходном коде без исправления падают тесты: {failed}."
    if reproduction["exit_code"] == 0:
        return "Unreproduced Fix: тесты из Fix проходят и на исходном коде без исправления."
    if reproduction["exit_code"] == TIMEOUT_EXIT_CODE:
        return "Unreproduced Fix: сборка исходного кода с тестами из Fix не уложилась в таймаут."
    return (
        "Unreproduced Fix: сборка исходного кода с тестами из Fix упала "
        f"(код возврата {reproduction['exit_code']}), но не на новых тестах — например, "
        "тест не компилируется без исправления или падают тесты, красные уже в Baseline. "
        f"Хвост лога:\n{reproduction['log_tail']}"
    )
