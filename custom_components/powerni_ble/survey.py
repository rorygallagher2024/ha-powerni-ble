"""14-day half-hourly energy survey parser (no Home Assistant imports)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from .const import (
    RSP_SURVEY_BLOCK_HEADER,
    SURVEY_BLOCK_SIZE,
    SURVEY_BLOCKS_PER_DAY,
)

SURVEY_YEAR_EPOCH = 1988
SURVEY_EMPTY_SENTINEL = 0x1FFF
SURVEY_COUNTER_MASK = 0x1FFF
SURVEY_COUNTER_WRAP = 8000
SURVEY_KWH_PER_COUNT = 0.00625


@dataclass(frozen=True)
class SurveySlot:
    """Single 30-minute energy interval decoded from the meter survey."""

    start: datetime
    kwh: float
    counter: int | None
    has_baseline: bool = True


@dataclass(frozen=True)
class HourlyEnergy:
    """Completed 1-hour energy consumption bucket in UTC."""

    start: datetime
    kwh: float


def decode_survey_date(
    header_block: bytes, tz: tzinfo = UTC
) -> datetime | None:
    """Decode the midnight date header of a 13-block survey day."""
    if len(header_block) < 3 or header_block[0] != RSP_SURVEY_BLOCK_HEADER:
        return None
    word = int.from_bytes(header_block[1:3], "little")
    year = (word >> 9) + SURVEY_YEAR_EPOCH
    day_offset = word & 0x1FF
    if not (2000 <= year <= 2099) or not (0 <= day_offset <= 366):
        return None
    return datetime(year, 1, 1, tzinfo=tz) + timedelta(days=day_offset)


def counter_delta_kwh(previous: int | None, current: int | None) -> float:
    """Return kWh consumed between two wrap-around survey counters."""
    if previous is None or current is None:
        return 0.0
    delta = current - previous
    if delta < 0:
        delta += SURVEY_COUNTER_WRAP
    return round(delta * SURVEY_KWH_PER_COUNT, 5)


def parse_survey_blocks(
    blocks: Sequence[bytes], tz: tzinfo = UTC
) -> list[SurveySlot]:
    """Decode raw 10-byte 0x8A survey blocks into chronological 30-minute slots.

    The meter transmits days newest-first, with 12 counter blocks (4 slots each)
    followed by 1 date-header block per day. Only complete 13-block days are
    decoded.
    """
    day_count = len(blocks) // SURVEY_BLOCKS_PER_DAY
    if day_count <= 0:
        return []

    slots: list[SurveySlot] = []
    prev_counter: int | None = None

    for day_idx in range(day_count - 1, -1, -1):
        base = day_idx * SURVEY_BLOCKS_PER_DAY
        day_start = decode_survey_date(
            blocks[base + SURVEY_BLOCKS_PER_DAY - 1], tz=tz
        )
        if day_start is None:
            prev_counter = None
            continue

        for block_offset in range(SURVEY_BLOCKS_PER_DAY - 1):
            block = blocks[base + block_offset]
            if len(block) < SURVEY_BLOCK_SIZE or block[0] != RSP_SURVEY_BLOCK_HEADER:
                prev_counter = None
                continue
            for slot_in_block in range(4):
                pos = 1 + slot_in_block * 2
                raw = int.from_bytes(block[pos : pos + 2], "little") & SURVEY_COUNTER_MASK
                counter = (
                    None
                    if raw == SURVEY_EMPTY_SENTINEL
                    else (raw % SURVEY_COUNTER_WRAP)
                )
                has_baseline = prev_counter is not None and counter is not None
                kwh = counter_delta_kwh(prev_counter, counter)
                slot_start = day_start + timedelta(
                    minutes=30 * (block_offset * 4 + slot_in_block)
                )
                slots.append(
                    SurveySlot(
                        start=slot_start,
                        kwh=kwh,
                        counter=counter,
                        has_baseline=has_baseline,
                    )
                )
                if counter is not None:
                    prev_counter = counter

    return slots


def aggregate_hourly(
    slots: Sequence[SurveySlot], now_utc: datetime
) -> list[HourlyEnergy]:
    """Group complete 30-minute survey slots into completed UTC hourly buckets."""
    current_hour_utc = now_utc.astimezone(UTC).replace(
        minute=0, second=0, microsecond=0
    )
    by_hour: dict[datetime, list[SurveySlot]] = defaultdict(list)

    for slot in slots:
        if slot.counter is None or not slot.has_baseline:
            continue
        hour_utc = slot.start.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0
        )
        if hour_utc >= current_hour_utc:
            continue
        by_hour[hour_utc].append(slot)

    hourly: list[HourlyEnergy] = []
    for hour_utc in sorted(by_hour):
        bucket = by_hour[hour_utc]
        if len(bucket) < 2:
            continue
        hourly.append(
            HourlyEnergy(
                start=hour_utc,
                kwh=round(sum(item.kwh for item in bucket), 5),
            )
        )
    return hourly
