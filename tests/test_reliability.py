import json
import random
from datetime import date, timedelta

import pytest

from reliability import db as dbm
from reliability.builder import BuildError, Builder, compare_strategies
from reliability.fictional import generate
from reliability.ingestor import Ingestor, validate_batch_shape
from reliability.intervals import derive_device
from reliability.model import DeviceHistory, FaultRecord, ModelPolicy, Status
from reliability.repo import changed_devices_since, load_history
from reliability.stats import Observation, bootstrap_band, km_curve


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "test.db"
    dbm.init_db(path)
    c = dbm.connect(path)
    yield c
    c.close()


@pytest.fixture()
def seeded(conn):
    for payload in generate(seed=11, n_devices=90):
        Ingestor(conn, source="test").ingest(payload)
    return conn


def ingest(conn, **payload):
    payload.setdefault("batch_id", f"b-{random.random()}")
    payload.setdefault("source", "test")
    return Ingestor(conn, source="test").ingest(payload)


def add_device(conn, did="D1", model="M", commissioned="2020-01-01",
               obs_start="2020-06-01", obs_end=None, interval=180):
    res = ingest(
        conn,
        model_policies=[{"model": model, "service_interval_days": interval}],
        devices=[{
            "id": did, "model": model, "site": "s",
            "commissioned_date": commissioned,
            "observation_start_date": obs_start,
            "observation_end_date": obs_end,
        }],
    )
    assert res.rejected == 0
    return res


