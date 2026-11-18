"""Canonical-store access: turn append-only revision rows into effective facts.

The rest of the system only ever sees ``DeviceHistory`` objects at a stated
``as_of`` date and, for reporting, a stated ``data_revision``.  Everything
about latest-revision selection and retraction lives here.

Late and revised data
---------------------
``as_of`` is the analysis cutoff (what we know the *world* to be) and is
independent of when a row reached us (``reported_date``, ``data_revision``).
A fault reported today can carry a ``fault_date`` three months ago; it is
included in any analysis whose cutoff is after that fault date.  That is the
intended behaviour for "持续补报" (continuing late reporting) and is why the
ingestor bumps the revision and the builder re-derives affected devices.
"""
from __future__ import annotations

import json
import sqlite3

from .model import (
    DeviceHistory,
    FaultRecord,
    ModelPolicy,
    RepairRecord,
    parse_iso,
)


def _latest_by_key(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    best: dict[str, sqlite3.Row] = {}
    for r in rows:
        prev = best.get(r["event_key"])
        if prev is None or r["revision"] > prev["revision"]:
            best[r["event_key"]] = r
    return [r for r in best.values() if not r["retracted"]]


def load_policies(conn: sqlite3.Connection) -> dict[str, ModelPolicy]:
    rows = conn.execute("SELECT model, service_interval_days FROM model_policies").fetchall()
    return {
        r["model"]: ModelPolicy(r["model"], int(r["service_interval_days"]))
        for r in rows
    }


def load_history(
    conn: sqlite3.Connection,
    device_id: str,
    policies: dict[str, ModelPolicy] | None = None,
) -> DeviceHistory | None:
    d = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if d is None:
        return None
    faults = _latest_by_key(
        conn.execute(
            "SELECT * FROM faults WHERE device_id=? ORDER BY id", (device_id,)
        ).fetchall()
    )
    repairs = _latest_by_key(
        conn.execute(
            "SELECT * FROM repairs WHERE device_id=? ORDER BY id", (device_id,)
        ).fetchall()
    )
    if policies is None:
        policies = load_policies(conn)
    return _to_history(d, faults, repairs, policies)


def load_histories(
    conn: sqlite3.Connection,
    device_ids: list[str] | None = None,
    policies: dict[str, ModelPolicy] | None = None,
) -> list[DeviceHistory]:
    if policies is None:
        policies = load_policies(conn)
    if device_ids is None:
        devices = conn.execute("SELECT * FROM devices ORDER BY id").fetchall()
        fault_rows = conn.execute("SELECT * FROM faults ORDER BY id").fetchall()
        repair_rows = conn.execute("SELECT * FROM repairs ORDER BY id").fetchall()
        faults_by: dict[str, list[sqlite3.Row]] = {}
        repairs_by: dict[str, list[sqlite3.Row]] = {}
        for r in fault_rows:
            faults_by.setdefault(r["device_id"], []).append(r)
        for r in repair_rows:
            repairs_by.setdefault(r["device_id"], []).append(r)
        return [
            _to_history(
                d,
                _latest_by_key(faults_by.get(d["id"], [])),
                _latest_by_key(repairs_by.get(d["id"], [])),
                policies,
            )
            for d in devices
        ]

    out: list[DeviceHistory] = []
    for did in device_ids:
        h = load_history(conn, did, policies)
        if h is not None:
            out.append(h)
    return out


def _to_history(
    d: sqlite3.Row,
    faults: list[sqlite3.Row],
    repairs: list[sqlite3.Row],
    policies: dict[str, ModelPolicy],
) -> DeviceHistory:
    return DeviceHistory(
        device_id=d["id"],
        model=d["model"],
        site=d["site"],
        commissioned_date=parse_iso(d["commissioned_date"]),
        observation_start_date=parse_iso(d["observation_start_date"]),
        observation_end_date=parse_iso(d["observation_end_date"]),
        observation_revision=int(d["observation_revision"]),
        faults=[
            FaultRecord(
                id=f["id"],
                device_id=f["device_id"],
                event_key=f["event_key"],
                fault_date=parse_iso(f["fault_date"]),
                fault_date_earliest=parse_iso(f["fault_date_earliest"]),
                component=f["component"],
                severity=f["severity"],
                reported_date=parse_iso(f["reported_date"]),
                note=f["note"],
                revision=int(f["revision"]),
            )
            for f in faults
        ],
        repairs=[
            RepairRecord(
                id=r["id"],
                device_id=r["device_id"],
                event_key=r["event_key"],
                fault_event_key=r["fault_event_key"],
                repair_date=parse_iso(r["repair_date"]),
                action=r["action"],
                downtime_days=r["downtime_days"],
                revision=int(r["revision"]),
            )
            for r in repairs
        ],
        policy=policies.get(d["model"]),
    )


def revision_of(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='data_revision'").fetchone()
    return int(row["value"]) if row else 0


def dataset_summary(conn: sqlite3.Connection) -> dict:
    def one(sql: str, *args) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    return {
        "revision": revision_of(conn),
        "devices": one("SELECT COUNT(*) FROM devices"),
        "fault_revisions": one("SELECT COUNT(*) FROM faults"),
        "fault_events": one(
            "SELECT COUNT(*) FROM (SELECT device_id, event_key FROM faults "
            "WHERE retracted=0 GROUP BY device_id, event_key)"
        ),
        "repairs": one("SELECT COUNT(*) FROM (SELECT device_id, event_key FROM repairs "
                       "WHERE retracted=0 GROUP BY device_id, event_key)"),
        "batches": one("SELECT COUNT(*) FROM ingest_batches"),
        "pending_late_faults": one(
            "SELECT COUNT(*) FROM faults f WHERE f.retracted=0 AND f.revision = "
            "(SELECT MAX(revision) FROM faults g WHERE g.device_id=f.device_id "
            " AND g.event_key=f.event_key) AND f.reported_date > f.fault_date"
        ),
        "reports": one("SELECT COUNT(*) FROM reports"),
        "builds": one("SELECT COUNT(*) FROM builds"),
    }


def device_detail(conn: sqlite3.Connection, device_id: str) -> dict | None:
    h = load_history(conn, device_id)
    if h is None:
        return None
    return {
        "device_id": h.device_id,
        "model": h.model,
        "site": h.site,
        "commissioned_date": h.commissioned_date.isoformat(),
        "observation_start_date": h.observation_start_date.isoformat(),
        "observation_end_date": (
            h.observation_end_date.isoformat() if h.observation_end_date else None
        ),
        "observation_revision": h.observation_revision,
        "service_interval_days": h.service_interval if h.has_policy else None,
        "clock_origin": h.clock_origin().isoformat(),
        "pre_entry_days": h.pre_entry_days(),
        "entry_age": h.entry_age(),
        "faults": [
            {
                "event_key": f.event_key,
                "fault_date": f.fault_date.isoformat(),
                "component": f.component,
                "severity": f.severity,
                "reported_date": f.reported_date.isoformat(),
                "revision": f.revision,
                "note": f.note,
                "late_report_days": (f.reported_date - f.fault_date).days,
            }
            for f in h.faults
        ],
        "fault_history": [
            {
                "event_key": r["event_key"],
                "revision": r["revision"],
                "fault_date": r["fault_date"],
                "component": r["component"],
                "severity": r["severity"],
                "retracted": bool(r["retracted"]),
                "data_revision": r["data_revision"],
                "created_at": r["created_at"],
            }
            for r in conn.execute(
                "SELECT * FROM faults WHERE device_id=? ORDER BY event_key, revision",
                (device_id,),
            ).fetchall()
        ],
        "repairs": [
            {
                "event_key": r.event_key,
                "repair_date": r.repair_date.isoformat(),
                "action": r.action,
                "downtime_days": r.downtime_days,
                "fault_event_key": r.fault_event_key,
            }
            for r in h.repairs
        ],
        "observation_history": [
            {
                "prev_start": r["prev_start"],
                "new_start": r["new_start"],
                "prev_end": r["prev_end"],
                "new_end": r["new_end"],
                "note": r["note"],
                "data_revision": r["data_revision"],
                "recorded_at": r["recorded_at"],
            }
            for r in conn.execute(
                "SELECT * FROM observation_events WHERE device_id=? ORDER BY id",
                (device_id,),
            ).fetchall()
        ],
    }


def changed_devices_since(conn: sqlite3.Connection, revision: int) -> list[str]:
    """Devices whose canonical facts changed after ``revision``.

    Used by the incremental builder to touch only affected devices.  Includes
    late-reported faults (new revision), repairs, revised observation windows,
    and every device whose model policy changed.  ``revision`` is a parent
    build's revision, whose rows are already incorporated, hence strict ``>``.
    """
    rows = conn.execute(
        """
        SELECT device_id FROM faults WHERE data_revision > ?
        UNION SELECT device_id FROM repairs WHERE data_revision > ?
        UNION SELECT device_id FROM observation_events WHERE data_revision > ?
        """,
        (revision, revision, revision),
    ).fetchall()
    out = {r["device_id"] for r in rows}
    policy_models = [
        r["model"]
        for r in conn.execute(
            "SELECT model FROM model_policies WHERE data_revision > ?", (revision,)
        ).fetchall()
    ]
    for model in policy_models:
        out |= {
            r["id"]
            for r in conn.execute(
                "SELECT id FROM devices WHERE model=?", (model,)
            ).fetchall()
        }
    return sorted(out)


def batches_after(conn: sqlite3.Connection, revision: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ingest_batches WHERE data_revision > ? ORDER BY id", (revision,)
    ).fetchall()


def json_dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str)
