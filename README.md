# Bug Investigator

Агент-помощник разработчика: получает Bug Report, находит причину в Target Repository, готовит Fix, проверяет его сборкой с тестами в Docker и после Approval человека создаёт Fix PR в GitHub. Агент никогда не мержит PR.

Термины — в [`GLOSSARY.md`](GLOSSARY.md), спецификация — в [`.scratch/bug-investigator/spec.md`](.scratch/bug-investigator/spec.md), решения — в [`docs/adr/`](docs/adr), материалы к защите — в [`docs/defense.md`](docs/defense.md). Схема графа — [`Bug Investigator.drawio.xml`](Bug%20Investigator.drawio.xml) (открывается в diagrams.net).

## Как устроено

Граф LangGraph с жёстким порядком этапов (ADR 0001). Модель работает только внутри Analyst, Planner и Coder (ReAct с поиском, чтением и записью файлов) и в Critic (без инструментов). Git, Docker и GitHub вызывает граф.

```
Подготовка → Baseline → Analyst ─┬ Подтверждено → Planner → [Plan Approval] → Coder → Executor → Critic ─┬ прошло → [Fix Approval] → Fix PR
                                 ├ Не баг → [Approval] → комментарий в issue          ↑                     │
                                 ├ Недостаточно данных → комментарий в issue          └──── не прошло ──────┘ (до 3 попыток)
                                 └ Заблокировано → комментарий в issue + связанное issue
```

`[...]` — паузы Human-in-the-Loop: «Одобрить», «Отклонить», «Доработать» с комментарием. В конце любого маршрута рабочая копия удаляется.

## Требования

- Python 3.13 и [uv](https://docs.astral.sh/uv/)
- Docker (демон запущен) — Verification и Baseline делаются через `docker build`
- git с правом push в Target Repository
- [GitHub CLI](https://cli.github.com/) `gh`, авторизованный в Target Repository: `gh auth login` или переменная `GH_TOKEN`. Через него читаются и ищутся issue, пишутся комментарии и создаётся Fix PR
- Ключ OpenAI или Groq; ключ LangSmith для трейсов

## Требования к Target Repository

Агент не знает языка проекта: для него «проверено» = «`docker build` в корне репозитория завершился с кодом 0» (ADR 0002). Поэтому репозиторий обязан:

1. **Иметь `Dockerfile` в корне, который компилирует проект и прогоняет тесты.** Для Maven — `mvn package` без `-DskipTests`. Упавший тест должен ронять сборку.
2. **Гонять тесты без внешних сервисов.** Внутри `docker build` нет базы, очередей и сети к ним: используйте H2 в памяти, Testcontainers не подойдут.
3. **Держать тесты под `src/test/**`** (или `<module>/src/test/**` для многомодульных проектов). По этому пути Executor отделяет тесты из Fix от исправления, чтобы проверить Reproduction.
4. **Иметь зелёную целевую ветку.** Если Baseline красный из-за проблемы, не связанной с Bug Report, Analyst выносит Verdict «Заблокировано»: любой Fix всё равно не пройдёт Verification.
5. **Быть в конфиге стенда.** Код и Dockerfile исполняются вашим локальным Docker, поэтому агент работает только с репозиторием из `BI_TARGET_REPO`.

Совет: ставьте зависимости отдельным слоем (`COPY pom.xml` → `mvn dependency:go-offline` → `COPY src` → `mvn package`), иначе каждая сборка будет скачивать их заново. Таймаут одной сборки — 5 минут.

Пример — стенд [`resourceBooking`](https://github.com/angryelizar/resourceBooking) (Java 17, Spring Boot, H2):

```dockerfile
FROM maven:3.9.9-eclipse-temurin-17 AS build
WORKDIR /app
COPY pom.xml .
RUN mvn -B -q dependency:go-offline
COPY src ./src
RUN mvn -B package

FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /app/target/ResourceBooking-1.0.jar app.jar
ENTRYPOINT ["java", "-jar", "app.jar"]
```

## Установка и запуск

```bash
uv sync
cp .env.example .env   # заполните ключи и BI_TARGET_REPO
gh auth status         # gh должен видеть Target Repository
```

**Веб-чат (Chainlit):**

```bash
uv run chainlit run src/bug_investigator/ui.py
```

Откройте http://localhost:8000, вставьте текст Bug Report (желательно со стек-трейсом) или ссылку на issue из Target Repository. На каждой паузе — кнопки «Одобрить», «Отклонить», «Доработать».

**Терминал:**

```bash
uv run bug-investigator "текст Bug Report"
uv run bug-investigator --issue https://github.com/<owner>/<repo>/issues/12
uv run bug-investigator --file bug.txt
cat bug.txt | uv run bug-investigator
```

Паузы отвечаются в терминале: `1` — одобрить, `2` — отклонить, `3` — доработать с комментарием. Код выхода 0 — Fix PR создан или подтверждён «Не баг», 1 — другой итог, 2 — ошибка конфигурации или ввода.

Паузы живут в памяти процесса (`InMemorySaver`): перезапуск прерывает Investigation.

## Конфигурация

Все переменные — в `.env` (шаблон — [`.env.example`](.env.example)). Переменные `BI_*` читает `Settings` ([`config.py`](src/bug_investigator/config.py)); ключи читают SDK провайдеров напрямую и никогда не попадают в state и трейсы.

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `BI_TARGET_REPO` | — (обязательна) | URL Target Repository для `git clone` |
| `BI_TARGET_BRANCH` | `main` | Ветка: от неё клонируется рабочая копия, в неё открывается Fix PR |
| `BI_PROVIDER` | `groq` | `groq` или `openai` |
| `BI_MODEL` | — (обязательна) | Модель провайдера, например `gpt-6.2-sol` или `openai/gpt-oss-120b` |
| `BI_TEMPERATURE` | не задана | Не задавайте для reasoning-моделей: они отклоняют параметр |
| `BI_MAX_TOOL_CALLS` | `15` | Вызовов инструментов на один заход агента |
| `BI_MAX_ATTEMPTS` | `3` | Попыток петли Planner → Critic, потом «агент сдался» |
| `BI_MAX_OUTPUT_CHARS` | `6000` | Лимит ответа одного инструмента (чтение, поиск) |
| `BI_MAX_RETRIES` | `4` | Повторы с растущей паузой при таймаутах, rate limit и сетевых сбоях |
| `BI_REQUEST_TIMEOUT_S` | `60` | Таймаут запроса к модели |
| `BI_BUILD_TIMEOUT_S` | `300` | Таймаут одной `docker build` |
| `OPENAI_API_KEY` / `GROQ_API_KEY` | — | Ключ выбранного провайдера |
| `LANGSMITH_TRACING` | — | `true` — трейсы в LangSmith |
| `LANGSMITH_API_KEY` | — | Ключ LangSmith |
| `LANGSMITH_PROJECT` | — | Проект LangSmith, например `bug-investigator` |
| `GH_TOKEN` | — | Необязательно: токен для `gh` вместо `gh auth login` |

Трейсы называются по Bug Report («Investigation #1: …», «… · после Plan Approval») и группируются в LangSmith по Investigation (Threads).

## Разработка

```bash
uv run pytest          # тесты графа, Executor, инструментов, git и GitHub — без модели и Docker
uv run pyright         # типы
uv run ruff check .    # линтер
```

Задачи — в [`.scratch/bug-investigator/issues/`](.scratch/bug-investigator/issues), по одной на файл.