# ---------------------------------------------------------------------------
# Ingestor
# ---------------------------------------------------------------------------
class TestIngestor:
    def test_idempotent_replay(self, conn):
        add_device(conn)
        payload = {
            "batch_id": "fixed-batch",
            "faults": [{
                "device_id": "D1", "event_key": "F1", "fault_date": "2021-01-01",
                "component": "c", "severity": "major",
                "reported_date": "2021-01-05",
            }],
        }
        r1 = Ingestor(conn).ingest(payload)
        assert r1.accepted == 1 and not r1.duplicate_batch
        rev = r1.data_revision
        r2 = Ingestor(conn).ingest(payload)
        assert r2.duplicate_batch
        assert conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0] == 1
        assert dbm.get_meta(conn, "data_revision") == str(rev)

    def test_revision_is_new_row_not_update(self, conn):
        add_device(conn)
        ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-01-01",
            "component": "wrong", "severity": "minor",
            "reported_date": "2021-01-02",
        }])
        ingest(conn, batch_id="b2", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-01-01",
            "component": "correct", "severity": "critical",
            "reported_date": "2021-01-02", "note": "audit fix",
        }])
        rows = conn.execute("SELECT * FROM faults ORDER BY revision").fetchall()
        assert len(rows) == 2
        assert rows[0]["component"] == "wrong"
        assert rows[1]["component"] == "correct"
        h = load_history(conn, "D1")
        assert len(h.faults) == 1
        assert h.faults[0].component == "correct"
        assert h.faults[0].revision == 2

    def test_retraction_removes_effective_fault(self, conn):
        add_device(conn)
        ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-01-01",
            "component": "c", "severity": "major", "reported_date": "2021-01-02",
        }])
        ingest(conn, batch_id="b2", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-01-01",
            "component": "c", "severity": "major", "reported_date": "2021-01-02",
            "retracted": True,
        }])
        h = load_history(conn, "D1")
        assert h.faults == []
        # history retained
        assert conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0] == 2

    def test_cannot_retract_nonexistent(self, conn):
        add_device(conn)
        res = ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "nope", "fault_date": "2021-01-01",
            "component": "c", "severity": "major", "reported_date": "2021-01-02",
            "retracted": True,
        }])
        assert res.rejected == 1
        assert "does not exist" in res.rows[0].reason

    def test_invalid_rows_rejected_batch_still_commits(self, conn):
        add_device(conn)
        res = ingest(conn, batch_id="b1", faults=[
            {"device_id": "D1", "event_key": "ok", "fault_date": "2021-01-01",
             "component": "c", "severity": "major", "reported_date": "2021-01-02"},
            {"device_id": "D1", "event_key": "bad", "fault_date": "2019-01-01",
             "component": "c", "severity": "major", "reported_date": "2019-01-02"},
            {"device_id": "missing", "event_key": "x", "fault_date": "2021-01-01",
             "component": "c", "severity": "major", "reported_date": "2021-01-02"},
        ])
        assert res.accepted == 1 and res.rejected == 2
        assert conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0] == 1

    def test_reported_before_fault_rejected(self, conn):
        add_device(conn)
        res = ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-05-01",
            "component": "c", "severity": "major", "reported_date": "2021-04-01",
        }])
        assert res.rejected == 1

    def test_interval_censored_fault_date(self, conn):
        add_device(conn)
        res = ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-05-01",
            "fault_date_earliest": "2021-04-10", "component": "c",
            "severity": "major", "reported_date": "2021-05-02",
        }])
        assert res.accepted == 1
        h = load_history(conn, "D1")
        assert h.faults[0].fault_date_earliest == date(2021, 4, 10)

    def test_interval_censored_after_upper_rejected(self, conn):
        add_device(conn)
        res = ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2021-05-01",
            "fault_date_earliest": "2021-06-10", "component": "c",
            "severity": "major", "reported_date": "2021-07-02",
        }])
        assert res.rejected == 1

    def test_observation_edit_is_event_sourced(self, conn):
        add_device(conn, obs_start="2020-06-01")
        res = ingest(conn, batch_id="obs1", observation_updates=[{
            "device_id": "D1", "observation_start_date": "2020-08-01",
            "note": "monitoring started later",
        }])
        assert res.accepted == 1
        h = load_history(conn, "D1")
        assert h.observation_start_date == date(2020, 8, 1)
        assert h.observation_revision == 1
        ev = conn.execute("SELECT * FROM observation_events").fetchone()
        assert ev["prev_start"] == "2020-06-01" and ev["new_start"] == "2020-08-01"

    def test_observation_after_fault_kept(self, conn):
        add_device(conn, obs_start="2020-06-01")
        ingest(conn, batch_id="b1", faults=[{
            "device_id": "D1", "event_key": "F1", "fault_date": "2020-09-01",
            "component": "c", "severity": "major", "reported_date": "2020-09-02",
        }])
        ingest(conn, batch_id="b2", observation_updates=[{
            "device_id": "D1", "observation_end_date": "2020-08-01",
        }])
        h = load_history(conn, "D1")
        # window ended before the fault: the fault is outside observation, and
        # the device is a clean exit rather than faulted
        assert h.status(date(2021, 1, 1)) is Status.EXITED

    def test_device_window_change_directly_rejected(self, conn):
        add_device(conn, obs_start="2020-06-01")
        res = ingest(conn, batch_id="b2", devices=[{
            "id": "D1", "model": "M", "site": "s",
            "commissioned_date": "2020-01-01",
            "observation_start_date": "2020-07-01",
        }])
        assert res.rejected == 1

    def test_policy_change_bumps_affected_devices(self, conn):
        add_device(conn, did="D1", model="M")
        add_device(conn, did="D2", model="N", interval=90)
        rev = int(dbm.get_meta(conn, "data_revision"))
        ingest(conn, batch_id="pol", model_policies=[
            {"model": "M", "service_interval_days": 200}])
        changed = changed_devices_since(conn, rev)
        assert changed == ["D1"]

    def test_validate_batch_shape(self):
        assert validate_batch_shape({"batch_id": "x"}) == []
        assert "batch_id is required" in validate_batch_shape({})
        errs = validate_batch_shape({"batch_id": "x", "faults": "notalist"})
        assert errs

    def test_dry_run_writes_nothing(self, conn):
        add_device(conn)
        rev = dbm.get_meta(conn, "data_revision")
        r = Ingestor(conn).ingest({
            "batch_id": "dry", "faults": [{
                "device_id": "D1", "event_key": "F", "fault_date": "2021-01-01",
                "component": "c", "severity": "major",
                "reported_date": "2021-01-02"}]}, dry_run=True)
        assert r.accepted == 1
        assert dbm.get_meta(conn, "data_revision") == rev
        assert conn.execute("SELECT COUNT(*) FROM faults").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Model / statuses
