# 06 — Проверка Red: Reproduced / Unreproduced

Status: ready-for-agent
Blocked by: 02

Первый кандидат на выброс, если не хватает времени: тогда все Fix — Unreproduced.

## Готово, когда

- [ ] Executor собирает сборку A (только файлы `src/test/**` из Fix) и сборку B (весь Fix)
- [ ] `reproduced` = A упала и B прошла
- [ ] Reproduced/Unreproduced виден на Fix Approval и в описании Fix PR
