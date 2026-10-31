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

## Code Review Rules

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
