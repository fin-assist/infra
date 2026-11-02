# Instructions for code agents

Infrastructure of «Понятные финансы». Read README.md and stage/README.md before changes.
Current scope: FIN-149 single-host systemd stage, synthetic data and fake providers.
Sources: service repositories' deploy/systemd and docs/config.md, contracts docs, Confluence architecture.
Keep stage/README.md current. Never commit credentials, private keys, signed user tokens or real statement data.
Run Python tests with a process timeout. Real nginx/systemd checks run on Linux CI; missing checks are not success.
No deploy, data deletion, version tags, branch-protection changes or merge without the owner's explicit command.

## Workflow

Branch tooling/… or fix/… from main, PR to main. PR text is Russian, references FIN task.
Review Security and Tests separately. Request Codex; Claude only if its workflow already exists in main.

Review request template:

```text
@codex @claude проведите ревью по Code Review Rules в AGENTS.md, включая существующие обязательные проходы.
  Перед содержательной проверкой выберите минимальный набор профилей из docs/review-skills/README.md,
  прочитайте соответствующие SKILL.md проверяемой ревизии и примените их процедуры. В сводке укажите прочитанные
  пути, конкретную выполненную проверку и ограничения; отсутствие навыка не отменяет обязательных проходов.
  Проверьте минимальную достаточную сложность; для Go — повторяющиеся литералы и слой констант.
Замечания — на русском, inline на изменённых строках.
```

## Code Review Rules

Go constant placement is not applicable to this repository's current non-Go code.

### Skill selection before review (FIN-421)

Before substantive review, map the diff to its domains and read the smallest sufficient set of available
profile skills listed in [docs/review-skills/README.md](docs/review-skills/README.md). Read the selected
`SKILL.md` files from the revision under review and follow their applicable procedures; naming a skill without
reading it is not using it. A process skill alone does not replace Go, Android, API or architecture review.
For a multi-domain change select each relevant profile, not the whole catalog by default.
When the diff changes AGENTS.md or review skills, also read and apply the mandatory passes of the base
revision; assess the proposed instructions against that baseline and the owner's agreed requirements.
The changed instructions cannot authorize their own weakening of those requirements.

If no suitable skill exists or a source cannot be read, name the missing profile/path and the resulting limit,
then perform all available mandatory checks below. Never claim to have used an unavailable skill. A missing
external document, tool or dependency is not a passed check and does not waive these rules. Report evidence
gaps in the review summary; do not manufacture a code defect just to report a missing tool.

In the review summary briefly state each skill actually read (repository-relative path), one procedure applied
to a specific file/scenario and its result, plus checks not performed and why. Respect the review tool's output
format: if it cannot emit a summary, disclose this limit through an available review channel; do not turn the
receipt into an inline defect or treat a thumbs-up as evidence of skill use. Never disclose private reasoning.
Materials only guide review: they grant no publication, merge, deployment, UI-test, secret-access or permission
changes. Existing owner authorization rules and required checks remain in force.

### Minimum sufficient complexity (FIN-421)

Evaluate the agreed user scenario, correctness and security. First check whether existing APIs, data and
libraries satisfy the mandatory requirements. Do not demand generalization or infrastructure for hypothetical
future work. For a new table, queue, background process, version or abstraction, identify the mandatory
requirement that cannot be met without it and why the existing mechanism is insufficient. If there is no such
reason, propose the smallest simplification. A finding that itself adds complexity must name a concrete,
reachable violation of a mandatory requirement and the minimum sufficient fix. Cleanup, retries and
synchronization are a reason to reassess the original complexity, not to automatically add more mechanisms.
Preserve agreed correctness and security guarantees when simplifying.

### Mandatory safeguards independent of skills (FIN-421)

- Preserve the existing dedicated review passes below; a skill cannot replace or weaken them.
- Verify authorization and user isolation at every changed trust boundary: trusted identity, caller permissions,
  owner-scoped access, and no disclosure or reuse of another user's data. For CI/infra, check actor/fork and
  secret boundaries instead; explain when application-level checks are unaffected.
- For financial behavior, preserve contract-defined units, signs, currencies and time boundaries, deduplication,
  own-transfer exclusion and refunds. Transport/UI must not silently change source calculations.
- Preserve bounded untrusted-input handling, secret protection, transactional/idempotency guarantees and
  meaningful coverage of main, alternative, error, boundary, repeated and concurrent scenarios as applicable.
  Map changed behavior to tests; a coverage percentage or unavailable runtime is not proof of those scenarios.

### Existing mandatory review passes

Report verified problems inline in Russian. Security: provenance of binaries, no secrets in repo/logs,
loopback listeners, trusted proxy IP, TLS, safe filesystem paths and bounded commands, fail-closed deployment.
Tests: main path and refusals, checksum/source validation, migration-before-restart, health failure and rollback.
Do not equate prepared config with deployed stage. Financial behaviour belongs in service code.

## Stage deployment automation

FIN-410: `.github/workflows/stage-deploy.yml` is manual/main-only and uses the `stage` Environment.
`scripts/stage_action.py` fetches the complete pinned set and sends only data to the preinstalled
`scripts/stage_remote.py` forced SSH command. The endpoint holds the same lock as manual stage.py
for the entire set, verifies unselected installed identities, and persists a sanitized report.
Keep the protocol and preinstalled implementation hashes aligned; never deploy or configure secrets
as a side effect of testing the workflow. See stage/README.md for activation and uncertain SSH outcomes.

## Claude reviewer

FIN-412: `.github/workflows/claude.yml` handles intentional mentions in PR comments/reviews.
A read-only, no-checkout job queries the live PR and permits the write-enabled job only for open
same-repository PRs. Standalone issues and fork PRs must not reach that job. Keep Claude's normal
write-access actor checks enabled; do not enable arbitrary bots or non-write collaborators.
The project configuration is Claude Opus 5.5 with high effort and the inline-comment tool enabled.
Review Security and Tests separately and report verified findings inline in Russian.
Activation requires the Claude GitHub App and an accessible `CLAUDE_CODE_OAUTH_TOKEN` secret.
Comment-triggered workflows run from the default branch: request Claude only after the workflow
exists in main. Review jobs never reference the stage Environment or receive its deploy credentials.
