# 04 — Паузы, вход по issue и Fix PR

Status: ready-for-agent
Blocked by: 02

## Готово, когда

- [ ] `InMemorySaver` + `interrupt` для Plan Approval, Fix Approval и подтверждения «Не баг»
- [ ] Ответы «Одобрить» / «Отклонить» / «Доработать с комментарием» и их переходы
- [ ] Вход по ссылке на issue: заголовок и текст, только из Target Repository
- [ ] Публикация: коммит, push ветки `bug-investigator/<…>`, Fix PR в `main` с описанием по шаблону и `Fixes #N`
- [ ] Комментарий в issue для «Не баг» после Approval
- [ ] Временная рабочая копия (`bug-investigator-*` во временной папке) удаляется после финала Investigation; сейчас остаётся после каждого запуска
