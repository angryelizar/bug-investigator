# Bug Investigator

Агент получает Bug Report (текст или ссылку на GitHub issue), находит причину в Target Repository, готовит Fix, проверяет его сборкой с тестами в Docker и после одобрения человека открывает Fix PR. Мержит всегда человек.

## Флоу

```
Подготовка → Baseline → Analyst ─┬ Подтверждено → Planner → [Plan Approval] → Coder → Executor → Critic ─┬ прошло → [Fix Approval] → Fix PR
                                 └ Не баг / Недостаточно данных / Заблокировано → комментарий в issue   ↑   └ не прошло (до 3 попыток)
                                                                                                         └───────────┘
```

- Порядок этапов жёсткий, маршрутизация — в коде, а не в модели ([ADR 0001](docs/adr/0001-fixed-agent-pipeline.md)).
- Fix проверяется через `docker build` Target Repository: сборка с Fix должна пройти, а тесты из Fix на исходном коде должны упасть. Тогда Fix — Reproduced, иначе — Unreproduced ([ADR 0002](docs/adr/0002-verification-via-docker-build.md)).
- `[...]` — паузы: «Одобрить», «Отклонить», «Доработать» с комментарием.

Подробнее: схема — [`Bug Investigator.drawio.xml`](Bug%20Investigator.drawio.xml), термины — [`GLOSSARY.md`](GLOSSARY.md), материалы к защите — [`docs/defense.md`](docs/defense.md).

## Запуск

Нужны Python 3.13, [uv](https://docs.astral.sh/uv/), запущенный Docker и [`gh`](https://cli.github.com/), авторизованный в Target Repository (`gh auth login`), с правом push.

```bash
uv sync
cp .env.example .env    # BI_TARGET_REPO, BI_PROVIDER, BI_MODEL, ключи
uv run chainlit run src/bug_investigator/ui.py    # http://localhost:8000
```

Или из терминала: `uv run bug-investigator --issue https://github.com/<owner>/<repo>/issues/1`.

**Провайдер.** Берите OpenAI (`BI_PROVIDER=openai`) или платный тариф Groq. На бесплатных тарифах Investigation упирается в лимит токенов в минуту и не доходит до конца. Полный прогон трёх багов на OpenAI стоил около $1.

**Target Repository** должен иметь в корне `Dockerfile`, который компилирует проект и прогоняет тесты (для Maven — `mvn package` без `-DskipTests`). Тесты не должны требовать внешних сервисов и должны лежать под `src/test/**`. Пример — [`resourceBooking`](https://github.com/angryelizar/resourceBooking).

Остальные настройки (лимиты, таймауты, LangSmith) — в [`.env.example`](.env.example).
