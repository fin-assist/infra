# Источники и лицензии навыков

Снимок установленных существующих навыков — 04.11.2026. Материалы взяты из установленного набора; исходные и адаптированные
файлы различаются по provenance и хешам снимка. Проектные правила применения находятся в README.
Ревизия upstream и локально адаптированный снимок различаются: local_differences/adaptation в SNAPSHOT.json
не выдаются за неизменённый upstream. Полные SHA256 всех поставленных исходников и лицензий — в [SNAPSHOT.json](SNAPSHOT.json).

| Навык | Источник / ревизия сравнения | Лицензия |
| --- | --- | --- |
| code-review-and-quality | [https://github.com/addyosmani/agent-skills](https://github.com/addyosmani/agent-skills); `301b02b12bff98e4688e6984af6556802322bbde`; через Engineering Suite 1.1.1 | MIT; [addyosmani-MIT.txt](licenses/addyosmani-MIT.txt); [engineering-suite-MIT.txt](licenses/engineering-suite-MIT.txt) |
| security-and-hardening | [https://github.com/addyosmani/agent-skills](https://github.com/addyosmani/agent-skills); `301b02b12bff98e4688e6984af6556802322bbde`; через Engineering Suite 1.1.1 | MIT; [addyosmani-MIT.txt](licenses/addyosmani-MIT.txt); [engineering-suite-MIT.txt](licenses/engineering-suite-MIT.txt) |
| cicd-security | [https://github.com/briiirussell/cybersecurity-skills](https://github.com/briiirussell/cybersecurity-skills); `18642b272e9fe57ed6bdf3fd96614915e8a4d71d` | MIT; [briiirussell-MIT.txt](licenses/briiirussell-MIT.txt) |
| github-actions-validator | [https://github.com/akin-ozer/cc-devops-skills](https://github.com/akin-ozer/cc-devops-skills); `276af751e659315aaf56d3ad13d7c26f4e72e28a` | Apache-2.0; [akin-ozer-Apache-2.0.txt](licenses/akin-ozer-Apache-2.0.txt) |
| cicd-pipelines | [https://github.com/san-npm/skills-ws](https://github.com/san-npm/skills-ws); `300409ffe7ee7998955a5fd41741c77abb3888da` | MIT; [skills-ws-MIT.txt](licenses/skills-ws-MIT.txt) |
| postgres-backup-restore | [https://codexskills.dev/skills/databases/postgres-backup-restore/](https://codexskills.dev/skills/databases/postgres-backup-restore/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |
| database-observability | [https://codexskills.dev/skills/databases/database-observability/](https://codexskills.dev/skills/databases/database-observability/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |
| connection-pooling | [https://codexskills.dev/skills/databases/connection-pooling/](https://codexskills.dev/skills/databases/connection-pooling/); `снимок без immutable revision` | MIT declared in SKILL.md; https://codexskills.dev/about/#licensing; [codexskills-MIT.txt](licenses/codexskills-MIT.txt) |
| runbook-generator | [https://github.com/alirezarezvani/claude-skills](https://github.com/alirezarezvani/claude-skills); `c85b17e1fb621ba760478baaacc4fcb9be225e74` | MIT; [alirezarezvani-MIT.txt](licenses/alirezarezvani-MIT.txt) |

Для `code-review-and-quality`, `security-and-hardening` исходные references указывают посредника EngineeringSuite 1.1.1.
Сохранено [исходное MIT-уведомление Engineering Suite contributors](licenses/engineering-suite-MIT.txt) из [закреплённой копии go-service-template](https://github.com/fin-assist/go-service-template/blob/77491fb876eb124fd9c085deb24a9ff9432e4514/docs/review-skills/licenses/MIT.txt). Цепочка Addy Osmani → Engineering Suite 1.1.1 → установленный снимок отражена в поле
`intermediary` SNAPSHOT.json. Пакет сейчас не установлен; список `local_differences` означает отличия от
закреплённого Addy upstream, а не утверждение об авторстве каждого отличия. Исходные reference-тексты не менялись.

У поставленных навыков Codex Skills исходные SKILL.md содержат объявление MIT; отдельная copyright-строка
в дистрибутиве отсутствует. Разрешение распространения подтверждено [условиями источника](https://codexskills.dev/about/#licensing).
Сохранено [отдельное уведомление с текстом MIT](licenses/codexskills-MIT.txt); связь каждого навыка с ним указана
в поле `licenses` SNAPSHOT.json и в таблице выше.

Для источников GitHub поставлены исходные LICENSE/NOTICE либо закреплённая лицензия автора; прямые ссылки
приведены в таблице, а связь каждого навыка с файлами лицензий — в поле `licenses` SNAPSHOT.json.


## QA-навыки FIN-430

Источник: [petrkindlmann/qa-skills](https://github.com/petrkindlmann/qa-skills), upstream comparison revision
`62434253c2592f27b494609c27f224939c86346d`. Это установленный адаптированный снимок Plainfin,
а не заявление о побайтовом совпадении с upstream. Copyright (c) 2026 Petr Kindlmann, MIT.

- `ai-qa-review`: [MIT LICENSE](vendor/ai-qa-review/LICENSE); [SKILL.md](vendor/ai-qa-review/SKILL.md) и используемые ресурсы.
- `release-readiness`: [MIT LICENSE](vendor/release-readiness/LICENSE); [SKILL.md](vendor/release-readiness/SKILL.md) и используемые ресурсы.
- `qa-project-context`: [MIT LICENSE](vendor/qa-project-context/LICENSE); поставлен только общий reference policy, без SKILL.md/роутера.

Адаптации FIN-430: переносимые ссылки, приоритет правил репозитория и формата ревью,
отсутствие новых полномочий/CI gates из generic примеров. Исправления database-testing, если он поставлен:
реальный Go/PG harness, сохранение данных и применимый recovery, разрешённые constraints/SQLSTATE,
репрезентативная оценка полного плана без запрета Seq Scan или обязательного DROP INDEX.
Исходные хеши файлов установленного снимка и отличающиеся файлы записаны в skills;
files_sha256 фиксирует конечные поставленные файлы. Исходные лицензии сохранены.
