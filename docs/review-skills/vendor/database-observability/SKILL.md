---
name: database-observability
description: Sets up and reads database health signals for PostgreSQL and MySQL - pg_stat_statements and performance_schema digests for top queries, slow query logs and auto_explain, lock waits, table and index bloat, autovacuum and transaction ID wraparound, replication lag and slots, and Prometheus exporters - then turns them into a ranked findings report. Use when asked why the database is slow or growing, to find the most expensive queries, to check vacuum and bloat, to set up slow query logging, or to add database metrics and alerts.
license: MIT
---

# Database observability

Done well means: the expensive queries are ranked by total time with evidence, vacuum
and wraparound risk are known numbers rather than guesses, every finding names the
object, the metric and the next action, and nothing was changed on the server without
the user agreeing first.

## Ground rules

- Observation is read-only. Changing settings, resetting statistics, killing sessions,
  `VACUUM FULL`, `pg_repack` or `REINDEX` all need explicit user confirmation, and the
  last three need a maintenance plan because of locks and I/O.
- Read `AGENTS.md` for how to reach the database (bastion, read replica, connection
  string variable) and use a read-only role with `pg_monitor` (Postgres) or
  `PROCESS` plus `SELECT` on `performance_schema` and `sys` (MySQL).
- Note when statistics were last reset; numbers since a reset last week mean something
  different from numbers since last year.

## Workflow

```
- [ ] 1. Identify engine, version, hosting (self-managed, RDS/Aurora, Cloud SQL, Supabase)
- [ ] 2. Confirm which telemetry exists: pg_stat_statements, slow log, exporters, dashboards
- [ ] 3. Snapshot top queries by total time, mean time and I/O
- [ ] 4. Check current activity: long transactions, lock waits, idle in transaction
- [ ] 5. Check vacuum health: dead tuples, last autovacuum, wraparound age
- [ ] 6. Check bloat and unused or duplicate indexes
- [ ] 7. Check replication: lag, slots holding WAL
- [ ] 8. Write the findings report, ranked by impact
```

## PostgreSQL: enable the basics

```ini
# postgresql.conf (restart needed for shared_preload_libraries)
shared_preload_libraries = 'pg_stat_statements,auto_explain'
pg_stat_statements.track = top
log_min_duration_statement = '500ms'      # slow query log
log_lock_waits = on                        # logs waits longer than deadlock_timeout
log_temp_files = 0                         # every spill to disk
log_autovacuum_min_duration = '10s'
auto_explain.log_min_duration = '2s'
auto_explain.log_analyze = off             # on adds timing overhead to every query
track_io_timing = on                       # I/O time in pg_stat_statements and EXPLAIN
```

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

On managed services the same settings live in the parameter group or flags; restarts are
scheduled by the user. Use `ALTER SYSTEM` only with approval, then `SELECT pg_reload_conf();`
for reloadable settings.

## Top queries

```sql
SELECT round(total_exec_time::numeric / 1000, 1)        AS total_s,
       calls,
       round(mean_exec_time::numeric, 2)                AS mean_ms,
       round((100 * total_exec_time / sum(total_exec_time) OVER ())::numeric, 1) AS pct,
       rows / nullif(calls, 0)                          AS rows_per_call,
       shared_blks_read + shared_blks_hit               AS blks,
       temp_blks_written,
       left(regexp_replace(query, '\s+', ' ', 'g'), 120) AS query
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 20;
```

Read it three ways:

| Sort by | Finds | Typical fix |
|---|---|---|
| `total_exec_time` | Where the server spends its time overall | Index, rewrite, or cache the top 3 |
| `mean_exec_time` with `calls > 100` | Individually slow queries | `$postgres-query-tuning` |
| `calls` | Chatty access, N+1 | `$n-plus-one-queries` |
| `temp_blks_written` | Sorts and hashes spilling to disk | Index for the sort, or raise `work_mem` for that role |

The column names are `total_exec_time` and `mean_exec_time` since Postgres 13 (older:
`total_time`). Resetting with `pg_stat_statements_reset()` erases history; ask first and
record a snapshot before doing it.

## Live activity and locks

```sql
SELECT pid, usename, state, wait_event_type, wait_event,
       now() - xact_start  AS xact_age,
       now() - query_start AS query_age,
       pg_blocking_pids(pid) AS blocked_by,
       left(query, 100) AS query
FROM pg_stat_activity
WHERE state <> 'idle' AND pid <> pg_backend_pid()
ORDER BY xact_start NULLS LAST;
```

- `idle in transaction` for minutes holds locks and blocks vacuum cleanup.
- A chain where one pid appears in many `blocked_by` arrays is the head blocker.
- Terminating a backend (`pg_terminate_backend(pid)`) rolls back its work: show the user
  the session and get a yes first.

