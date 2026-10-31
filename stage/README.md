# Один VPS для stage (FIN-149)

**Состояние:** подготовка. SSH-доступ к VPS проверен 27.10.2026, изменений на хосте не было.
Параметры подключения, выбранный домен и результаты осмотра сохраняются локально; здесь остаётся типовая конфигурация.
Предварительные read-only проверки IAM/KMS/бакета выполнены отдельно; объектные записи, ключи сервисов
и фактическая выкладка этим репозиторием не подтверждаются. Облачные идентификаторы здесь не публикуются.
Примеры не являются production-конфигурацией и не подтверждают функциональную готовность FIN-354.

Stage: systemd, nginx, один PostgreSQL18 с четырьмя базами/ролями, пять реальных сервисов,
fake-phone (только синтетические номера) и fake-llm. Для первого запуска используются синтетические данные.
Расчёты и хранение настоящие; внешняя модель и звонки заменены фейками. K3s/Argo/production — отдельные задачи.

## Адреса и ресурсы

Один публичный IPv4, DNS A домена stage на него. Открыты только HTTPS443, HTTP80 для ACME/редиректа
и ограниченный SSH. PostgreSQL5432, все gRPC, operational HTTP и fake-llm доступны только на loopback.
У nginx нет другого доверенного upstream-прокси: он заменяет X-Forwarded-For на $remote_addr.
BFF доверяет только 127.0.0.1/32. При добавлении внешнего прокси эту цепочку нужно пересмотреть вместе.
В env BFF обязательны адреса всех четырёх соседей, включая `ANALYTICS_GRPC_ADDR`
для экрана аналитики и периодов; актуальный пример указывает на `127.0.0.1:9093`.

| Сервис | gRPC / публичный HTTP за nginx | Operational HTTP |
|---|---|---|
| user | 127.0.0.1:9090 | 127.0.0.1:19091 |
| bank-statement-data | 127.0.0.1:9092 | 127.0.0.1:19092 |
| analytics | 127.0.0.1:9093 | 127.0.0.1:19093 |
| assistant | 127.0.0.1:9094 | 127.0.0.1:19094 |
| bank-bff | 127.0.0.1:8080 | 127.0.0.1:9091 |
| fake-llm | 127.0.0.1:8092 | — |

Примеры systemd ограничивают каждый основной сервис MemoryHigh512M / MemoryMax1G и CPUQuota200%.
Это начальные предохранители, а не измеренный расход или обещание вместимости: на стенде измерить RSS,
пулы и пиковые импорты/пересчёты. Не складывать MemoryMax как уже зарезервированную память.
8ГБ — начальная оценка для пяти сервисов; при росте до15 сервисов владелец рассматривает16ГБ.
Сборки выполняются в CI, на VPS доставляются бинарные артефакты.

## Подготовка хоста

1. Linux с systemd LoadCredential/DynamicUser, nginx с HTTP/2 (проверяется nginx -t), PostgreSQL18.
   Создать четыре отдельные LOGIN-роли/БД: user, bank-statement-data, analytics, assistant.
   Сервисы не получают superuser/доступ к чужим БД. PostgreSQL слушает localhost; pg_hba — SCRAM.
   SQL и размеры пулов проверить владельцу до применения. Примеры env ограничивают обычные пулы8,
   user дополнительно auth4 и outbox6. Настройку max_connections согласовать с суммой всех пулов и резервом.
2. Зафиксировать SSH, firewall, DNS, время/NTP, диск и бэкап. Минимум перед первой миграцией сохранить
   проверяемый pg_dump каждой БД в закрытом хранилище; до реальных данных согласовать расписание/retention
   и проверить восстановление. Конкретная политика бэкапа пока не принята — не считать её настроенной.
3. /etc/finassist — root0755, общий stage-public.jwks — root0644; каталоги сервисов — root0700,
   секреты — root0600. Приватные ключи Ed25519 PKCS#8 по одному на сервис; общий JWKS содержит
   kid=sub имени каждого сервиса, issuer finassist-stage, audience finassist-internal.
   Ключ user access JWT отдельный. Два AES-256 raw-ключа user (auth-idempotency и phone-result)
   различны; сохранить на сроки из docs/config.md user. Не генерировать секреты в Git/терминальный отчёт.
