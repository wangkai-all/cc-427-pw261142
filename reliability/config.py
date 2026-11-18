"""Configuration shared by the three services.

Everything is explicit and small: no environment magic outside ``RELIABILITY_*``
variables, and no implicit paths.  The web service and the CLI must be pointed
at the *same* database to cooperate; that is the only shared state.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Build strategies
# ---------------------------------------------------------------------------
# The builder can derive per-device risk intervals in two ways.  Both produce
# the same statistical definition of a curve; they differ in what is cached and
# in what has to be redone when data changes.  Reports always show the measured
# cost of whichever was used, and a side-by-side comparison is available.
STRATEGIES: dict[str, dict] = {
    "materialized": {
        "label": "Per-device interval cache",
        "description": (
            "Each device's closed risk intervals are written to "
            "risk_intervals and reused by later builds while the device's "
            "canonical input hash is unchanged.  Only the open tail interval "
            "is recomputed per cutoff."
        ),
        "cost": (
            "More rows and disk per device, but incremental rebuilds touch "
            "only devices whose faults/observation window changed."
        ),
        "tradeoff": "storage and write cost in exchange for cheap incremental rebuilds",
    },
    "recompute": {
        "label": "Whole-dataset recompute",
        "description": (
            "Every build re-derives every device's intervals in memory and "
            "never writes risk_intervals.  Identical numbers, no cache to "
            "invalidate."
        ),
        "cost": (
            "No interval cache; each build costs time proportional to all "
            "devices regardless of how little data changed.  The samples and "
            "report tables are still written."
        ),
        "tradeoff": "no cache invalidation risk in exchange for full recompute cost",
    },
}
DEFAULT_STRATEGY = "materialized"

# Explicit statement used in every report and comparison view so the choice is
# never presented as purely technical.
COST_NOTE = (
    "Neither strategy estimates a lifetime from fault records alone.  Curves "
    "are Kaplan-Meier estimates over observed exposure with delayed entry; "
    "cost figures below are measured build costs, not model quality."
)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    artifacts_dir: Path
    host: str = "127.0.0.1"
    port: int = 8701

    @staticmethod
    def from_env() -> "Settings":
        here = Path(__file__).resolve().parent.parent
        return Settings(
            db_path=Path(os.environ.get("RELIABILITY_DB", here / "var" / "fleet.db")),
            artifacts_dir=Path(
                os.environ.get("RELIABILITY_ARTIFACTS", here / "artifacts")
            ),
            host=os.environ.get("RELIABILITY_HOST", "127.0.0.1"),
            port=int(os.environ.get("RELIABILITY_PORT", "8701")),
        )


# A report is only valid for the exact data revision it was built from.
# Bumping this invalidates pinned reports deliberately -- do it only when the
# statistical definition changes, and record it in docs/methodology.md.
ALGORITHM_VERSION = "km-delayed-entry-v1"

DEFAULT_COHORTS = ("overall",) + ("safe", "watch", "danger")

MAX_AS_OF_DAYS_AHEAD = 366 * 5
