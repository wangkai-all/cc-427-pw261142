# Fleet reliability — descriptive survival from append-only maintenance data

Three independently runnable services over one SQLite file:

| service | entry point | owns |
|---|---|---|
| **Ingestor** | `python -m reliability.cli ingest`, `python -m reliability.web` (`POST /api/ingest`) | append-only fault, repair and observation-window events |
| **Sample builder** | `python -m reliability.cli build`, or a build queued in the UI | per-device risk intervals and Kaplan–Meier reports |
| **Report service** | `python -m reliability.web` | read API, charts, device drill-down, build control |

The services share nothing but the database path, so each can be started,
stopped and upgraded on its own. Module boundaries are one-directional: the
ingestor imports no analysis code. The report service reads canonical data and
reports, and owns exactly two write paths — it forwards an observation-window
revision to the ingestor (which appends an event) and requests builds through
the builder. It never writes facts itself.

All data in this repository is **fictional** (generated, seeded, reproducible).
Nothing here is a medical finding, a diagnosis, or a trained prediction model.

---

## Quickstart, from an empty directory

Requires Python 3.10+ and Flask (`pip install flask`). No other dependencies.

```bash
# 1. end-to-end run: generate fictional batches, ingest, build, compare
#    strategies, add a late fault, rebuild incrementally
python -m reliability.cli demo --reset

# 2. start the report service (same database the demo just wrote)
python -m reliability.web
#    open http://127.0.0.1:8701
```

Defaults: database `var/fleet.db`, UI on `127.0.0.1:8701`, cutoff
`2026-09-30`. Override with `RELIABILITY_DB`, `RELIABILITY_PORT`,
`RELIABILITY_HOST`, `RELIABILITY_ARTIFACTS`, or `--db` / `--as-of` on the CLI.

### A fuller written report

```bash
python -m reliability.demo_run --workdir /tmp/fleet-demo --devices 400
```

This is the same pipeline but writes `/tmp/fleet-demo/SUMMARY.md` with the
actual statistics (fleet composition, per-cohort KM table, at-risk table,
Greenwood and bootstrap bands, the naive-average contrast, the measured
strategy-cost table, the late-report rebuild), plus `devices.csv` and
`devices.json` for per-device entry ages and risk-set membership. It prints the
same text. Start from a fresh directory with `--workdir`; `--no-reset` keeps an
existing one.

### Manual pipeline (each service on its own)

```bash
export RELIABILITY_DB=/tmp/fleet/fleet.db

python -m reliability.cli init
python -m reliability.cli generate-fictional /tmp/fleet/batches --devices 240
python -m reliability.cli ingest-dir /tmp/fleet/batches
python -m reliability.cli build --as-of 2026-09-30 --strategy materialized
python -m reliability.cli compare --as-of 2026-09-30
python -m reliability.cli reports
python -m reliability.cli device DEV-0007
```

Late reports and corrections are just more batches against the same devices:

```bash
python -m reliability.cli ingest late-faults.json          # append-only
python -m reliability.cli build --as-of 2026-09-30 \
    --since-build 1 --kind incremental                     # only changed devices
```

`--since-build` computes the changed-device set from the parent build's data
revision, copies every unchanged device from the parent, and rebuilds only the
rest. The old report is never rewritten; the new one records what supersedes it.

---

## What the data can express

A batch is one JSON object. Every list is optional:

```json
{
  "batch_id": "2026-11-02-nightly",
  "source": "site-collector",
  "model_policies": [{"model": "M-100", "service_interval_days": 365}],
  "devices": [{
    "id": "DEV-0007", "model": "M-100", "site": "north",
    "commissioned_date": "2022-03-01",
    "observation_start_date": "2022-04-15",
    "observation_end_date": null
  }],
  "faults": [{
    "device_id": "DEV-0007", "event_key": "DEV-0007-F1",
    "fault_date": "2024-06-02", "fault_date_earliest": "2024-05-20",
    "component": "door-sensor", "severity": "major",
    "reported_date": "2024-06-05"
  }],
  "repairs": [{
    "device_id": "DEV-0007", "event_key": "DEV-0007-R1",
    "fault_event_key": "DEV-0007-F1", "repair_date": "2024-06-09",
    "action": "replaced sensor", "downtime_days": 2
  }],
  "observation_updates": [{
    "device_id": "DEV-0007",
    "observation_start_date": "2022-04-15",
    "observation_end_date": "2026-08-31",
    "note": "site closed; monitoring removed"
  }]
}
```

Rules the ingestor enforces (a bad row is rejected, the batch still applies):

- **Idempotent**: the SHA-256 of the payload is unique. Replaying a batch is a
  no-op, reported as `duplicate_batch`.
- **Append-only revisioning**: `(device_id, event_key)` identifies an event.
  A correction is the same key sent again with different values; the ingestor
  writes it as a *new row* with `revision` one higher (the server assigns the
  number; clients do not send one). A retraction is a new row with
  `retracted: true`. There is no UPDATE and no DELETE. `repo._latest_by_key`
  takes the highest revision per key.
- **Interval-censored fault dates**: `fault_date_earliest` records "faulted
  somewhere in [fault_date_earliest, fault_date]"; it must be ≤ `fault_date`
  and ≥ commissioning. The point date is used for the event age.
- **Observation windows are event-sourced**: every `observation_updates` entry
  appends to `observation_events` with the previous and new start/end, so the
  window at any revision can be reconstructed. Closing a window turns
  "not yet failed" into "exited", not into a fault.
- A fault reported after its device's window closed is **not** counted as an
  event in the window; it appears in the device's fault list and, if it is the
  first fault and predates observation, as `pre_entry_fault`.

