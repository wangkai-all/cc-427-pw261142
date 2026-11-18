# Methodology

This document defines what the numbers are, how the estimator is built, what
the uncertainty bands mean, and what the system deliberately does **not** claim.
It is the reference for `ALGORITHM_VERSION = km-delayed-entry-v1` and
`interval_version = risk-intervals-v1`; changing either constant invalidates
pinned reports on purpose.

The data are fictional throughout. Nothing here is a medical conclusion or a
prediction model.

---

## 1. The unit of analysis

One row per **device**, followed on an **age scale** (days since its service
clock origin). The analysis cutoff `as_of` fixes what is knowable: faults with
a date after the cutoff, reports that arrive after it, and observation endings
after it do not affect that report. A report records the exact
`data_revision` it was built from; appended data later makes it *stale*, never
silently different.

### 1.1 The service clock

The clock origin is commissioning, or the latest repair **at or before the
observation start**. A repair *inside* the observation window does not reset
the survival clock: if a device faults in-window it has already left the
first-fault cohort, so resetting would let it re-enter the risk set after
failing. Repairs are still recorded for maintenance summaries.

### 1.2 The three histories, kept apart

Three things look superficially like "time without a fault" and are never
collapsed:

**Pre-entry time (left truncation).** A device commissioned 300 days before
monitoring began has `pre_entry_days = 300`. Those 300 days are *not* observed
fault-free time. The device joins the risk set at `entry_age = 300` and
contributes nothing at younger ages. Treating commissioning as the start of
observation — the classic left-truncation bias — would overstate early
survival, because devices that failed before observation simply never appear.

**Not yet failed (open censoring).** Under observation at the cutoff with no
fault. Censored at the cutoff as `not_yet_failed`. More data may still change
this device.

**Exited observation (administrative censoring).** The window closed before the
cutoff with no fault — retired, transferred, monitoring removed. Censored at the
exit date as `exited`. It is neither a fault nor the same kind of missingness as
"still running": an exit is a decision, while open observation is the edge of
what is known. The two carry different censor codes in the schema, the sample
rows, the report and the UI.

A device with a fault before observation and no repair has an unknown entry
state; it is `pre_entry_fault` and is **excluded** from risk sets rather than
assumed fault-free. A device whose observation starts after the cutoff is
`not_yet_observed`; a window ending before it starts is `void`. Both are
reported in the composition table and excluded from curves.

### 1.3 Maintenance-phase risk sets

Each device's clock position is `elapsed_days / service_interval_days` for its
model. The interval is a policy constant supplied with the data, not a learned
parameter:

| set | fraction of service interval |
|---|---|
| `safe` | `[0.00, 0.34)` |
| `watch` | `[0.34, 0.67)` |
| `danger` | `[0.67, ∞)` |

The per-device intervals are contiguous: they start at `entry_age`, break at
band boundaries, and end at the first in-window fault, the observation exit, or
the cutoff. Each interval becomes one observation in its band cohort, entry at
the interval start, censored as `band_transition` at a boundary — a device can
therefore contribute to several band cohorts. The `overall` cohort instead
takes one observation per device: entry at `entry_age`, end at first in-window
fault / exit / cutoff. It does not depend on the partition.

Risk sets are a **bookkeeping label** used to stratify descriptive curves. They
are not probabilities, severity grades, or risk scores.

---

## 2. Estimator

Kaplan–Meier product-limit estimate on the age scale with delayed entry and
right censoring.

At each distinct time *t* on the grid of entry times, event times and censoring
times:

1. add devices whose observation **starts** at *t* to the risk set;
2. apply events at *t*;
3. apply censorings at *t*, then remove them.

So a device censored at *t* is counted at risk at *t*, and an event and a
censoring at the same *t* are ordered events-first. Devices enter at their
entry age, never at 0. The product-limit update at an event time is
`S(t) = S(t−) · (1 − d_t / n_t)`, where `d_t` is the number of events and `n_t`
the risk-set size *before* removals at *t*.

Events after the largest observed age do not exist by construction; the curve
is flat from the last knot to the largest observed age, and nothing is drawn
beyond it.

### 2.1 Summaries

- **Median age.** The first observed crossing of `S = 0.5`, linearly
  interpolated between the two bracketing knots (both knots are stated in the
  report). If `S` never reaches 0.5, the report says **"median not reached
  within observed ages; no value extrapolated"** and prints no number.
- **Restricted mean age.** Area under the step curve from 0 (or the earliest
  observed age) to the **largest observed age**. It is explicitly not an
  estimate beyond that age.
- **At-risk table.** Counts at a fixed age grid. This is where a thin tail is
  visible: a final survival value resting on one or two devices is shown with
  its at-risk count beside it.
- **Person-days.** Total observed exposure, reported for context.

### 2.2 The naive average, shown only as a contrast

Every report includes the mean age at first fault computed **only over faulted
devices**, alongside how many censored devices that omits, the KM median, and
the restricted mean. The naive figure is biased toward short lives by
construction because devices that survived to the cutoff or exited without a
fault are dropped. It appears in the report only so the difference is explicit;
no part of the system uses it as a lifetime estimate.

---

## 3. Uncertainty

Two 95% intervals are reported for every curve.

