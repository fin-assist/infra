# Diagnostic queries

All queries are read-only. Run them on the primary unless noted; replica statistics
(`pg_stat_user_tables`, `pg_stat_statements`) describe only the replica's own workload.

## PostgreSQL

### Table bloat (measured)

`pgstattuple` ships with Postgres contrib. `CREATE EXTENSION` is a change: ask first.
`pgstattuple_approx` samples the visibility map and is far cheaper than `pgstattuple`,
but still reads pages; run it on one table at a time, off-peak for very large tables.

```sql
CREATE EXTENSION IF NOT EXISTS pgstattuple;
SELECT table_len, approx_tuple_percent, dead_tuple_percent, approx_free_percent
FROM pgstattuple_approx('public.events');
```

`approx_free_percent` above about 30% on a large table means real bloat worth
`pg_repack`; under 20% is normal headroom for updates.

### Index bloat (measured, B-tree)

```sql
SELECT avg_leaf_density, leaf_fragmentation,
       pg_size_pretty(pg_relation_size('events_created_at_idx')) AS size
FROM pgstatindex('events_created_at_idx');
```

`avg_leaf_density` below about 50% on an index that should be dense suggests
`REINDEX INDEX CONCURRENTLY`.

### Largest relations

```sql
SELECT c.relname, c.relkind,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total,
       pg_size_pretty(pg_relation_size(c.oid))       AS heap,
       pg_size_pretty(pg_indexes_size(c.oid))        AS indexes
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema') AND c.relkind IN ('r', 'm')
ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 20;
```

### Unused indexes

```sql
SELECT s.relname AS table, s.indexrelname AS index, s.idx_scan,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique AND NOT i.indisprimary
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

Check every replica too: an index unused on the primary may serve replica reads. Counters
reset on stats reset and, on some managed services, on failover. Hand drop decisions to
`postgres-indexing`.

### Duplicate indexes (same columns, same predicate)

```sql
SELECT indrelid::regclass AS table,
       array_agg(indexrelid::regclass) AS indexes
FROM pg_index
GROUP BY indrelid, indkey, indclass, indexprs::text, indpred::text
HAVING count(*) > 1;
```

### Cache hit ratio per table

```sql
SELECT relname,
       heap_blks_hit, heap_blks_read,
       round(100.0 * heap_blks_hit / nullif(heap_blks_hit + heap_blks_read, 0), 2) AS hit_pct
FROM pg_statio_user_tables
ORDER BY heap_blks_read DESC LIMIT 15;
```

A hot OLTP table below about 99% hit rate is reading from disk on every few queries.

### Autovacuum settings per table

```sql
SELECT relname, reloptions FROM pg_class
WHERE reloptions IS NOT NULL AND relkind = 'r';
```

### Running autovacuum workers and progress

```sql
SELECT p.pid, p.relid::regclass AS table, p.phase,
       p.heap_blks_scanned, p.heap_blks_total,
       now() - a.xact_start AS running_for
FROM pg_stat_progress_vacuum p JOIN pg_stat_activity a USING (pid);
```

### Oldest snapshot holders (what blocks vacuum)

```sql
SELECT pid, usename, state, backend_xmin, age(backend_xmin) AS xmin_age,
       now() - xact_start AS xact_age, left(query, 80) AS query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY age(backend_xmin) DESC LIMIT 10;

SELECT slot_name, slot_type, active, xmin, catalog_xmin,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal
FROM pg_replication_slots;

SELECT gid, prepared, owner FROM pg_prepared_xacts;
```

An inactive slot retaining WAL fills the disk and blocks freezing. Dropping a slot
(`pg_drop_replication_slot`) breaks whatever consumer owns it (CDC, a replica): identify
the owner and get confirmation first.

### Replication lag (on the primary)

```sql
SELECT application_name, client_addr, state,
       write_lag, flush_lag, replay_lag,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)) AS replay_bytes
FROM pg_stat_replication;
```

On a replica: `SELECT now() - pg_last_xact_replay_timestamp();` (grows on an idle primary
too, so read it alongside write activity).

### I/O and checkpoints

```sql
-- Postgres 16+: I/O by backend type and context
SELECT backend_type, object, context, reads, writes, extends, fsyncs
FROM pg_stat_io WHERE reads > 0 OR writes > 0 ORDER BY reads + writes DESC LIMIT 15;

-- Postgres 17+: checkpoint stats moved out of pg_stat_bgwriter
SELECT num_timed, num_requested, write_time, sync_time, buffers_written
FROM pg_stat_checkpointer;
```

Requested checkpoints far outnumbering timed ones means `max_wal_size` is too small for
the write rate.

### Settings that differ from defaults

```sql
SELECT name, setting, unit, source FROM pg_settings
WHERE source NOT IN ('default', 'override') ORDER BY name;
```

## MySQL 8

### Statements doing full scans

```sql
SELECT query, db, exec_count, total_latency, no_index_used_count, rows_examined_avg
FROM sys.statements_with_full_table_scans
ORDER BY no_index_used_count DESC LIMIT 15;
```

### Unused and redundant indexes

```sql
SELECT * FROM sys.schema_unused_indexes WHERE object_schema = 'app';
SELECT table_name, redundant_index_name, dominant_index_name, sql_drop_index
FROM sys.schema_redundant_indexes WHERE table_schema = 'app';
```

`sql_drop_index` is a suggestion string; do not execute it without review and approval.

### Long transactions and lock waits

```sql
SELECT trx_id, trx_state, trx_started, now() - trx_started AS age,
       trx_rows_locked, trx_mysql_thread_id, left(trx_query, 100) AS query
FROM information_schema.innodb_trx ORDER BY trx_started LIMIT 10;

SELECT * FROM sys.innodb_lock_waits\G
SELECT * FROM sys.schema_table_lock_waits\G     -- metadata locks (DDL waiting)
```

### Table size and fragmentation

```sql
SELECT table_name,
       round((data_length + index_length) / 1024 / 1024) AS size_mb,
       round(data_free / 1024 / 1024) AS free_mb
FROM information_schema.tables
WHERE table_schema = 'app' ORDER BY data_length + index_length DESC LIMIT 20;
```

`information_schema.tables` statistics are cached (`information_schema_stats_expiry`,
default 86400 s); run `ANALYZE TABLE` first for current numbers.

### Replication

```sql
SHOW REPLICA STATUS\G      -- 8.0.22+; Seconds_Behind_Source, Replica_IO_Running, Replica_SQL_Running
```

`Seconds_Behind_Source` is coarse and reads 0 when the SQL thread is idle waiting for
events; `pt-heartbeat` gives true lag.

### InnoDB health

```sql
SHOW ENGINE INNODB STATUS\G     -- History list length, deadlocks, semaphores
SELECT name, count FROM information_schema.innodb_metrics
WHERE name IN ('trx_rseg_history_len', 'lock_deadlocks', 'lock_timeouts');
```

A history list length that keeps growing into the millions means purge is blocked by a
long-running transaction or snapshot.
