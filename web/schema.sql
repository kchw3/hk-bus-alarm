-- Schema for the bus schedule chart database (D1).
--
-- Apply with:
--   wrangler d1 execute hk-bus-alarm --local  --file ./schema.sql
--   wrangler d1 execute hk-bus-alarm --remote --file ./schema.sql

CREATE TABLE IF NOT EXISTS schedule_log (
    -- Run timestamp exactly as logged by set_alarm_with_bus_eta.py, e.g.
    -- '2026-08-17T06:29:05+08:00'.
    ts           TEXT    NOT NULL,
    -- Same instant in epoch milliseconds, so ordering and range filters stay
    -- correct even if rows arrive with different UTC offsets.
    ts_epoch     INTEGER NOT NULL,
    route_id     TEXT    NOT NULL,
    -- Human-readable schedule cell, e.g. '2026-08-17T07:26+08:00 (57m)'.
    bus_schedule TEXT    NOT NULL DEFAULT '',
    -- Matched schedule as a full ISO 8601 timestamp; '' when none was found.
    eta_iso      TEXT    NOT NULL DEFAULT '',
    eta_epoch    INTEGER,
    alarm_time   TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    -- One row per run per route; makes re-ingest (and backfill) idempotent.
    PRIMARY KEY (ts, route_id)
);

CREATE INDEX IF NOT EXISTS idx_schedule_log_ts_epoch
    ON schedule_log (ts_epoch);

CREATE INDEX IF NOT EXISTS idx_schedule_log_route_eta
    ON schedule_log (route_id, eta_epoch);