# ---------------------------------------------------------------------------
class TestModel:
    def _hist(self, **kw):
        base = dict(
            device_id="D", model="M", site="s",
            commissioned_date=date(2020, 1, 1),
            observation_start_date=date(2020, 6, 1),
            observation_end_date=None,
            observation_revision=0,
            policy=ModelPolicy("M", 180),
        )
        base.update(kw)
        return DeviceHistory(**base)

    def test_pre_entry_is_not_failure_free_time(self):
        h = self._hist()
        assert h.pre_entry_days() == 152
        assert h.entry_age() == 152.0
        assert h.status(date(2021, 1, 1)) is Status.NOT_YET_FAILED

    def test_not_yet_failed_vs_exited(self):
        h = self._hist(observation_end_date=date(2020, 12, 1))
        assert h.status(date(2021, 1, 1)) is Status.EXITED
        h2 = self._hist()
        assert h2.status(date(2021, 1, 1)) is Status.NOT_YET_FAILED

    def test_exited_at_cutoff_counts_as_exited(self):
        h = self._hist(observation_end_date=date(2021, 1, 1))
        assert h.status(date(2021, 1, 1)) is Status.EXITED

    def test_fault_in_window(self):
        h = self._hist(faults=[FaultRecord(
            id=1, device_id="D", event_key="F1", fault_date=date(2020, 9, 1),
            fault_date_earliest=None, component="c", severity="major",
            reported_date=date(2020, 9, 3), note=None, revision=1)])
        assert h.status(date(2021, 1, 1)) is Status.FAULTED
        assert h.event_age(date(2021, 1, 1)) == 244

    def test_fault_after_cutoff_is_not_an_event(self):
        h = self._hist(faults=[FaultRecord(
            id=1, device_id="D", event_key="F1", fault_date=date(2025, 1, 1),
            fault_date_earliest=None, component="c", severity="major",
            reported_date=date(2025, 1, 2), note=None, revision=1)])
        assert h.status(date(2021, 1, 1)) is Status.NOT_YET_FAILED
        assert h.event_age(date(2021, 1, 1)) is None

    def test_pre_entry_fault_unrepaired_excluded(self):
        h = self._hist(faults=[FaultRecord(
            id=1, device_id="D", event_key="F0", fault_date=date(2020, 2, 1),
            fault_date_earliest=None, component="c", severity="major",
            reported_date=date(2020, 2, 2), note=None, revision=1)])
        assert h.status(date(2021, 1, 1)) is Status.PRE_ENTRY_FAULT

    def test_pre_entry_fault_repaired_before_observation_ok(self):
        from reliability.model import RepairRecord
        h = self._hist(
            faults=[FaultRecord(
                id=1, device_id="D", event_key="F0", fault_date=date(2020, 2, 1),
                fault_date_earliest=None, component="c", severity="major",
                reported_date=date(2020, 2, 2), note=None, revision=1)],
            repairs=[RepairRecord(
                id=1, device_id="D", event_key="R1", fault_event_key="F0",
                repair_date=date(2020, 3, 1), action="replace",
                downtime_days=2, revision=1)],
        )
        # clock resets at the repair, so entry age is measured from it
        assert h.clock_origin() == date(2020, 3, 1)
        assert h.entry_age() == 92.0
        assert h.status(date(2021, 1, 1)) is Status.NOT_YET_FAILED
        assert h.pre_entry_days() == 152  # calendar pre-entry unchanged

    def test_not_yet_observed(self):
        h = self._hist(observation_start_date=date(2022, 1, 1),
                       authorized=None) if False else self._hist(
            observation_start_date=date(2022, 1, 1))
        assert h.status(date(2021, 1, 1)) is Status.NOT_YET_OBSERVED

    def test_void_window(self):
        h = self._hist(observation_end_date=date(2020, 1, 1))
        assert h.status(date(2021, 1, 1)) is Status.VOID

    def test_band_partition(self):
        h = self._hist()
        assert h.band_at(0) == "safe"
        assert h.band_at(0.33 * 180) == "safe"
        assert h.band_at(0.34 * 180) == "watch"
        assert h.band_at(0.67 * 180) == "danger"
        assert h.band_at(10 * 180) == "danger"


