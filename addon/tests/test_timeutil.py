"""Tests for utils/timeutil.py, with DST as the point of the exercise.

A day is not 86400 seconds twice a year, and the bug these tests guard against
is subtle in exactly the wrong way: `start + 86400` looks obviously correct and
silently drops an hour of events every spring and double-counts one every
autumn. Europe/Warsaw is used because it is the reference install's zone and it
observes DST; the assertions are about the arithmetic, not about Poland.
"""
import os
import time

import pytest

from petkit_local.utils.timeutil import (
    cloud_timestamp, local_day_bounds, local_day_start, local_offset_hours,
    offset_hours_for_locale, parse_date,
)

DAY = 86400


@pytest.fixture
def tz():
    """Pin the process timezone, restoring whatever the developer had."""
    original = os.environ.get("TZ")

    def _set(name):
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def test_an_ordinary_day_is_24_hours(tz):
    tz("Europe/Warsaw")
    start, end, label = local_day_bounds("2026-07-26")
    assert end - start == DAY
    assert label == "2026-07-26"


def test_the_spring_forward_day_is_23_hours(tz):
    """2026-03-29: Europe/Warsaw jumps 02:00 -> 03:00.

    A `start + 86400` end would run an hour into the next local day, so an
    event just after the following midnight would appear on both days.
    """
    tz("Europe/Warsaw")
    start, end, _ = local_day_bounds("2026-03-29")
    assert end - start == 23 * 3600


def test_the_autumn_back_day_is_25_hours(tz):
    """2026-10-25: the clocks go back, so this local day really is 25 hours.

    With a fixed 86400 the last hour of the day would fall outside the window
    and its events would show up nowhere at all.
    """
    tz("Europe/Warsaw")
    start, end, _ = local_day_bounds("2026-10-25")
    assert end - start == 25 * 3600


def test_consecutive_days_tile_without_gap_or_overlap(tz):
    """Across a DST boundary too -- every event must land in exactly one day."""
    tz("Europe/Warsaw")
    for first, second in (("2026-03-28", "2026-03-29"),
                          ("2026-03-29", "2026-03-30"),
                          ("2026-10-24", "2026-10-25"),
                          ("2026-10-25", "2026-10-26")):
        _, end_a, _ = local_day_bounds(first)
        start_b, _, _ = local_day_bounds(second)
        assert end_a == start_b, f"{first} -> {second} does not tile"


def test_the_window_is_local_midnight_not_utc(tz):
    """The whole point: at UTC+2, a local day starts two hours before UTC's."""
    tz("Europe/Warsaw")
    start, _, _ = local_day_bounds("2026-07-26")
    utc_midnight = 1785024000.0  # 2026-07-26T00:00:00Z
    assert start == utc_midnight - 2 * 3600


def test_utc_install_is_unaffected(tz):
    tz("UTC")
    start, end, _ = local_day_bounds("2026-07-26")
    assert start == 1785024000.0 and end - start == DAY


def test_a_bad_date_falls_back_to_today_and_says_so(tz):
    """The value arrives from a query string, so it must never raise."""
    tz("Europe/Warsaw")
    now = 1785024000.0 + 43200
    for junk in ("", None, "garbage", "2026-13-45", "26/07/2026"):
        start, end, label = local_day_bounds(junk, now=now)
        assert end - start == DAY
        assert label == "2026-07-26"  # the day `now` falls in, locally


def test_parse_date_rejects_junk_without_raising():
    assert parse_date("2026-07-26") is not None
    for junk in ("", None, "nope", "2026-99-99", 7):
        assert parse_date(junk) is None


def test_local_day_start_matches_the_bounds(tz):
    tz("America/Los_Angeles")
    now = time.time()
    assert local_day_start(now) == local_day_bounds(now=now)[0]


@pytest.mark.parametrize("zone,expected", [
    ("UTC", 0.0),
    ("Europe/Warsaw", 2.0),      # CEST in July
    ("America/Los_Angeles", -7.0),
    ("Asia/Kolkata", 5.5),       # a half-hour zone, since we report a float
])
def test_offset_hours_is_what_we_tell_a_device(tz, zone, expected):
    tz(zone)
    assert local_offset_hours(1785024000.0) == expected


# A summer instant (2026-07-26) so DST zones are in their summer offset.
_SUMMER = 1785024000.0


@pytest.mark.parametrize("locale,expected", [
    ("Europe/Warsaw", 2.0),          # CEST
    ("America/New_York", -4.0),      # EDT, and NOT the -5.0 a frozen number holds
    ("Asia/Kolkata", 5.5),           # half-hour zone
])
def test_locale_offset_is_dst_correct(locale, expected):
    """The whole point of preferring the zone NAME over a stored number: it is
    right in summer too, which a provisioning-time offset is not."""
    assert offset_hours_for_locale(locale, _SUMMER) == expected


@pytest.mark.parametrize("bad", [None, "", "auto", "UTC", "Warsaw", 2.0, "Not/AZone"])
def test_unusable_locale_returns_none_for_fallback(bad):
    """Anything that is not a resolvable IANA zone name yields None so the
    caller drops to the numeric sources. `UTC` has no slash and is handled by
    the numeric path's 0.0 anyway."""
    assert offset_hours_for_locale(bad, _SUMMER) is None


def test_cloud_timestamp_defaults_to_utc():
    """No offset given -> the historical UTC/`+0000`, for an unidentified
    device we cannot speak an offset for. `_SUMMER` is UTC midnight."""
    assert cloud_timestamp(_SUMMER) == "2026-07-26T00:00:00.000+0000"


def test_cloud_timestamp_localizes_to_the_offset():
    """UTC midnight is 02:00 at +2, and the offset is stamped `+0200`.

    This is the `dev_state_report` `time` field the firmware reads to set its
    RTC: served `+0000` on a UTC+2 box it fires every scheduled meal two hours
    late, so the wall-clock must really move, not just the suffix be relabelled.
    """
    assert cloud_timestamp(_SUMMER, offset_hours=2.0) == "2026-07-26T02:00:00.000+0200"


@pytest.mark.parametrize("offset,suffix", [
    (0.0, "+0000"),
    (-7.0, "-0700"),      # west of UTC (PDT)
    (5.75, "+0545"),      # quarter-hour zone -> whole-minute +HHMM, never sub-minute
    (5.5, "+0530"),       # half-hour zone
    (14.0, "+1400"),      # the far end of api_timezone's valid range
])
def test_cloud_timestamp_offset_is_hhmm(offset, suffix):
    """`+HHMM`, no colon and no seconds -- the shape the firmware parses."""
    assert cloud_timestamp(_SUMMER, offset_hours=offset).endswith(suffix)
