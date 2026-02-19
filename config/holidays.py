"""Configurable holiday dates used by pricing rules."""

from __future__ import annotations

from datetime import date

# Complete list of Swedish public holidays (roda dagar) for 2025-2028.
# Update this list when new years need to be added.
# Weekend pricing is always covered by Saturday/Sunday checks even if a date
# would be missing here.
HOLIDAY_DATES = {
    # 2025
    "2025-01-01",
    "2025-01-06",
    "2025-04-18",
    "2025-04-20",
    "2025-04-21",
    "2025-05-01",
    "2025-05-29",
    "2025-06-06",
    "2025-06-08",
    "2025-06-21",
    "2025-11-01",
    "2025-12-25",
    "2025-12-26",
    # 2026
    "2026-01-01",
    "2026-01-06",
    "2026-04-03",
    "2026-04-05",
    "2026-04-06",
    "2026-05-01",
    "2026-05-14",
    "2026-06-06",
    "2026-05-24",
    "2026-06-20",
    "2026-10-31",
    "2026-12-25",
    "2026-12-26",
    # 2027
    "2027-01-01",
    "2027-01-06",
    "2027-03-26",
    "2027-03-28",
    "2027-03-29",
    "2027-05-01",
    "2027-05-06",
    "2027-05-16",
    "2027-06-06",
    "2027-06-26",
    "2027-11-06",
    "2027-12-25",
    "2027-12-26",
    # 2028
    "2028-01-01",
    "2028-01-06",
    "2028-04-14",
    "2028-04-16",
    "2028-04-17",
    "2028-05-01",
    "2028-05-25",
    "2028-06-04",
    "2028-06-06",
    "2028-06-24",
    "2028-11-04",
    "2028-12-25",
    "2028-12-26",
}


def is_weekend_or_holiday(day: date) -> bool:
    """Return True for Saturday/Sunday or a configured holiday date."""
    return day.isoweekday() in {6, 7} or day.isoformat() in HOLIDAY_DATES
