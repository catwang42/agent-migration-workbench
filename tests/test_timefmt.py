"""Singapore-time rendering of the UTC timestamps the site prints.

The thing worth testing here is not "does it add eight hours" — it is the two
ways a cosmetic timestamp helper can quietly corrupt provenance: dropping the
date when the conversion crosses midnight, and silently reinterpreting a naive
timestamp as the build machine's local time. Both produce a plausible-looking
string that is wrong by hours, on a page whose whole claim is that its numbers
can be reconciled against ``artifacts/``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from amw.reporting.timefmt import SGT, sgt_line, sgt_window, to_sgt_text


def test_offset_is_utc_plus_eight_with_no_dst():
    """Singapore has no daylight saving; the offset is the same in Jan and Jul."""
    winter = datetime(2026, 1, 15, tzinfo=timezone.utc).astimezone(SGT)
    summer = datetime(2026, 7, 15, tzinfo=timezone.utc).astimezone(SGT)
    assert winter.utcoffset() == summer.utcoffset() == timedelta(hours=8)


@pytest.mark.parametrize(
    "iso,expected",
    [
        # The recording window the site quotes, both ends.
        ("2026-08-09T16:07:15+00:00", "10 Aug 2026, 12:07 AM (SGT)"),
        ("2026-08-11T06:20:37+00:00", "11 Aug 2026, 2:20 PM (SGT)"),
        # The cross-check run.
        ("2026-08-11T03:52:19+00:00", "11 Aug 2026, 11:52 AM (SGT)"),
        # Zulu spelling is the same instant as +00:00.
        ("2026-08-11T03:52:19Z", "11 Aug 2026, 11:52 AM (SGT)"),
        # Noon and midnight are the two the 12-hour clock gets wrong.
        ("2026-08-11T04:00:00+00:00", "11 Aug 2026, 12:00 PM (SGT)"),
        ("2026-08-11T16:00:00+00:00", "12 Aug 2026, 12:00 AM (SGT)"),
    ],
)
def test_known_timestamps(iso, expected):
    assert to_sgt_text(iso) == expected


def test_conversion_that_crosses_midnight_moves_the_date():
    """16:07 UTC on the 9th is 00:07 on the *10th* in Singapore.

    Printing "9 Aug, 12:07 AM" would be off by a whole day while looking
    entirely reasonable — the failure mode this format exists to avoid.
    """
    assert to_sgt_text("2026-08-09T16:07:15+00:00").startswith("10 Aug 2026")


def test_naive_timestamp_is_read_as_utc_not_as_host_local_time():
    """No tzinfo means UTC, because that is what this project writes.

    Falling back to the host's timezone would make the rendered time depend on
    which machine ran the build.
    """
    assert to_sgt_text("2026-08-09T16:07:15") == to_sgt_text("2026-08-09T16:07:15+00:00")


def test_an_offset_that_is_not_utc_is_honoured():
    """08:00 at +08:00 is already Singapore wall-clock time: no shift."""
    assert to_sgt_text("2026-08-11T08:00:00+08:00") == "11 Aug 2026, 8:00 AM (SGT)"


def test_sgt_line_keeps_the_iso_original():
    """Ground rule 2: provenance is printed, not paraphrased."""
    out = sgt_line("2026-08-11T03:52:19+00:00", prefix="Run ")
    assert "Run 11 Aug 2026, 11:52 AM (SGT)" in out
    assert "`2026-08-11T03:52:19+00:00`" in out
    assert ".amw-provenance" in out


def test_sgt_window_converts_both_ends():
    """A window with one converted end and one raw end is worse than neither."""
    out = sgt_window("2026-08-09T16:07:15+00:00", "2026-08-11T06:20:37+00:00")
    assert "10 Aug 2026, 12:07 AM (SGT)" in out
    assert "11 Aug 2026, 2:20 PM (SGT)" in out
    assert "`2026-08-09T16:07:15+00:00`" in out
    assert "`2026-08-11T06:20:37+00:00`" in out


def test_no_leading_zero_on_day_or_hour():
    """The format is "9 Aug", not "09 Aug"; "2:20 PM", not "02:20 PM".

    Built by hand rather than with strftime's %-d/%-I, which are glibc-only.
    """
    text = to_sgt_text("2026-08-08T18:05:00+00:00")
    assert text == "9 Aug 2026, 2:05 AM (SGT)"
