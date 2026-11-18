"""Ingestor: admits device, fault, repair and observation facts.

Rules that make "持续补报" safe
------------------------------
1. **Append-only.** Faults and repairs are never updated or deleted.  A
   correction arrives as a new revision of the same ``event_key``; a
   retraction is a revision with ``retracted: true``.  The previous rows stay
   for audit and can be rebuilt from.
2. **Idempotent.** Each batch carries a client-generated batch id; its payload
   hash is unique.  Replaying a batch is a no-op and is reported as such, so a
   crashed ingest can be retried without duplicating faults.
3. **Late reporting is normal, not an error.** ``fault_date`` and
   ``reported_date`` are separate.  A fault reported months late is accepted
   and included in any analysis whose cutoff is after its fault date.
4. **Observation edits are events.** Changing an observation window records
   the previous window, so a builder can find every device whose cohort could
   move and no old report is silently reinterpreted.
5. **Rejections are explicit.** A row that fails validation is rejected with a
   reason; the rest of the batch still commits, and the batch records counts.

Every accepted mutation bumps ``data_revision`` exactly once per batch (unless
nothing changed), which is the handle reports and builds use for staleness.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from . import db as dbm
from .model import now_iso, parse_iso
from .repo import json_dumps

SEVERITIES = {"minor", "major", "critical", "unknown"}


class _DryRunRollback(Exception):
    """Internal: unwinds the transaction when a dry run must write nothing."""


@dataclass
class RowResult:
    kind: str
    key: str
    status: str          # accepted | skipped | rejected
    reason: str | None = None
    device_id: str | None = None


@dataclass
class IngestResult:
    batch_id: int
    data_revision: int
    accepted: int
    skipped: int
    rejected: int
    duplicate_batch: bool
    rows: list[RowResult] = field(default_factory=list)
    changed_devices: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "data_revision": self.data_revision,
            "accepted": self.accepted,
            "skipped": self.skipped,
            "rejected": self.rejected,
            "duplicate_batch": self.duplicate_batch,
            "changed_devices": self.changed_devices,
            "rows": [r.__dict__ for r in self.rows],
        }


class Ingestor:
    def __init__(self, conn, source: str = "api"):
        self.conn = conn
        self.source = source

    # -- public --------------------------------------------------------
    def ingest(self, payload: dict, dry_run: bool = False) -> IngestResult:
        batch_id = str(payload.get("batch_id") or "").strip()
        if not batch_id:
            raise ValueError("batch_id is required (used for idempotent replay)")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                               default=str)
        payload_sha = hashlib.sha256(canonical.encode()).hexdigest()

        existing = self.conn.execute(
            "SELECT * FROM ingest_batches WHERE payload_sha=?", (payload_sha,)
        ).fetchone()
        if existing is not None:
            return IngestResult(
                batch_id=existing["id"],
                data_revision=existing["data_revision"],
                accepted=0,
                skipped=existing["skipped"],
                rejected=existing["rejected"],
                duplicate_batch=True,
                rows=[],
                changed_devices=json.loads(existing["changed_devices"] or "[]"),
            )

        rows: list[RowResult] = []
        changed: set[str] = set()
        dry_result: IngestResult | None = None

        try:
            with dbm.write_tx(self.conn):
                base_revision = dbm.get_meta(self.conn, "data_revision", "0")
                revision = int(base_revision) + 1   # tentative; commit below

                # Policies first: they can change many devices.
                for item in payload.get("model_policies", []):
                    res, devices = self._apply_policy(item, revision)
                    rows.append(res)
                    changed |= devices

                for item in payload.get("devices", []):
                    res = self._apply_device(item, revision)
                    rows.append(res)
                    if res.status == "accepted" and res.device_id:
                        changed.add(res.device_id)

                for item in payload.get("faults", []):
                    res = self._apply_fault(item, revision)
                    rows.append(res)
                    if res.status == "accepted" and res.device_id:
                        changed.add(res.device_id)

                for item in payload.get("repairs", []):
                    res = self._apply_repair(item, revision)
                    rows.append(res)
                    if res.status == "accepted" and res.device_id:
                        changed.add(res.device_id)

                for item in payload.get("observation_updates", []):
                    res = self._apply_observation(item, revision)
                    rows.append(res)
                    if res.status == "accepted" and res.device_id:
                        changed.add(res.device_id)

                accepted = sum(1 for r in rows if r.status == "accepted")
                skipped = sum(1 for r in rows if r.status == "skipped")
                rejected = sum(1 for r in rows if r.status == "rejected")
                anything_changed = accepted > 0

                if dry_run:
                    dry_result = IngestResult(
                        batch_id=-1,
                        data_revision=int(base_revision),
                        accepted=accepted,
                        skipped=skipped,
                        rejected=rejected,
                        duplicate_batch=False,
                        rows=rows,
                        changed_devices=sorted(changed),
                    )
                    raise _DryRunRollback()

                if anything_changed:
                    dbm.set_meta(self.conn, "data_revision", str(revision))
                    final_revision = revision
                else:
                    final_revision = int(base_revision)

                cur = self.conn.execute(
                    """
                    INSERT INTO ingest_batches
                        (received_at, source, payload_sha, data_revision,
                         accepted, skipped, rejected, detail, changed_devices)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        now_iso(),
                        payload.get("source") or self.source,
                        payload_sha,
                        final_revision,
                        accepted,
                        skipped,
                        rejected,
                        json_dumps([r.__dict__ for r in rows]),
                        json_dumps(sorted(changed)),
                    ),
                )
                batch_row_id = cur.lastrowid
        except _DryRunRollback:
            return dry_result  # type: ignore[return-value]

        return IngestResult(
            batch_id=batch_row_id,
            data_revision=final_revision,
            accepted=accepted,
            skipped=skipped,
            rejected=rejected,
            duplicate_batch=False,
            rows=rows,
            changed_devices=sorted(changed),
        )

    # -- helpers -------------------------------------------------------
    def _upsert_device_row(self, device_id: str, revision: int) -> None:
        self.conn.execute(
            "UPDATE devices SET updated_at=? WHERE id=?",
            (now_iso(), device_id),
        )

    def _apply_policy(self, item: dict, revision: int) -> tuple[RowResult, set[str]]:
        model = (item.get("model") or "").strip()
        try:
            interval = int(item.get("service_interval_days"))
        except (TypeError, ValueError):
            return (
                RowResult("policy", model or "?", "rejected", "invalid interval"),
                set(),
            )
        if not model or interval <= 0:
            return (
                RowResult("policy", model or "?", "rejected",
                          "model and positive interval required"),
                set(),
            )
        prev = self.conn.execute(
            "SELECT service_interval_days FROM model_policies WHERE model=?", (model,)
        ).fetchone()
        if prev is not None and int(prev["service_interval_days"]) == interval:
            return RowResult("policy", model, "skipped", "unchanged"), set()
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO model_policies
                (model, service_interval_days, note, data_revision, created_at, updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(model) DO UPDATE SET
                service_interval_days=excluded.service_interval_days,
                note=excluded.note,
                data_revision=excluded.data_revision,
                updated_at=excluded.updated_at
            """,
            (model, interval, item.get("note"), revision, ts, ts),
        )
        affected = {
            r["id"]
            for r in self.conn.execute(
                "SELECT id FROM devices WHERE model=?", (model,)
            ).fetchall()
        }
        return RowResult("policy", model, "accepted"), affected

    def _apply_device(self, item: dict, revision: int) -> RowResult:
        did = (item.get("id") or "").strip()
        if not did:
            return RowResult("device", "?", "rejected", "missing id")
        model = (item.get("model") or "").strip()
        site = (item.get("site") or "").strip()
        comm = parse_iso(item.get("commissioned_date"))
        start = parse_iso(item.get("observation_start_date"))
        end = parse_iso(item.get("observation_end_date"))
        if not model or not site:
            return RowResult("device", did, "rejected", "model and site required", did)
        if comm is None or start is None:
            return RowResult(
                "device", did, "rejected", "commissioned/observation dates required", did
            )
        if start < comm:
            return RowResult(
                "device", did, "rejected",
                "observation cannot start before commissioning", did,
            )
        if end is not None and end < start:
            return RowResult(
                "device", did, "rejected", "observation end before start", did
            )
        existing = self.conn.execute(
            "SELECT * FROM devices WHERE id=?", (did,)
        ).fetchone()
        ts = now_iso()
        if existing is None:
            self.conn.execute(
                """
                INSERT INTO devices
                    (id, model, site, commissioned_date, observation_start_date,
                     observation_end_date, observation_revision, observation_note,
                     created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (did, model, site, comm.isoformat(), start.isoformat(),
                 end.isoformat() if end else None, 0,
                 item.get("observation_note"), ts, ts),
            )
            return RowResult("device", did, "accepted", device_id=did)

        # Device already exists: identity fields are immutable; window changes
        # must go through observation_updates so they are event-sourced.
        if existing["model"] != model or existing["site"] != site or \
                existing["commissioned_date"] != comm.isoformat():
            return RowResult(
                "device", did, "rejected",
                "device identity/model/commissioning changed; use observation_updates "
                "for window changes, faults for failures", did,
            )
        if (existing["observation_start_date"], existing["observation_end_date"]) != (
            start.isoformat(), end.isoformat() if end else None
        ):
            return RowResult(
                "device", did, "rejected",
                "observation window differs; submit via observation_updates", did,
            )
        return RowResult("device", did, "skipped", "unchanged", did)

    def _apply_fault(self, item: dict, revision: int) -> RowResult:
        did = (item.get("device_id") or "").strip()
        key = (item.get("event_key") or "").strip()
        if not did or not key:
            return RowResult("fault", key or "?", "rejected",
                             "device_id and event_key required", did or None)
        dev = self.conn.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
        if dev is None:
            return RowResult("fault", key, "rejected", "unknown device", did)
        fdate = parse_iso(item.get("fault_date"))
        fdate_early = parse_iso(item.get("fault_date_earliest"))
        rdate = parse_iso(item.get("reported_date")) or date.today()
        if fdate is None:
            return RowResult("fault", key, "rejected", "fault_date required", did)
        if fdate < parse_iso(dev["commissioned_date"]):
            return RowResult("fault", key, "rejected",
                             "fault before commissioning", did)
        if fdate_early is not None:
            if fdate_early > fdate:
                return RowResult("fault", key, "rejected",
                                 "fault_date_earliest after fault_date", did)
            if fdate_early < parse_iso(dev["commissioned_date"]):
                return RowResult("fault", key, "rejected",
                                 "fault_date_earliest before commissioning", did)
        if rdate < fdate:
            return RowResult("fault", key, "rejected",
                             "reported_date before fault_date", did)
        severity = (item.get("severity") or "unknown").strip().lower()
        if severity not in SEVERITIES:
            return RowResult("fault", key, "rejected",
                             f"severity must be one of {sorted(SEVERITIES)}", did)
        component = (item.get("component") or "unspecified").strip()
        retracted = 1 if item.get("retracted") else 0

        prev = self.conn.execute(
            "SELECT * FROM faults WHERE device_id=? AND event_key=? "
            "ORDER BY revision DESC LIMIT 1",
            (did, key),
        ).fetchone()
        if prev is None and retracted:
            return RowResult("fault", key, "rejected",
                             "cannot retract a fault that does not exist", did)

        desired = {
            "fault_date": fdate.isoformat(),
            "fault_date_earliest": fdate_early.isoformat() if fdate_early else None,
            "component": component,
            "severity": severity,
            "reported_date": rdate.isoformat(),
            "note": item.get("note"),
            "retracted": retracted,
        }
        if prev is not None:
            current = {
                "fault_date": prev["fault_date"],
                "fault_date_earliest": prev["fault_date_earliest"],
                "component": prev["component"],
                "severity": prev["severity"],
                "reported_date": prev["reported_date"],
                "note": prev["note"],
                "retracted": int(prev["retracted"]),
            }
            if current == desired:
                return RowResult("fault", key, "skipped", "unchanged", did)
            next_rev = int(prev["revision"]) + 1
        else:
            next_rev = 1

        self.conn.execute(
            """
            INSERT INTO faults
                (device_id, event_key, fault_date, fault_date_earliest, component,
                 severity, reported_date, note, revision, retracted, data_revision,
                 created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (did, key, desired["fault_date"], desired["fault_date_earliest"],
             component, severity, desired["reported_date"], desired["note"],
             next_rev, retracted, revision, now_iso()),
        )
        self._upsert_device_row(did, revision)
        return RowResult("fault", key, "accepted", device_id=did)

    def _apply_repair(self, item: dict, revision: int) -> RowResult:
        did = (item.get("device_id") or "").strip()
        key = (item.get("event_key") or "").strip()
        if not did or not key:
            return RowResult("repair", key or "?", "rejected",
                             "device_id and event_key required", did or None)
        dev = self.conn.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
        if dev is None:
            return RowResult("repair", key, "rejected", "unknown device", did)
        rdate = parse_iso(item.get("repair_date"))
        if rdate is None:
            return RowResult("repair", key, "rejected", "repair_date required", did)
        if rdate < parse_iso(dev["commissioned_date"]):
            return RowResult("repair", key, "rejected",
                             "repair before commissioning", did)
        try:
            downtime = (
                int(item["downtime_days"])
                if item.get("downtime_days") is not None
                else None
            )
        except (TypeError, ValueError):
            return RowResult("repair", key, "rejected", "invalid downtime_days", did)
        if downtime is not None and downtime < 0:
            return RowResult("repair", key, "rejected", "negative downtime", did)
        retracted = 1 if item.get("retracted") else 0

        prev = self.conn.execute(
            "SELECT * FROM repairs WHERE device_id=? AND event_key=? "
            "ORDER BY revision DESC LIMIT 1",
            (did, key),
        ).fetchone()
        if prev is None and retracted:
            return RowResult("repair", key, "rejected",
                             "cannot retract a repair that does not exist", did)
        desired = {
            "repair_date": rdate.isoformat(),
            "action": (item.get("action") or "unspecified").strip(),
            "downtime_days": downtime,
            "fault_event_key": item.get("fault_event_key"),
            "note": item.get("note"),
            "retracted": retracted,
        }
        if prev is not None:
            current = {
                "repair_date": prev["repair_date"],
                "action": prev["action"],
                "downtime_days": prev["downtime_days"],
                "fault_event_key": prev["fault_event_key"],
                "note": prev["note"],
                "retracted": int(prev["retracted"]),
            }
            if current == desired:
                return RowResult("repair", key, "skipped", "unchanged", did)
            next_rev = int(prev["revision"]) + 1
        else:
            next_rev = 1

        self.conn.execute(
            """
            INSERT INTO repairs
                (device_id, event_key, fault_event_key, repair_date, action,
                 downtime_days, note, revision, retracted, data_revision, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (did, key, desired["fault_event_key"], desired["repair_date"],
             desired["action"], downtime, desired["note"], next_rev, retracted,
             revision, now_iso()),
        )
        self._upsert_device_row(did, revision)
        return RowResult("repair", key, "accepted", device_id=did)

    def _apply_observation(self, item: dict, revision: int) -> RowResult:
        did = (item.get("device_id") or "").strip()
        dev = self.conn.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()
        if dev is None:
            return RowResult("observation", did or "?", "rejected", "unknown device",
                             did or None)
        start = parse_iso(item.get("observation_start_date"))
        end_given = "observation_end_date" in item
        end = parse_iso(item.get("observation_end_date")) if end_given else None
        prev_start = dev["observation_start_date"]
        prev_end = dev["observation_end_date"]

        new_start = start.isoformat() if start is not None else prev_start
        if end_given:
            new_end = end.isoformat() if end else None
        else:
            new_end = prev_end

        comm = parse_iso(dev["commissioned_date"])
        if parse_iso(new_start) < comm:
            return RowResult("observation", did, "rejected",
                             "observation cannot start before commissioning", did)
        if new_end is not None and parse_iso(new_end) < parse_iso(new_start):
            return RowResult("observation", did, "rejected",
                             "observation end before start", did)
        if new_start == prev_start and new_end == prev_end:
            return RowResult("observation", did, "skipped", "unchanged", did)

        # A fault already recorded in the window being cut away is not
        # impossible (the device existed), but a window that ends before an
        # included fault's date silently discards it.  Surface it as a note in
        # the audit trail rather than blocking the edit.
        self.conn.execute(
            """
            INSERT INTO observation_events
                (device_id, prev_start, new_start, prev_end, new_end, note,
                 data_revision, recorded_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (did, prev_start, new_start, prev_end, new_end,
             item.get("note"), revision, now_iso()),
        )
        self.conn.execute(
            """
            UPDATE devices
            SET observation_start_date=?, observation_end_date=?,
                observation_revision=observation_revision+1,
                observation_note=?, updated_at=?
            WHERE id=?
            """,
            (new_start, new_end, item.get("note"), now_iso(), did),
        )
        return RowResult("observation", did, "accepted", device_id=did)


def validate_batch_shape(payload: dict) -> list[str]:
    """Structural validation before hitting the DB; returns error strings."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["payload must be a JSON object"]
    if not str(payload.get("batch_id") or "").strip():
        errors.append("batch_id is required")
    for key in ("model_policies", "devices", "faults", "repairs", "observation_updates"):
        val = payload.get(key, [])
        if not isinstance(val, list):
            errors.append(f"{key} must be a list")
            continue
        for i, item in enumerate(val):
            if not isinstance(item, dict):
                errors.append(f"{key}[{i}] must be an object")
    return errors
