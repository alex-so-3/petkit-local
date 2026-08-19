"""Local-time helpers: what "today" means, and what offset to tell a device.

Everything in this codebase stores UTC epochs, and that is correct -- the
devices report correct UTC and nothing here changes that. The bug this module
exists to fix is one layer up: a *day* was being cut at UTC midnight while the
box sits in someone's house. On a UTC+2 install that files every event between
local midnight and 02:00 under the previous day, which measured out at 50 of
268 events in the reference corpus, including six complete after-midnight
toilet visits.

Where "local" comes from
------------------------
The Supervisor sets the add-on container's timezone from Home Assistant's own
configuration (`TZ=Europe/Warsaw` on the reference install), so the process
already knows. `datetime.astimezone()` on a NAIVE datetime interprets it as
local wall-clock time and attaches the offset that was in force *on that date*
-- which is what makes the DST handling below work without a tz database
dependency or a configured zone name.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: The wire format the panel's date picker and `?date=` parameter both use.
DATE_FORMAT = "%Y-%m-%d"


def cloud_timestamp(when: float | None = None,
                    offset_hours: float | None = None) -> str:
    """An instant in the format PetKit's cloud puts on the wire, at an offset.

    `2026-08-19T15:45:51.170+0200` — milliseconds, and a `+HHMM` offset with no
    colon. NOT `datetime.isoformat()`, which produces `+02:00` and would be a
    different string to whatever parses it on the device.

    `offset_hours` is the UTC offset to render the instant AT — the device's own
    (`Device.timezone_offset`). It is NOT cosmetic. `dev_state_report`'s reply is
    where the firmware reads its timezone: it parses this `time`, extracts the
    offset (`get_ctime_timezone`) and sets its RTC from it, and its scheduled
    feeds fire against that RTC. Confirmed on a live D4H2 — the real cloud
    answered `+0200` and a meal fired on the wall-clock minute, while our own
    `+0000` set the box to UTC and fired every meal two hours late. The stored
    epoch is still UTC (this codebase changes nothing there); only the wire
    string is localized. Rounded to whole minutes so the offset is always the
    `+HHMM` the device reads, never a sub-minute `%z` variant.

    Omitted (or None) keeps the historical UTC/`+0000`, for a caller with no
    device to speak for — an unidentified device is the only one left.
    """
    tz = timezone.utc if offset_hours is None \
        else timezone(timedelta(minutes=round(offset_hours * 60)))
    now = datetime.fromtimestamp(when, tz) if when is not None \
        else datetime.now(tz)
    return (now.strftime("%Y-%m-%dT%H:%M:%S.")
            + f"{now.microsecond // 1000:03d}"
            + now.strftime("%z"))


def local_offset_hours(when: float | None = None) -> float:
    """Hours east of UTC in force locally, e.g. 2.0 for Warsaw in summer.

    This is the unit PetKit's own API uses for its `timezone` field, which is
    what we report to a device. Returns 0.0 if the platform will not tell us.
    """
    moment = datetime.fromtimestamp(when) if when is not None else datetime.now()
    offset = moment.astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else 0.0


def offset_hours_for_locale(locale: str | None,
                            when: float | None = None) -> float | None:
    """Current UTC offset in hours for an IANA zone name, or None.

    A device reports its own zone as a NAME (`locale: "Europe/Warsaw"`), and a
    name is a better answer than a number for the `timezone` field two ways: it
    is unambiguous, and — unlike an offset frozen at provisioning — it moves
    with DST, so it is right in both halves of the year. A device that only ever
    got a locale and never a numeric offset reports the offset as 0, which is
    how a box plainly in `Europe/Warsaw` ends up telling us it is in UTC; this
    recovers the real offset from the name it did send.

    Returns None for an absent, non-string, or unknown zone (and for a bare name
    with no `/`, which no real zone this matters for lacks — `UTC` included,
    whose 0 the numeric path already yields) so the caller can fall back.
    """
    if not isinstance(locale, str) or "/" not in locale:
        return None
    try:
        tz = ZoneInfo(locale)
    except (ZoneInfoNotFoundError, ValueError):
        # Unknown zone, or a name the OS tz database does not carry.
        return None
    moment = datetime.fromtimestamp(when, tz) if when is not None \
        else datetime.now(tz)
    offset = moment.utcoffset()
    return offset.total_seconds() / 3600 if offset is not None else None


def parse_date(date_str: str | None) -> date | None:
    """Parse a `YYYY-MM-DD` string, or None if it is absent or malformed.

    Never raises: the value reaches us from a query string.
    """
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, DATE_FORMAT).date()
    except (TypeError, ValueError):
        return None


def local_day_bounds(date_str: str | None = None,
                     now: float | None = None) -> tuple[float, float, str]:
    """The UTC epoch span of one LOCAL calendar day.

    Args:
        date_str: The day to bound, `YYYY-MM-DD`. Absent or unparseable falls
            back to today, matching the panel's existing contract.
        now: Epoch to treat as "now" when falling back. For tests.

    Returns:
        `(start_ts, end_ts, day_label)` -- start inclusive, end exclusive, and
        the `YYYY-MM-DD` actually used so a caller can echo it back.

    The end is midnight of the NEXT day re-localized, never `start + 86400`.
    Those differ twice a year: Europe/Warsaw has a 23-hour day when DST starts
    and a 25-hour one when it ends, so the fixed-86400 form would silently drop
    an hour of events in spring and double-count one in autumn.
    """
    day = parse_date(date_str)
    if day is None:
        moment = datetime.fromtimestamp(now) if now is not None else datetime.now()
        day = moment.date()

    midnight = datetime(day.year, day.month, day.day)
    start = midnight.astimezone()
    end = (midnight + timedelta(days=1)).astimezone()
    return start.timestamp(), end.timestamp(), day.strftime(DATE_FORMAT)


def local_day_start(now: float | None = None) -> float:
    """Epoch of the most recent local midnight -- the "today" of daily counts."""
    start, _, _ = local_day_bounds(now=now if now is not None else time.time())
    return start
