-- Migration: carry `seq` end-to-end (schedule_log), reset arrival tracking.
--
-- Apply with:
--   cd web
--   npx wrangler d1 export  hk-bus-alarm --remote --output ./backup-pre-seq.sql
--   npx wrangler d1 execute hk-bus-alarm --remote --file ./migrations/0001_add_seq.sql
--
-- TAKE THE BACKUP FIRST. D1 does not accept BEGIN/COMMIT in a file, so the
-- statements below run one at a time with no rollback, and the rebuild drops
-- the original table.
--
-- Two unrelated operations, deliberately in one file because they must land
-- together with the Worker that requires `seq`:
--
--   1. schedule_log  — rebuilt to add `seq` (and `stop_id`) and widen the
--      primary key. SQLite cannot ALTER a primary key, hence the rebuild.
--      Existing rows are all attributed to seq 5: every historical run of
--      set_alarm_with_bus_eta.py used -seq 5.
--
--   2. arrival_track — DROPPED, not migrated. Its rows were produced by a cron
--      job wrongly invoking -seq 6, so they describe the wrong stop entirely.
--      They are in the backup if ever wanted.

-- --- 1. Rebuild schedule_log --------------------------------------------

CREATE TABLE schedule_log_new (
    ts           TEXT    NOT NULL,
    ts_epoch     INTEGER NOT NULL,
    route_id     TEXT    NOT NULL,
    seq          INTEGER NOT NULL,
    stop_id      TEXT    NOT NULL DEFAULT '',
    bus_schedule TEXT    NOT NULL DEFAULT '',
    eta_iso      TEXT    NOT NULL DEFAULT '',
    eta_epoch    INTEGER,
    alarm_time   TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (ts, route_id, seq)
);

INSERT INTO schedule_log_new
    (ts, ts_epoch, route_id, seq, stop_id,
     bus_schedule, eta_iso, eta_epoch, alarm_time, reason)
SELECT
    ts, ts_epoch, route_id, 5, '',
    bus_schedule, eta_iso, eta_epoch, alarm_time, reason
FROM schedule_log;

DROP TABLE schedule_log;

ALTER TABLE schedule_log_new RENAME TO schedule_log;

CREATE INDEX IF NOT EXISTS idx_schedule_log_ts_epoch
    ON schedule_log (ts_epoch);

CREATE INDEX IF NOT EXISTS idx_schedule_log_route_eta
    ON schedule_log (route_id, seq, eta_epoch);

-- --- 2. Reset arrival_track ----------------------------------------------

DROP TABLE IF EXISTS arrival_track;

CREATE TABLE arrival_track (
    session_id       TEXT    NOT NULL,
    route_id         TEXT    NOT NULL,
    seq              INTEGER NOT NULL,
    stop_id          TEXT    NOT NULL DEFAULT '',
    eta_iso          TEXT    NOT NULL,
    eta_epoch        INTEGER NOT NULL,
    first_seen       TEXT    NOT NULL,
    first_seen_epoch INTEGER NOT NULL,
    last_seen        TEXT    NOT NULL,
    last_seen_epoch  INTEGER NOT NULL,
    polls            INTEGER NOT NULL DEFAULT 1,
    window_from      TEXT    NOT NULL DEFAULT '',
    window_to        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, eta_iso)
);

CREATE INDEX IF NOT EXISTS idx_arrival_track_session
    ON arrival_track (session_id);

CREATE INDEX IF NOT EXISTS idx_arrival_track_route_eta
    ON arrival_track (route_id, seq, eta_epoch);