## Vacuum, bloat and wraparound

```sql
-- tables autovacuum is losing against
SELECT relname, n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC LIMIT 15;

-- transaction ID wraparound headroom
SELECT datname, age(datfrozenxid) AS xid_age,
       round(100.0 * age(datfrozenxid) / 2147483647, 1) AS pct_to_wraparound
FROM pg_database ORDER BY xid_age DESC;
```

| Signal | Threshold | Action |
|---|---|---|
| `dead_pct` | Above 20% on a large table and rising | Lower `autovacuum_vacuum_scale_factor` for that table (for example 0.02) |
| `xid_age` | Above `autovacuum_freeze_max_age` (200M default) and climbing | Find what blocks freezing (below) |
| `pct_to_wraparound` | Above 50% | Treat as an incident; at about 3M XIDs left Postgres refuses writes |

Vacuum can only remove rows older than the oldest snapshot anyone still needs. When dead
tuples will not go away, check, in order: long-running or idle-in-transaction sessions,
replication slots with an old `xmin` or `catalog_xmin`, `hot_standby_feedback` from a
replica running long queries, and forgotten prepared transactions (`pg_prepared_xacts`).

For table and index bloat estimates, unused and duplicate index queries, replication slot
checks and the MySQL equivalents, read
[references/diagnostic-queries.md](references/diagnostic-queries.md) when step 6, 7 or a
MySQL server is in scope.

Fixing bloat: `VACUUM` reclaims space for reuse but rarely shrinks files.
`REINDEX INDEX CONCURRENTLY` (Postgres 12+) rebuilds a bloated index online. `pg_repack`
rewrites a table online but needs free disk equal to the table plus indexes and a brief
exclusive lock at the end. `VACUUM FULL` blocks all access for the whole rewrite: never on
a live table without a maintenance window and explicit approval.

## MySQL quick view

```sql
SELECT schema_name, digest_text, count_star,
       round(sum_timer_wait / 1e12, 1) AS total_s,
       round(avg_timer_wait / 1e9, 1)  AS avg_ms,
       sum_rows_examined, sum_rows_sent, sum_no_index_used
FROM performance_schema.events_statements_summary_by_digest
ORDER BY sum_timer_wait DESC LIMIT 20;
```

Slow log: `slow_query_log = ON`, `long_query_time = 0.5`, then summarize with
`pt-query-digest /var/log/mysql/slow.log`. A `rows_examined / rows_sent` ratio above
about 100 signals a missing index (`$mysql-tuning`). Long-running transactions show in
`information_schema.innodb_trx`; history list length (`SHOW ENGINE INNODB STATUS`) is the
MySQL analogue of dead tuples piling up.

## Metrics and alerts

Export continuously instead of running these queries by hand:

- Prometheus: `postgres_exporter` or `mysqld_exporter`, dashboards in Grafana
  (`$prometheus-metrics`, `$grafana-dashboards`).
- Managed: RDS Performance Insights / Database Insights, Cloud SQL Query Insights.
- Commercial query analytics: pganalyze, Datadog DBM.

Alert on symptoms with clear actions:

| Alert | Condition |
|---|---|
| Connections near limit | Used above 80% of `max_connections` for 5 min |
| Wraparound | `age(datfrozenxid)` above 1 billion |
| Replication lag | Replay lag above the app's staleness budget for 5 min |
| Slot retaining WAL | Retained WAL above 20% of free disk |
| Long transaction | Any transaction older than 30 min (excluding known batch roles) |
| Disk | Free space below 15% or projected full within 7 days |

## Verification

- Every finding cites the query output it came from and the time window it covers.
- Re-run the top-queries snapshot after a fix and compare totals for the same interval.
- After autovacuum tuning, `n_dead_tup` trends down over the next day.
- Alerts fire in a test (for example a deliberately long transaction on staging).

## Report template

```markdown
**Server:** Postgres 16.4 on RDS db.r6g.2xlarge; stats since 2026-10-01 (27 days)
**Top time consumers:** 1) orders search 41% (mean 220 ms, 1.9M calls) 2) ...
**Locks:** 2 sessions idle in transaction > 1 h from reporting role (pids 812, 9931)
**Vacuum:** events table 38% dead tuples; last autovacuum 6 days ago
**Wraparound:** max xid_age 412M (19%); blocked by slot "old_cdc" (inactive, 180 GB WAL)
**Ranked actions:**
1. Drop inactive slot old_cdc after the owner confirms (frees 180 GB, unblocks freezing)
2. Index for orders search (see postgres-query-tuning)
3. events: autovacuum_vacuum_scale_factor 0.02
**Changed on server:** nothing (all read-only)
```
