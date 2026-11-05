# infra

[Подготовка systemd-stage](stage/README.md), FIN-149: nginx/TLS/SSE, изолированные
loopback-порты, проверка артефактов FIN-148, миграции, откат бинаря и внешний IAM refresh.

SSH-доступ проверен; параметры подключения и результаты осмотра хранятся локально и в репозиторий не публикуются. Репозиторий содержит подготовленный комплект;
он не подтверждает фактическую выкладку или сквозную приёмку FIN-362.
Production/k3s/Argo CD сюда пока не входят.

Ревью Claude (FIN-412): `.github/workflows/claude.yml` реагирует на намеренное упоминание
`@claude` в комментарии или ревью открытого PR из этого же репозитория. Перед job с правами
записи отдельный read-only job проверяет актуальный origin без checkout; fork PR и обычные
issues не запускают Claude. Модель — Claude Opus 5.5, effort high; правила из `AGENTS.md`
подключены через `CLAUDE.md`. Нужны Claude GitHub App и доступный репозиторию секрет
`CLAUDE_CODE_OAUTH_TOKEN`. Секреты Environment `stage` этому workflow не передаются.
Workflow начинает работать после merge в `main`; наличие файла не подтверждает успешный запуск.
Регрессионные тесты этого gate исполняют фактический JavaScript из workflow: для локального
`python3 -m unittest discover -s scripts -p 'test_*.py'` нужны Python3 и Node.js в PATH; npm-пакеты не требуются.

## Профильное ревью (FIN-421)

Перед ревью применяются [профильные навыки](docs/review-skills/README.md) из проверяемой ревизии.
Обязательные требования, выбор и ограничения — в `AGENTS.md`, раздел `Code Review Rules`.

## Обновления зависимостей

[Политика Renovate](docs/renovate-policy.md), FIN-447/FIN-211: отдельный main-only контроллер
проверяет подписанные ботские commits, актуальную цель и все обязательные checks (`stage`
и оба policy jobs), сливает допустимые обновления без обхода защиты и даёт один retry красного CI.
Настройка поставляет безопасный disabled gate; bootstrap, защиты и активация — отдельные действия
владельца. Контроллер не выполняет stage deployment и не получает Environment `stage`.
