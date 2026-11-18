"""Report service: a small JSON API plus the web UI.

It only reads canonical data and derived reports.  The one write path it owns
is *requesting* a build or an observation-window revision; builds run in a
worker thread using the same builder, and ingestion goes through the ingestor.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import date
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from . import db as dbm
from .builder import BuildError, Builder, compare_strategies
from .config import ALGORITHM_VERSION, COST_NOTE, DEFAULT_STRATEGY, STRATEGIES
from .ingestor import Ingestor, validate_batch_shape
from .model import BAND_KEY_VERSION, RISK_SETS, Status, now_iso
from .repo import dataset_summary, device_detail, revision_of

STATIC_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(db_path: str | Path) -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    app.config["DB_PATH"] = str(db_path)
    dbm.init_db(db_path)

    _worker_lock = threading.Lock()
    _workers: dict[int, threading.Thread] = {}

    def conn():
        return dbm.connect(app.config["DB_PATH"])

    # -- static -------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    # -- dataset / status ---------------------------------------------
    @app.get("/api/status")
    def status():
        c = conn()
        try:
            return jsonify({
                "dataset": dataset_summary(c),
                "algorithm_version": ALGORITHM_VERSION,
                "bands_key": BAND_KEY_VERSION,
                "risk_sets": list(RISK_SETS),
                "strategies": STRATEGIES,
                "default_strategy": DEFAULT_STRATEGY,
                "cost_note": COST_NOTE,
                "statuses": [s.value for s in Status],
            })
        finally:
            c.close()

    @app.get("/api/devices")
    def devices():
        c = conn()
        try:
            q = (request.args.get("q") or "").strip()
            model = request.args.get("model")
            site = request.args.get("site")
            limit = min(int(request.args.get("limit", 100)), 1000)
            clause = []
            args: list = []
            if q:
                clause.append("(id LIKE ? OR model LIKE ? OR site LIKE ?)")
                args += [f"%{q}%"] * 3
            if model:
                clause.append("model=?")
                args.append(model)
            if site:
                clause.append("site=?")
                args.append(site)
            where = ("WHERE " + " AND ".join(clause)) if clause else ""
            rows = c.execute(
                f"SELECT id, model, site, commissioned_date, observation_start_date, "
                f"observation_end_date FROM devices {where} ORDER BY id LIMIT ?",
                (*args, limit),
            ).fetchall()
            return jsonify({"devices": [dict(r) for r in rows]})
        finally:
            c.close()

    @app.get("/api/devices/<device_id>")
    def device(device_id):
        c = conn()
        try:
            d = device_detail(c, device_id)
            if d is None:
                return jsonify({"error": "unknown device"}), 404
            # latest report explaining each risk set the device entered
            rows = c.execute(
                """
                SELECT rd.report_id, rd.status, rd.pre_entry_days, rd.entry_age,
                       rd.followup_age, rd.first_fault_age, rd.entered_sets,
                       rd.basis, r.as_of, r.data_revision, r.created_at,
                       r.algorithm_version
                FROM report_devices rd JOIN reports r ON r.id = rd.report_id
                WHERE rd.device_id=? ORDER BY rd.report_id DESC
                """,
                (device_id,),
            ).fetchall()
            seen = set()
            explanations = []
            for r in rows:
                if r["report_id"] in seen:
                    continue
                seen.add(r["report_id"])
                explanations.append({
                    "report_id": r["report_id"],
                    "as_of": r["as_of"],
                    "data_revision": r["data_revision"],
                    "created_at": r["created_at"],
                    "algorithm_version": r["algorithm_version"],
                    "status": r["status"],
                    "pre_entry_days": r["pre_entry_days"],
                    "entry_age": r["entry_age"],
                    "followup_age": r["followup_age"],
                    "first_fault_age": r["first_fault_age"],
                    "entered_sets": json.loads(r["entered_sets"] or "[]"),
                    "basis": json.loads(r["basis"] or "{}"),
                })
            d["risk_explanations"] = explanations
            return jsonify(d)
        finally:
            c.close()

    @app.get("/api/devices/<device_id>/intervals")
    def device_intervals(device_id):
        """Risk intervals for a device at a cutoff (derived live, not cached)."""
        from .intervals import derive_device
        from .repo import load_history

        c = conn()
        try:
            as_of = _parse_date(request.args.get("as_of")) or date.today()
            h = load_history(c, device_id)
            if h is None:
                return jsonify({"error": "unknown device"}), 404
            deriv = derive_device(h, as_of)
            return jsonify({
                "device_id": device_id,
                "as_of": as_of.isoformat(),
                "status": deriv.status.value,
                "pre_entry_days": deriv.pre_entry_days,
                "entry_age": deriv.entry_age,
                "followup_age": deriv.followup_age,
                "first_fault_age": deriv.first_fault_age,
                "censor_kind": deriv.censor_kind,
                "basis": deriv.basis,
                "intervals": [
                    {
                        "index": iv.index,
                        "cohort": iv.cohort,
                        "start_date": iv.start_date.isoformat(),
                        "end_date": iv.end_date.isoformat(),
                        "start_age": iv.start_age,
                        "end_age": iv.end_age,
                        "event": iv.event,
                        "end_reason": iv.end_reason,
                        "basis": iv.basis,
                    }
                    for iv in deriv.intervals
                ],
            })
        finally:
            c.close()

    # -- observation edits ----------------------------------------------
    @app.post("/api/devices/<device_id>/observation")
    def edit_observation(device_id):
        body = request.get_json(force=True, silent=True) or {}
        payload = {
            "batch_id": body.get("batch_id") or f"ui-obs-{device_id}-{now_iso()}",
            "source": "web",
            "observation_updates": [
                {
                    "device_id": device_id,
                    "observation_start_date": body.get("observation_start_date"),
                    "observation_end_date": body.get("observation_end_date"),
                    "note": body.get("note") or "edited in web UI",
                }
            ],
        }
        c = conn()
        try:
            res = Ingestor(c, source="web").ingest(payload)
            return jsonify(res.as_dict())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            c.close()

    # -- ingest ---------------------------------------------------------
    @app.post("/api/ingest")
    def ingest():
        body = request.get_json(force=True, silent=True)
        if body is None:
            return jsonify({"error": "JSON body required"}), 400
        errors = validate_batch_shape(body)
        if errors:
            return jsonify({"error": "invalid batch", "details": errors}), 400
        dry = bool(request.args.get("dry_run"))
        c = conn()
        try:
            res = Ingestor(c, source=body.get("source") or "api").ingest(body, dry_run=dry)
            return jsonify(res.as_dict())
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            c.close()

    @app.get("/api/batches")
    def batches():
        c = conn()
        try:
            rows = c.execute(
                "SELECT id, received_at, source, data_revision, accepted, skipped, "
                "rejected, changed_devices FROM ingest_batches ORDER BY id DESC LIMIT 50"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["changed_devices"] = json.loads(d["changed_devices"] or "[]")
                out.append(d)
            return jsonify(out)
        finally:
            c.close()

    # -- builds ---------------------------------------------------------
    @app.get("/api/builds")
    def builds():
        c = conn()
        try:
            rows = c.execute(
                "SELECT * FROM builds ORDER BY id DESC LIMIT 60"
            ).fetchall()
            return jsonify([_build_dict(r) for r in rows])
        finally:
            c.close()

    @app.get("/api/builds/<int:build_id>")
    def build(build_id):
        c = conn()
        try:
            r = c.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            if r is None:
                return jsonify({"error": "unknown build"}), 404
            d = _build_dict(r)
            if r["report_id"]:
                d["report"] = {"id": r["report_id"]}
            return jsonify(d)
        finally:
            c.close()

    @app.post("/api/builds")
    def start_build():
        body = request.get_json(force=True, silent=True) or {}
        strategy = body.get("strategy") or DEFAULT_STRATEGY
        as_of = _parse_date(body.get("as_of"))
        if strategy not in STRATEGIES:
            return jsonify({"error": f"unknown strategy {strategy!r}"}), 400
        c = conn()
        try:
            parent = body.get("parent_build_id")
            kind = "incremental" if parent else "full"
            changed = None
            if parent:
                p = c.execute("SELECT * FROM builds WHERE id=?", (parent,)).fetchone()
                if p is None or p["status"] != "done":
                    return jsonify({"error": "parent build must be complete"}), 400
                from .repo import changed_devices_since

                changed = changed_devices_since(c, int(p["data_revision"]))
                as_of = date.fromisoformat(p["as_of"])
            if as_of is None:
                as_of = date.today()
            builder = Builder(c)
            build_id = builder.create_build(
                as_of=as_of,
                strategy=strategy,
                kind=kind,
                parent_build_id=parent,
                changed_devices=changed,
                bootstrap_reps=int(body.get("bootstrap_reps", 300)),
                title=body.get("title"),
            )
        except BuildError as e:
            return jsonify({"error": str(e)}), 400
        finally:
            c.close()

        # Run in the background in chunks so pause/cancel take effect promptly
        # and a long build never blocks an HTTP request.
        with _worker_lock:
            if build_id in _workers and _workers[build_id].is_alive():
                return jsonify({"build_id": build_id, "status": "already running"})
            t = threading.Thread(
                target=_run_build_worker,
                args=(app.config["DB_PATH"], build_id),
                daemon=True,
            )
            _workers[build_id] = t
            t.start()
        return jsonify({"build_id": build_id, "status": "queued"}), 202

    @app.post("/api/builds/<int:build_id>/pause")
    def pause_build(build_id):
        c = conn()
        try:
            Builder(c).pause(build_id)
            return jsonify({"ok": True, "build_id": build_id, "requested": "pause"})
        finally:
            c.close()

    @app.post("/api/builds/<int:build_id>/cancel")
    def cancel_build(build_id):
        c = conn()
        try:
            Builder(c).cancel(build_id)
            return jsonify({"ok": True, "build_id": build_id, "requested": "cancel"})
        finally:
            c.close()

    @app.post("/api/builds/<int:build_id>/resume")
    def resume_build(build_id):
        c = conn()
        try:
            r = c.execute("SELECT * FROM builds WHERE id=?", (build_id,)).fetchone()
            if r is None:
                return jsonify({"error": "unknown build"}), 404
            if r["status"] == "done":
                return jsonify({"error": "build already complete"}), 400
            c.execute("UPDATE builds SET control='run' WHERE id=?", (build_id,))
        finally:
            c.close()
        with _worker_lock:
            if build_id in _workers and _workers[build_id].is_alive():
                return jsonify({"build_id": build_id, "status": "already running"})
            t = threading.Thread(
                target=_run_build_worker,
                args=(app.config["DB_PATH"], build_id),
                daemon=True,
            )
            _workers[build_id] = t
            t.start()
        return jsonify({"build_id": build_id, "status": "resuming"}), 202

    def _run_build_worker(path: str, build_id: int) -> None:
        c = dbm.connect(path)
        try:
            builder = Builder(c)
            while True:
                out = builder.run(build_id, max_units=25)
                if out["status"] != "running" or not out.get("chunk"):
                    break
        except Exception as e:  # pragma: no cover - surfaced via build row
            try:
                with dbm.write_tx(c):
                    c.execute(
                        "UPDATE builds SET status='failed', error=?, finished_at=? "
                        "WHERE id=?",
                        (f"{type(e).__name__}: {e}\n{traceback.format_exc()[-2000:]}",
                         now_iso(), build_id),
                    )
            except Exception:
                pass
        finally:
            c.close()

    # -- comparison ------------------------------------------------------
    @app.get("/api/compare")
    def compare():
        c = conn()
        try:
            as_of = _parse_date(request.args.get("as_of")) or date.today()
            limit = request.args.get("limit")
            out = compare_strategies(
                c, as_of, int(limit) if limit else None
            )
            # Enrich with measured disk use of the interval cache.
            row = c.execute(
                "SELECT COUNT(*) AS n, COUNT(DISTINCT device_id) AS d FROM risk_intervals"
            ).fetchone()
            out["interval_cache"] = {"rows": row["n"], "devices": row["d"]}
            out["db_bytes"] = dbm.db_size(app.config["DB_PATH"])
            out["strategies"] = STRATEGIES
            out["cost_note"] = COST_NOTE
            return jsonify(out)
        finally:
            c.close()

    # -- reports ---------------------------------------------------------
    @app.get("/api/reports")
    def reports():
        c = conn()
        try:
            cur = revision_of(c)
            rows = c.execute(
                "SELECT id, build_id, data_revision, strategy, as_of, title, "
                "created_at, supersedes FROM reports ORDER BY id DESC"
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["stale"] = cur > int(r["data_revision"])
                out.append(d)
            return jsonify({"current_revision": cur, "reports": out})
        finally:
            c.close()

    @app.get("/api/reports/<int:report_id>")
    def report(report_id):
        c = conn()
        try:
            r = c.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if r is None:
                return jsonify({"error": "unknown report"}), 404
            payload = json.loads(r["payload"])
            cur = revision_of(c)
            payload["_report"] = {
                "id": r["id"],
                "build_id": r["build_id"],
                "data_revision": r["data_revision"],
                "strategy": r["strategy"],
                "as_of": r["as_of"],
                "title": r["title"],
                "created_at": r["created_at"],
                "supersedes": r["supersedes"],
                "current_revision": cur,
                "stale": cur > int(r["data_revision"]),
            }
            return jsonify(payload)
        finally:
            c.close()

    @app.get("/api/reports/<int:report_id>/devices")
    def report_devices(report_id):
        c = conn()
        try:
            cohort = request.args.get("cohort")
            status_filter = request.args.get("status")
            q = (request.args.get("q") or "").strip()
            limit = min(int(request.args.get("limit", 500)), 2000)
            rows = c.execute(
                "SELECT * FROM report_devices WHERE report_id=? ORDER BY device_id",
                (report_id,),
            ).fetchall()
            out = []
            for r in rows:
                entered = json.loads(r["entered_sets"] or "[]")
                if cohort and cohort not in entered:
                    continue
                if status_filter and r["status"] != status_filter:
                    continue
                if q and q not in r["device_id"]:
                    continue
                out.append({
                    "device_id": r["device_id"],
                    "status": r["status"],
                    "pre_entry_days": r["pre_entry_days"],
                    "entry_age": r["entry_age"],
                    "followup_age": r["followup_age"],
                    "first_fault_age": r["first_fault_age"],
                    "entered_sets": entered,
                    "basis": json.loads(r["basis"] or "{}"),
                })
                if len(out) >= limit:
                    break
            return jsonify({"report_id": report_id, "devices": out,
                            "count": len(out)})
        finally:
            c.close()

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify({"error": "not found"}), 404
        return send_from_directory(STATIC_DIR, "index.html")

    return app


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _build_dict(r) -> dict:
    d = dict(r)
    d["cost"] = json.loads(d.get("cost") or "{}")
    d["analysis_params"] = json.loads(d.get("analysis_params") or "{}")
    return d


app = None


def main() -> None:
    from .config import Settings

    s = Settings.from_env()
    app = create_app(s.db_path)
    app.run(host=s.host, port=s.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
