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
    -- 1-based stop sequence, the `-seq` the run was invoked with. Half the
    -- series identity: without it two stops of the same route share a primary
    -- key and silently overwrite each other.
    seq          INTEGER NOT NULL,
    -- Operator stop id, captured at log time. Unlike `seq` it survives a route
    -- being re-surveyed, so it anchors a historical row to the right stop.
    -- '' for rows migrated or replayed from a CSV.
    stop_id      TEXT    NOT NULL DEFAULT '',
    -- Human-readable schedule cell, e.g. '2026-08-17T07:26+08:00 (57m)'.
    bus_schedule TEXT    NOT NULL DEFAULT '',
    -- Matched schedule as a full ISO 8601 timestamp; '' when none was found.
    eta_iso      TEXT    NOT NULL DEFAULT '',
    eta_epoch    INTEGER,
    alarm_time   TEXT    NOT NULL DEFAULT '',
    reason       TEXT    NOT NULL DEFAULT '',
    -- One row per run per route per stop; makes re-ingest (and backfill)
    -- idempotent without merging two stops polled in the same second.
    PRIMARY KEY (ts, route_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_schedule_log_ts_epoch
    ON schedule_log (ts_epoch);

CREATE INDEX IF NOT EXISTS idx_schedule_log_route_eta
    ON schedule_log (route_id, seq, eta_epoch);


-- Arrival tracking (track_bus_arrival.py). One row per distinct ETA value seen
-- during one tracking session, NOT one row per poll: an unchanged estimate
-- advances last_seen and polls on the existing row.
--
-- The final row of a session is what deduces the arrival. eta_iso is the last
-- estimate the operator published, and last_seen is the last moment the bus was
-- still listed — an upper bound, because the feed keeps showing an overdue bus
-- with an ETA in the past until it is gone.
CREATE TABLE IF NOT EXISTS arrival_track (
    -- '<route_id>|<seq>|<run start ISO>'. Deterministic, so replaying a log
    -- upserts instead of duplicating.
    session_id       TEXT    NOT NULL,
    route_id         TEXT    NOT NULL,
    seq              INTEGER NOT NULL,
    -- Operator stop id, captured at track time. '' when replayed from a CSV,
    -- which carries only `seq`.
    stop_id          TEXT    NOT NULL DEFAULT '',
    -- One distinct ETA value observed, e.g. '2026-08-19T13:45:32+08:00'.
    eta_iso          TEXT    NOT NULL,
    eta_epoch        INTEGER NOT NULL,
    -- Poll timestamps bracketing the period this estimate was published.
    first_seen       TEXT    NOT NULL,
    first_seen_epoch INTEGER NOT NULL,
    last_seen        TEXT    NOT NULL,
    last_seen_epoch  INTEGER NOT NULL,
    polls            INTEGER NOT NULL DEFAULT 1,
    -- Search window this session tracked, for context when reading raw rows.
    window_from      TEXT    NOT NULL DEFAULT '',
    window_to        TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (session_id, eta_iso)
);

CREATE INDEX IF NOT EXISTS idx_arrival_track_session
    ON arrival_track (session_id);

CREATE INDEX IF NOT EXISTS idx_arrival_track_route_eta
    ON arrival_track (route_id, seq, eta_epoch);
