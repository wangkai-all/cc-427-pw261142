"""Domain model: resolve canonical facts into an analysable device history.

Three states that must never be merged
--------------------------------------
The data problem names them explicitly, so they are three distinct statuses and
three distinct censor codes throughout the schema and the API:

``not_yet_failed``
    The device was under observation at the analysis cutoff, no fault seen.
    Right-censored at the cutoff; future information may still change it.

``exited``
    Observation was administratively closed before the cutoff (retired,
    transferred, monitoring removed).  Right-censored at the exit date.  It is
    *not* evidence of a fault, and it is not the same kind of missingness as
    "still running": an exit is a decision, open observation is simply the end
    of what we know.

``pre_entry``
    Time the device ran before observation began.  This is left truncation,
    **not** failure-free time we observed.  A device commissioned 300 days
    before monitoring started has ``pre_entry_days = 300`` and enters every
    risk set at age 300; it contributes nothing to risk at younger ages.  The
    alternative -- silently pretending observation began at commissioning --
    is the classic left-truncation bias and is exactly what this project
    refuses to do.

Risk sets are a maintenance-phase partition
-------------------------------------------
Each device is on a **service clock** that starts at commissioning and resets at
each recorded repair.  The clock position ``elapsed / service_interval_days``
for the device's model maps to a risk set:

    safe    [0.00, 0.34)     recently serviced
    watch   [0.34, 0.67)     mid-interval
    danger  [0.67, inf)      due / overdue for service

``service_interval_days`` is a *policy constant per model* supplied with the
data (a maintenance interval), not a learned parameter.  Nothing here is a
prediction model: the risk set is a bookkeeping label used to stratify
descriptive survival curves, and the report says so.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

RISK_SETS: tuple[str, ...] = ("safe", "watch", "danger")

# Upper bounds (exclusive) of each band as a fraction of the service interval.
# danger is open-ended so overdue equipment stays in danger.
BAND_CUTOFFS: tuple[tuple[str, float], ...] = (
    ("safe", 0.34),
    ("watch", 0.67),
)
BAND_KEY_VERSION = "phase-0.34-0.67-v1"


class Status(str, Enum):
    FAULTED = "faulted"
    NOT_YET_FAILED = "not_yet_failed"
    EXITED = "exited"
    PRE_ENTRY_FAULT = "pre_entry_fault"   # fault before observation, no repair
    NOT_YET_OBSERVED = "not_yet_observed" # observation starts after the cutoff
    VOID = "void"                         # invalid window: end < start

    @property
    def at_risk(self) -> bool:
        return self in (Status.FAULTED, Status.NOT_YET_FAILED, Status.EXITED)


CENSOR_CODES = {
    Status.FAULTED: "fault",
    Status.NOT_YET_FAILED: "not_yet_failed",
    Status.EXITED: "exited",
}


@dataclass(frozen=True)
class FaultRecord:
    id: int
    device_id: str
    event_key: str
    fault_date: date
    fault_date_earliest: date | None
    component: str
    severity: str
    reported_date: date
    note: str | None
    revision: int


@dataclass(frozen=True)
class RepairRecord:
    id: int
    device_id: str
    event_key: str
    fault_event_key: str | None
    repair_date: date
    action: str
    downtime_days: int | None
    revision: int


@dataclass(frozen=True)
class ModelPolicy:
    model: str
    service_interval_days: int


@dataclass
class DeviceHistory:
    device_id: str
    model: str
    site: str
    commissioned_date: date
    observation_start_date: date
    observation_end_date: date | None
    observation_revision: int
    faults: list[FaultRecord] = field(default_factory=list)
    repairs: list[RepairRecord] = field(default_factory=list)
    policy: ModelPolicy | None = None

    def __post_init__(self) -> None:
        self.faults.sort(key=lambda f: (f.fault_date, f.id))
        self.repairs.sort(key=lambda r: (r.repair_date, r.id))

    # -- clock ---------------------------------------------------------
    @property
    def service_interval(self) -> int:
        return self.policy.service_interval_days if self.policy else 0

    @property
    def has_policy(self) -> bool:
        return self.policy is not None and self.service_interval > 0

    def last_repair_before(self, day: date) -> date | None:
        candidates = [r.repair_date for r in self.repairs if r.repair_date <= day]
        return max(candidates) if candidates else None

    def clock_origin(self) -> date:
        """Start of the current service cycle: commissioning or latest repair
        at/before observation start.  Repairs inside the observed window are
        deliberately *not* used: a device that faults and is repaired leaves
        the first-fault cohort, so its post-repair clock only matters for
        descriptive maintenance summaries."""
        repair = self.last_repair_before(self.observation_start_date)
        if repair is not None:
            return max(self.commissioned_date, repair)
        return self.commissioned_date

    def pre_entry_days(self) -> int:
        """Calendar days the device existed before observation began.

        Reported on its own; it is time we did **not** observe.  It is never
        treated as fault-free survival.
        """
        return max(0, (self.observation_start_date - self.commissioned_date).days)

    def entry_age(self) -> float:
        """Delayed-entry age on the service clock when observation begins.

        Equals ``pre_entry_days`` unless a repair before the observation window
        reset the clock, in which case the device entered observation early in
        a fresh service cycle.
        """
        return float(max(0, (self.observation_start_date - self.clock_origin()).days))

    # -- status --------------------------------------------------------
    def _first_in_window_fault(self, cutoff: date) -> FaultRecord | None:
        """First fault inside the *observed* window, bounded by the cutoff and
        by any observation end.  A fault dated after observation closed is a
        fault we did not observe; it must not turn an exit into an event.
        """
        last = self.followup_end(cutoff)
        for f in self.faults:
            if self.observation_start_date <= f.fault_date <= last:
                return f
        return None

    def _unrepaired_pre_entry_fault(self) -> FaultRecord | None:
        """A fault before observation with no repair at/before the start.

        We cannot establish what state the device was in when it entered
        observation, so it is excluded from risk sets rather than guessed.
        """
        last_repair = self.last_repair_before(self.observation_start_date)
        for f in self.faults:
            if f.fault_date >= self.observation_start_date:
                break
            if last_repair is None or last_repair < f.fault_date:
                return f
        return None

    def status(self, cutoff: date) -> Status:
        if self.observation_start_date > cutoff:
            return Status.NOT_YET_OBSERVED
        if (
            self.observation_end_date is not None
            and self.observation_end_date < self.observation_start_date
        ):
            return Status.VOID
        if self._first_in_window_fault(cutoff) is not None:
            return Status.FAULTED
        if self._unrepaired_pre_entry_fault() is not None:
            return Status.PRE_ENTRY_FAULT
        if (
            self.observation_end_date is not None
            and self.observation_end_date <= cutoff
        ):
            return Status.EXITED
        return Status.NOT_YET_FAILED

    # -- timing --------------------------------------------------------
    def followup_end(self, cutoff: date) -> date:
        """Last day the device was observed (bounded by cutoff)."""
        end = cutoff
        if self.observation_end_date is not None:
            end = min(end, self.observation_end_date)
        return max(end, self.observation_start_date)

    def end_age(self, cutoff: date) -> float:
        return (self.followup_end(cutoff) - self.clock_origin()).days

    def event_age(self, cutoff: date) -> float | None:
        f = self._first_in_window_fault(cutoff)
        if f is None:
            return None
        return (f.fault_date - self.clock_origin()).days

    def phase(self, day: date) -> float:
        if not self.has_policy:
            return 0.0
        return (day - self.clock_origin()).days / self.service_interval

    def band_at(self, age_days: float) -> str:
        frac = age_days / self.service_interval if self.has_policy else 0.0
        for name, upper in BAND_CUTOFFS:
            if frac < upper:
                return name
        return "danger"

    def band_boundaries(self) -> list[tuple[float, str]]:
        """(age, band) start points, excluding age 0."""
        if not self.has_policy:
            return []
        return [
            (upper * self.service_interval, name)
            for name, upper in BAND_CUTOFFS
        ]


def parse_iso(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def device_input_hash(hist: DeviceHistory) -> str:
    """Hash of every canonical input that can change this device's analysis.

    Deliberately excludes the cutoff date so that closed risk intervals stay
    reusable when ``as_of`` advances.  The open tail interval is always derived
    per build and never cached.
    """
    import hashlib
    import json

    payload = {
        "device": {
            "id": hist.device_id,
            "model": hist.model,
            "site": hist.site,
            "commissioned": hist.commissioned_date.isoformat(),
            "obs_start": hist.observation_start_date.isoformat(),
            "obs_end": hist.observation_end_date.isoformat()
            if hist.observation_end_date
            else None,
            "obs_revision": hist.observation_revision,
        },
        "faults": [
            [
                f.event_key,
                f.fault_date.isoformat(),
                f.fault_date_earliest.isoformat() if f.fault_date_earliest else None,
                f.component,
                f.severity,
                f.revision,
            ]
            for f in hist.faults
        ],
        "repairs": [
            [r.event_key, r.repair_date.isoformat(), r.action, r.downtime_days]
            for r in hist.repairs
        ],
        "policy": hist.policy.service_interval_days if hist.policy else None,
        "bands": [name for name, _ in BAND_CUTOFFS],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