4. Скопировать env/*.example в защищённые /etc/finassist/SERVICE/env и заполнить домен/issuer,
   минимальную версию клиента, bucket/KMS. Это обычный systemd EnvironmentFile, без shell-команд,
   кавычек/экспортов. ENV, identity и *_FILE, которые задаёт unit/drop-in, туда не добавлять:
   EnvironmentFile перекрывает Environment. `python3 scripts/check_env.py SERVICE /etc/finassist/SERVICE/env`
   отказывает при таком перекрытии, placeholder, повторах и прямом DSN.
5. Источники credential-файлов перечислены в systemd/*.service. Скопировать units и assistant.service.d
   в /etc/systemd/system, проверить systemd-analyze verify, затем daemon-reload. Unit-миграции сохраняют
   EnvironmentFile и LoadCredential сервиса, но запускают migrate up как oneshot до рестарта.
   Миграции не исполняются для stateless BFF и fake-llm.
6. Подключить fake-llm.service и assistant.service.d/fake-llm.conf из исправления FIN-149 assistant.
   Требуется непустой **синтетический** /etc/finassist/assistant/fake-model-key (root0600).
   ENV=stage и AUTH_MODE=verify сохраняются. Ни dev-токены, ни reflection на stage не включать. Drop-in использует Wants+After: отказ/остановка фейка не должны останавливать историю, лимит и purge всего assistant.
   user PHONE_PROVIDER=fake принимает только синтетические номера; платный провайдер не вызывается.
7. nginx/finassist.conf установить в http context после замены stage.example.invalid и получения TLS-сертификата.
   Сначала HTTP80/ACME, затем включить TLS-конфигурацию; nginx -t до reload. Пример использует совместимый
   с nginx1.24 синтаксис listen ssl http2 (на новых nginx допустимо предупреждение о deprecated syntax).
   SSE без buffering/cache; три маршрута имеют отдельные таймауты, upload —10МиБ+64КиБ envelope,
   streaming request без дисковой буферизации. Остальные пути не выставляют operational endpoints.
   Access log отключён, чтобы не сохранять токены и идентификаторы из query/path. Для диагностики —
   безопасные логи BFF и journald, без секретов/тел запросов. Реальный webhook отключён при fake-phone;
   zvonok.location.conf.disabled — только заготовка для отдельно принятой активации.

## Приватный Object Storage и IAM

До запуска bank-statement-data нужен отдельный private Yandex Object Storage stage в РФ, KMS и IAM.
По текущему docs/import.md сервиса: versioning никогда не включался (Suspended тоже запрещён),
Object Lock выключен, нет expiration-правил, стирающих пустые защитные маркеры. Сервис проверяет три
настройки до listener; приватность и IAM проверяет оператор. Аккаунту доступны объектные операции,
чтение bucket versioning/lifecycle/object-lock и нужный KMS, но запрещено менять эти защитные настройки.
Точные права/приватность проверить на реальном бакете и записать результат без токенов.

IAM-токен обновляется **вне сервиса**: `scripts/refresh_iam.py` и
`systemd/finassist-iam-bootstrap.service`, `finassist-iam-refresh.service` / `.timer`.
Это подготовленные файлы, на хосте они ещё не установлены.
Python3 stdlib и `/usr/bin/openssl` формируют PS256 JWT на5мин из authorized JSON RSA-ключа SA;
обмен идёт только на `https://iam.api.cloud.yandex.net/iam/v1/tokens` с системным TLS trust store,
без proxy и redirect. [Формат JWT](https://yandex.cloud/en/docs/iam/operations/iam-token/create-for-sa)
и [ответ IAM](https://yandex.cloud/en/docs/iam/api-ref/IamToken/create) — официальные контракты Yandex Cloud.
Ответ ограничен64КиБ; непустой однострочный токен принимается только с валидным expiresAt и запасом
не менее5ч (не более13ч). Timer срабатывает через4ч после включения и затем каждые4ч (+до90с jitter/accuracy),
а не каждые30мин: каждый restart обрывает текущие импорты/Watch. HTTP — не более3 попыток с паузами5/10с;
таймауты crypto10с, HTTP15с на операцию/ограниченное чтение, systemctl60с; oneshot ограничен280с.
После одного неудачного четырёхчасового цикла нельзя откладывать реакцию ещё на полный цикл:
минимальный принимаемый TTL оставляет около1ч запаса после очередного срока обновления.

Установка после отдельного разрешения владельца:

1. Установить проверенный checkout infra в root-owned `/opt/finassist-infra` (0755, без записи другими),
   включая `scripts/refresh_iam.py`. JSON-ключ SA хранить только в
   `/etc/finassist/iam-refresh/authorized-key.json` (root0600, каталог root0700).
   BSD этот долгоживущий ключ не получает: oneshot читает его через LoadCredential.
   Не добавлять JSON, IAM/JWT, значения переменных или идентификаторы аккаунта в Git, Actions и отчёты.
   **До первого deploy** установить bootstrap/refresh `.service` и `.timer`, сделать daemon-reload;
   timer пока не включать. BSD unit требует bootstrap, поэтому нельзя копировать только BSD unit.
2. **До первого deploy BSD** создать первичный токен, без рестарта ещё не установленного бинаря:

   ```sh
   sudo install -d -m 0700 /etc/finassist/bank-statement-data
   sudo systemctl start finassist-iam-bootstrap.service
   ```

   Bootstrap вызывает `initial`, только подготавливает credential и не активирует BSD.
   RemainAfterExit сохраняет успешный bootstrap до следующей загрузки, не обновляя token второй раз при deploy.
   Сначала создать первый токен, затем обычная миграция/deploy BSD и его readiness.
3. После успешного первого deploy проверить один управляемый цикл
   `systemctl start finassist-iam-refresh.service` в согласованное окно,
   проверить `/readyz`, затем `systemctl enable --now finassist-iam-refresh.timer`.
   Первый таймер не запускается немедленно. При reboot (в том числе после простоя >12ч) BSD ждёт
   `Requires`+`After` bootstrap: новый source token появляется **до** загрузки LoadCredential BSD.
   Цикла зависимостей нет: bootstrap не требует и не запускает BSD; DB-migrate от облака не зависит.
   При остановленном BSD recurring всё равно обновляет source token, но не активирует BSD.
   При crash BSD повторяет serve через30с без StartLimit; после обновления token следующий старт может
   восстановиться. Явный `systemctl stop bank-statement-data` отменяет эти автоматические рестарты.
   Если сам bootstrap исчерпал bounded HTTP retry или не смог подписать JWT, зависимый BSD не стартует:
   ошибка dependency **не** гарантирует on-failure retry BSD. После устранения причины оператор выполняет
   `systemctl reset-failed finassist-iam-bootstrap.service bank-statement-data.service`, затем
   `systemctl start bank-statement-data.service`; он заново потребует bootstrap. Полная автоматическая
   реанимация после провала boot dependency не заявляется; timer обновляет файл, не обходит намеренный stop.
4. Контролировать `systemctl list-timers finassist-iam-refresh.timer`, failed-state oneshot и безопасный
   journald; подключить failed-state к операторскому мониторингу перед автономной работой.
   Ошибка не маскируется успехом: unit возвращает ненулевой код, сообщения не содержат ключей,
   JWT, IAM-ответа или provider error body. На настоящем хосте проверить успешное обновление и обработку отказа.

Скрипт требует root, доверенные каталоги без writable предков/symlink и owner-only regular files;
новый token записывается root0600 во временный файл того же каталога, fsync выполняется до atomic rename.
Параллельные ручные/timer запуски исключены flock. При sign/HTTP/write ошибке старый token сохраняется.
LoadCredential BSD делает неизменяемый снимок, поэтому **после** замены для изначально активного BSD
нужен `try-restart`: он не поднимает сервис, остановленный во время обмена. Затем проверяется active-state.
Если BSD был неактивен, файл обновлён, но никакая команда активации не исполняется.
Если рестарт не удался, новый source token сохраняется, unit failed; другие credential-файлы не меняются.
Health после рестарта и сквозная приёмка остаются отдельными проверками; active-state их не заменяет.
GET настроек бакета тоже не доказывает PUT/KMS: до приёмки проверить синтетический файл и пустой
зашифрованный fence теми же SSE-KMS headers, что production `objectstore.Store`.

## Получение набора и выкладка

FIN-148 должен быть влит, а post-merge CI — успешен. Заполнить set.example.json реальными40-символьными
SHA и Actions run/attempt каждого сервиса; fake-llm использует те же SHA/run/attempt, что assistant.
Шаблон с placeholder намеренно не проходит проверку. Сохранить заполненный set как согласованный набор.

```sh
python3 scripts/stage.py fetch /secure/stage-set.json /secure/artifacts-new
python3 scripts/stage.py verify /secure/stage-set.json /secure/artifacts-new
```

Fetch проверяет через авторизованный gh API: repo, path CI, headSHA, main, push, success, attempt.
Он скачивает только именованный artifact и сохраняет run.json. Verify сверяет эти данные с manifest,
checksum и платформой, требует одну версию contracts/gen/go у пяти сервисов. Совпадение версии контракта
само по себе совместимость не доказывает: нужна приёмка FIN-362. run.json и manifest — сохранённая
проверка доверенного источника, а не криптографическая подпись: после fetch каталог/set должны быть
доступны на запись только оператору, затем перенесены в root0700 staging VPS.
Не выполнять неизвестные файлы до проверки. Исполняемые права восстанавливаются install-логикой.

После подготовки секретов/units/бакета и проверки обратной совместимости миграций:

```sh
# Команды выполняются на VPS вручную из корня проверенного checkout infra.
sudo python3 scripts/stage.py deploy /secure/stage-set.json /secure/artifacts-new --service user
# Далее: analytics, bank-statement-data, fake-llm, assistant, bank-bff (по одному).
```

Перед первым запуском заполнить весь набор и все env; службы начнут связываться после появления соседей.
Каждый запуск проверяет весь набор до изменения хоста, блокирует параллельный deploy через flock,
устанавливает immutable releases/SERVICE/SHA. Миграция запускается из нового immutable пути через временный runtime drop-in; рабочий symlink /opt/finassist/SERVICE остаётся прежним до её успеха, включая автоматические рестарты systemd. Затем ссылка переключается атомарно.
Для четырёх сервисов systemctl start SERVICE-migrate выполняется раньше restart, затем readyz с bounded retry.
Старый процесс до restart продолжает использовать прежний бинарь. У fake-llm после is-active —
bounded TCP readiness на127.0.0.1:8092 (до20 попыток, connect2с, пауза1с); ошибка запускает тот же rollback.
После успешного запуска включить нужные units на загрузку ОС; настройки/секреты сами не копируются скриптом.

**Откат:** только на совместимую со схемой версию. Автоматическое восстановление прежнего symlink при
сбое установки/health не откатывает БД; down-миграции не запускаются. Это безопасно лишь при принятом
двухэтапном правиле миграций. Перед несовместимой схемой этот сценарий не применять: отдельный план.
Сохранить прежний проверенный set и artifacts, затем выполнить deploy с ним для нужного сервиса.
Скрипт сверяет уже установленный бинарь с checksum. previous/SERVICE хранит прежний путь для диагностики;
источник ручного отката — проверенный набор, а не произвольная ссылка. Если первого рабочего релиза нет,
после ошибки health сначала снимается текущая ссылка, затем останавливается unit; ошибка stop не возвращает выбор неуспешного релиза. Сбой самой миграции вообще не выбирает новый релиз. Не удалять старые releases/секреты в этой задаче.

## Что подтвердить на настоящем хосте

Подготовка сквозной HTTP/SSE-приёмки FIN-362: [runner, synthetic fixtures и протокол доказательств](acceptance/README.md).
Его базовый smoke не подтверждает полную приёмку; оставшиеся обязательные сценарии отмечены явно.

- Версии ОС/PG18/nginx/systemd, SHA каждого бинаря, shared JWKS и проверка service/user identity.
- Env не перекрывает unit; ss -ltn не показывает внешний PG/gRPC/operational/fake listener.
- BFF IPAddressAllow/Deny реально работает (cgroup eBPF и parent slices — инструкция в его unit).
- TLS/HTTP2 и SSE без буферизации; настоящий клиентский IP; upload на границе10МиБ и ошибки контракта.
- Миграции, каждый readyz, перезапуск, откат на предыдущий совместимый SHA и сохранность данных.
- Приватность/KMS/IAM/настройки бакета, token refresh; бэкап и восстановление.
- Сквозной API-путь и синтетические сценарии — FIN-362, не заменяются этим infrastructure smoke.

Локальные Python-тесты покрывают provenance/refusals/checksum, symlink предков, immutable-путь миграции и сохранение прежней ссылки до успеха, порядок миграции/рестарта,
неуспешный первый запуск и восстановление прежней ссылки. Linux CI проверяет nginx -t и синтаксис units
(systemd-analyze с подставленным /usr/bin/true: исполняемые бинарные пути CI не устанавливает).
Неизвестные key/lvalue/section в диагностике считаются ошибкой даже при exit0.
Тот же Linux entrypoint реально выполняет `systemd-analyze condition` с directive из recurring IAM unit:
`ConditionFileIsExecutable` проходит для executable regular file и отказывает для отсутствующего файла,
файла без executable bit и каталога. Это проверка deployment guard, без запуска сервисов.
VPS/runtime эти проверки не поднимают.

## Источники units и правила nginx

Базовые units взяты из main27.10.2026: user9081335, bank-statement-data86e64c4, analytics5894f48,
assistant7303448, bank-bff255d0ba. В infra добавлены адреса/лимиты, oneshot-миграции,
IAM bootstrap dependency и политика рестарта BSD; fake unit/drop-in —
из FIN-149 assistant PR51 (7edb92b). При обновлении сервисных units сверять credential/config изменения до rollout.
Точные настройки и deployment-ограничения: README/docs/config.md/docs/import.md соответствующих сервисов.
[nginx proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[HTTP/2](https://nginx.org/en/docs/http/ngx_http_v2_module.html).

## Ручная выкладка из GitHub Actions (FIN-410)

Workflow **Deploy stage** (`.github/workflows/stage-deploy.yml`) запускается только через
`workflow_dispatch` на `main` репозитория `fin-assist/infra`. Автоматического запуска после CI нет.
Наличие workflow не означает настройку доступа или выполненный deploy. В рамках PR FIN-410
Environment, секреты и VPS не меняются; первый фактический запуск требует отдельной команды владельца.

### Однократная настройка после merge

1. Создать GitHub Environment **stage**, разрешить deployment только из ветки `main`
   (Selected branches and tags → branch `main`, без разрешённых tags). При необходимости включить
   required reviewers. Не ослаблять защиты ветки. У workflow есть дополнительный main/repository guard.
2. В этом Environment сохранить секреты:

   | Секрет | Назначение |
   |---|---|
   | `STAGE_ARTIFACTS_TOKEN` | Fine-grained PAT или заранее полученный installation token: только Actions read и Metadata read в пяти сервисных репозиториях. Обычный GITHUB_TOKEN infra не даёт доступа к чужим private Actions artifacts. |
   | `STAGE_SSH_HOST` | DNS-имя или IPv4 VPS, без URL, логина и shell-синтаксиса. |
   | `STAGE_SSH_KEY` | Отдельный приватный SSH-ключ только для stage deploy. |
   | `STAGE_SSH_KNOWN_HOSTS` | Заранее проверенная строка known_hosts; при нестандартном порте имя имеет вид `[host]:port`. |

   Переменная Environment `STAGE_SSH_PORT` необязательна (по умолчанию22). Fingerprint проверяется
   по независимому доверенному каналу; `ssh-keyscan` во время запуска не используется.
   Не передавать секреты через workflow inputs, set JSON или комментарии.
3. После отдельного разрешения установить **проверенный merged checkout infra** в root-owned
   `/opt/finassist-infra`, без записи другими. Обязательны `scripts/stage_remote.py`, `stage.py`,
   `check_env.py` и `stage/systemd/`. Не перезаписывать конфигурацию и credentials действующего stage.
   Runner передаёт SHA-256 трёх Python-файлов; несовпадение с предустановленными файлами останавливает
   запуск до миграции. После изменения этих файлов обновлять endpoint отдельно из проверенного main.
4. Установить публичный deploy-ключ в root authorized_keys с ограничением:

   ```text
   restrict,command="python3 /opt/finassist-infra/scripts/stage_remote.py" ssh-ed25519 PUBLIC_KEY stage-deploy
   ```

   Ключ не должен разрешать shell, forwarding или PTY. SSH обязан допускать этот forced command
   для root; существующий административный доступ не менять и общий root password login не включать.
   Обработчик принимает только bundle с данными; исполняемые скрипты по SSH не доставляются.
   Выделенный ключ даёт право выкладывать бинарные артефакты на stage: ограничить доступ к Environment
   и право изменять main соответственно. Происхождение через GitHub API проверяет runner, VPS проверяет
   сохранённое evidence и checksum; это доверенный канал оператора, не подпись от независимого издателя.
5. Проверить запас диска для временного полного набора (до800МиБ), настроенные units/env,
   резервную копию и совместимость миграций. Endpoint требует Linux root, создаёт private staging
   и `/var/lib/finassist-deploy/reports/`; credentials сервисов не уходят с VPS.

### Запуск

Открыть Actions → **Deploy stage** → **Run workflow**, выбрать `main`, `selection` и вставить
**полный** заполненный JSON по `stage/set.example.json` в `stage_set`. На каждый компонент обязательны
точные SHA, run_id и run_attempt; `latest`, название ветки и нулевые placeholders не поддерживаются.
Выбирать run последнего успешного **полного** CI push/main: проверяются итог run и отдельные
завершённые успешные gates `go`, `lint`, `image` именно указанной попытки. Re-run, начавшийся во время
скачивания, отклоняется повторной проверкой run; manifest дополнительно фиксирует attempt.
Историческая попытка после нового re-run не выбирается: нужен актуальный полный успешный attempt.
Никакой сборки при deploy нет.

`selection=all` выполняет user → analytics → bank-statement-data → fake-llm → assistant → bank-bff.
Выбор assistant **или** fake-llm всегда выкладывает пару fake-llm → assistant из одного run.
Остальные значения выбирают один сервис. Всегда скачивается и проверяется весь набор из шести
артефактов; у пяти Go-сервисов должна совпадать версия `contracts/gen/go`. На VPS невыбранные
компоненты должны **уже** иметь SHA/run/attempt из этого набора, с проверенным checksum установленного
бинаря. Поэтому изменение общей версии contracts требует полного набора, а частичный запуск
не выдаёт согласованный входной JSON за проверенную совместимость с другими установленными версиями.
До первой миграции проверяются также env всех выбранных сервисов.

GitHub concurrency не отменяет работающий deploy при новом запуске; pending-запуски могут заменяться
по правилам Actions. VPS держит тот же `/run/finassist-deploy.lock`, что ручной `stage.py`, **на весь набор**,
включая получение bundle, preflight, миграции и health. Занятый lock завершает запуск ошибкой без deploy.
Одна операция ограничена30мин на VPS, SSH —2000с, внешний процесс —2200с, job —40мин.

### Отчёт и восстановление

Artifact `stage-deploy-report-RUN_ID-ATTEMPT` хранится30дней и содержит только pins, before/after,
результат preflight, этапы сервисов и rollback. Бинарь, env, host, ключи, raw SSH/systemd output и
пользовательские данные не публикуются. На VPS сохраняется root-only JSON
`/var/lib/finassist-deploy/reports/RUN_ID-ATTEMPT.json`; идентификатор — **run infra**, не сервисного CI.
Повторная доставка с тем же run/attempt отклоняется, не перезапуская deploy.

При ошибке миграции ссылка остаётся старой. При restart/readiness failure действует существующий
откат бинаря; `previous-restarted-unverified` означает восстановленную ссылку и успешную команду
рестарта, **без доказательства readiness** старой версии. `failed` означает ошибку отката; требуется
проверка оператора. `after` фиксирует checksum-проверенную выбранную ссылкой версию на диске,
а не независимое доказательство версии уже работающего процесса. Успешный шаг требует readiness.
БД автоматически не откатывается. После первой ошибки остальные сервисы не выкладываются:
в многошаговом наборе ранее успешные сервисы остаются обновлёнными. Весь набор не является
атомарной транзакцией; для восстановления нужен сохранённый согласованный комплект и совместимая схема.

При разрыве SSH, отмене Actions или ошибке транспорта результат удалённой операции может быть
**неизвестен**. Endpoint игнорирует SSH SIGHUP и сохраняет результат после bounded операции, но авария
ОС/убийство процесса этого не гарантирует. Не повторять вслепую: сначала дождаться освобождения lock,
проверить root-only отчёт отдельным административным каналом, installed symlinks и readiness. Начальный
failed-отчёт без steps может означать прерванный запуск; он не доказывает отсутствие изменений.
При необходимости использовать предыдущий проверенный set и ручной deploy из раздела выше.
Файлы отчётов автоматически не удаляются; retention на VPS и закрытое хранение rollback artifacts
остаются обязанностью оператора. После настройки первый запуск и его runtime-результат фиксируются отдельно.
