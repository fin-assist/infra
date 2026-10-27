# Один VPS для stage (FIN-149)

**Состояние:** подготовка. 27.10.2026 владелец сообщил, что VPS готов; данные подключения ожидаются.
Пока не проверены адрес/домен, установленный PostgreSQL, бакет, ключи и результаты выкладки.
Примеры не являются production-конфигурацией и не подтверждают функциональную готовность FIN-354.

Stage: systemd, nginx, один PostgreSQL18 с четырьмя базами/ролями, пять реальных сервисов,
fake-phone (только синтетические номера) и fake-llm. Для первого запуска используются синтетические данные.
Расчёты и хранение настоящие; внешняя модель и звонки заменены фейками. K3s/Argo/production — отдельные задачи.

## Адреса и ресурсы

Один публичный IPv4, DNS A домена stage на него. Открыты только HTTPS443, HTTP80 для ACME/редиректа
и ограниченный SSH. PostgreSQL5432, все gRPC, operational HTTP и fake-llm доступны только на loopback.
У nginx нет другого доверенного upstream-прокси: он заменяет X-Forwarded-For на $remote_addr.
BFF доверяет только 127.0.0.1/32. При добавлении внешнего прокси эту цепочку нужно пересмотреть вместе.

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
   ENV=stage и AUTH_MODE=verify сохраняются. Ни dev-токены, ни reflection на stage не включать.
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

IAM-токен обновляется **вне сервиса** атомарной заменой root0600 файла и рестартом bank-statement-data
до истечения срока: LoadCredential делает неизменяемый снимок на старте. Установить внешний timer/agent
с учётными данными service account, обработкой ошибки обновления и сигналом оператору.
Такого cloud-аккаунта здесь пока нет, поэтому таймер/выдача и проверка прав не выполнены.
Документация и примеры не доказывают безопасную настройку бакета.

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
Старый процесс до restart продолжает использовать прежний бинарь. У fake-llm — проверка is-active.
После успешного запуска включить нужные units на загрузку ОС; настройки/секреты сами не копируются скриптом.

**Откат:** только на совместимую со схемой версию. Автоматическое восстановление прежнего symlink при
сбое установки/health не откатывает БД; down-миграции не запускаются. Это безопасно лишь при принятом
двухэтапном правиле миграций. Перед несовместимой схемой этот сценарий не применять: отдельный план.
Сохранить прежний проверенный set и artifacts, затем выполнить deploy с ним для нужного сервиса.
Скрипт сверяет уже установленный бинарь с checksum. previous/SERVICE хранит прежний путь для диагностики;
источник ручного отката — проверенный набор, а не произвольная ссылка. Если первого рабочего релиза нет,
после ошибки health сначала снимается текущая ссылка, затем останавливается unit; ошибка stop не возвращает выбор неуспешного релиза. Сбой самой миграции вообще не выбирает новый релиз. Не удалять старые releases/секреты в этой задаче.

## Что подтвердить на настоящем хосте

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
VPS/runtime эти проверки не поднимают.

## Источники units и правила nginx

Базовые units взяты из main27.10.2026: user9081335, bank-statement-data86e64c4, analytics5894f48,
assistant7303448, bank-bff255d0ba. В infra добавлены адреса/лимиты и oneshot-миграции; fake unit/drop-in —
из FIN-149 assistant. При обновлении сервисных units сверять credential/config изменения до rollout.
Точные настройки и deployment-ограничения: README/docs/config.md/docs/import.md соответствующих сервисов.
[nginx proxy](https://nginx.org/en/docs/http/ngx_http_proxy_module.html),
[HTTP/2](https://nginx.org/en/docs/http/ngx_http_v2_module.html).
