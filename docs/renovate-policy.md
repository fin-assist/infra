# Автоматизация Renovate (FIN-447 / FIN-211)

Mend Renovate создаёт обновления, а `.github/workflows/renovate-policy.yml` применяет
общую политику FIN-211 через GitHub REST API без административного обхода защиты.
`automerge: false` в `renovate.json` намеренно сохраняется: native auto-merge не используется.

## Границы и обязательные проверки

Контроллер исполняет только код default branch `main`, не checkout PR и не artifacts/cache
от чужого workflow. Точное значение `RENOVATE_POLICY_ENABLED=true` пропускает непривилегированный
shell gate; unset/false/TRUE/True не допускают вход в Environment и не требуют credential.
Disabled CLI прекращает работу до чтения config/event и любых API-запросов, включая синхронизацию
baseline. Сам hosted Renovate продолжает создавать обновления по сохранённым presets.

Цель ровно одна: самая новая активная `release/X.Y.Z`, иначе `main`. Ветка завершена только
при наличии и смёрженного PR release → main этого репозитория, и подтверждённого предка main.
Перед merge цель, открытый PR, полный набор коммитов, head SHA и актуальные run/attempt читаются
повторно. Merge передаёт ожидаемый SHA; смена целевой ветки между последним чтением и merge
не является атомарной операцией GitHub. Устаревший PR не получает отложенного права на слияние.

`.github/renovate-policy.json` задаёт `ciWorkflow: stage.yml`: главный CI — **Stage configuration**,
required job `stage`. Это проверка конфигурации, **не** `stage-deploy.yml` и не deployment.
Дополнительно обязательны `Renovate policy tests` и `Renovate runtime contract` отдельного workflow.
События от обоих validation workflows и получасовая сверка запускают reconciliation;
ручной запуск — из trusted main. Секреты и Environment `stage` контроллеру не передаются.
Без `ciWorkflow` общий runtime сохраняет прежний default `ci.yml`.

Нужны завершённые успешные обязательные jobs текущего head SHA, плюс все дополнительные native
required checks/statuses. Отсутствующие, pending, skipped, cancelled и конфликт блокируют merge.
Проверки разных workflow и попыток не подменяют друг друга. Ошибка одного PR не скрывает остальные:
контроллер продолжает сверку и завершает run ошибкой с номерами проблемных PR без секретов.

## Только полностью ботские обновления

PR создан проверенным Renovate ID29139614/login `renovate[bot]`, head в том же репозитории,
ветка `renovate/`. Проверяется полный список до250 коммитов: author ID, действительная подпись
и trusted signer/committer. Человеческий коммит переводит PR в ручное сопровождение.
Название ветки, label и редактируемый PR body сами по себе не дают полномочий.
Структурированные metadata берутся из подписанного head commit Renovate; старый PR без metadata
остаётся владельцу. Настройка `commitBody` добавляет эти данные в новые Renovate commits.

Существующий baseline infra сохраняет ровно `config:recommended`: группировки, расписания и версии
не меняются данным внедрением. Разрешённые обновления, включая major/GitHub Actions, проходят общие
guards FIN-211. Обновлённый action после merge считается доверенным кодом default branch; main-only
Environment ограничивает controller job, но organization secret доступен и другим workflows в своей
области. Это принятая FIN-211 граница доверия. В infra нет Go compiler/toolchain или AndroidX/Compose
зависимостей; общий контроллер сохраняет ручное исключение AndroidX/Compose и смешанных групп.

## Один повтор и карантин

Только завершённый failure обязательного CI даёт один автоматический повтор упавших jobs.
До запроса rerun сохраняется receipt по PR+SHA в защищённом `state.json` с optimistic file SHA;
повтор события и удаление комментария бюджет не восстанавливают. Перед retry/close заново проверяются
авторство, SHA, attempt и цель. Подтверждённый повторный failure закрывает полностью ботский PR
с одним пояснением: SHA, упавшие checks, ссылки, результат повтора и способ повторить обновление.
Pending/skipped/cancelled/отсутствующий CI, конфликт или человеческие коммиты не дают автозакрытие.
Неизвестный результат dispatch сохраняет расходованный бюджет и требует проверки Actions владельцем.

После закрытия generated `policy.json` требует Dashboard approval для точного failed значения/digest.
Recovery ограничен зависимостью/datasource/major (для digest — tag): новая исправленная версия
остаётся рассматриваемой, точная неудачная версия не пересоздаётся бесконечно. Глобальный
`recreateWhen=always` не включается. В этой ограниченной recovery family ручное закрытие может
вызвать новый PR: окончательное отклонение — точный quarantine либо reviewable удаление recovery rule.
Для явной новой попытки владелец использует Dependency Dashboard; ручной rebase сам по себе бюджет
старого SHA не сбрасывает. Старые receipts сохраняются; автоматического удаления истории нет.
Missing/corrupt state, CAS conflict и лимиты1000 правил/1MB receipts блокируют действия.

## Подготовка и отдельная активация

Этот PR поставляет код и проверки; он не подтверждает настройку защит или включение контроллера.
Права/активация выполняются владельцем отдельным шагом после merge и успешного main CI.

