"""Risk-interval derivation and cohort construction.

A device's observed life is cut into adjacent intervals, each labelled with a
risk set.  The labels come from the maintenance-phase clock (see model.py):

  * the clock starts at commissioning, or at the latest repair **at/before
    observation start** (a repair inside the window means the device had its
    first fault in-window and leaves the first-fault cohort; we still record
    the repair for the maintenance summary, but it does not reset the survival
    clock, otherwise the device would re-enter the risk set after failing);
  * band boundaries are policy fractions of the model's service interval;
  * a closing interval ends at a fault, an observation exit, or the cutoff;
  * entry into observation is delayed until ``entry_age`` -- the first
    interval starts at the observation entry age, never at zero.

Only the intervals the device actually contributes are produced.  Devices that
had a fault before observation with no repair, or that were not yet observed by
the cutoff, produce none and say why.

Two views of the same data
--------------------------
``overall`` cohort
    One row per at-risk device, delayed entry at ``entry_age``, ending at first
    fault / exit / cutoff.  This is the headline descriptive curve and is
    independent of the risk-set partition.

``safe`` / ``watch`` / ``danger`` cohorts
    Each closed interval becomes one observation whose entry is the interval
    start and whose end is the interval end, censored at band transitions
    (``end_reason=band_transition``) and observed at a fault inside the band.
    This is what "which risk set" actually means for the curves, and it is why
    a device can contribute to more than one cohort.

The strategies differ only in *caching*, never in these definitions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .model import (
    BAND_CUTOFFS,
    CENSOR_CODES,
    DeviceHistory,
    Status,
    device_input_hash,
)

ALGORITHM_VERSION = "risk-intervals-v1"


@dataclass
class Interval:
    index: int
    cohort: str
    start_date: date
    end_date: date
    start_age: float
    end_age: float
    event: bool
    end_reason: str            # band_transition | fault | exit | cutoff
    basis: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return max(0.0, self.end_age - self.start_age)


@dataclass
class DeviceDerivation:
    device_id: str
    status: Status
    pre_entry_days: int
    entry_age: float
    followup_age: float
    first_fault_age: float | None
    censor_kind: str
    intervals: list[Interval]
    basis: dict
    input_hash: str
    reused: bool = False

    @property
    def at_risk(self) -> bool:
        return self.status.at_risk


def derive_device(
    hist: DeviceHistory, cutoff: date, reuse_intervals: list[Interval] | None = None
) -> DeviceDerivation:
    """Derive one device's intervals at ``cutoff``.

    ``reuse_intervals`` are previously cached closed intervals for an unchanged
    input hash; they save recomputation but are re-checked against the clock so
    a policy or band change can never be masked by a stale cache.
    """
    status = hist.status(cutoff)
    basis: dict = {
        "status": status.value,
        "status_reason": _status_reason(hist, cutoff, status),
        "clock_origin": hist.clock_origin().isoformat(),
        "service_interval_days": hist.service_interval if hist.has_policy else None,
        "band_cutoffs": {name: upper for name, upper in BAND_CUTOFFS},
        "observation": {
            "start": hist.observation_start_date.isoformat(),
            "end": hist.observation_end_date.isoformat()
            if hist.observation_end_date
            else None,
            "revision": hist.observation_revision,
        },
        "faults_in_window": [],
    }

    entry_age = hist.entry_age()
    followup_age = hist.end_age(cutoff)
    first_fault = hist._first_in_window_fault(cutoff)  # noqa: SLF001
    first_fault_age = hist.event_age(cutoff)
    censor_kind = CENSOR_CODES.get(status, "not_applicable")
    pre = hist.pre_entry_days()

    if first_fault is not None:
        basis["faults_in_window"].append(
            {
                "event_key": first_fault.event_key,
                "date": first_fault.fault_date.isoformat(),
                "age": first_fault_age,
                "late_report_days": (
                    first_fault.reported_date - first_fault.fault_date
                ).days,
                "revision": first_fault.revision,
                "interval_censored": bool(first_fault.fault_date_earliest),
            }
        )

    intervals: list[Interval] = []
    if status in (Status.FAULTED, Status.NOT_YET_FAILED, Status.EXITED) and hist.has_policy:
        intervals = _build_intervals(
            hist,
            cutoff,
            entry_age,
            followup_age,
            first_fault_age,
            status,
            reuse_intervals or [],
            basis,
        )
    elif not hist.has_policy and status.at_risk:
        basis["exclusion"] = (
            "no maintenance interval configured for model "
            f"{hist.model!r}; risk sets undefined, device excluded from "
            "cohort curves but retained in the overall curve"
        )

    # A device with no policy still belongs to the overall curve.
    intervals_overall = _overall_interval(
        hist, cutoff, entry_age, followup_age, first_fault_age, status
    )

    return DeviceDerivation(
        device_id=hist.device_id,
        status=status,
        pre_entry_days=pre,
        entry_age=entry_age,
        followup_age=followup_age,
        first_fault_age=first_fault_age,
        censor_kind=censor_kind,
        intervals=intervals + intervals_overall,
        basis=basis,
        input_hash=device_input_hash(hist),
    )


def _status_reason(hist: DeviceHistory, cutoff: date, status: Status) -> str:
    if status is Status.NOT_YET_OBSERVED:
        return f"observation begins {hist.observation_start_date} after cutoff {cutoff}"
    if status is Status.VOID:
        return "observation end precedes start; window is invalid"
    if status is Status.FAULTED:
        f = hist._first_in_window_fault(cutoff)  # noqa: SLF001
        return (
            f"first in-window fault {f.fault_date} (reported "
            f"{(f.reported_date - f.fault_date).days}d later, revision {f.revision})"
        )
    if status is Status.PRE_ENTRY_FAULT:
        f = hist._unrepaired_pre_entry_fault()  # noqa: SLF001
        return (
            f"fault on {f.fault_date} precedes observation start "
            f"{hist.observation_start_date} with no repair recorded; entry state "
            "unknown, excluded from risk sets rather than assumed healthy"
        )
    if status is Status.EXITED:
        return (
            f"observation closed {hist.observation_end_date} before cutoff {cutoff}; "
            "right-censored as an administrative exit, distinct from a fault"
        )
    return f"under observation at cutoff {cutoff} with no fault; right-censored open"


def _overall_interval(
    hist: DeviceHistory,
    cutoff: date,
    entry_age: float,
    followup_age: float,
    first_fault_age: float | None,
    status: Status,
) -> list[Interval]:
    if not status.at_risk:
        return []
    end_age = first_fault_age if first_fault_age is not None else followup_age
    end_date = (
        hist._first_in_window_fault(cutoff).fault_date  # noqa: SLF001
        if first_fault_age is not None
        else hist.followup_end(cutoff)
    )
    start_date = hist.observation_start_date
    return [
        Interval(
            index=0,
            cohort="overall",
            start_date=start_date,
            end_date=end_date,
            start_age=entry_age,
            end_age=float(end_age),
            event=first_fault_age is not None,
            end_reason=(
                "fault"
                if first_fault_age is not None
                else ("exit" if status is Status.EXITED else "cutoff")
            ),
            basis={
                "delayed_entry": True,
                "entry_age": entry_age,
                "pre_entry_days": hist.pre_entry_days(),
                "clock_origin": hist.clock_origin().isoformat(),
            },
        )
    ]


def _build_intervals(
    hist: DeviceHistory,
    cutoff: date,
    entry_age: float,
    followup_age: float,
    first_fault_age: float | None,
    status: Status,
    reuse: list[Interval],
    basis: dict,
) -> list[Interval]:
    """Cut [entry_age, followup_age] at band boundaries and the fault."""
    end_date = (
        hist._first_in_window_fault(cutoff).fault_date  # noqa: SLF001
        if first_fault_age is not None
        else hist.followup_end(cutoff)
    )
    entry_date = hist.observation_start_date
    final_age = first_fault_age if first_fault_age is not None else followup_age
    total_days = max(0.0, end_date.toordinal() - entry_date.toordinal())

    # Band boundaries strictly inside the observed window.
    boundaries = [
        age
        for age, _name in hist.band_boundaries()
        if entry_age < age < final_age
    ]

    cuts: list[float] = [entry_age] + boundaries + [float(final_age)]
    cached = {round(iv.start_age, 6): iv for iv in reuse if iv.cohort != "overall"}

    intervals: list[Interval] = []
    for i in range(len(cuts) - 1):
        start_age = cuts[i]
        end_age = cuts[i + 1]
        if end_age <= start_age:
            continue
        is_last = i == len(cuts) - 2
        if is_last and first_fault_age is not None:
            end_reason = "fault"
            event = True
        elif is_last and status is Status.EXITED:
            end_reason = "exit"
            event = False
        elif is_last:
            end_reason = "cutoff"
            event = False
        else:
            end_reason = "band_transition"
            event = False

        s_date = _date_at_age(hist, start_age)
        e_date = end_date if is_last else _date_at_age(hist, end_age)
        band = hist.band_at(start_age)
        basis_iv = {
            "band_rule": f"phase fraction in [{_band_lower(band)}, {_band_upper(band)})"
            if band != "danger"
            else f"phase fraction >= {_band_lower('danger')}",
            "phase_at_start": round(
                (start_age / hist.service_interval) if hist.has_policy else 0.0, 4
            ),
            "phase_at_end": round(
                (end_age / hist.service_interval) if hist.has_policy else 0.0, 4
            ),
            "entry_age": entry_age,
            "pre_entry_days": hist.pre_entry_days(),
            "started_in_observation": i == 0,
        }

        cached_iv = cached.get(round(start_age, 6))
        if (
            cached_iv is not None
            and cached_iv.end_age == end_age
            and cached_iv.cohort == band
            and cached_iv.end_reason == end_reason
            and cached_iv.event == event
        ):
            intervals.append(
                Interval(
                    index=len(intervals),
                    cohort=band,
                    start_date=cached_iv.start_date,
                    end_date=cached_iv.end_date,
                    start_age=start_age,
                    end_age=end_age,
                    event=event,
                    end_reason=end_reason,
                    basis=basis_iv,
                )
            )
            continue

        intervals.append(
            Interval(
                index=len(intervals),
                cohort=band,
                start_date=s_date,
                end_date=e_date,
                start_age=start_age,
                end_age=end_age,
                event=event,
                end_reason=end_reason,
                basis=basis_iv,
            )
        )

    basis["intervals_total_days"] = total_days
    basis["bands_entered"] = sorted({iv.cohort for iv in intervals})
    return intervals


def _date_at_age(hist: DeviceHistory, age: float) -> date:
    from datetime import timedelta

    return hist.clock_origin() + timedelta(days=int(round(age)))


def _band_lower(name: str) -> float:
    prev = 0.0
    for n, upper in BAND_CUTOFFS:
        if n == name:
            return prev
        prev = upper
    return prev


def _band_upper(name: str) -> float:
    for n, upper in BAND_CUTOFFS:
        if n == name:
            return upper
    return float("inf")
