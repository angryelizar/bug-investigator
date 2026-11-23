# 06 — Проверка Red: Reproduced / Unreproduced

Status: done
Blocked by: 02

Первый кандидат на выброс, если не хватает времени: тогда все Fix — Unreproduced.

## Готово, когда

- [x] Executor собирает сборку A (только файлы `src/test/**` из Fix) и сборку B (весь Fix)
- [x] `reproduced` = A упала и B прошла
- [x] Reproduced/Unreproduced виден на Fix Approval и в описании Fix PR
- [x] Промпты Planner и Coder: Reproduction компилируется на исходном коде (только существующий API — иначе сборка A падает на компиляции и Fix ложно Reproduced) и проверяет именно поведение из Bug Report
- [x] Coder добавляет новые тесты или тестовые методы, но не удаляет и не ослабляет существующие проверки; Critic это проверяет
