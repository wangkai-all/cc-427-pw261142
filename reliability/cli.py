"""Command-line entry point for the standalone services.

Subcommands mirror the three services plus the fictional-data workflow::

    python -m reliability.cli init
    python -m reliability.cli ingest FILE [--dry-run]
    python -m reliability.cli generate-fictional DIR
    python -m reliability.cli build --as-of DATE --strategy materialized
    python -m reliability.cli build ... --resume BUILD_ID
    python -m reliability.cli pause BUILD_ID / cancel BUILD_ID
    python -m reliability.cli compare --as-of DATE
    python -m reliability.cli reports / report REPORT_ID
    python -m reliability.cli status
    python -m reliability.cli demo [--reset]

The ingestor and builder do not need the web service to run; they only share
the SQLite file.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from . import db as dbm
from .config import ALGORITHM_VERSION, DEFAULT_STRATEGY, STRATEGIES, Settings
from .repo import dataset_summary, device_detail, revision_of


def _settings(args) -> Settings:
    s = Settings.from_env()
    if getattr(args, "db", None):
        s = Settings(db_path=Path(args.db), artifacts_dir=s.artifacts_dir,
                     host=s.host, port=s.port)
    return s


def _connect(args):
    s = _settings(args)
    dbm.init_db(s.db_path)
    return dbm.connect(s.db_path), s


def cmd_init(args) -> int:
    s = _settings(args)
    dbm.init_db(s.db_path)
    print(f"initialised {s.db_path}")
    return 0


def cmd_ingest(args) -> int:
    from .ingestor import Ingestor, validate_batch_shape

    conn, _ = _connect(args)
    try:
        payload = json.loads(Path(args.file).read_text())
        errors = validate_batch_shape(payload)
        if errors:
            print(json.dumps({"ok": False, "errors": errors}, indent=2))
            return 2
        result = Ingestor(conn).ingest(payload, dry_run=args.dry_run)
        print(json.dumps(result.as_dict(), indent=2))
        return 0 if result.rejected == 0 else 1
    finally:
        conn.close()


def cmd_generate(args) -> int:
    from .fictional import write_batches

    paths = write_batches(args.directory, seed=args.seed, n_devices=args.devices)
    print(json.dumps({"written": [str(p) for p in paths]}, indent=2))
    return 0


def cmd_ingest_dir(args) -> int:
    from .ingestor import Ingestor

    conn, _ = _connect(args)
    results = []
    try:
        for p in sorted(Path(args.directory).glob("*.json")):
            payload = json.loads(p.read_text())
            r = Ingestor(conn, source="fictional-generator").ingest(payload)
            results.append({"file": p.name, **{
                "accepted": r.accepted,
                "skipped": r.skipped,
                "rejected": r.rejected,
                "data_revision": r.data_revision,
                "changed_devices": len(r.changed_devices),
                "duplicate": r.duplicate_batch,
            }})
        print(json.dumps(results, indent=2))
        return 0
    finally:
        conn.close()


def cmd_build(args) -> int:
    from .builder import BuildError, Builder
    from .repo import changed_devices_since

    conn, _ = _connect(args)
    try:
        builder = Builder(conn, _settings(args).artifacts_dir)
        if args.resume:
            out = builder.run(args.resume)
            print(json.dumps(out, indent=2, default=str))
            return 0 if out.get("status") != "failed" else 1

        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        strategy = args.strategy or DEFAULT_STRATEGY
        kind = "dry-run" if args.dry_run else (args.kind or "full")
        parent = None
        changed = None
        if args.since_build:
            parent = args.since_build
            prev = conn.execute("SELECT * FROM builds WHERE id=?", (parent,)).fetchone()
            if prev is None:
                raise BuildError(f"parent build {parent} not found")
            changed = changed_devices_since(conn, int(prev["data_revision"]))
            kind = "incremental"
            print(f"incremental: {len(changed)} device(s) changed since revision "
                  f"{prev['data_revision']}", file=sys.stderr)
        build_id = builder.create_build(
            as_of=as_of,
            strategy=strategy,
            kind=kind,
            parent_build_id=parent,
            changed_devices=changed,
            mode="dry-run" if args.dry_run else "apply",
            bootstrap_reps=args.bootstrap_reps,
            title=args.title,
        )
        if args.step:
            out = builder.run(build_id)   # step mode is handled by the web/API
        else:
            out = builder.run(build_id)
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("status") == "done" else 1
    except BuildError as e:
        print(json.dumps({"ok": False, "error": str(e)}, indent=2))
        return 2
    finally:
        conn.close()


def cmd_pause(args) -> int:
    from .builder import Builder

    conn, _ = _connect(args)
    try:
        Builder(conn).pause(args.build_id)
        print(json.dumps({"ok": True, "build_id": args.build_id,
                          "requested": "pause"}))
        return 0
    finally:
        conn.close()


def cmd_cancel(args) -> int:
    from .builder import Builder

    conn, _ = _connect(args)
    try:
        Builder(conn).cancel(args.build_id)
        print(json.dumps({"ok": True, "build_id": args.build_id,
                          "requested": "cancel"}))
        return 0
    finally:
        conn.close()


def cmd_compare(args) -> int:
    from .builder import compare_strategies

    conn, _ = _connect(args)
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
        print(json.dumps(compare_strategies(conn, as_of, args.limit), indent=2))
        return 0
    finally:
        conn.close()


def cmd_status(args) -> int:
    conn, _ = _connect(args)
    try:
        summary = dataset_summary(conn)
        builds = [dict(r) for r in conn.execute(
            "SELECT id, kind, strategy, as_of, status, progress, data_revision, "
            "report_id, error FROM builds ORDER BY id DESC LIMIT 20"
        ).fetchall()]
        reports = [dict(r) for r in conn.execute(
            "SELECT id, build_id, data_revision, strategy, as_of, title, created_at "
            "FROM reports ORDER BY id DESC LIMIT 20"
        ).fetchall()]
        print(json.dumps({"summary": summary, "builds": builds,
                          "reports": reports, "algorithm_version":
                          ALGORITHM_VERSION,
                          "strategies": list(STRATEGIES)}, indent=2))
        return 0
    finally:
        conn.close()


def cmd_reports(args) -> int:
    conn, _ = _connect(args)
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.build_id, r.data_revision, r.strategy, r.as_of,
                   r.title, r.created_at, r.supersedes,
                   (SELECT value FROM meta WHERE key='data_revision') AS current_revision
            FROM reports r ORDER BY r.id DESC
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stale"] = int(r["current_revision"] or 0) > int(r["data_revision"])
            out.append(d)
        print(json.dumps(out, indent=2))
        return 0
    finally:
        conn.close()


def cmd_report(args) -> int:
    conn, _ = _connect(args)
    try:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (args.report_id,)).fetchone()
        if row is None:
            print(json.dumps({"ok": False, "error": "report not found"}))
            return 1
        payload = json.loads(row["payload"])
        payload["_report"] = {
            "id": row["id"], "build_id": row["build_id"],
            "data_revision": row["data_revision"], "title": row["title"],
            "created_at": row["created_at"], "supersedes": row["supersedes"],
            "current_revision": revision_of(conn),
            "stale": revision_of(conn) > int(row["data_revision"]),
        }
        print(json.dumps(payload, indent=2))
        return 0
    finally:
        conn.close()


def cmd_device(args) -> int:
    conn, _ = _connect(args)
    try:
        d = device_detail(conn, args.device_id)
        if d is None:
            print(json.dumps({"ok": False, "error": "unknown device"}))
            return 1
        print(json.dumps(d, indent=2))
        return 0
    finally:
        conn.close()


def cmd_demo(args) -> int:
    """Full fictional pipeline from an empty directory, one command."""
    from .builder import Builder, compare_strategies
    from .fictional import write_batches
    from .ingestor import Ingestor

    s = _settings(args)
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(s.db_path) + suffix)
            if p.exists():
                p.unlink()
    dbm.init_db(s.db_path)
    data_dir = Path(args.data_dir or (Path(s.db_path).parent / "fictional-batches"))
    paths = write_batches(data_dir, seed=args.seed, n_devices=args.devices)
    conn = dbm.connect(s.db_path)
    try:
        print(f"[1/4] data written to {data_dir} ({len(paths)} batches)")
        revisions = []
        for p in paths:
            payload = json.loads(p.read_text())
            r = Ingestor(conn, source="fictional-demo").ingest(payload)
            revisions.append(
                {"batch": p.stem, "accepted": r.accepted,
                 "skipped": r.skipped, "rejected": r.rejected,
                 "revision": r.data_revision,
                 "changed_devices": len(r.changed_devices)}
            )
        print(json.dumps(revisions, indent=2))

        as_of = date.fromisoformat(args.as_of) if args.as_of else date(2026, 9, 30)
        builder = Builder(conn, s.artifacts_dir)
        print(f"[2/4] full build (materialized) as of {as_of}")
        bid = builder.create_build(as_of, "materialized", kind="full",
                                   bootstrap_reps=args.bootstrap_reps,
                                   title="Fictional fleet, full build")
        out = builder.run(bid)
        print(json.dumps({k: v for k, v in out.items() if k != "metrics"}, indent=2))
        first_report = out.get("report_id")

        print("[3/4] cost comparison: materialized vs recompute")
        cmp = compare_strategies(conn, as_of)
        print(json.dumps({
            "headline": cmp["headline"],
            "arms": {k: {
                "elapsed_ms": v["elapsed_ms"],
                "intervals_derived": v["intervals_derived"],
                "devices_reusing_cache": v["devices_reusing_cache"],
                "cost": v["cost"],
            } for k, v in cmp["arms"].items()},
        }, indent=2))

        print("[4/4] incremental rebuild after a late fault")
        # Choose a device that is still open-censored at this cutoff so the
        # late fault becomes its first observed event and the curve moves.
        target = conn.execute(
            """
            SELECT bd.device_id, d.observation_start_date, d.observation_end_date
            FROM build_devices bd
            JOIN devices d ON d.id = bd.device_id
            WHERE bd.build_id=? AND bd.status='not_yet_failed'
            ORDER BY bd.device_id LIMIT 1
            """,
            (bid,),
        ).fetchone()
        if target is None:
            print("no open-censored device available for the late-fault step")
            return 1
        obs_start = date.fromisoformat(target["observation_start_date"])
        obs_end = min(
            date.fromisoformat(target["observation_end_date"] or as_of.isoformat()),
            as_of,
        )
        fault_date = min(obs_start + timedelta(days=150), obs_end)
        late_device = target["device_id"]
        late = {
            "batch_id": "fictional-demo-late-correction",
            "source": "fictional-demo",
            "faults": [
                {
                    "device_id": late_device,
                    "event_key": f"{late_device}-LATE",
                    "fault_date": fault_date.isoformat(),
                    "component": "door-sensor",
                    "severity": "major",
                    "reported_date": "2026-09-20",
                    "note": "found on site log during final audit",
                }
            ],
        }
        r = Ingestor(conn, source="fictional-demo").ingest(late)
        print(json.dumps({"late": r.as_dict()["accepted"],
                          "revision": r.data_revision,
                          "changed_devices": r.changed_devices}, indent=2))
        from .repo import changed_devices_since

        prev = conn.execute("SELECT * FROM builds WHERE id=?", (bid,)).fetchone()
        changed = changed_devices_since(conn, int(prev["data_revision"]))
        bid2 = builder.create_build(
            as_of, "materialized", kind="incremental", parent_build_id=bid,
            changed_devices=changed, bootstrap_reps=args.bootstrap_reps,
            title="Fictional fleet, after late report",
        )
        out2 = builder.run(bid2)
        print(json.dumps({
            "build_id": bid2, "status": out2["status"],
            "report_id": out2.get("report_id"),
            "previous_report_kept": first_report,
            "metrics": out2.get("metrics"),
        }, indent=2, default=str))
        print("\nDone. Start the web UI with: python -m reliability.web")
        return 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reliability")
    p.add_argument("--db", help="path to SQLite database")
    # ``--db`` is also accepted after the subcommand.  default=SUPPRESS means a
    # subcommand that omits it does not overwrite a value given before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", default=argparse.SUPPRESS,
                        help="path to SQLite database")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_parser(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    sp = add_parser("init", help="create schema")
    sp.set_defaults(func=cmd_init)

    sp = add_parser("ingest", help="ingest one JSON batch")
    sp.add_argument("file")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_ingest)

    sp = add_parser("ingest-dir", help="ingest every JSON batch in a directory")
    sp.add_argument("directory")
    sp.set_defaults(func=cmd_ingest_dir)

    sp = add_parser("generate-fictional", help="write fictional batches")
    sp.add_argument("directory")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--devices", type=int, default=240)
    sp.set_defaults(func=cmd_generate)

    sp = add_parser("build", help="run a build (optionally incremental/resume)")
    sp.add_argument("--as-of")
    sp.add_argument("--strategy", choices=list(STRATEGIES), default=DEFAULT_STRATEGY)
    sp.add_argument("--kind", choices=["full", "incremental"])
    sp.add_argument("--since-build", type=int,
                    help="parent build id; rebuild only changed devices")
    sp.add_argument("--resume", type=int, help="resume an existing build id")
    sp.add_argument("--dry-run", action="store_true",
                    help="derive and measure cost without writing a report")
    sp.add_argument("--step", action="store_true", help=argparse.SUPPRESS)
    sp.add_argument("--bootstrap-reps", type=int, default=300)
    sp.add_argument("--title")
    sp.set_defaults(func=cmd_build)

    sp = add_parser("pause", help="pause a running build")
    sp.add_argument("build_id", type=int)
    sp.set_defaults(func=cmd_pause)

    sp = add_parser("cancel", help="cancel a build")
    sp.add_argument("build_id", type=int)
    sp.set_defaults(func=cmd_cancel)

    sp = add_parser("compare", help="measure both build strategies")
    sp.add_argument("--as-of")
    sp.add_argument("--limit", type=int, help="compare on first N devices only")
    sp.set_defaults(func=cmd_compare)

    sp = add_parser("status", help="dataset/build/report summary")
    sp.set_defaults(func=cmd_status)

    sp = add_parser("reports", help="list reports with staleness")
    sp.set_defaults(func=cmd_reports)

    sp = add_parser("report", help="print one report as JSON")
    sp.add_argument("report_id", type=int)
    sp.set_defaults(func=cmd_report)

    sp = add_parser("device", help="show one device's canonical history")
    sp.add_argument("device_id")
    sp.set_defaults(func=cmd_device)

    sp = add_parser("demo", help="generate fictional data and run the pipeline")
    sp.add_argument("--as-of")
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--devices", type=int, default=240)
    sp.add_argument("--bootstrap-reps", type=int, default=300)
    sp.add_argument("--data-dir")
    sp.add_argument("--reset", action="store_true")
    sp.set_defaults(func=cmd_demo)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
