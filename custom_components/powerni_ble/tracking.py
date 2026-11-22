"""Freshness and top-up tracking for Power NI readings (no Home Assistant imports)."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

from .client import PowerNIMeterData

# A reading older than this is reported as unavailable. Never shorter than
# three poll intervals, so a long configured interval (up to 24 h) does not
# make every sensor flicker unavailable between polls.
STALE_AFTER_MIN = timedelta(hours=24)
STALE_AFTER_POLL_MULTIPLE = 3

# Balance only falls between top-ups (by pennies per poll), so a rise of at
# least this much is treated as a top-up rather than rounding jitter.
TOP_UP_MIN_INCREASE = 1.0

FIELD_NAMES: tuple[str, ...] = tuple(
    field.name for field in dataclasses.fields(PowerNIMeterData)
)


def stale_after(scan_interval_minutes: int) -> timedelta:
    """Return the age at which a reading is treated as stale."""
    return max(
        STALE_AFTER_MIN,
        timedelta(minutes=scan_interval_minutes * STALE_AFTER_POLL_MULTIPLE),
    )


def fresh_fields(data: PowerNIMeterData) -> list[str]:
    """Return the fields a single (unmerged) read actually returned."""
    return [name for name in FIELD_NAMES if getattr(data, name) is not None]


def record_read_times(
    read_times: dict[str, datetime], data: PowerNIMeterData, now: datetime
) -> None:
    """Stamp every field present in an unmerged read with the read time."""
    for name in fresh_fields(data):
        read_times[name] = now
    # "NO DATA" leaves days_left empty but is still a fresh answer.
    if data.days_left_status is not None:
        read_times["days_left"] = now


def is_stale(last_read: datetime | None, now: datetime, max_age: timedelta) -> bool:
    """Return True if a reading was never taken or is older than max_age."""
    return last_read is None or now - last_read > max_age


def detect_top_up(
    previous_balance: float | None, new_balance: float | None
) -> float | None:
    """Return the top-up amount if the balance rose enough between reads."""
    if previous_balance is None or new_balance is None:
        return None
    increase = round(new_balance - previous_balance, 2)
    if increase >= TOP_UP_MIN_INCREASE:
        return increase
    return None
