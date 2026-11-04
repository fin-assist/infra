---
name: postgres-backup-restore
description: Sets up and tests PostgreSQL backups that actually restore - logical dumps with pg_dump/pg_restore (custom and directory formats, parallel jobs, globals), physical backups with pg_basebackup and WAL archiving, point-in-time recovery, pgBackRest stanzas, retention and encryption, managed-service snapshots, and timed restore drills with verification queries. Use when designing a Postgres backup strategy, restoring or cloning a database, recovering to a timestamp before a bad deploy or DELETE, or proving RPO and RTO with a drill.
license: MIT
---

# Postgres backup and restore

A backup exists only once it has been restored, verified and timed. Done well means the
team knows its RPO (how much data it can lose) and RTO (how long recovery takes), both
are measured by a recent drill, backups live outside the failure domain of the server,
and the restore runbook works when read by someone tired at 3 a.m.

## Pick the mechanism

| Need | Mechanism |
|---|---|
| Copy one database, migrate versions, seed staging | Logical: `pg_dump -Fc` / `-Fd` + `pg_restore` |
| Recover to any second in the last N days | Physical base backup + continuous WAL archive (PITR) |
| Self-managed production | pgBackRest (or WAL-G / Barman) doing full + incremental + WAL archive |
| RDS, Aurora, Cloud SQL, Azure, Neon, Supabase | Provider PITR and snapshots; add periodic logical dumps to a separate account for vendor-independence |
| Table-level undo after a bad DELETE | PITR to a scratch instance, then copy the rows back |

Logical dumps alone give an RPO equal to the dump interval and a slow restore for large
databases (indexes rebuild). They complement, not replace, PITR for production.

## Workflow

1. Read `AGENTS.md`, infra code (Terraform, Helm, Ansible) and any existing backup jobs.
   Find where backups go, retention, encryption, and who gets alerted on failure.
2. Ask for (or propose) RPO and RTO targets. They drive every choice below.
3. Configure backups (sections below), then run a restore drill and record timings.
4. Write or update the runbook; schedule recurring drills.

Restoring over an existing database, dropping databases, or changing `archive_command`
on a production server are destructive or risky: describe the exact command and target,
and get explicit confirmation from the user before running them. Default to restoring
into a new, separate instance or database.

## Logical backups

```bash
# One database, compressed custom format (allows selective, parallel restore)
pg_dump -h db -U backup -d app -Fc -Z zstd:3 -f app_$(date -u +%Y%m%dT%H%M%SZ).dump
# (-Z with zstd/lz4 needs PG 16+ client; older clients use -Z 6 for gzip)

# Large database: directory format with parallel workers
pg_dump -d app -Fd -j 8 -f app_dir/

# Roles and tablespaces are NOT in pg_dump output
pg_dumpall --globals-only -f globals.sql

# Restore into a fresh database
createdb -T template0 app_restore
pg_restore -d app_restore -j 8 --no-owner --exit-on-error app.dump

# Inspect or select contents
pg_restore -l app.dump > toc.txt      # edit toc.txt, then:
pg_restore -L toc.txt -d app_restore app.dump
```

- Use a `pg_dump` client at least as new as the server; restore into the same or newer
  major version.
- `pg_dump` takes a consistent snapshot but holds it for the whole run; on a busy
  primary this delays vacuum. Dump from a replica when possible (watch
  `max_standby_streaming_delay` cancellations).
- Plain SQL format (`-Fp`) cannot be restored in parallel or selectively; avoid for
  anything large.
- Dumps contain all the data: encrypt at rest and restrict access like production.

## Physical backups and PITR (self-managed)

Core settings in `postgresql.conf`:

```ini
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza=main archive-push %p'
archive_timeout = 60          # bounds RPO on quiet systems (seconds)
```

pgBackRest, `/etc/pgbackrest/pgbackrest.conf`:

