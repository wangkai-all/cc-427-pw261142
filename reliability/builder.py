"""Sample builder: derives cohorts and writes versioned reports.

Two strategies, identical statistics
------------------------------------
``materialized``
    Per-device closed intervals are persisted in ``risk_intervals`` and reused
    by later builds while the device's canonical input hash is unchanged.  Only
    the open tail is recomputed.  Cheap incremental rebuilds, more storage.

``recompute``
    Every build re-derives every device from canonical data in memory and
    writes no interval cache.  Same numbers, no invalidation logic, full cost
    per build.

Neither strategy computes a lifetime by averaging fault records.  The report's
``naive_comparison`` block shows that number explicitly next to the censored
estimates, so the difference is visible rather than asserted.

Interruptible by design
-----------------------
Each device is a unit of work committed in its own transaction together with
the build cursor.  Pausing stops after the current device; resuming continues
from the cursor; a crashed build can be resumed without duplicating samples.

Late data and revisions
-----------------------
An incremental build takes a parent build and a set of changed devices (from
ingest batches, or recomputed from revision numbers).  It recomputes only those
devices and copies the parent's rows for the rest.  A new report supersedes the
old one; the old report stays readable and is flagged stale at read time.
"""
from __future__ import annotations

import json
import os
import time
from datetime import date

from . import db as dbm
from .config import ALGORITHM_VERSION, STRATEGIES
from .intervals import ALGORITHM_VERSION as INTERVAL_VERSION
from .intervals import DeviceDerivation, Interval, derive_device
from .model import BAND_KEY_VERSION, RISK_SETS, now_iso
from .repo import (
    json_dumps,
    load_histories,
    load_policies,
    revision_of,
)
from .stats import (
    Observation,
    at_risk_table,
    bootstrap_band,
    greenwood_band,
    km_curve,
)

COHORTS = ("overall",) + RISK_SETS


class BuildError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Cache access
# ---------------------------------------------------------------------------
def _load_cached_intervals(conn, device_id: str, cutoff: date) -> list[Interval]:
    bands_key = BAND_KEY_VERSION
    rows = conn.execute(
        """
        SELECT * FROM risk_intervals
        WHERE device_id=? AND bands_key=? AND end_reason != 'cutoff'
        ORDER BY as_of DESC, interval_index ASC
        """,
        (device_id, bands_key),
    ).fetchall()
    seen: set[int] = set()
    out: list[Interval] = []
    for r in rows:
        if r["interval_index"] in seen:
            continue
        if r["end_date"] and r["end_date"][:10] > cutoff.isoformat():
            continue
        seen.add(r["interval_index"])
        out.append(
            Interval(
                index=r["interval_index"],
                cohort=r["cohort"],
                start_date=date.fromisoformat(r["start_date"]),
                end_date=date.fromisoformat(r["end_date"]),
                start_age=r["start_age"],
                end_age=r["end_age"],
                event=bool(r["event"]),
                end_reason=r["end_reason"],
                basis=json.loads(r["basis"] or "{}"),
            )
        )
    out.sort(key=lambda iv: iv.start_age)
    return out


