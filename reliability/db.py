"""Schema definition and connection helpers.

Design notes
------------
* Canonical facts are append-only.
  - A fault is identified by a stable client ``event_key``.  A correction is a
    *new row* with the same ``event_key`` and a higher ``revision``; the
    effective fault set is "latest revision per event_key".  A retraction is a
    revision with ``retracted=1``.  Nothing is ever deleted or updated.
  - Observation windows are corrected through ``observation_events``, which
    keeps the previous window; ``devices`` holds the *current effective* window
    plus its revision counter for convenience.
  - Repairs are append-only rows keyed by ``event_key``.
* Derived state (risk intervals, samples, builds, reports) is rebuildable and
  never hand-edited.
* Report snapshots are immutable.  A new build writes a new report row that
  supersedes the old; the old row remains queryable.
* WAL + BEGIN IMMEDIATE gives readers consistent snapshots while the ingestor
  writes.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Reference data (maintenance policy)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_policies (
    model                TEXT PRIMARY KEY,
    service_interval_days INTEGER NOT NULL,
    note                 TEXT,
    data_revision        INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

-- ---------------------------------------------------------------------
-- Canonical data (append / revise only)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id                     TEXT PRIMARY KEY,
    model                  TEXT NOT NULL,
    site                   TEXT NOT NULL,
    commissioned_date      TEXT NOT NULL,
    observation_start_date TEXT NOT NULL,
    observation_end_date   TEXT,               -- NULL => still under observation
    observation_revision   INTEGER NOT NULL DEFAULT 0,
    observation_note       TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_model ON devices(model);

CREATE TABLE IF NOT EXISTS observation_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL REFERENCES devices(id),
    prev_start    TEXT NOT NULL,
    new_start     TEXT NOT NULL,
    prev_end      TEXT,
    new_end       TEXT,
    note          TEXT,
    data_revision INTEGER NOT NULL,
    recorded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_obs_events_device ON observation_events(device_id);

CREATE TABLE IF NOT EXISTS faults (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      TEXT NOT NULL REFERENCES devices(id),
    event_key      TEXT NOT NULL,             -- stable client key for revisions
    fault_date     TEXT NOT NULL,             -- latest possible date of failure
    fault_date_earliest TEXT,                 -- optional; interval-censored date
    component      TEXT NOT NULL,
    severity       TEXT NOT NULL,             -- minor | major | critical | unknown
    reported_date  TEXT NOT NULL,             -- when it reached us (may be late)
    note           TEXT,
    revision       INTEGER NOT NULL,
    retracted      INTEGER NOT NULL DEFAULT 0,
    data_revision  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(device_id, event_key, revision)
);
CREATE INDEX IF NOT EXISTS idx_faults_device ON faults(device_id);
CREATE INDEX IF NOT EXISTS idx_faults_key ON faults(device_id, event_key, revision);

CREATE TABLE IF NOT EXISTS repairs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id     TEXT NOT NULL REFERENCES devices(id),
    event_key     TEXT NOT NULL,
    fault_event_key TEXT,                      -- optional link to a fault key
    repair_date   TEXT NOT NULL,
    action        TEXT NOT NULL,
    downtime_days INTEGER,
    note          TEXT,
    revision      INTEGER NOT NULL,
    retracted     INTEGER NOT NULL DEFAULT 0,
    data_revision INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE(device_id, event_key, revision)
);
CREATE INDEX IF NOT EXISTS idx_repairs_device ON repairs(device_id);
CREATE INDEX IF NOT EXISTS idx_repairs_key ON repairs(device_id, event_key, revision);

CREATE TABLE IF NOT EXISTS ingest_batches (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   TEXT NOT NULL,
    source        TEXT NOT NULL,
    payload_sha   TEXT NOT NULL UNIQUE,        -- idempotent replay
    data_revision INTEGER NOT NULL,
    accepted      INTEGER NOT NULL,
    skipped       INTEGER NOT NULL,
    rejected      INTEGER NOT NULL,
    detail        TEXT NOT NULL,               -- JSON: per-row outcome
    changed_devices TEXT NOT NULL DEFAULT '[]' -- JSON list for incremental rebuild
);
CREATE INDEX IF NOT EXISTS idx_batches_revision ON ingest_batches(data_revision);

-- ---------------------------------------------------------------------
-- Derived state (rebuildable; never hand-edited)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS builds (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL,            -- full | incremental | dry-run
    strategy         TEXT NOT NULL,            -- materialized | recompute
    as_of            TEXT NOT NULL,
    bands_key        TEXT NOT NULL,
    analysis_params  TEXT NOT NULL,            -- JSON
    data_revision    INTEGER NOT NULL,
    status           TEXT NOT NULL,            -- queued|running|paused|done|failed|cancelled
    control          TEXT NOT NULL DEFAULT 'run',
    stage            TEXT,
    progress         REAL NOT NULL DEFAULT 0,
    total_units      INTEGER,
    done_units       INTEGER NOT NULL DEFAULT 0,
    cursor           TEXT,                     -- last processed device id
    started_at       TEXT,
    finished_at      TEXT,
    error            TEXT,
    cost             TEXT,                     -- JSON measured cost metrics
    mode             TEXT NOT NULL DEFAULT 'apply',
    pid              INTEGER,
    report_id        INTEGER,
    parent_build_id  INTEGER REFERENCES builds(id),
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_builds_status ON builds(status, created_at);

CREATE TABLE IF NOT EXISTS build_inputs (
    build_id      INTEGER NOT NULL REFERENCES builds(id),
    input_kind    TEXT NOT NULL,               -- device | revision | policy
    input_ref     TEXT NOT NULL,
    input_hash    TEXT NOT NULL,
    PRIMARY KEY (build_id, input_kind, input_ref)
);

CREATE TABLE IF NOT EXISTS build_devices (
    build_id        INTEGER NOT NULL REFERENCES builds(id),
    device_id       TEXT NOT NULL,
    status          TEXT NOT NULL,              -- faulted|not_yet_failed|exited|pre_entry_fault|not_yet_observed|void
    pre_entry_days  INTEGER NOT NULL,
    entry_age       REAL NOT NULL,
    followup_age    REAL NOT NULL,
    first_fault_age REAL,
    censor_kind     TEXT NOT NULL,
    entered_sets    TEXT NOT NULL,              -- JSON list of risk-set entries
    basis           TEXT NOT NULL,              -- JSON rationale
    input_hash      TEXT NOT NULL,
    reused          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (build_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_build_devices_hash ON build_devices(device_id, input_hash);

-- Optional cache written only by strategy=materialized.  Closed intervals only;
-- the open tail interval is always recomputed per cutoff.
CREATE TABLE IF NOT EXISTS risk_intervals (
    device_id       TEXT NOT NULL,
    as_of           TEXT NOT NULL,
    bands_key       TEXT NOT NULL,
    interval_index  INTEGER NOT NULL,
    cohort          TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    start_age       REAL NOT NULL,
    end_age         REAL NOT NULL,
    event           INTEGER NOT NULL,
    end_reason      TEXT NOT NULL,              -- band_transition|fault|exit|cutoff
    basis           TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    computed_at     TEXT NOT NULL,
    PRIMARY KEY (device_id, as_of, bands_key, interval_index)
);
CREATE INDEX IF NOT EXISTS idx_risk_intervals_hash
    ON risk_intervals(device_id, bands_key, input_hash);

-- Build-scoped analysis samples: one row per cohort interval, plus one
-- "overall" row per at-risk device carrying its delayed-entry time.
CREATE TABLE IF NOT EXISTS samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id       INTEGER NOT NULL REFERENCES builds(id),
    device_id      TEXT NOT NULL,
    cohort         TEXT NOT NULL,              -- overall|safe|watch|danger
    entry_age      REAL NOT NULL,
    end_age        REAL NOT NULL,
    event          INTEGER NOT NULL,
    censor_kind    TEXT NOT NULL,
    interval_index INTEGER,
    start_date     TEXT,
    end_date       TEXT,
    pre_entry_days INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_samples_build ON samples(build_id, cohort);
CREATE INDEX IF NOT EXISTS idx_samples_device ON samples(build_id, device_id);

CREATE TABLE IF NOT EXISTS sample_meta (
    build_id      INTEGER NOT NULL REFERENCES builds(id),
    cohort        TEXT NOT NULL,
    device_count  INTEGER NOT NULL,
    row_count     INTEGER NOT NULL,
    event_count   INTEGER NOT NULL,
    censor_exited INTEGER NOT NULL,
    censor_open   INTEGER NOT NULL,
    person_days   REAL NOT NULL,
    PRIMARY KEY (build_id, cohort)
);

CREATE TABLE IF NOT EXISTS reports (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    build_id          INTEGER NOT NULL REFERENCES builds(id),
    data_revision     INTEGER NOT NULL,
    strategy          TEXT NOT NULL,
    as_of             TEXT NOT NULL,
    bands_key         TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    title             TEXT NOT NULL,
    payload           TEXT NOT NULL,           -- JSON curves + uncertainty
    created_at        TEXT NOT NULL,
    supersedes        INTEGER REFERENCES reports(id)
);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at);

CREATE TABLE IF NOT EXISTS report_devices (
    report_id      INTEGER NOT NULL REFERENCES reports(id),
    device_id      TEXT NOT NULL,
    status         TEXT NOT NULL,
    pre_entry_days INTEGER NOT NULL,
    entry_age      REAL NOT NULL,
    followup_age   REAL NOT NULL,
    first_fault_age REAL,
    entered_sets   TEXT NOT NULL,              -- JSON
    basis          TEXT NOT NULL,              -- JSON
    PRIMARY KEY (report_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_report_devices_device ON report_devices(device_id);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: Path | str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('data_revision', '0')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('dataset_kind', 'unspecified')"
        )
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """Serialise writers; readers in WAL mode never block."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def bump_revision(conn: sqlite3.Connection) -> int:
    """Must be called inside write_tx."""
    cur = int(get_meta(conn, "data_revision", "0") or "0")
    new = cur + 1
    set_meta(conn, "data_revision", str(new))
    return new


def db_size(path: Path | str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total
