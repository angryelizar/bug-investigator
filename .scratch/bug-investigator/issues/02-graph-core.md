# 02 — Ядро графа в терминале

Status: ready-for-agent
Blocked by: —

Граф без пауз запускается из терминала на одном баге и доходит до проверенного Fix в рабочей копии.

## Готово, когда

- [ ] Конфиг: Target Repository, ветка, провайдер и модель (Groq / OpenAI), лимиты; секреты из переменных окружения
- [ ] State по спецификации, без секретов
- [ ] Узлы: Подготовка, Analyst (пока только «Подтверждено»), Planner, Coder, Executor (только сборка B), Critic; петля Critic → Planner, максимум 3 попытки
- [ ] Инструменты: `fff-search` (`grep`, `find_files`), `FileManagementToolkit` (`list_directory`, `read_file`, `write_file` только у Coder), с ограничением объёма ответа
- [ ] `recursion_limit` на заход агента
- [ ] Трейсы видны в LangSmith с первого запуска
