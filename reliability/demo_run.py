"""End-to-end fictional-data run, writing a plain-text statistical summary.

From an empty directory this:

1. generates fictional batches (deterministic seed);
2. ingests them one at a time, including late reports, corrections and
   observation revisions;
3. runs a full materialized build;
4. measures both build strategies on the same data;
5. appends one late fault and runs an incremental rebuild;
6. writes ``SUMMARY.md`` with the actual numbers and the uncertainty
   statement, plus per-device CSV/JSON only for the fictional devices.

It prints the same content to stdout.  Everything it reports is descriptive.

Usage::

    python -m reliability.demo_run --workdir /tmp/fleet-demo
    python -m reliability.demo_run --workdir /tmp/fleet-demo --devices 400
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

from . import db as dbm
from .builder import Builder, compare_strategies
from .fictional import write_batches
from .ingestor import Ingestor
from .repo import changed_devices_since, revision_of


def _hr(title: str) -> str:
    return f"\n## {title}"


def run(workdir: Path, seed: int, devices: int, as_of: date,
        bootstrap_reps: int, reset: bool) -> Path:
    if reset and workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    data_dir = workdir / "batches"
    db_path = workdir / "fleet.db"

    lines: list[str] = []
    log = lines.append

    log("# Fictional fleet reliability — actual run")
    log("")
    log(f"Generated {date.today().isoformat()} from an empty directory. "
        f"Seed {seed}, {devices} devices, cutoff {as_of.isoformat()}.")
    log("")
    log("> All data are synthetic. Nothing below is a medical finding, an "
        "individual diagnosis, or a prediction. The estimates are descriptive "
        "survival summaries with uncertainty, restricted to observed ages.")

    dbm.init_db(db_path)
    conn = dbm.connect(db_path)
    try:
        paths = write_batches(data_dir, seed=seed, n_devices=devices)
        log(_hr("Data intake"))
        log("Batches are ingested in reporting order; each is append-only and "
            "idempotent.")
        log("")
        log("| batch | accepted | skipped | rejected | data revision | changed devices |")
        log("|---|---|---|---|---|---|")
        for p in paths:
            payload = json.loads(p.read_text())
            res = Ingestor(conn, source="demo").ingest(payload)
            log(f"| {p.stem} | {res.accepted} | {res.skipped} | {res.rejected} "
                f"| {res.data_revision} | {len(res.changed_devices)} |")

        builder = Builder(conn)
        full_bid = builder.create_build(
            as_of, "materialized", kind="full", bootstrap_reps=bootstrap_reps,
            title="Fictional fleet — full build")
        full_out = builder.run(full_bid)
        report = _load_report(conn, full_out["report_id"])

        log(_hr("Fleet composition"))
        log("")
        log("| status | devices | meaning |")
        log("|---|---|---|")
        meanings = {
            "faulted": "first fault observed inside the window",
            "not_yet_failed": "under observation at cutoff, no fault (censored, open)",
            "exited": "observation closed before cutoff, no fault (censored, exit)",
            "pre_entry_fault": "fault before observation with no repair — entry state unknown, excluded",
            "not_yet_observed": "observation begins after cutoff",
            "void": "invalid observation window",
        }
        for k, v in sorted(report["status_counts"].items(),
                           key=lambda kv: -kv[1]):
            log(f"| {k} | {v} | {meanings.get(k, '')} |")

        tr = report["truncation"]
        log("")
        log(f"**Pre-entry (left truncation).** {tr['with_pre_entry']} of "
            f"{tr['count']} devices entered observation after commissioning; "
            f"median gap {tr['median']:.0f} d, max {tr['max']} d. Those ages are "
            f"**not** in any risk set and are not estimated.")

        log(_hr("Survival by cohort (Kaplan–Meier, delayed entry)"))
        log("")
        log("| cohort | devices | interval rows | first faults | open-censored | "
            "exit-censored | band-transition-censored | median age | restricted mean age |")
        log("|---|---|---|---|---|---|---|---|---|")
        for key in ("overall", "safe", "watch", "danger"):
            c = report["cohorts"][key]
            med = ("not reached" if c["median_age"] is None
                   else f"{c['median_age']:.0f} d")
            rm = ("—" if c["restricted_mean_age"] is None
                  else f"{c['restricted_mean_age']:.0f} d")
            log(f"| {key} | {c['n_devices']} | {c['n_intervals']} | "
                f"{c['n_events']} | {c['n_censored_open']} | "
                f"{c['n_censored_exited']} | {c['n_censored_transition']} | "
                f"{med} | {rm} |")

        overall = report["cohorts"]["overall"]
        log("")
        log("**At-risk table (overall cohort).** A tail resting on one or two "
            "devices is visible here; no estimate is extrapolated past the "
            "largest observed age.")
        log("")
        log("| age (d) | at risk | cumulative faults | survival |")
        log("|---|---|---|---|")
        surv = _survival_step(overall["points"])
        for row in overall["at_risk_table"]:
            s = surv(row["age"])
            log(f"| {row['age']:.0f} | {row['at_risk']} | {row['events_cum']} | "
                f"{'—' if s is None else f'{s:.3f}'} |")

        log(_hr("Uncertainty"))
        boot = overall["bootstrap"]
        last_gw = [g for g in overall["greenwood"] if g["lower"] is not None]
        log("")
        log(f"* Greenwood 95% band (log–log): available from the first event "
            f"until the curve reaches the edge of the data. At the last age with "
            f"a finite band the estimate is "
            f"{last_gw[-1]['survival']:.3f} "
            f"[{last_gw[-1]['lower']:.3f}, {last_gw[-1]['upper']:.3f}]."
            if last_gw else
            "* Greenwood band: not available (no event with a finite risk set).")
        log(f"* Bootstrap 95% band: {boot['reps']} device-level resamples, seed "
            f"{boot['seed']} (reproducible). It resamples whole device histories, "
            f"not rows, because a device's intervals are dependent.")
        if boot["ages"]:
            i = len(boot["ages"]) - 1
            log(f"* At the largest bootstrapped age ({boot['ages'][i]:.0f} d) the "
                f"bootstrap band is [{boot['lower'][i]:.3f}, {boot['upper'][i]:.3f}].")
        log(f"* The two bands answer the same question differently; where they "
            f"disagree, the tail is thin. Neither is a confidence claim about "
            f"future devices.")
        overall_median = (
            "not reached — no value extrapolated"
            if overall["median_age"] is None
            else f"{overall['median_age']:.0f} d"
        )
        log(f"* Median age of first observed fault (overall): {overall_median}.")

        naive = report["naive_comparison"]
        log(_hr("Why an average of fault records is not an average lifetime"))
        log("")
        log(f"* Naive mean age at first fault (faulted devices only): "
            f"**{naive['naive_mean_age_at_fault']:.0f} d** over "
            f"{naive['naive_n_faulted']} devices.")
        log(f"* That naive figure drops {naive['censored_devices_excluded_from_naive']} "
            f"devices that left observation without a fault, so it is biased "
            f"toward short lives by construction.")
        km_median = (
            "not reached" if naive["km_median_age"] is None
            else f"{naive['km_median_age']:.0f} d"
        )
        log(f"* Kaplan–Meier median: {km_median}; "
            f"restricted mean {naive['km_restricted_mean_age']:.0f} d "
            f"(area under the curve to the last observed age).")
        log("* Neither number is a lifetime estimate beyond the observed range.")

        log(_hr("Build-strategy cost (measured, same statistics)"))
        cmp = compare_strategies(conn, as_of)
        log("")
        log("| strategy | measured derive time | intervals derived | devices reusing cache | rows written if materialized |")
        log("|---|---|---|---|---|")
        for key, arm in cmp["arms"].items():
            log(f"| {key} | {arm['elapsed_ms']} ms | {arm['intervals_derived']} | "
                f"{arm['devices_reusing_cache']} | "
                f"{arm['interval_rows_persisted_if_materialized']} |")
        log("")
        log(cmp["headline"])
        log("")
        log("The choice trades storage and cache invalidation for rebuild cost. "
            "It does not change any curve above.")

        log(_hr("Late report and incremental rebuild"))
        # Pick a device that is censored at the cutoff, so a genuinely late
        # fault turns an open tail into an observed event and the estimate
        # actually moves.  The report is queried, not assumed.
        target = conn.execute(
            """
            SELECT rd.device_id, d.observation_start_date, d.observation_end_date
            FROM report_devices rd
            JOIN devices d ON d.id = rd.device_id
            WHERE rd.report_id=? AND rd.status='not_yet_failed'
            ORDER BY rd.device_id LIMIT 1
            """,
            (full_out["report_id"],),
        ).fetchone()
        if target is None:
            raise RuntimeError("no open-censored device available for the late report")
        late_device = target["device_id"]
        obs_start = date.fromisoformat(target["observation_start_date"])
        obs_end = min(
            date.fromisoformat(target["observation_end_date"] or as_of.isoformat()),
            as_of,
        )
        fault_date = min(obs_start + timedelta(days=150), obs_end)
        late = {
            "batch_id": "demo-late-fault",
            "source": "demo",
            "faults": [{
                "device_id": late_device,
                "event_key": f"{late_device}-LATE",
                "fault_date": fault_date.isoformat(),
                "component": "door-sensor",
                "severity": "major",
                "reported_date": "2026-09-20",
                "note": "found during final audit; reported late",
            }],
        }
        res = Ingestor(conn, source="demo").ingest(late)
        prev = conn.execute("SELECT * FROM builds WHERE id=?", (full_bid,)).fetchone()
        changed = changed_devices_since(conn, int(prev["data_revision"]))
        inc_bid = builder.create_build(
            as_of, "materialized", kind="incremental", parent_build_id=full_bid,
            changed_devices=changed, bootstrap_reps=bootstrap_reps,
            title="Fictional fleet — after late report")
        inc_out = builder.run(inc_bid)
        inc = _load_report(conn, inc_out["report_id"])
        log("")
        log(f"* Late fault accepted at data revision {res.data_revision}; "
            f"changed devices: {', '.join(res.changed_devices)}.")
        log(f"* Incremental build #{inc_bid} rebuilt "
            f"{inc_out['metrics']['devices_worked']} device(s) and copied "
            f"{inc_out['metrics']['devices_inherited']} unchanged device(s) "
            f"from build #{full_bid}.")
        log(f"* Old report #{full_out['report_id']} is kept unchanged and is now "
            f"**stale** (built at revision "
            f"{report['meta']['data_revision']}; current "
            f"{revision_of(conn)}).")
        before = report["cohorts"]["overall"]
        after = inc["cohorts"]["overall"]
        log(f"* Overall cohort: {before['n_events']} → {after['n_events']} "
            f"observed first faults; median "
            f"{_fmt_age(before['median_age'])} → {_fmt_age(after['median_age'])}.")

        _write_per_device(workdir, conn, as_of, inc_out["report_id"])
        log(_hr("Per-device output"))
        log("")
        log("`devices.csv` and `devices.json` list every fictional device with "
            "its status, pre-entry days, entry age, and the risk sets it "
            "entered, plus the basis for each classification. These are "
            "bookkeeping labels from the maintenance-clock partition, not "
            "risk scores.")

        log(_hr("What this report does not claim"))
        log("")
        for c in report["caveats"]:
            log(f"* {c}")

        summary = "\n".join(lines) + "\n"
        (workdir / "SUMMARY.md").write_text(summary)
    finally:
        conn.close()
    return workdir / "SUMMARY.md"


def _survival_step(points: list[dict]):
    """Right-continuous step lookup: value in force at each age."""
    ordered = sorted(points, key=lambda p: p["age"])

    def value(age: float):
        s = None
        for p in ordered:
            if p["age"] <= age + 1e-9:
                s = p["survival"]
            else:
                break
        return s

    return value


def _fmt_age(v) -> str:
    return "not reached" if v is None else f"{v:.0f} d"


def _load_report(conn, report_id) -> dict:
    row = conn.execute("SELECT payload FROM reports WHERE id=?", (report_id,)).fetchone()
    return json.loads(row["payload"])


def _write_per_device(workdir: Path, conn, as_of: date, report_id: int) -> None:
    rows = conn.execute(
        "SELECT * FROM report_devices WHERE report_id=? ORDER BY device_id",
        (report_id,),
    ).fetchall()
    csv_path = workdir / "devices.csv"
    json_path = workdir / "devices.json"
    out_json = []
    with csv_path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["device_id", "status", "pre_entry_days", "entry_age",
                    "followup_age", "first_fault_age", "entered_risk_sets"])
        for r in rows:
            sets = json.loads(r["entered_sets"] or "[]")
            w.writerow([r["device_id"], r["status"], r["pre_entry_days"],
                        r["entry_age"], r["followup_age"],
                        "" if r["first_fault_age"] is None else r["first_fault_age"],
                        ";".join(sets)])
            out_json.append({
                "device_id": r["device_id"],
                "status": r["status"],
                "pre_entry_days": r["pre_entry_days"],
                "entry_age": r["entry_age"],
                "followup_age": r["followup_age"],
                "first_fault_age": r["first_fault_age"],
                "entered_risk_sets": sets,
                "basis": json.loads(r["basis"] or "{}"),
            })
    json_path.write_text(json.dumps({
        "as_of": as_of.isoformat(),
        "report_id": report_id,
        "note": "fictional devices; risk sets are maintenance-phase labels, "
                "not predictions",
        "devices": out_json,
    }, indent=2) + "\n")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="reliability.demo_run")
    p.add_argument("--workdir", required=True)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--devices", type=int, default=400)
    p.add_argument("--as-of", default="2026-09-30")
    p.add_argument("--bootstrap-reps", type=int, default=300)
    p.add_argument("--no-reset", action="store_true")
    args = p.parse_args(argv)
    path = run(Path(args.workdir), args.seed, args.devices,
               date.fromisoformat(args.as_of), args.bootstrap_reps,
               reset=not args.no_reset)
    print(path.read_text())
    print(f"\nWritten to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
