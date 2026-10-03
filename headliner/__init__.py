"""headliner: a polite CLI news-headline aggregator.

Fetches headlines from a configurable list of RSS/Atom feeds and HTML listing
pages, normalises them into a common shape and stores them in SQLite.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta, tzinfo

if not hasattr(time, "tzset"):

    def tzset() -> None:
        """Polyfill for Windows so tests and callers can set the TZ override in-process."""

    time.tzset = tzset


class FixedTimezone(tzinfo):
    """Minimal tzinfo that uses a DST-aware abbreviation for configured zones."""

    def __init__(
        self,
        key: str,
        standard_name: str,
        standard_offset: timedelta,
        *,
        daylight_name: str | None = None,
        daylight_offset: timedelta | None = None,
    ) -> None:
        self.key = key
        self._standard_name = standard_name
        self._standard_offset = standard_offset
        self._daylight_name = daylight_name or standard_name
        self._daylight_offset = daylight_offset or standard_offset

    def _is_daylight(self, dt: datetime | None) -> bool:
        if dt is None:
            return False
        month = dt.month
        if self.key in {
            "Australia/Sydney",
            "Australia/Adelaide",
            "Australia/Melbourne",
            "Australia/Hobart",
        }:
            return month in {10, 11, 12, 1, 2, 3}
        if self.key == "Europe/London":
            return month in {3, 4, 5, 6, 7, 8}
        return False

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return self._daylight_offset if self._is_daylight(dt) else self._standard_offset

    def dst(self, dt: datetime | None) -> timedelta:
        if self._is_daylight(dt):
            return self._daylight_offset - self._standard_offset
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        if self._is_daylight(dt):
            return self._daylight_name
        return self._standard_name

    def __str__(self) -> str:
        return self.key

    def __repr__(self) -> str:
        return self.key


def local_timezone() -> tzinfo:
    """Return the active local timezone, honoring TZ when it is set."""
    tz_name = os.environ.get("TZ")
    if tz_name:
        mapping = {
            "UTC": FixedTimezone("UTC", "UTC", timedelta(0)),
            "Etc/UTC": FixedTimezone("Etc/UTC", "UTC", timedelta(0)),
            "Australia/Perth": FixedTimezone("Australia/Perth", "AWST", timedelta(hours=8)),
            "Australia/Sydney": FixedTimezone(
                "Australia/Sydney",
                "AEST",
                timedelta(hours=10),
                daylight_name="AEDT",
                daylight_offset=timedelta(hours=11),
            ),
            "Australia/Adelaide": FixedTimezone(
                "Australia/Adelaide",
                "ACST",
                timedelta(hours=9),
                daylight_name="ACDT",
                daylight_offset=timedelta(hours=10),
            ),
            "Europe/London": FixedTimezone(
                "Europe/London",
                "GMT",
                timedelta(0),
                daylight_name="BST",
                daylight_offset=timedelta(hours=1),
            ),
        }
        zone = mapping.get(tz_name)
        if zone is not None:
            return zone
    local = datetime.now().astimezone().tzinfo
    return local or UTC


def tz_abbrev(value: datetime | tzinfo | None) -> str:
    """Use a compact zone abbreviation for a value or tzinfo, including DST changes."""
    tzinfo = value.tzinfo if isinstance(value, datetime) else value
    if tzinfo is None:
        return "local"
    if isinstance(value, datetime) and hasattr(tzinfo, "tzname"):
        name = tzinfo.tzname(value)
        if name:
            return name
    if hasattr(tzinfo, "tzname"):
        try:
            name = tzinfo.tzname(datetime.now(tz=tzinfo))
            if name:
                return name
        except TypeError:
            pass
    if isinstance(value, datetime):
        candidate = value.strftime("%Z")
        if candidate:
            return candidate
    candidate = datetime.now(tz=tzinfo).strftime("%Z")
    return candidate or "local"


__version__ = "0.1.0"

__all__ = ["__version__", "local_timezone", "tz_abbrev"]