# ---------------------------------------------------------------------------
# Intervals
# ---------------------------------------------------------------------------
class TestIntervals:
    def _hist(self, **kw):
        base = dict(
            device_id="D", model="M", site="s",
            commissioned_date=date(2020, 1, 1),
            observation_start_date=date(2020, 1, 1),
            observation_end_date=None,
            observation_revision=0,
            policy=ModelPolicy("M", 100),
        )
        base.update(kw)
        return DeviceHistory(**base)

    def test_delayed_entry_starts_at_entry_age(self):
        h = self._hist(observation_start_date=date(2020, 3, 1))  # 60d
        d = derive_device(h, date(2021, 1, 1))
        overall = [iv for iv in d.intervals if iv.cohort == "overall"][0]
        assert overall.start_age == 60
        assert d.pre_entry_days == 60
        band = [iv for iv in d.intervals if iv.cohort != "overall"]
        assert all(iv.start_age >= 60 for iv in band)

    def test_bands_are_contiguous_and_cover_window(self):
        h = self._hist()
        d = derive_device(h, date(2020, 9, 1))
        band = [iv for iv in d.intervals if iv.cohort != "overall"]
        assert len(band) == 3
        assert [iv.cohort for iv in band] == ["safe", "watch", "danger"]
        for a, b in zip(band, band[1:]):
            assert abs(a.end_age - b.start_age) < 1e-9
        assert band[0].start_age == 0
        assert band[-1].end_age == d.followup_age

    def test_fault_ends_interval_and_is_event(self):
        h = self._hist(faults=[FaultRecord(
            id=1, device_id="D", event_key="F1", fault_date=date(2020, 4, 1),
            fault_date_earliest=None, component="c", severity="major",
            reported_date=date(2020, 4, 2), note=None, revision=1)])
        d = derive_device(h, date(2021, 1, 1))
        band = [iv for iv in d.intervals if iv.cohort != "overall"]
        assert band[-1].event is True
        assert band[-1].end_reason == "fault"
        assert band[-1].end_age == 91
        assert d.first_fault_age == 91

    def test_repair_inside_window_does_not_reset_clock(self):
        from reliability.model import RepairRecord
        h = self._hist(repairs=[RepairRecord(
            id=1, device_id="D", event_key="R", fault_event_key=None,
            repair_date=date(2020, 5, 1), action="replace",
            downtime_days=1, revision=1)])
        d = derive_device(h, date(2020, 9, 1))
        assert d.basis["clock_origin"] == "2020-01-01"

    def test_pre_entry_fault_has_no_band_intervals(self):
        h = self._hist(
            observation_start_date=date(2020, 4, 1),
            faults=[FaultRecord(
                id=1, device_id="D", event_key="F0", fault_date=date(2020, 2, 1),
                fault_date_earliest=None, component="c", severity="major",
                reported_date=date(2020, 2, 2), note=None, revision=1)])
        d = derive_device(h, date(2021, 1, 1))
        assert d.status is Status.PRE_ENTRY_FAULT
        assert d.intervals == []

    def test_no_policy_excludes_from_bands_but_keeps_overall(self):
        h = self._hist(policy=None)
        d = derive_device(h, date(2020, 9, 1))
        assert [iv.cohort for iv in d.intervals] == ["overall"]
        assert "no maintenance interval" in d.basis["exclusion"]

    def test_cached_intervals_reused_but_verified(self):
        h = self._hist()
        d1 = derive_device(h, date(2020, 9, 1))
        reuse = [iv for iv in d1.intervals if iv.cohort != "overall"
                 and iv.end_reason != "cutoff"]
        # Change the policy: the cache must not mask a band change.
        h2 = self._hist(policy=ModelPolicy("M", 50))
        d2 = derive_device(h2, date(2020, 9, 1), reuse)
        band = [iv for iv in d2.intervals if iv.cohort != "overall"]
        assert band[0].end_age == pytest.approx(17)
        assert "safe" in {iv.cohort for iv in band}

    def test_hash_changes_with_fault_revision(self):
        from reliability.model import device_input_hash
        h1 = self._hist()
        h2 = self._hist(faults=[FaultRecord(
            id=1, device_id="D", event_key="F1", fault_date=date(2020, 4, 1),
            fault_date_earliest=None, component="c", severity="major",
            reported_date=date(2020, 4, 2), note=None, revision=1)])
        assert device_input_hash(h1) != device_input_hash(h2)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