```ini
[global]
repo1-type=s3
repo1-path=/pgbackrest
repo1-s3-bucket=acme-pg-backups
repo1-s3-endpoint=s3.eu-west-1.amazonaws.com
repo1-s3-region=eu-west-1
repo1-retention-full=2
repo1-cipher-type=aes-256-cbc
compress-type=zst
process-max=4

[main]
pg1-path=/var/lib/postgresql/18/main
```

Keep `repo1-cipher-pass` and S3 credentials in the secret store, not in the repo.

```bash
pgbackrest --stanza=main stanza-create
pgbackrest --stanza=main check                 # verifies archiving end to end
pgbackrest --stanza=main --type=full backup
pgbackrest --stanza=main --type=diff backup    # schedule: weekly full, daily diff/incr
pgbackrest --stanza=main info                  # backups, WAL range, sizes
```

Point-in-time restore onto a stopped, separate host:

```bash
pgbackrest --stanza=main --delta \
  --type=time "--target=2026-10-27 14:29:00+00" \
  --target-action=promote restore
```

Without pgBackRest, PostgreSQL's own path is `pg_basebackup -D ... -Ft -z -X stream`,
then on restore: `restore_command` plus `recovery_target_time` in `postgresql.conf` and an
empty `recovery.signal` file. PG 17+ also supports `pg_basebackup --incremental` with
`summarize_wal = on`, combined with `pg_combinebackup`.

## Managed services

- Confirm automated backups are on and retention matches RPO needs (RDS allows up to
  35 days of PITR).
- Snapshots in the same account and region do not survive account compromise or a
  region outage. Copy snapshots cross-account/region, or ship logical dumps elsewhere.
- PITR on managed services creates a new instance; plan DNS or connection string
  changes in the runbook.

## Restore drill

Run at least quarterly, and after any backup config change:

- [ ] Pick a target time; restore into a new instance from backup storage only
- [ ] Record wall-clock time for each phase (fetch, restore, WAL replay, ready)
- [ ] Verify integrity and content:

```sql
SELECT count(*) FROM orders;                                  -- compare with source at target time
SELECT max(created_at) FROM orders;                           -- should be just before target
SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 20;
```

```bash
pg_amcheck -d app_restore --heapallindexed     # PG 14+, checks heap and B-tree consistency
```

- [ ] Run the application's smoke tests against the restored instance
- [ ] Compare measured RTO and RPO to targets; file fixes if they miss
- [ ] Destroy the drill instance (it holds production data)

## Recovering from a bad change (the common emergency)

1. Note the exact time of the bad statement (from logs, `pg_stat_statements`, app logs).
2. Do not restore over production. PITR to a new instance at just before that time.
3. Extract the affected rows (`COPY ... TO`, or `pg_dump -t table --data-only`).
4. Show the user a diff of what will be re-inserted or updated, then apply in a
   transaction on production after confirmation.

## Gotchas

- `archive_command` that returns success without storing the file (e.g. `cp` to a full
  disk with errors ignored) makes PITR silently impossible. Use a tool that verifies,
  and alert on `pg_stat_archiver.failed_count` and `last_failed_time`.
- A failing archive fills `pg_wal` until the disk is full and the primary stops.
  Monitor disk and archiver together.
- Backups of only the primary data directory miss config files kept elsewhere
  (`/etc/postgresql/...`). Back up config with infra code.
- Extensions and their versions must exist on the restore target.
- Time zones: give `--target` with an explicit offset.
- Retention that deletes the only full backup older than the WAL you need breaks PITR
  for that window; `repo1-retention-full` counts full backups, plan accordingly.

## Output template

```markdown
## Backup plan: <cluster>
**Targets:** RPO <min>, RTO <min>
**Mechanism:** <pgBackRest/WAL-G/provider>, schedule <full/diff/incr>, WAL archive <where>
**Retention:** <days / count>, encryption <how>, off-site copy <where>
**Monitoring:** <archiver failures, backup age, disk>
**Last drill:** <date>, restored to <time>, RTO measured <min>, issues <list>
**Runbook:** <link/path>
```
