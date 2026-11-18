"""Descriptive survival statistics.  No prediction model, no fitting.

What is computed
----------------
Kaplan-Meier product-limit estimate of the survival function on the **age
scale** (days since the service clock origin), from **left-truncated,
right-censored** observations:

* delayed entry -- a device enters the risk set only at its observed age, so
  early ages are absent from the denominator instead of being enriched with
  long-lived devices (left-truncation bias);
* right censoring -- ``exited`` and ``not_yet_failed`` are both censored, but
  counted under different codes so the report can show what each tail rests on;
* tied times -- entrants at age ``t`` are included in the risk set at ``t``,
  then events and censorings are processed (events before censorings).  The
  convention is stated so numbers can be reproduced.

Uncertainty
-----------
* Greenwood variance with a log-log transformed 95% band.
* A seeded, device-level **bootstrap** (resampling whole device histories, not
  rows) as a cross-check; reports state the seed and replication count.
* An at-risk table at fixed ages, so a tail resting on one device is visible.

Everything here is standard, published methodology.  The code states the
estimator rather than delegating to a library so the delayed-entry convention
and censor-code handling remain auditable.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

Z95 = 1.959963984540054


@dataclass(frozen=True)
class Observation:
    """One subject's interval of observed life on the age scale.

    The subject is under observation for ages in ``[entry, end]`` and is
    event-free at ``entry``.  If ``event`` is true, the event occurs at ``end``.
    """

    device_id: str
    entry: float
    end: float
    event: bool
    censor_kind: str          # fault | exited | not_yet_failed | band_transition
    left: float | None = None  # optional interval-censoring lower bound

    @property
    def observed_days(self) -> float:
        return max(0.0, self.end - self.entry)


@dataclass
class CurvePoint:
    age: float
    survival: float
    at_risk: int
    events: int
    censored: int
    greenwood_var: float


@dataclass
class Curve:
    cohort: str = ""
    points: list[CurvePoint] = field(default_factory=list)
    n_devices: int = 0
    n_intervals: int = 0
    n_events: int = 0
    n_censored_exited: int = 0
    n_censored_open: int = 0
    n_censored_transition: int = 0
    person_days: float = 0.0
    median_age: float | None = None
    median_note: str | None = None
    restricted_mean_age: float | None = None
    restricted_mean_note: str | None = None
    last_observed_age: float | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------
def _rows(observations: list[Observation]) -> list[tuple[float, float, int, str]]:
    """Rows for the risk-set loop; interval-censored events become two rows."""
    rows: list[tuple[float, float, int, str]] = []
    for ob in observations:
        if ob.end < ob.entry:
            continue
        if ob.event and ob.left is not None and ob.entry <= ob.left < ob.end:
            if ob.left > ob.entry:
                rows.append((ob.entry, ob.left, 0, "band_transition"))
            rows.append((max(ob.left, ob.entry), ob.end, 1, ob.censor_kind))
        else:
            rows.append((ob.entry, ob.end, 1 if ob.event else 0, ob.censor_kind))
    return rows


def km_curve(observations: list[Observation], cohort: str = "") -> Curve:
    curve = Curve(cohort=cohort)
    if not observations:
        curve.warnings.append("no observations")
        return curve

    rows = _rows(observations)
    events: dict[float, int] = {}
    censors: dict[float, int] = {}
    entries: dict[float, int] = {}
    for start, stop, ev, _kind in rows:
        if ev:
            events[stop] = events.get(stop, 0) + 1
        else:
            censors[stop] = censors.get(stop, 0) + 1
        entries.setdefault(start, 0)
        entries[start] += 1

    times = sorted(set(events) | set(censors) | set(entries))
    at_risk = 0
    surv = 1.0
    greenwood = 0.0
    until_risk = 0     # entrants strictly later than this time
    points: list[CurvePoint] = []
    n_events = 0

    # Entrants are known per start time; at each time add them, then events,
    # then censorings.  ``end == stop`` contributes to the risk set at that
    # time, so a censoring is visible before it leaves.
    for t in times:
        at_risk += entries.get(t, 0)
        d = events.get(t, 0)
        c = censors.get(t, 0)
        if d:
            if at_risk <= 0:
                curve.warnings.append(f"event at age {t} with empty risk set")
            else:
                surv *= 1.0 - d / at_risk
                if at_risk - d > 0:
                    greenwood += d / (at_risk * (at_risk - d))
                else:
                    greenwood = float("inf")
            n_events += d
        points.append(
            CurvePoint(t, surv, at_risk, d, c, greenwood)
        )
        at_risk -= d + c
        if at_risk < 0:
            at_risk = 0

    curve.points = points
    curve.n_devices = len({ob.device_id for ob in observations})
    curve.n_intervals = len(observations)
    curve.n_events = n_events
    curve.n_censored_exited = sum(
        1 for ob in observations if not ob.event and ob.censor_kind == "exited"
    )
    curve.n_censored_open = sum(
        1 for ob in observations if not ob.event and ob.censor_kind == "not_yet_failed"
    )
    curve.n_censored_transition = sum(
        1 for ob in observations if not ob.event and ob.censor_kind == "band_transition"
    )
    curve.person_days = sum(ob.observed_days for ob in observations)
    curve.last_observed_age = max((ob.end for ob in observations), default=None)

    _describe_limits(curve)
    return curve


def _describe_limits(curve: Curve) -> None:
    pts = curve.points
    if not pts:
        return

    # Median: first observed crossing of 0.5, with linear interpolation
    # between the two bracketing knots stated in the report.  If never reached,
    # nothing is extrapolated.
    median = None
    for i, p in enumerate(pts):
        if p.survival <= 0.5:
            prev_s = pts[i - 1].survival if i > 0 else 1.0
            prev_t = pts[i - 1].age if i > 0 else 0.0
            if prev_s == p.survival:
                median = p.age
            else:
                median = prev_t + (prev_s - 0.5) / (prev_s - p.survival) * (
                    p.age - prev_t
                )
            break
    curve.median_age = median
    if median is None:
        curve.median_note = (
            "median not reached within observed ages; no value extrapolated"
        )

    # Restricted mean survival age = area under the KM curve up to the last
    # observed age (not beyond it).
    last = curve.last_observed_age or 0.0
    area = 0.0
    prev_t = 0.0
    prev_s = 1.0
    for p in pts:
        if p.age > prev_t:
            area += prev_s * (p.age - prev_t)
        prev_t = p.age
        prev_s = p.survival
    if last > prev_t:
        area += prev_s * (last - prev_t)
    curve.restricted_mean_age = area
    curve.restricted_mean_note = (
        "restricted mean age is the area under the curve to the largest "
        "observed age; it is not an estimate beyond that age"
    )


# ---------------------------------------------------------------------------
# Uncertainty
# ---------------------------------------------------------------------------
def greenwood_band(curve: Curve, z: float = Z95) -> list[dict]:
    """Log-log transformed Greenwood 95% band; nulls where variance is unusable."""
    out = []
    for p in curve.points:
        note = None
        if p.survival <= 0.0 or p.survival >= 1.0:
            lo = hi = None
            note = "endpoint of the observed range; no finite interval"
        elif not math.isfinite(p.greenwood_var):
            lo = hi = None
            note = "last subject left the risk set; no finite interval"
        elif p.greenwood_var <= 0:
            lo = hi = None
            note = "before the first event"
        else:
            se_loglog = math.sqrt(p.greenwood_var) / abs(math.log(p.survival))
            loglog = math.log(-math.log(p.survival))
            lo = p.survival ** math.exp(z * se_loglog)
            hi = p.survival ** math.exp(-z * se_loglog)
        out.append(
            {
                "age": p.age,
                "survival": p.survival,
                "lower": lo,
                "upper": hi,
                "note": note,
            }
        )
    return out


def bootstrap_band(
    observations: list[Observation], reps: int = 300, seed: int = 20261118
) -> dict:
    """Seeded device-level bootstrap band; reproducible from stated inputs."""
    if not observations:
        return {"reps": 0, "seed": seed, "ages": [], "lower": [], "upper": []}
    ages = sorted({p.age for p in km_curve(observations).points})
    if not ages:
        return {"reps": 0, "seed": seed, "ages": [], "lower": [], "upper": []}

    rng = random.Random(seed)
    n = len(observations)
    draws: dict[float, list[float]] = {a: [] for a in ages}
    for _ in range(reps):
        sample = [observations[rng.randrange(n)] for _ in range(n)]
        c = km_curve(sample)
        j = 0
        s = 1.0
        for a in ages:
            while j < len(c.points) and c.points[j].age <= a:
                s = c.points[j].survival
                j += 1
            draws[a].append(s)
    return {
        "reps": reps,
        "seed": seed,
        "ages": ages,
        # _quantile requires sorted input; draws are in resample order, so sort
        # each age's values before taking the 2.5/97.5 percentiles.
        "lower": [_quantile(sorted(draws[a]), 0.025) for a in ages],
        "upper": [_quantile(sorted(draws[a]), 0.975) for a in ages],
    }


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def at_risk_table(curve: Curve, ages: list[float]) -> list[dict]:
    """At-risk counts and cumulative events at fixed ages, from the curve's
    declared risk-set convention.  Before the first knot the whole observed
    cohort that is event-free and under observation at that age is at risk."""
    rows = []
    j = 0
    ev = 0
    last_at_risk = 0
    for a in ages:
        while j < len(curve.points) and curve.points[j].age <= a:
            p = curve.points[j]
            ev += p.events
            last_at_risk = p.at_risk
            j += 1
        rows.append(
            {
                "age": a,
                "at_risk": last_at_risk if j > 0 else 0,
                "events_cum": ev,
            }
        )
    return rows
