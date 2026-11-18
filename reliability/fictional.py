"""Deterministic fictional fleet data generator.

The data are synthetic and deliberately shaped to exercise the hard cases:

* devices commissioned long before monitoring began (pre-entry / left
  truncation);
* a few devices with a fault *before* observation began and no repair, so the
  entry state is unknown and they must be excluded from risk sets;
* late-reported faults (the fault happened months before it was reported);
* corrected faults (wrong component), a retracted duplicate, and interval-
  censored fault dates (fault known only to lie in an inspection window);
* observation windows moved later, closed, and re-opened after the fact;
* devices still running at the cutoff, and devices that exited observation
  without a fault -- both censored, but counted separately.

No real equipment, site, person or clinical variable is involved.  Same seed
reproduces the same batches exactly.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

COMPONENTS = [
    "drive-motor", "bearing-assembly", "control-board", "coolant-pump",
    "door-sensor", "power-supply",
]
SEVERITIES = ["minor", "minor", "major", "major", "critical"]
MODELS = {
    "MX-100": 180,
    "MX-220": 240,
    "LX-50": 120,
}

CUTOFF = date(2026, 9, 30)


def _iso(d: date) -> str:
    return d.isoformat()


def _component(rng: random.Random) -> str:
    return rng.choice(COMPONENTS)


def generate(seed: int = 7, n_devices: int = 400) -> list[dict]:
    """Return ingest payloads in chronological reporting order."""
    rng = random.Random(seed)
    sites = ["north-yard", "harbor", "inland"]

    policies = [
        {"model": m, "service_interval_days": iv} for m, iv in MODELS.items()
    ]

    plans = []
    for i in range(n_devices):
        model = rng.choice(list(MODELS))
        interval = MODELS[model]
        # Commissioned across ~7 years, so some devices are observed almost
        # from day one and others enter after years of unseen running.
        commissioned = date(2019, 1, 1) + timedelta(days=rng.randint(0, 2200))
        pre = rng.choice([0, 0, 0, 0, 7, 14, 30, 45, 60, 90, 150, 260, 420, 700])
        # Observation cannot start after the cutoff for devices we analyse;
        # cap the pre-entry gap for recently commissioned units.
        max_pre = (CUTOFF - commissioned).days - 10
        if max_pre < 0:
            commissioned = CUTOFF - timedelta(days=rng.randint(400, 2200))
            max_pre = (CUTOFF - commissioned).days - 10
        pre = min(pre, max_pre)
        obs_start = commissioned + timedelta(days=pre)

        # Latent age at first fault, measured from commissioning.  Spread over
        # several service cycles so all three phase bands see faults.
        fault_age = int(interval * rng.uniform(0.12, 6.5))
        # A small tail: never faults within the observation horizon.
        never = rng.random() < 0.06
        fault_date = commissioned + timedelta(days=fault_age)

        capacity = (CUTOFF - obs_start).days
        will_fault = (not never) and fault_age <= capacity + pre and fault_date <= CUTOFF

        obs_end = None
        exited = False
        if not will_fault:
            if rng.random() < 0.42 and capacity > 60:
                exit_day = rng.randint(30, capacity - 1)
                obs_end = obs_start + timedelta(days=exit_day)
                exited = True

        pre_entry_fault = None
        if i % 97 == 0 and i > 0:
            # fault strictly before observation, with no repair recorded
            pf_day = max(1, pre - rng.randint(10, max(11, pre)))
            pre_entry_fault = commissioned + timedelta(days=pf_day)
            will_fault = False
            obs_end = None
            exited = False

        plans.append(
            {
                "id": f"DEV-{i:04d}",
                "model": model,
                "site": rng.choice(sites),
                "commissioned": commissioned,
                "obs_start": obs_start,
                "obs_end": obs_end,
                "fault_date": fault_date if will_fault else None,
                "pre_entry_fault": pre_entry_fault,
                "exited": exited,
                "interval": interval,
                "severity": rng.choice(SEVERITIES),
                "component": _component(rng),
                "second_fault": rng.random() < 0.15 if will_fault else False,
                "repaired": rng.random() < 0.6,
            }
        )

    # ---- batch 1: reference data + devices ---------------------------
    batch1 = {
        "batch_id": f"fictional-001-baseline-s{seed}",
        "source": "fictional-generator",
        "model_policies": policies,
        "devices": [
            {
                "id": p["id"],
                "model": p["model"],
                "site": p["site"],
                "commissioned_date": _iso(p["commissioned"]),
                "observation_start_date": _iso(p["obs_start"]),
                "observation_end_date": _iso(p["obs_end"]) if p["obs_end"] else None,
                "observation_note": "initial upload",
            }
            for p in plans
        ],
    }

    # ---- batch 2: faults known by mid-2024 ---------------------------
    faults_early = []
    for p in plans:
        fd = p["fault_date"]
        if fd and fd <= date(2024, 6, 30):
            key = f"{p['id']}-F1"
            faults_early.append(
                {
                    "device_id": p["id"],
                    "event_key": key,
                    "fault_date": _iso(fd),
                    "component": p["component"],
                    "severity": p["severity"],
                    "reported_date": _iso(fd + timedelta(days=rng.randint(0, 15))),
                    "note": "initial report",
                }
            )
            if p["second_fault"]:
                fd2 = fd + timedelta(days=rng.randint(30, 400))
                if fd2 <= CUTOFF:
                    faults_early.append(
                        {
                            "device_id": p["id"],
                            "event_key": f"{p['id']}-F2",
                            "fault_date": _iso(fd2),
                            "component": _component(rng),
                            "severity": rng.choice(SEVERITIES),
                            "reported_date": _iso(fd2 + timedelta(days=rng.randint(0, 25))),
                        }
                    )
    # pre-entry faults arrive as a separate report stream
    for p in plans:
        if p["pre_entry_fault"] is not None:
            faults_early.append(
                {
                    "device_id": p["id"],
                    "event_key": f"{p['id']}-FPRE",
                    "fault_date": _iso(p["pre_entry_fault"]),
                    "component": _component(rng),
                    "severity": rng.choice(SEVERITIES),
                    "reported_date": _iso(p["obs_start"] + timedelta(days=5)),
                    "note": "fault found in pre-monitoring paperwork; no repair "
                            "record, entry state unknown",
                }
            )
    batch2 = {
        "batch_id": f"fictional-002-faults-through-2024h1-s{seed}",
        "source": "fictional-generator",
        "faults": faults_early,
    }

    # ---- batch 3: repairs ------------------------------------------
    repairs = []
    for f in faults_early:
        if f["event_key"].endswith("-FPRE"):
            continue
        if rng.random() < 0.55:
            fdate = date.fromisoformat(f["fault_date"])
            rdate = fdate + timedelta(days=rng.randint(1, 30))
            if rdate <= CUTOFF:
                repairs.append(
                    {
                        "device_id": f["device_id"],
                        "event_key": f"R-{f['event_key']}",
                        "fault_event_key": f["event_key"],
                        "repair_date": _iso(rdate),
                        "action": rng.choice(
                            ["replaced", "adjusted", "cleaned", "firmware-update"]
                        ),
                        "downtime_days": rng.randint(0, 6),
                    }
                )
    batch3 = {
        "batch_id": f"fictional-003-repairs-s{seed}",
        "source": "fictional-generator",
        "repairs": repairs,
    }

    # ---- batch 4: late-reported faults ------------------------------
    faults_late = []
    for p in plans:
        fd = p["fault_date"]
        if fd and date(2024, 7, 1) <= fd <= date(2025, 9, 30):
            key = f"{p['id']}-F1"
            faults_late.append(
                {
                    "device_id": p["id"],
                    "event_key": key,
                    "fault_date": _iso(fd),
                    "component": p["component"],
                    "severity": p["severity"],
                    "reported_date": _iso(fd + timedelta(days=rng.randint(120, 420))),
                    "note": "late report recovered from site paperwork",
                }
            )
    batch4 = {
        "batch_id": f"fictional-004-late-faults-s{seed}",
        "source": "fictional-generator",
        "faults": faults_late,
    }

    # ---- batch 5: corrections, retraction, interval-censored dates --
    corrections = []
    early = [f for f in faults_early if not f["event_key"].endswith("-FPRE")]
    for f in early[: max(1, len(early) // 15)]:
        corrections.append(
            {
                "device_id": f["device_id"],
                "event_key": f["event_key"],
                "fault_date": f["fault_date"],
                "component": "bearing-assembly",
                "severity": "major",
                "reported_date": f["reported_date"],
                "note": "component corrected during audit",
            }
        )
    for f in early[max(1, len(early) // 15): max(2, len(early) // 7)]:
        corrections.append(
            {
                "device_id": f["device_id"],
                "event_key": f["event_key"],
                "fault_date": f["fault_date"],
                "component": f["component"],
                "severity": f["severity"],
                "reported_date": f["reported_date"],
                "retracted": True,
                "note": "duplicate of another report; withdrawn",
            }
        )
    for f in early[-(len(early) // 9):]:
        fd = date.fromisoformat(f["fault_date"])
        corrections.append(
            {
                "device_id": f["device_id"],
                "event_key": f["event_key"],
                "fault_date": f["fault_date"],
                "fault_date_earliest": _iso(fd - timedelta(days=21)),
                "component": f["component"],
                "severity": f["severity"],
                "reported_date": f["reported_date"],
                "note": "quarterly inspection bracket; exact day unknown",
            }
        )
    batch5 = {
        "batch_id": f"fictional-005-corrections-s{seed}",
        "source": "fictional-generator",
        "faults": corrections,
    }

    # ---- batch 6: observation-window revisions ---------------------
    obs_updates = []
    open_at_cutoff = [p for p in plans if p["obs_end"] is None and p["fault_date"] is None
                      and p["pre_entry_fault"] is None]
    # (a) monitoring actually began later than first recorded
    for p in open_at_cutoff[: max(1, len(open_at_cutoff) // 10)]:
        new_start = p["obs_start"] + timedelta(days=rng.randint(10, 60))
        if new_start >= CUTOFF:
            continue
        obs_updates.append(
            {
                "device_id": p["id"],
                "observation_start_date": _iso(new_start),
                "observation_end_date": None,
                "note": "monitoring actually began later than first recorded",
            }
        )
    # (b) devices closed out of observation without a fault
    for p in open_at_cutoff[
        len(open_at_cutoff) // 10: len(open_at_cutoff) // 10 + max(1, len(open_at_cutoff) // 12)
    ]:
        end = min(CUTOFF, p["obs_start"] + timedelta(days=rng.randint(120, 900)))
        obs_updates.append(
            {
                "device_id": p["id"],
                "observation_end_date": _iso(end),
                "note": "removed from monitoring programme; no fault recorded",
            }
        )
    batch6 = {
        "batch_id": f"fictional-006-observation-revisions-s{seed}",
        "source": "fictional-generator",
        "observation_updates": obs_updates,
    }

    # ---- batch 7: recent faults, plus re-opened observation ----------
    faults_recent = []
    for p in plans:
        fd = p["fault_date"]
        if fd and date(2025, 10, 1) <= fd <= CUTOFF:
            key = f"{p['id']}-F1"
            faults_recent.append(
                {
                    "device_id": p["id"],
                    "event_key": key,
                    "fault_date": _iso(fd),
                    "component": p["component"],
                    "severity": p["severity"],
                    "reported_date": _iso(fd + timedelta(days=rng.randint(0, 15))),
                }
            )
    reopened = [
        {
            "device_id": u["device_id"],
            "observation_start_date": None,
            "observation_end_date": None,
            "note": "closure reinstated: device returned to monitored pool",
        }
        for u in obs_updates[:1]
    ]
    batch7 = {
        "batch_id": f"fictional-007-recent-faults-s{seed}",
        "source": "fictional-generator",
        "faults": faults_recent,
        "observation_updates": reopened,
    }

    return [batch1, batch2, batch3, batch4, batch5, batch6, batch7]


def write_batches(directory, seed: int = 7, n_devices: int = 400) -> list:
    """Write the batches as JSON files, one per reported batch, in order."""
    import json
    from pathlib import Path

    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for payload in generate(seed, n_devices):
        p = d / f"{payload['batch_id']}.json"
        p.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
        paths.append(p)
    return paths
