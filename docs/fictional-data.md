# Fictional data generator

`reliability/fictional.py` produces a synthetic maintenance dataset designed to
exercise the awkward cases a real intake pipeline meets. It is deterministic:
`generate(seed, n_devices)` returns the same batches for the same seed, and
`write_batches(dir, seed, n_devices)` writes them as JSON files in reporting
order. Nothing is real — no equipment, site, person or clinical variable is
involved.

## Shape

- `n_devices` devices (default 400; the CLI demo defaults to 240, `demo_run.py`
  to 400), across three fictional models with different maintenance intervals.

  | model | service interval |
  |---|---|
  | `MX-100` | 180 days |
  | `MX-220` | 240 days |
  | `LX-50` | 120 days |

- Commissioned across roughly seven years before the cutoff `2026-09-30`, so
  observed ages span a wide range and some devices are under observation almost
  from commissioning.
- Six components (`drive-motor`, `bearing-assembly`, `control-board`,
  `coolant-pump`, `door-sensor`, `power-supply`) and five severity labels.

## Deliberate hard cases

| case | how it appears | what it tests |
|---|---|---|
| pre-entry running | commissioned up to 700 days before monitoring begins | left truncation / delayed entry |
| unknown entry state | a fault before observation with no repair, every 97th device | `pre_entry_fault` exclusion from risk sets |
| late reports | faults reported 120–420 days after the fault date | late/backfilled data is still counted by the cutoff rule |
| corrections | component rewritten on an earlier report | append-only revision (`revision` 1) |
| retraction | an earlier fault withdrawn | retracted rows drop out of the canonical set |
| interval-censored dates | `fault_date_earliest` from a quarterly inspection | bounds recorded, point date used for the event age |
| second faults and repairs | ~15% of faulted devices fault again; ~55% of early faults are repaired | repairs on the service clock |
| observation moved later | monitoring actually started 10–60 days after the recorded date | window revision event-sourcing |
| observation closed | removed from monitoring before the cutoff with no fault | `exited` censoring, distinct from open |
| observation re-opened | one closure reinstated in the last batch | window history is reconstructible |
| open censoring | devices still under observation at the cutoff | `not_yet_failed` censoring |
| mixed fault timing | faults in early, middle and recent windows | the full age range is populated |

## Batches, in reporting order

| # | batch | contents |
|---|---|---|
| 1 | `fictional-001-baseline` | model policies + all devices with their initial windows |
| 2 | `fictional-002-faults-through-2024h1` | early faults, second faults, pre-entry faults |
| 3 | `fictional-003-repairs` | repairs for a subset of early faults |
| 4 | `fictional-004-late-faults` | faults reported long after they happened |
| 5 | `fictional-005-corrections` | component corrections, a retraction, interval-censored dates |
| 6 | `fictional-006-observation-revisions` | windows moved later and closed |
| 7 | `fictional-007-recent-faults` | recent faults, plus one re-opened window |

Each batch has the form documented in the README and is ingested through the
same path as real data — there is no special case for fictional input.

## Reproducing

```bash
python -m reliability.cli generate-fictional /tmp/batches --seed 7 --devices 400
python -m reliability.cli ingest-dir /tmp/batches
```

The strategy-cost comparison and the demo's late-report step are measured on
whatever data is present; they are not hard-coded to this generator.
