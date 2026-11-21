# Verification — это `docker build` Target Repository

Механическая часть Verification — одна команда `docker build` в рабочей копии Target Repository. Dockerfile обязан компилировать проект и прогонять тесты (для Maven — `mvn package` без `-DskipTests`), поэтому «образ собрался» означает «скомпилировалось и тесты прошли». Так агенту безразличен язык Target Repository, а у Executor ровно один инструмент и один сигнал: код возврата и хвост лога.

## Consequences

- Любой Target Repository обязан иметь такой Dockerfile; репозиторий без него агент проверить не может.
- Тесты должны работать без внешних сервисов внутри `docker build` (например, H2 в памяти вместо Postgres).
- Если на исходном коде уже падают тесты, не связанные с Bug Report, любой Fix не пройдёт Verification.
- Код и Dockerfile Target Repository исполняются локальным Docker, поэтому разрешены только репозитории из конфигурации стенда.