1. До merge `renovate.json` подготовить собственную config-only ветку `renovate-policy`:
   только `base.json` (точная копия `renovate-base.json` выбранной цели), `policy.json` (единственная
   цель и generated quarantine/recovery) и `state.json` (собственные receipts, bootstrap `{}`).
   Оба refs должны разрешаться: `local>fin-assist/infra:base#renovate-policy` и
   `local>fin-assist/infra:policy#renovate-policy`, именно baseline → generated policy.
   Чужие receipts/quarantine не переносить; существующую ветку bootstrap не перезаписывает.
   Явный `GITHUB_REPOSITORY=fin-assist/infra python3 scripts/renovate_policy.py --bootstrap`
   требует установленного `RENOVATE_POLICY_TOKEN` даже при disabled; секрет не печатать и не писать
   в историю команд. Hosted preset acceptance проверить отдельно настоящим resolver.
2. После разрешённого merge сверить strict protections `main` и `release/*` с App15368 для всех
   трёх required checks, `enforce_admins:true`, owner-only push (`controllerLogin=avtomon`, teams/apps
   пусты), без force/delete. `release/*` также запрещает создание matching веток другим actor.
   Защитить `renovate-policy` owner-only с enforce_admins и без force/delete. Существующие обычные
   защиты не ослаблять. Любые active/неизвестные inherited/repository rulesets блокируют runtime,
   пока их совместимость не подтверждена. В текущем аудите FIN-447 main infra не защищён;
   установка флага без setup не является готовой автоматизацией.
3. Существующий organization secret `RENOVATE_POLICY_TOKEN` сохраняется: новый PAT или перенос
   в Environment не требуются. Проверить доступ infra и права fine-grained PAT владельца
   controllerLogin: Contents/Pull requests/Issues/Actions/Workflows write,
   Administration/Commit statuses/Metadata read. Identity фактически проверяет runtime;
   installation token GitHub App этим контроллером не поддержан. Check-runs читает отдельный
   read-only `GITHUB_TOKEN` с `checks:read`; для merge/mutations он не используется, иначе
   последующие Actions события подавлялись бы.
4. Environment `renovate-policy` ограничить custom deployment branch policy ровно `main`, тип branch.
   Он не использует deploy credentials `stage`. Проверить отсутствие bypass и наличие baseline
   в активной release. Затем отдельным решением владельца поставить точное
   `RENOVATE_POLICY_ENABLED=true`, проверить текущий trusted-main run: `reconcile` должен
   **выполниться**, а не быть skipped. Общий success с skipped reconcile не доказывает активацию.
5. Проверить допустимый реальный подписанный Renovate PR и post-merge CI; отсутствие такого PR
   не доказывает merge/retry/close. Обычные PR/release → main по-прежнему требуют команды владельца.
   Фактическая выкладка stage, секреты SSH, UI-прогоны и теги остаются вне этой автоматизации.

## Проверки

Python policy/lifecycle/CLI/gate tests: `python3 -m unittest discover -s scripts -p 'test_renovate*.py'`
с process timeout60s. Общий `stage` CI исполняет полный Python набор с timeout120s и настоящие Linux
nginx/systemd checks. Ошибка или недоступность этих проверок не считается успехом.

Runtime и integrity lock закреплены в `scripts/renovate-runtime/package.json`/`package-lock.json`.
На поддерживаемом Node24 установить `npm ci --ignore-scripts --no-audit --no-fund` с timeout600s в этом каталоге.
Затем с `RENOVATE_RUNTIME=$PWD/scripts/renovate-runtime` выполнить отдельно
`node --experimental-test-module-mocks scripts/test_renovate_runtime.mjs` и
`node --experimental-test-module-mocks scripts/test_renovate_metadata_runtime.mjs`, каждый с timeout90s.
Они проверяют настоящий Renovate matcher/grouping/recovery/digest и rendering metadata;
сетевые/mutation endpoints — фейки. Install scripts выключены: fixture сообщает JavaScript RegExp
fallback при отсутствии native RE2; это не доказательство hosted RE2/загрузки presets.
Версия читается из manifest, не дублируется в assertions. Локальный node_modules не коммитится.

`RENOVATE_RUNTIME=<runtime> node scripts/verify_renovate_presets.mjs` с timeout180s проверяет
опубликованные refs настоящим resolver; отдельный read credential можно передать в
`RENOVATE_TOKEN` без вывода значения. До создания refs эта проверка не может быть успешной.

Код перенесён из действующего FIN-211/FIN-237 механизма
[fin-assist/go-postgres 1d02eff](https://github.com/fin-assist/go-postgres/tree/1d02eff31276d9dbbb07755d7c9ee4f7cc0a9580);
адаптация infra — config/trigger основного validation workflow, собственный baseline и docs.
[Политика FIN-211](https://finassist.youtrack.cloud/issue/FIN-211),
[задача внедрения FIN-447](https://finassist.youtrack.cloud/issue/FIN-447),
[Renovate presets](https://docs.renovatebot.com/config-presets/),
[GitHub merge API](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).