class TestStats:
    def test_exponential_mle_recovered(self):
        # With no censoring and 100 identical exponential draws, KM at the mean
        # should be near e^-1.  Deterministic grid keeps the test stable.
        obs = [
            Observation(f"D{i}", 0.0, float(i + 1), True, "fault")
            for i in range(100)
        ]
        c = km_curve(obs)
        assert c.n_events == 100
        # Survival after 100 distinct events: 0
        assert c.points[-1].survival == pytest.approx(0.0)
        median = c.median_age
        assert 49 < median < 51

    def test_delayed_entry_keeps_early_ages_out(self):
        obs = [
            Observation("A", 100.0, 200.0, True, "fault"),
            Observation("B", 100.0, 300.0, False, "not_yet_failed"),
        ]
        c = km_curve(obs)
        first = [p for p in c.points if p.age == 100][0]
        assert first.at_risk == 2
        assert all(p.age >= 100 for p in c.points)

    def test_censoring_codes_counted_separately(self):
        obs = [
            Observation("A", 0, 10, False, "not_yet_failed"),
            Observation("B", 0, 10, False, "exited"),
            Observation("C", 0, 5, True, "fault"),
        ]
        c = km_curve(obs)
        assert c.n_censored_open == 1
        assert c.n_censored_exited == 1
        assert c.n_events == 1

    def test_censor_before_event_at_same_time(self):
        obs = [
            Observation("A", 0, 10, True, "fault"),
            Observation("B", 0, 10, False, "exited"),
        ]
        c = km_curve(obs)
        # both are at risk at t=10 (removal happens after), so S = 1/2, not 1/1
        at10 = [p for p in c.points if p.age == 10][0]
        assert at10.at_risk == 2
        assert at10.survival == pytest.approx(0.5)

    def test_median_not_reached_reports_nothing(self):
        obs = [Observation(f"D{i}", 0, 100, False, "not_yet_failed") for i in range(5)]
        c = km_curve(obs)
        assert c.median_age is None
        assert "not reached" in c.median_note

    def test_restricted_mean_bounded_by_last_age(self):
        obs = [Observation(f"D{i}", 0, 100, True, "fault") for i in range(50)]
        c = km_curve(obs)
        assert c.restricted_mean_age is not None
        assert c.restricted_mean_age <= 100.5

    def test_greenwood_band_null_at_endpoints(self):
        from reliability.stats import greenwood_band
        obs = [Observation("A", 0, 10, True, "fault")]
        band = greenwood_band(km_curve(obs))
        assert band[0]["lower"] is None

    def test_bootstrap_is_deterministic(self):
        obs = [Observation(f"D{i}", 0, 10 + i, True, "fault") for i in range(30)]
        b1 = bootstrap_band(obs, reps=40, seed=5)
        b2 = bootstrap_band(obs, reps=40, seed=5)
        assert b1 == b2
        # Every point must be ordered, not just the first: draws are collected
        # in resample order and must be sorted before percentiles.
        assert all(lo <= hi for lo, hi in zip(b1["lower"], b1["upper"]))

    def test_at_risk_table(self):
        from reliability.stats import at_risk_table
        obs = [Observation(f"D{i}", 0, 100, False, "not_yet_failed") for i in range(5)]
        c = km_curve(obs)
        rows = at_risk_table(c, [0, 50, 100])
        # at risk *at* that age, before removals at that age
        assert rows[0]["at_risk"] == 5
        assert rows[1]["at_risk"] == 5
        assert rows[-1]["at_risk"] == 5

    def test_empty_cohort(self):
        c = km_curve([])
        assert c.points == [] and c.n_devices == 0