def _load_cached_hash(conn, device_id: str) -> str | None:
    row = conn.execute(
        "SELECT input_hash FROM risk_intervals WHERE device_id=? AND bands_key=? "
        "ORDER BY computed_at DESC LIMIT 1",
        (device_id, BAND_KEY_VERSION),
    ).fetchone()
    return row["input_hash"] if row else None


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------
class Builder:
    def __init__(self, conn, artifacts_dir=None):
        self.conn = conn
        self.artifacts_dir = artifacts_dir

    # -- creation ------------------------------------------------------
    def create_build(
        self,
        as_of: date,
        strategy: str,
        kind: str = "full",
        parent_build_id: int | None = None,
        changed_devices: list[str] | None = None,
        mode: str = "apply",
        bootstrap_reps: int = 300,
        title: str | None = None,
    ) -> int:
        if strategy not in STRATEGIES:
            raise BuildError(f"unknown strategy {strategy!r}")
        params = {
            "as_of": as_of.isoformat(),
            "strategy": strategy,
            "bands_key": BAND_KEY_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "interval_version": INTERVAL_VERSION,
            "bootstrap_reps": bootstrap_reps,
            "title": title,
        }
        changed = sorted(set(changed_devices or []))
        ts = now_iso()
        with dbm.write_tx(self.conn):
            cur = self.conn.execute(
                """
                INSERT INTO builds
                    (kind, strategy, as_of, bands_key, analysis_params,
                     data_revision, status, stage, progress, cursor,
                     created_at, mode, parent_build_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    kind, strategy, as_of.isoformat(), BAND_KEY_VERSION,
                    json_dumps(params), revision_of(self.conn), "queued",
                    "created", 0.0, None, ts, mode, parent_build_id,
                ),
            )
            build_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO build_inputs(build_id, input_kind, input_ref, input_hash) "
                "VALUES (?,?,?,?)",
                (build_id, "revision", str(revision_of(self.conn)), ""),
            )
            if changed:
                for did in changed:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO build_inputs"
                        "(build_id, input_kind, input_ref, input_hash) VALUES (?,?,?,?)",
                        (build_id, "device", did, ""),
                    )
        return build_id

    def pause(self, build_id: int) -> None:
        with dbm.write_tx(self.conn):
            self.conn.execute(
                "UPDATE builds SET control='pause' WHERE id=? AND status='running'",
                (build_id,),
            )

    def cancel(self, build_id: int) -> None:
        with dbm.write_tx(self.conn):
            self.conn.execute(
                "UPDATE builds SET control='cancel' WHERE id=? AND status IN "
                "('running','paused','queued')",
                (build_id,),
            )

    # -- execution -----------------------------------------------------
    def run(self, build_id: int, max_units: int | None = None) -> dict:
        """Run until done, paused, cancelled, or ``max_units`` devices processed.

        ``max_units`` lets the web service run a build in bounded chunks so a
        request never blocks for the whole dataset; passing ``None`` runs to
        completion.  Calling again with the same build id resumes exactly where
        the cursor stopped.
        """
        build = self.conn.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
        if build is None:
            raise BuildError(f"build {build_id} not found")
        if build["status"] in ("done", "cancelled"):
            return {"build_id": build_id, "status": build["status"], "resumed": False}
        if build["control"] == "cancel":
            self._set_status(build_id, "cancelled", stage="cancelled")
            return {"build_id": build_id, "status": "cancelled", "resumed": False}

        params = json.loads(build["analysis_params"])
        as_of = date.fromisoformat(params["as_of"])
        strategy = build["strategy"]
        mode = build["mode"]
        t_start = time.perf_counter()
        resumed = build["cursor"] is not None or (build["done_units"] or 0) > 0

        policies = load_policies(self.conn)
        all_histories = load_histories(self.conn, policies=policies)
        by_id = {h.device_id: h for h in all_histories}

        parent_id = build["parent_build_id"]
        incremental = build["kind"] == "incremental" and parent_id is not None
        changed: set[str] = set()
        if incremental:
            parent = self.conn.execute(
                "SELECT * FROM builds WHERE id=?", (parent_id,)
            ).fetchone()
            if parent is None or parent["status"] != "done":
                raise BuildError("incremental build requires a completed parent build")
            if parent["bands_key"] != build["bands_key"]:
                raise BuildError("parent build uses different band definitions")
            if parent["as_of"] != build["as_of"]:
                # Every device's open tail interval is bounded by the cutoff;
                # copying rows across a different as_of would mix cutoffs.
                raise BuildError(
                    "incremental build must use the parent's as_of "
                    f"({parent['as_of']}); got {build['as_of']}"
                )
            changed = {
                r["input_ref"]
                for r in self.conn.execute(
                    "SELECT input_ref FROM build_inputs "
                    "WHERE build_id=? AND input_kind='device'",
                    (build_id,),
                ).fetchall()
            }

        # Determine work queue, honouring the resume cursor.  The cursor is the
        # last *completed* device, so filtering with ``>`` cannot skip work.
        device_ids = sorted(by_id)
        if incremental:
            work = [d for d in device_ids if d in changed]
        else:
            work = device_ids
        cursor = build["cursor"]
        if cursor:
            work = [d for d in work if d > cursor]

        # Fix the denominator once so progress is stable across resumes.
        total = build["total_units"] or len(work)
        self._set_status(build_id, "running", stage="deriving",
                         total_units=total, pid=os.getpid())

        # For incremental builds, seed the new build from the parent's rows
        # before processing changes, so the final report covers every device.
        if incremental and not cursor:
            self._copy_parent(build_id, parent_id, changed)

        metrics = json.loads(build["cost"] or "{}")
        if not metrics or "devices_worked" not in metrics:
            metrics = {
                "devices_total": len(device_ids),
                "devices_worked": 0,
                "interval_cache_hits": 0,
                "interval_cache_misses": 0,
                "interval_rows_written": 0,
                "sample_rows_written": 0,
                "wall_seconds": 0.0,
                "strategy": strategy,
                "as_of": as_of.isoformat(),
                "mode": mode,
            }
        metrics["devices_total"] = len(device_ids)

        processed = 0
        idx = -1
        for idx, did in enumerate(work):
            if max_units is not None and processed >= max_units:
                break
            control = self.conn.execute(
                "SELECT control FROM builds WHERE id=?", (build_id,)
            ).fetchone()["control"]
            if control == "pause":
                metrics["wall_seconds"] = round(
                    metrics.get("wall_seconds", 0) + time.perf_counter() - t_start, 4)
                self._store_cost(build_id, metrics)
                self._set_status(build_id, "paused", stage="paused")
                return {"build_id": build_id, "status": "paused",
                        "cursor": cursor, "resumed": resumed}
            if control == "cancel":
                self._set_status(build_id, "cancelled", stage="cancelled")
                return {"build_id": build_id, "status": "cancelled",
                        "cursor": cursor, "resumed": resumed}

            hist = by_id.get(did)
            if hist is None:
                # Device vanished between listing and processing; skip it but
                # still advance the cursor so a resume cannot loop forever.
                cursor = did
                processed += 1
                self._advance(build_id, did,
                              (build["done_units"] or 0) + processed, total)
                continue
            reuse: list[Interval] = []
            if strategy == "materialized":
                cached_hash = _load_cached_hash(self.conn, did)
                from .model import device_input_hash

                if cached_hash == device_input_hash(hist):
                    reuse = _load_cached_intervals(self.conn, did, as_of)
                    if reuse:
                        metrics["interval_cache_hits"] += 1
                    else:
                        metrics["interval_cache_misses"] += 1
                else:
                    metrics["interval_cache_misses"] += 1
            deriv = derive_device(hist, as_of, reuse)
            deriv.reused = bool(reuse)
            metrics["devices_worked"] += 1
            metrics["interval_rows_written"] += len(deriv.intervals)

            if mode == "apply":
                metrics["sample_rows_written"] += len(deriv.intervals)
                self._write_device(build_id, deriv, strategy, as_of, reuse)
            cursor = did
            processed += 1
            self._advance(build_id, did,
                          (build["done_units"] or 0) + processed, total)
            self._store_cost(build_id, metrics)
        done_units = (build["done_units"] or 0) + processed

        if idx + 1 < len(work):
            # Chunk finished without exhausting the queue: hand control back.
            metrics["wall_seconds"] = round(
                metrics.get("wall_seconds", 0) + time.perf_counter() - t_start, 4)
            self._store_cost(build_id, metrics)
            return {"build_id": build_id, "status": "running", "cursor": cursor,
                    "resumed": resumed, "done_units": done_units,
                    "total_units": total, "chunk": True}

        if mode == "dry-run":
            metrics["wall_seconds"] = round(
                metrics.get("wall_seconds", 0) + time.perf_counter() - t_start, 4)
            self._set_status(build_id, "done", stage="dry-run complete", progress=1.0)
            self._store_cost(build_id, metrics)
            return {"build_id": build_id, "status": "done", "dry_run": True,
                    "metrics": metrics}

        report_id = self._assemble_report(build_id, as_of, strategy, params, metrics,
                                          incremental, changed if incremental else set(),
                                          parent_id)
        metrics["wall_seconds"] = round(
            metrics.get("wall_seconds", 0) + time.perf_counter() - t_start, 4)
        metrics["db_bytes"] = dbm.db_size(
            self.conn.execute("PRAGMA database_list").fetchone()[2])
        self._store_cost(build_id, metrics)
        self._set_status(build_id, "done", stage="complete", progress=1.0,
                         report_id=report_id)
        return {"build_id": build_id, "status": "done", "report_id": report_id,
                "metrics": metrics}

    # -- internals -----------------------------------------------------
    def _set_status(self, build_id, status, **fields) -> None:
        sets = ["status=?"]
        args: list = [status]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            args.append(v)
        if status == "running" and "started_at" not in fields:
            row = self.conn.execute("SELECT started_at FROM builds WHERE id=?",
                                    (build_id,)).fetchone()
            if row and not row["started_at"]:
                sets.append("started_at=?")
                args.append(now_iso())
        if status in ("done", "failed", "cancelled"):
            sets.append("finished_at=?")
            args.append(now_iso())
        args.append(build_id)
        with dbm.write_tx(self.conn):
            self.conn.execute(f"UPDATE builds SET {', '.join(sets)} WHERE id=?", args)

    def _advance(self, build_id, cursor, done, total) -> None:
        progress = (done / total) if total else 1.0
        with dbm.write_tx(self.conn):
            self.conn.execute(
                "UPDATE builds SET cursor=?, done_units=?, progress=? WHERE id=?",
                (cursor, done, min(progress, 0.999), build_id),
            )

    def _write_device(
        self,
        build_id: int,
        deriv: DeviceDerivation,
        strategy: str,
        as_of: date,
        reuse: list[Interval],
    ) -> None:
        ts = now_iso()
        entered = [iv.cohort for iv in deriv.intervals if iv.cohort != "overall"]
        with dbm.write_tx(self.conn):
            self.conn.execute(
                "DELETE FROM build_devices WHERE build_id=? AND device_id=?",
                (build_id, deriv.device_id),
            )
            self.conn.execute(
                "DELETE FROM samples WHERE build_id=? AND device_id=?",
                (build_id, deriv.device_id),
            )
            self.conn.execute(
                """
                INSERT INTO build_devices
                    (build_id, device_id, status, pre_entry_days, entry_age,
                     followup_age, first_fault_age, censor_kind, entered_sets,
                     basis, input_hash, reused)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    build_id, deriv.device_id, deriv.status.value,
                    deriv.pre_entry_days, deriv.entry_age, deriv.followup_age,
                    deriv.first_fault_age, deriv.censor_kind,
                    json_dumps(entered), json_dumps(deriv.basis),
                    deriv.input_hash, 1 if deriv.reused else 0,
                ),
            )
            sample_rows = [
                (
                    build_id, deriv.device_id, iv.cohort, iv.start_age, iv.end_age,
                    1 if iv.event else 0,
                    "fault" if iv.event else _censor_for(iv),
                    None if iv.cohort == "overall" else iv.index,
                    iv.start_date.isoformat(), iv.end_date.isoformat(),
                    deriv.pre_entry_days,
                )
                for iv in deriv.intervals
            ]
            self.conn.executemany(
                """
                INSERT INTO samples
                    (build_id, device_id, cohort, entry_age, end_age, event,
                     censor_kind, interval_index, start_date, end_date,
                     pre_entry_days)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                sample_rows,
            )
            if strategy == "materialized":
                for iv in deriv.intervals:
                    if iv.cohort == "overall":
                        continue
                    if iv.end_reason == "cutoff":
                        # open tail interval: never cached, it moves with as_of
                        continue
                    self.conn.execute(
                        """
                        INSERT INTO risk_intervals
                            (device_id, as_of, bands_key, interval_index, cohort,
                             start_date, end_date, start_age, end_age, event,
                             end_reason, basis, input_hash, algorithm_version,
                             computed_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(device_id, as_of, bands_key, interval_index)
                        DO UPDATE SET
                            cohort=excluded.cohort, start_date=excluded.start_date,
                            end_date=excluded.end_date, start_age=excluded.start_age,
                            end_age=excluded.end_age, event=excluded.event,
                            end_reason=excluded.end_reason, basis=excluded.basis,
                            input_hash=excluded.input_hash,
                            computed_at=excluded.computed_at
                        """,
                        (
                            deriv.device_id, as_of.isoformat(), BAND_KEY_VERSION,
                            iv.index, iv.cohort, iv.start_date.isoformat(),
                            iv.end_date.isoformat(), iv.start_age, iv.end_age,
                            1 if iv.event else 0, iv.end_reason,
                            json_dumps(iv.basis), deriv.input_hash,
                            INTERVAL_VERSION, ts,
                        ),
                    )

    def _copy_parent(self, build_id: int, parent_id: int, changed: set[str]) -> None:
        placeholders = ",".join("?" for _ in changed) or "''"
        with dbm.write_tx(self.conn):
            self.conn.execute(
                f"""
                INSERT INTO build_devices
                    (build_id, device_id, status, pre_entry_days, entry_age,
                     followup_age, first_fault_age, censor_kind, entered_sets,
                     basis, input_hash, reused)
                SELECT ?, device_id, status, pre_entry_days, entry_age,
                       followup_age, first_fault_age, censor_kind, entered_sets,
                       basis, input_hash, 1
                FROM build_devices
                WHERE build_id=? AND device_id NOT IN ({placeholders})
                """,
                (build_id, parent_id, *sorted(changed)),
            )
            self.conn.execute(
                f"""
                INSERT INTO samples
                    (build_id, device_id, cohort, entry_age, end_age, event,
                     censor_kind, interval_index, start_date, end_date,
                     pre_entry_days)
                SELECT ?, device_id, cohort, entry_age, end_age, event,
                       censor_kind, interval_index, start_date, end_date,
                       pre_entry_days
                FROM samples
                WHERE build_id=? AND device_id NOT IN ({placeholders})
                """,
                (build_id, parent_id, *sorted(changed)),
            )

    def _assemble_report(
        self,
        build_id: int,
        as_of: date,
        strategy: str,
        params: dict,
        metrics: dict,
        incremental: bool = False,
        changed: set[str] | None = None,
        parent_id: int | None = None,
    ) -> int:
        samples = self.conn.execute(
            "SELECT * FROM samples WHERE build_id=?", (build_id,)
        ).fetchall()
        devices = self.conn.execute(
            "SELECT * FROM build_devices WHERE build_id=?", (build_id,)
        ).fetchall()

        by_cohort: dict[str, list[Observation]] = {c: [] for c in COHORTS}
        for s in samples:
            if s["cohort"] not in by_cohort:
                continue
            by_cohort[s["cohort"]].append(
                Observation(
                    device_id=s["device_id"],
                    entry=float(s["entry_age"]),
                    end=float(s["end_age"]),
                    event=bool(s["event"]),
                    censor_kind=s["censor_kind"],
                )
            )

        payload_cohorts = {}
        for cohort in COHORTS:
            obs = by_cohort[cohort]
            curve = km_curve(obs, cohort=cohort)
            max_age = max((o.end for o in obs), default=0.0)
            grid = _age_grid(max_age)
            payload_cohorts[cohort] = {
                "cohort": cohort,
                "label": _cohort_label(cohort),
                "points": [
                    {
                        "age": p.age,
                        "survival": p.survival,
                        "at_risk": p.at_risk,
                        "events": p.events,
                        "censored": p.censored,
                    }
                    for p in curve.points
                ],
                "greenwood": greenwood_band(curve),
                "bootstrap": bootstrap_band(
                    obs, reps=int(params.get("bootstrap_reps", 300))
                ),
                "at_risk_table": at_risk_table(curve, grid),
                "n_devices": curve.n_devices,
                "n_intervals": curve.n_intervals,
                "n_events": curve.n_events,
                "n_censored_exited": curve.n_censored_exited,
                "n_censored_open": curve.n_censored_open,
                "n_censored_transition": curve.n_censored_transition,
                "person_days": curve.person_days,
                "median_age": curve.median_age,
                "median_note": curve.median_note,
                "restricted_mean_age": curve.restricted_mean_age,
                "restricted_mean_note": curve.restricted_mean_note,
                "last_observed_age": curve.last_observed_age,
                "warnings": curve.warnings,
            }

        status_counts: dict[str, int] = {}
        pre_entry_values: list[int] = []
        for d in devices:
            status_counts[d["status"]] = status_counts.get(d["status"], 0) + 1
            pre_entry_values.append(int(d["pre_entry_days"]))

        naive = _naive_comparison(devices, payload_cohorts["overall"])
        truncation = _truncation_summary(pre_entry_values)
        inherited = (
            len(devices) - metrics["devices_worked"] if incremental else 0
        )
        metrics["devices_inherited"] = max(0, inherited)
        metrics["incremental"] = incremental
        metrics["parent_build_id"] = parent_id
        metrics["changed_devices"] = sorted(changed or []) if incremental else []
        cost = self._cost_block(metrics, payload_cohorts)

        payload = {
            "meta": {
                "build_id": build_id,
                "as_of": as_of.isoformat(),
                "data_revision": revision_of(self.conn),
                "strategy": strategy,
                "strategy_label": STRATEGIES[strategy]["label"],
                "bands_key": BAND_KEY_VERSION,
                "band_cutoffs": {"safe": 0.34, "watch": 0.67,
                                 "danger": "open (>=0.67)"},
                "algorithm_version": ALGORITHM_VERSION,
                "interval_version": INTERVAL_VERSION,
                "created_at": now_iso(),
                "analysis_params": params,
                "cohort_definition": _cohort_definition(),
            },
            "cohorts": payload_cohorts,
            "status_counts": status_counts,
            "truncation": truncation,
            "naive_comparison": naive,
            "cost": cost,
            "caveats": _caveats(),
            "dataset": self._dataset_profile(devices),
            "lineage": self._lineage(build_id, incremental, parent_id, changed or set()),
        }

        title = params.get("title") or (
            f"Fleet fault-free survival at {as_of.isoformat()} "
            f"({STRATEGIES[strategy]['label'].lower()}, rev "
            f"{revision_of(self.conn)})"
        )
        prev = self.conn.execute(
            "SELECT id FROM reports ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        with dbm.write_tx(self.conn):
            cur = self.conn.execute(
                """
                INSERT INTO reports
                    (build_id, data_revision, strategy, as_of, bands_key,
                     algorithm_version, title, payload, created_at, supersedes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    build_id, revision_of(self.conn), strategy, as_of.isoformat(),
                    BAND_KEY_VERSION, ALGORITHM_VERSION, title,
                    json_dumps(payload), now_iso(), prev["id"] if prev else None,
                ),
            )
            report_id = cur.lastrowid
            rows = [
                (
                    report_id, d["device_id"], d["status"], d["pre_entry_days"],
                    d["entry_age"], d["followup_age"], d["first_fault_age"],
                    d["entered_sets"], d["basis"],
                )
                for d in devices
            ]
            self.conn.executemany(
                """
                INSERT INTO report_devices
                    (report_id, device_id, status, pre_entry_days, entry_age,
                     followup_age, first_fault_age, entered_sets, basis)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                rows,
            )
            self.conn.execute(
                "UPDATE builds SET report_id=? WHERE id=?", (report_id, build_id)
            )
        return report_id

    def _lineage(self, build_id, incremental, parent_id, changed) -> dict:
        row = self.conn.execute(
            "SELECT * FROM builds WHERE id=?", (build_id,)
        ).fetchone()
        prev_report = None
        if row and row["report_id"]:
            prev_report = self.conn.execute(
                "SELECT id FROM reports WHERE build_id=? AND id < ? "
                "ORDER BY id DESC LIMIT 1", (build_id, row["report_id"])
            ).fetchone()
        parent = None
        if parent_id:
            p = self.conn.execute(
                "SELECT id, data_revision, as_of, report_id FROM builds WHERE id=?",
                (parent_id,),
            ).fetchone()
            parent = dict(p) if p else None
        return {
            "kind": "incremental" if incremental else "full",
            "parent_build": parent,
            "changed_device_count": len(changed) if incremental else None,
            "changed_devices": sorted(changed)[:100] if incremental else [],
            "note": (
                "unchanged devices were copied from the parent build; every "
                "row in this report is still identified by this build id"
                if incremental
                else "all devices derived from canonical data at the build revision"
            ),
        }

    def _cost_block(self, metrics: dict, cohorts: dict) -> dict:
        sample_rows = sum(c["n_intervals"] for c in cohorts.values())
        return {
            **metrics,
            "sample_rows": sample_rows,
            "strategy_note": STRATEGIES[metrics["strategy"]]["description"],
            "strategy_cost": STRATEGIES[metrics["strategy"]]["cost"],
            "tradeoff": STRATEGIES[metrics["strategy"]]["tradeoff"],
            "interpretation": (
                "Measured wall-clock and row counts for this build.  A lower "
                "number is not a better estimate; both strategies produce the "
                "same curves.  The choice trades storage/write cost for "
                "incremental rebuild cost."
            ),
        }

    def _dataset_profile(self, devices) -> dict:
        from .repo import dataset_summary

        summary = dataset_summary(self.conn)
        summary["models"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT model, COUNT(*) AS n FROM devices GROUP BY model ORDER BY model"
            ).fetchall()
        ]
        summary["sites"] = [
            dict(r)
            for r in self.conn.execute(
                "SELECT site, COUNT(*) AS n FROM devices GROUP BY site ORDER BY site"
            ).fetchall()
        ]
        return summary

    def _store_cost(self, build_id: int, metrics: dict) -> None:
        with dbm.write_tx(self.conn):
            self.conn.execute(
                "UPDATE builds SET cost=? WHERE id=?",
                (json_dumps(metrics), build_id),
            )


# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------
def _censor_for(iv: Interval) -> str:
    return {
        "band_transition": "band_transition",
        "exit": "exited",
        "cutoff": "not_yet_failed",
        "fault": "fault",
    }[iv.end_reason]


def _age_grid(max_age: float) -> list[float]:
    if max_age <= 0:
        return [0.0]
    step = max_age / 10.0
    return [round(i * step, 1) for i in range(11)]


def _cohort_label(cohort: str) -> str:
    return {
        "overall": "All observed devices (delayed entry)",
        "safe": "Safe band",
        "watch": "Watch band",
        "danger": "Danger band",
    }.get(cohort, cohort)


def _cohort_definition() -> dict:
    return {
        "overall": (
            "one row per at-risk device, entry at observation start age, exit "
            "at first in-window fault / observation end / cutoff"
        ),
        "bands": (
            "each observed interval labelled by maintenance-phase clock; a "
            "fault inside a band ends that band's interval; band transitions "
            "are censoring, so band curves are conditional on reaching the band"
        ),
        "censoring_codes": {
            "fault": "observed failure",
            "not_yet_failed": "under observation at cutoff, no fault",
            "exited": "observation administratively closed before cutoff",
            "band_transition": "device moved to another risk band",
        },
        "not_models": (
            "descriptive Kaplan-Meier estimates; no covariates, no prediction, "
            "no extrapolation beyond the largest observed age"
        ),
    }


def _naive_comparison(devices, overall: dict) -> dict:
    faulted_ages = [
        float(d["first_fault_age"])
        for d in devices
        if d["first_fault_age"] is not None
    ]
    censored = sum(
        1 for d in devices if d["status"] in ("exited", "not_yet_failed")
    )
    n = len(faulted_ages)
    naive_mean = sum(faulted_ages) / n if n else None
    return {
        "naive_mean_age_at_fault": naive_mean,
        "naive_n_faulted": n,
        "naive_formula": "mean age at first fault among faulted devices only",
        "warning": (
            "This naive mean is shown only as a contrast.  It ignores censoring: "
            f"{censored} device(s) left observation without a fault are omitted, "
            "so it is biased toward short lives and is not an average lifetime."
        ),
        "censored_devices_excluded_from_naive": censored,
        "km_median_age": overall.get("median_age"),
        "km_median_note": overall.get("median_note"),
        "km_restricted_mean_age": overall.get("restricted_mean_age"),
        "km_restricted_mean_note": overall.get("restricted_mean_note"),
        "difference_note": (
            "The Kaplan-Meier median / restricted mean use all observed exposure "
            "with delayed entry and right censoring.  They are still restricted "
            "to the observed age range and are not predictions."
        ),
    }


def _truncation_summary(pre_entry_values: list[int]) -> dict:
    if not pre_entry_values:
        return {"count": 0, "with_pre_entry": 0, "max": 0, "median": 0}
    vals = sorted(pre_entry_values)
    mid = values_median(vals)
    return {
        "count": len(vals),
        "with_pre_entry": sum(1 for v in vals if v > 0),
        "min": vals[0],
        "median": mid,
        "max": vals[-1],
        "note": (
            "pre-entry days are left-truncation offsets: those ages are not in "
            "any risk set, so early-age survival is not estimated for devices "
            "that entered observation late"
        ),
    }


def values_median(vals: list[int]) -> float:
    n = len(vals)
    if n % 2:
        return float(vals[n // 2])
    return (vals[n // 2 - 1] + vals[n // 2]) / 2.0


def _caveats() -> list[str]:
    return [
        "Fictional, synthetic data.  No medical, clinical or individual "
        "diagnostic conclusion may be drawn.",
        "Survival curves are descriptive.  They are not a prediction model, not "
        "a failure-rate forecast, and do not extrapolate beyond observed ages.",
        "Devices that entered observation after commissioning contribute only "
        "from their entry age (delayed entry); earlier ages are not estimated.",
        "'not yet failed' and 'exited' are distinct censoring reasons and are "
        "reported separately.  Neither is a fault.",
        "Risk sets are a maintenance-phase bookkeeping partition, not a "
        "probability or a severity grade.",
        "Late-reported faults are included for any cutoff after the fault date; "
        "a report's data revision identifies exactly which revisions it used.",
        "Bootstrap bands are seeded and reproducible; Greenwood bands can be "
        "optimistic in tails supported by very few devices.",
    ]


# ---------------------------------------------------------------------------
# Strategy comparison
# ---------------------------------------------------------------------------
def compare_strategies(conn, as_of: date, sample_limit: int | None = None) -> dict:
    """Measure both strategies on the same data and report the cost difference.

    This performs the actual derivation work (no writes) so the numbers are
    measured, not estimated.  For the materialized arm, cached closed intervals
    with a matching input hash are loaded from ``risk_intervals`` exactly as a
    real incremental build would.
    """
    from .model import device_input_hash

    policies = load_policies(conn)
    histories = load_histories(conn, policies=policies)
    if sample_limit:
        histories = histories[:sample_limit]

    out = {"as_of": as_of.isoformat(), "devices": len(histories), "arms": {}}
    for strategy in ("materialized", "recompute"):
        t0 = time.perf_counter()
        intervals_total = 0
        reused_devices = 0
        reused_intervals = 0
        for h in histories:
            reuse: list[Interval] = []
            if strategy == "materialized":
                cached_hash = _load_cached_hash(conn, h.device_id)
                if cached_hash == device_input_hash(h):
                    reuse = _load_cached_intervals(conn, h.device_id, as_of)
                    if reuse:
                        reused_devices += 1
            deriv = derive_device(h, as_of, reuse)
            intervals_total += len(deriv.intervals)
            reused_intervals += len(reuse)
        elapsed = time.perf_counter() - t0
        out["arms"][strategy] = {
            "label": STRATEGIES[strategy]["label"],
            "description": STRATEGIES[strategy]["description"],
            "cost": STRATEGIES[strategy]["cost"],
            "tradeoff": STRATEGIES[strategy]["tradeoff"],
            "elapsed_ms": round(elapsed * 1000, 2),
            "intervals_derived": intervals_total,
            "devices_reusing_cache": reused_devices,
            "intervals_reused": reused_intervals,
            "interval_rows_persisted_if_materialized": (
                intervals_total if strategy == "materialized" else 0
            ),
        }

    cache_rows = conn.execute("SELECT COUNT(*) FROM risk_intervals").fetchone()[0]
    out["cache_rows_present"] = cache_rows
    out["headline"] = (
        "Both arms compute identical curves.  The materialized arm reuses closed "
        "intervals for devices whose canonical inputs are unchanged; if no cache "
        "exists yet its first run has no advantage and costs extra writes.  The "
        "recompute arm never pays invalidation cost and never benefits from "
        "incremental work."
        if cache_rows
        else
        "No interval cache exists yet, so the materialized arm has no reuse to "
        "show and its first build costs the same compute plus writes.  Run a "
        "materialized build, add or revise a few faults, then compare again: "
        "only unchanged devices should hit the cache."
    )
    out["cost_note"] = (
        "Cost here means measured build time and rows for the same statistics.  "
        "Neither arm estimates a lifetime by averaging fault records."
    )
    return out