**Greenwood with a log–log transform.** Variance
`Var(t) = S(t)² · Σ d_i / (n_i (n_i − d_i))` over event times up to *t*. The band
is `S^{exp(±z·se)}` with `se = sqrt(Var) / |log S|`, which keeps it inside
(0, 1) and behaves better in the tail than the plain Greenwood interval. The
band is **null** where it is not meaningful: before the first event, at
`S = 0` or `S = 1`, and once the risk set empties. The report states that
Greenwood can be optimistic when the tail rests on very few devices.

**Seeded device-level bootstrap.** Resample **whole devices** with replacement,
recompute the curve, and take the 2.5th/97.5th percentiles at each distinct
age. Devices are the resampling unit because a device's intervals are dependent
and resampling rows would understate variance. The seed is fixed (default
`20261118`) and printed in the report, so the band is exactly reproducible.
Bootstrapped percentiles are sorted before quantiles are taken; every point
satisfies `lower ≤ upper` (this is asserted in the tests).

The two bands answer different questions — an analytic variance approximation
versus a resampling distribution — and they need not agree. Where they diverge,
the tail is thin; the at-risk table is the evidence. Neither is a confidence
statement about future devices, and neither is extrapolated past the observed
age range.

---

## 4. Revisions, staleness and rebuild cost

### 4.1 Append-only facts

`(device_id, event_key)` identifies a fault or repair. A correction is the same
key sent with different values; the ingestor appends a new row with `revision`
one higher (the server assigns it). A retraction is a new row with
`retracted = true`. Nothing is updated or deleted, so the history of what was
believed and when is preserved. Observation windows are event-sourced the same
way through `observation_events` (previous and new start/end per change).

Batch payloads are hashed; a replay is a no-op.

### 4.2 Reports are immutable

A report is never rewritten. Building again appends a new report whose
`supersedes` points at the previous one. A report is **stale** whenever the
current `data_revision` differs from the one it recorded. The UI and
`cli reports` show staleness explicitly.

### 4.3 Two build strategies, one statistic

| | `materialized` | `recompute` |
|---|---|---|
| closed intervals | written to `risk_intervals`, reused while the device's canonical input hash is unchanged | re-derived every build |
| open tail | recomputed per cutoff | recomputed per cutoff |
| invalidation | cache key includes the input hash; a policy or band change cannot be masked by a stale cache | none |
| first build | compute **plus** cache writes (no reuse yet) | compute only |
| later builds | reuses unchanged devices | full cost every time |

The strategy changes caching and write cost, never the estimator. Both arms are
run and measured on the same data by `compare_strategies`, and the tests assert
the resulting curves are identical.

**Why not always recompute?** Because the measured trade-off is explicit:
materialized costs more rows and disk and pays invalidation bookkeeping, but
incremental rebuilds then touch only devices whose faults, repairs, observation
events or model policy changed. **Why not always materialize?** Because with no
prior cache its first build is strictly slower, and a small dataset that changes
wholesale gains nothing. The choice is shown with measured numbers, not argued.

### 4.4 Incremental builds and resumability

An incremental build (`--since-build N`) computes the changed-device set from
the parent's data revision, copies every unchanged device's rows from the
parent, and rebuilds only the changed ones; it must use the parent's `as_of`
because a different cutoff would mix window lengths. The report records the
parent, the changed device list, and how many devices were inherited.

Builds are chunked by device (`run(build_id, max_units)`) and resumable: the
cursor is the last *completed* device, so resuming can never skip work, and
per-device writes are idempotent, so a crash mid-build leaves no duplicates.
Pause, cancel and resume are exposed through the CLI and the UI.

---

## 5. What this system does not claim

These are printed in every report and shown on the Method tab:

- Data are fictional and synthetic. No medical, clinical or individual
  diagnostic conclusion may be drawn.
- Curves are descriptive. They are not a prediction model, a failure-rate
  forecast, or an extrapolation beyond observed ages.
- Devices entering observation after commissioning contribute only from their
  entry age; earlier ages are not estimated.
- `not_yet_failed` and `exited` are different censoring reasons, reported
  separately; neither is a fault.
- Risk sets are a maintenance-phase partition, not a probability or a severity
  grade.
- A late-reported fault counts for any cutoff after the fault date; a report's
  data revision identifies exactly which revisions it used.
- Bootstrap bands are seeded and reproducible; Greenwood bands can be
  optimistic where very few devices support the tail.
- No lifetime is computed as an average over fault records. Where such an
  average appears, it is labelled as a biased contrast.

---

## 6. Reproducing the numbers

```bash
python -m reliability.demo_run --workdir /tmp/fleet-demo --devices 400 --seed 7
```

writes `/tmp/fleet-demo/SUMMARY.md` from an empty directory: intake counts per
batch, fleet composition, the per-cohort KM table with all three censoring
counts, the at-risk table, the Greenwood and bootstrap statements, the naive
contrast, the measured strategy-cost table, and the late-report incremental
rebuild — followed by the non-claims above. The fictional generator and its
seed are described in [`fictional-data.md`](fictional-data.md).

The test suite (`python -m pytest tests/ -q`, 55 tests) pins the estimator's
behaviour: delayed entry, the separate censor codes, ties, median interpolation,
restricted mean, Greenwood nulls, bootstrap determinism and ordering, and
strategy equivalence.