---

## The three histories that must not be merged

These are distinct statuses end to end — database column, sample row, report
payload, chart, and per-device drill-down:

| status | meaning | in a risk set? | censor code |
|---|---|---|---|
| `not_yet_failed` | under observation at cutoff, no fault | yes, until cutoff | `not_yet_failed` (open) |
| `exited` | observation administratively closed before cutoff, no fault | yes, until exit | `exited` |
| `pre_entry_fault` | fault before observation with no repair — entry state unknown | **excluded** | — |

Plus two bookkeeping statuses shown in the composition table but never in a
curve: `not_yet_observed` (observation begins after the cutoff) and `void`
(window ends before it starts).

The fourth, easy-to-miss history — **time run before observation began** — is
not a status but an offset. `pre_entry_days` is kept on every device, and it
becomes the delayed-entry age at which the device joins the risk set. Those
earlier ages are never counted as fault-free time.

---

## What the statistics are

Kaplan–Meier fault-free survival on the **age scale**, with delayed entry and
right censoring. The estimator, the band construction, the handling of ties and
the explicit non-claims are in [`docs/methodology.md`](docs/methodology.md).

The short version:

- Each device contributes exposure intervals on its service clock (commissioning
  → first fault, or → exit, or → cutoff), resetting at recorded repairs.
- It enters each risk set at its entry age, not at 0.
- Faults are events; open and exit endings are right-censored with **different
  codes**, and band transitions are censored separately as administrative.
- Uncertainty: Greenwood variance with a log–log 95% band, **and** a seeded
  device-level bootstrap (resampling whole devices, not intervals).
- Median and restricted mean are reported only inside the observed age range. If
  the median is not reached, the report says so and extrapolates nothing.
- The naive mean age-at-fault over fault records is shown **only as a
  contrast**, with the number of censored devices it silently drops.

No lifetime is "computed as an average over fault records" anywhere in the
system, and the report says so in its own naive-comparison block.

---

## The two build strategies, and their measured cost

`materialized` writes each device's closed risk intervals to `risk_intervals`
and reuses them while the device's canonical input hash is unchanged; only the
open tail interval is recomputed for a new cutoff. `recompute` derives every
device every time and writes no interval cache.

Both produce identical curves — this is asserted in the tests and stated in
every comparison. What differs is measured cost: derive time, interval rows
written, cache hits. `compare_strategies()` performs both derivations on the
same data and reports the numbers, so the choice is made on evidence rather than
assertion. The Strategy tab in the UI shows the same measurement as a bar
comparison, and the build form lets you pick either.

Incremental builds are a separate, complementary lever: they rebuild only the
devices whose faults, repairs, observation events or model policy changed
(`repo.changed_devices_since`) and copy the rest from the parent build, which is
why they require the parent's `as_of`.

---

## Web UI

`python -m reliability.web` serves seven views:

1. **Curves & uncertainty** — KM step curves for the overall cohort and the
   three maintenance bands, with 95% band, crosshair tooltip, direct labels,
   at-risk table and a fixed-grid table view. Reports can be compared pairwise.
2. **Devices** — search/filter, then a device page with the observed lifetime
   on the service clock, the risk intervals at a cutoff, *why each risk set was
   entered*, effective faults and their revision history, and the
   observation-window history. The window can be revised from this page (it
   appends an `observation_events` row and marks affected reports stale).
3. **Builds** — queue a build (cutoff, strategy, parent, bootstrap reps), watch
   progress, **pause / resume / cancel**; a resumed build continues from its
   stored per-device cursor and produces no duplicate rows.
4. **Strategy cost** — measured side-by-side comparison of the two strategies.
5. **Reports** — every report, its data revision, whether it is stale, and its
   lineage (`supersedes` / `superseded by`, parent build, changed devices).
6. **Data intake** — paste a batch, submit, see accepted / skipped / rejected
   rows and the resulting data revision.
7. **Method** — the estimator and the explicit non-claims, in the UI itself.

Charts follow a validated four-hue categorical palette (light and dark
variants); identity is never color alone — every series has a legend entry, a
direct label where it fits, and a row in the table view.

---

## Repository layout

```
reliability/
  db.py          schema + connection helpers (WAL, BEGIN IMMEDIATE)
  model.py       device history, statuses, service clock, input hashing
  ingestor.py    append-only, idempotent batch ingestion
  repo.py        canonical reads, change detection, device detail
  intervals.py   per-device risk intervals + the basis for each
  stats.py       Kaplan–Meier, Greenwood, bootstrap, at-risk tables
  builder.py     chunked resumable builds, reports, strategy comparison
  web.py         report service (Flask JSON API + static UI)
  cli.py         standalone CLI for all three services + demo
  demo_run.py    end-to-end run that writes SUMMARY.md with real numbers
  fictional.py   deterministic fictional data generator (seed 7 by default)
web/             index.html, style.css, app.js (no build step)
tests/           pytest suite (55 tests)
docs/            methodology, data generator
```

---

## Tests

```bash
python -m pytest tests/ -q
```

Covers idempotent replay, revision/retraction semantics, interval censoring,
observation event-sourcing, delayed entry vs pre-entry, the three censoring
states, KM medians and ties, Greenwood nulls, bootstrap determinism and band
ordering, full vs incremental equality, pause/resume and crash-resume without
duplicates, dry runs, and strategy equivalence.

---

## Environment notes

- Python 3.10+; only Flask beyond the standard library.
- SQLite in WAL mode; the ingestor and builder may run while the web service is
  reading. Writes use `BEGIN IMMEDIATE` with a busy timeout.
- The web UI is static — no npm, no bundler. Open it from the Flask service.