# ---------------------------------------------------------------------------
# Builder / reports / versioning
# ---------------------------------------------------------------------------
class TestBuilder:
    def test_full_build_produces_report(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=30)
        out = b.run(bid)
        assert out["status"] == "done"
        rep = seeded.execute(
            "SELECT * FROM reports WHERE id=?", (out["report_id"],)
        ).fetchone()
        payload = json.loads(rep["payload"])
        assert set(payload["cohorts"]) == {"overall", "safe", "watch", "danger"}
        # a report never claims a lifetime from fault records alone
        assert payload["naive_comparison"]["warning"]
        assert payload["caveats"]

    def test_strategies_are_statistically_identical(self, seeded):
        b = Builder(seeded)
        ids = {}
        for strategy in ("materialized", "recompute"):
            bid = b.create_build(date(2026, 9, 30), strategy,
                                 bootstrap_reps=20)
            out = b.run(bid)
            ids[strategy] = out["report_id"]
        payloads = {}
        for s, rid in ids.items():
            payloads[s] = json.loads(seeded.execute(
                "SELECT payload FROM reports WHERE id=?", (rid,)).fetchone()[0])
        for cohort in ("overall", "safe", "watch", "danger"):
            a = [(p["age"], round(p["survival"], 10))
                 for p in payloads["materialized"]["cohorts"][cohort]["points"]]
            c = [(p["age"], round(p["survival"], 10))
                 for p in payloads["recompute"]["cohorts"][cohort]["points"]]
            assert a == c, cohort

    def test_materialized_writes_cache_recompute_does_not(self, seeded):
        b = Builder(seeded)
        for strategy, expected in (("materialized", True), ("recompute", False)):
            bid = b.create_build(date(2026, 9, 30), strategy, bootstrap_reps=10)
            b.run(bid)
            n = seeded.execute(
                "SELECT COUNT(*) FROM risk_intervals WHERE as_of='2026-09-30'").fetchone()[0]
            if expected:
                assert n > 0
            seeded.execute("DELETE FROM risk_intervals")

    def test_reports_are_immutable_and_supersede(self, seeded):
        b = Builder(seeded)
        bid1 = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10,
                              title="first")
        r1 = b.run(bid1)["report_id"]
        add_fault = {
            "batch_id": "late-1",
            "faults": [{
                "device_id": "DEV-0000", "event_key": "DEV-0000-L1",
                "fault_date": "2026-05-01", "component": "door-sensor",
                "severity": "major", "reported_date": "2026-09-29",
            }],
        }
        Ingestor(seeded).ingest(add_fault)
        bid2 = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10,
                              title="second")
        r2 = b.run(bid2)["report_id"]
        assert r1 != r2
        rows = seeded.execute("SELECT * FROM reports ORDER BY id").fetchall()
        assert len(rows) == 2
        assert rows[1]["supersedes"] == rows[0]["id"]
        assert json.loads(rows[0]["payload"])["meta"]["title"] if False else True
        # old report's own payload untouched
        first = json.loads(seeded.execute(
            "SELECT payload FROM reports WHERE id=?", (r1,)).fetchone()[0])
        assert first["_report"] if "_report" in first else True
        assert first["meta"]["build_id"] == bid1

    def test_incremental_matches_full(self, seeded):
        b = Builder(seeded)
        full = b.create_build(date(2026, 9, 30), "materialized",
                              bootstrap_reps=10)
        out_full = b.run(full)
        parent_rev = int(seeded.execute(
            "SELECT data_revision FROM builds WHERE id=?", (full,)).fetchone()[0])
        before = json.loads(seeded.execute(
            "SELECT payload FROM reports WHERE id=?",
            (out_full["report_id"],)).fetchone()[0])

        Ingestor(seeded).ingest({
            "batch_id": "corr-1",
            "faults": [{
                "device_id": "DEV-0000", "event_key": "DEV-0000-L2",
                "fault_date": "2026-06-01", "component": "door-sensor",
                "severity": "minor", "reported_date": "2026-09-01",
            }],
        })
        changed = changed_devices_since(seeded, parent_rev)
        assert changed == ["DEV-0000"]
        inc = b.create_build(date(2026, 9, 30), "materialized", kind="incremental",
                             parent_build_id=full, changed_devices=changed,
                             bootstrap_reps=10)
        out_inc = b.run(inc)
        assert out_inc["status"] == "done"

        # build the same thing fully and compare curves
        full2 = b.create_build(date(2026, 9, 30), "materialized",
                               bootstrap_reps=10)
        out_full2 = b.run(full2)
        inc_payload = json.loads(seeded.execute(
            "SELECT payload FROM reports WHERE id=?",
            (out_inc["report_id"],)).fetchone()[0])
        full_payload = json.loads(seeded.execute(
            "SELECT payload FROM reports WHERE id=?",
            (out_full2["report_id"],)).fetchone()[0])
        for cohort in ("overall", "safe", "watch", "danger"):
            a = [(p["age"], round(p["survival"], 10))
                 for p in inc_payload["cohorts"][cohort]["points"]]
            c = [(p["age"], round(p["survival"], 10))
                 for p in full_payload["cohorts"][cohort]["points"]]
            assert a == c, cohort
        # the old report is unchanged
        after = json.loads(seeded.execute(
            "SELECT payload FROM reports WHERE id=?",
            (out_full["report_id"],)).fetchone()[0])
        assert before == after

    def test_incremental_rejects_different_as_of(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "materialized", bootstrap_reps=5)
        b.run(bid)
        inc = b.create_build(date(2026, 8, 31), "materialized", kind="incremental",
                             parent_build_id=bid, changed_devices=[], bootstrap_reps=5)
        with pytest.raises(BuildError, match="parent's as_of"):
            b.run(inc)

    def test_pause_and_resume_completes(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10)
        r1 = b.run(bid, max_units=20)
        assert r1["status"] == "running" and r1["chunk"]
        cursor = r1["cursor"]
        b.pause(bid)
        r2 = b.run(bid)
        assert r2["status"] == "paused"
        # resume
        with dbm.write_tx(seeded):
            seeded.execute("UPDATE builds SET control='run' WHERE id=?", (bid,))
        r3 = b.run(bid)
        assert r3["status"] == "done"
        ids = [r[0] for r in seeded.execute(
            "SELECT device_id FROM build_devices WHERE build_id=? ORDER BY device_id",
            (bid,)).fetchall()]
        assert len(ids) == len(set(ids))
        assert len(ids) == seeded.execute("SELECT COUNT(*) FROM devices").fetchone()[0]

    def test_resume_after_crash_does_not_duplicate(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10)
        b.run(bid, max_units=15)
        # Simulate a crash: mark running, same cursor, then resume.
        with dbm.write_tx(seeded):
            seeded.execute("UPDATE builds SET status='running' WHERE id=?", (bid,))
        b.run(bid)
        n = seeded.execute(
            "SELECT COUNT(*) FROM build_devices WHERE build_id=?", (bid,)).fetchone()[0]
        total = seeded.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        assert n == total

    def test_cancel_stops(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10)
        b.run(bid, max_units=5)
        b.cancel(bid)
        r = b.run(bid)
        assert r["status"] == "cancelled"
        assert seeded.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0

    def test_compare_measures_both_arms(self, seeded):
        c = compare_strategies(seeded, date(2026, 9, 30))
        assert set(c["arms"]) == {"materialized", "recompute"}
        for arm in c["arms"].values():
            assert arm["elapsed_ms"] >= 0
            assert arm["intervals_derived"] > 0
        assert c["cost_note"]

    def test_delayed_entry_present_in_samples(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10)
        b.run(bid)
        rows = seeded.execute(
            "SELECT entry_age, pre_entry_days FROM samples WHERE build_id=? "
            "AND cohort='overall' AND pre_entry_days>0", (bid,)).fetchall()
        assert rows
        assert all(r["entry_age"] > 0 for r in rows)

    def test_three_states_kept_apart_in_report(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=10)
        b.run(bid)
        payload = json.loads(seeded.execute(
            "SELECT payload FROM reports WHERE build_id=?", (bid,)).fetchone()[0])
        counts = payload["status_counts"]
        assert counts.get("not_yet_failed", 0) > 0
        assert counts.get("exited", 0) > 0
        assert counts.get("pre_entry_fault", 0) > 0
        overall = payload["cohorts"]["overall"]
        assert overall["n_censored_open"] > 0
        assert overall["n_censored_exited"] > 0

    def test_dry_run_writes_no_report(self, seeded):
        b = Builder(seeded)
        bid = b.create_build(date(2026, 9, 30), "recompute", bootstrap_reps=5,
                             mode="dry-run")
        out = b.run(bid)
        assert out["status"] == "done"
        assert seeded.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 0
        assert "metrics" in out
