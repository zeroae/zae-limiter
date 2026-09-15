"""Tests for next_boundary (#222 §3.2)."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.schedule import (
    ScheduleEntry,
    effective_params,
    matches,
    next_boundary,
    parse_cron,
)

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

_DAY_MS = 86_400_000


def _ms(s: str, tz: ZoneInfo = NY) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=tz).timestamp() * 1000)


def _iso(ms: int, tz: ZoneInfo = NY) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).isoformat()


BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)


class TestNextBoundary:
    def test_none_without_a_schedule(self):
        assert next_boundary((), now_ms=_ms("2026-09-15 06:00")) is None

    def test_finds_a_window_opening(self):
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T09:00"
        )

    def test_finds_a_window_closing(self):
        """The half of the problem no cron library solves."""
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-15 14:00"))).startswith(
            "2026-09-15T18:00"
        )

    def test_standing_exactly_on_an_opening_returns_the_closing(self):
        """The scan starts strictly after `now`.

        A boundary returned at `now` would make `vu` expire the instant it is
        written, re-materialising the bucket on every single request.
        """
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-15 09:00"))).startswith(
            "2026-09-15T18:00"
        )

    def test_skips_the_weekend(self):
        assert _iso(next_boundary(BUSINESS, now_ms=_ms("2026-09-12 18:30"))).startswith(
            "2026-09-14T09:00"
        )

    def test_window_edge_follows_local_time_across_dst(self):
        before = next_boundary(BUSINESS, now_ms=_ms("2027-03-12 08:30"))
        after = next_boundary(BUSINESS, now_ms=_ms("2027-03-16 08:30"))
        assert datetime.fromtimestamp(before / 1000, UTC).hour == 14  # EST
        assert datetime.fromtimestamp(after / 1000, UTC).hour == 13  # EDT

    def test_no_transition_returns_now_plus_cap(self):
        """A schedule that always matches has no boundary; cap rather than loop forever.

        `* * * * *` constrains neither minutes nor hours, so it scans at **day**
        granularity and caps at 366 days — not the 31-day hour cap.
        """
        always = (ScheduleEntry(cron="* * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:00")
        assert next_boundary(always, now_ms=now) == now + 366 * _DAY_MS

    def test_hour_granularity_caps_the_scan_at_thirty_one_days(self):
        """Nothing else pins the hour cap constant.

        The first entry always matches, so the active index never changes and the
        scan runs to its horizon; the second entry constrains hours, which is what
        selects the granularity.
        """
        sched = (
            ScheduleEntry(cron="* * * * *", scale=0.5),
            ScheduleEntry(cron="* 9-17 * * *", scale=0.8),
        )
        now = _ms("2026-09-15 06:00")
        assert next_boundary(sched, now_ms=now) == now + 31 * _DAY_MS

    def test_minute_granularity_caps_the_scan_at_seven_days(self):
        """Nothing else pins the minute cap constant. Same shape as the hour case."""
        sched = (
            ScheduleEntry(cron="* * * * *", scale=0.5),
            ScheduleEntry(cron="*/15 * * * *", scale=0.8),
        )
        now = _ms("2026-09-15 06:00")
        assert next_boundary(sched, now_ms=now) == now + 7 * _DAY_MS

    def test_minute_granularity_steps_by_the_minute(self):
        """`*/15` next matches at :15, not at the top of the next hour.

        An hour-granularity scan would align its probes to the hour and report
        07:00 — a boundary 45 minutes late.
        """
        sched = (ScheduleEntry(cron="*/15 * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:07")
        assert _iso(next_boundary(sched, now_ms=now)).startswith("2026-09-15T06:15")

    def test_hour_granularity_boundary_lands_on_the_local_hour(self):
        """A half-hour offset puts local hour edges off the UTC hour grid.

        Asia/Kolkata is UTC+05:30, so 09:00 local is 03:30Z. A coarse scan whose
        probes sit on the UTC hour first sees the change at 04:00Z — 09:30 local,
        half an hour of the *old* limit inside the new window.
        """
        sched = (ScheduleEntry(cron="* 9-17 * * *", tz="Asia/Kolkata", scale=0.5),)
        now = _ms("2026-09-15 06:00", IST)
        assert _iso(next_boundary(sched, now_ms=now), IST).startswith("2026-09-15T09:00")

    def test_day_granularity_boundary_lands_on_local_midnight(self):
        """The same defect, one granularity coarser and twenty hours wide.

        `* * * * SAT,SUN` constrains neither minutes nor hours, so it scans by the
        day. Aligned to UTC midnight, the first probe that sees Saturday is
        Sunday 00:00Z — 20:00 Saturday in New York.
        """
        sched = (ScheduleEntry(cron="* * * * SAT,SUN", tz="America/New_York", capacity=2000),)
        friday = _ms("2026-09-11 10:00")
        assert _iso(next_boundary(sched, now_ms=friday)).startswith("2026-09-12T00:00")

    def test_a_short_local_day_is_not_stepped_over(self):
        """#540: a UTC-aligned day grid can skip a whole 23-hour matching day.

        Atlantic/Azores is UTC-1 and springs forward *at local midnight* on the
        last Sunday of March, so that Sunday runs 01:00Z -> 00:00Z, 23 hours
        sitting strictly between two UTC midnights. A 24-hour grid aligned to the
        UTC epoch has no probe inside it, so the scan walked clean over the day
        and reported the *following* Sunday — 167 hours late, a whole window of
        the wrong limits.
        """
        sched = (ScheduleEntry(cron="* * * * SUN", tz="Atlantic/Azores", scale=0.5),)
        now = _ms("2027-03-24 06:23", ZoneInfo("UTC"))
        assert _iso(next_boundary(sched, now_ms=now), ZoneInfo("UTC")).startswith(
            "2027-03-28T01:00"
        )

    def test_a_half_length_local_hour_is_not_stepped_over(self):
        """The same defect one granularity finer, via a 30-minute DST shift.

        Lord Howe Island moves 02:00 -> 02:30 on the first Sunday of October, so
        local hour 2 lasts thirty minutes that day. Its zone offset puts local
        hour starts on the UTC half hour, so an hourly UTC grid brackets that
        half hour without ever probing inside it.
        """
        sched = (ScheduleEntry(cron="* 2 * * *", tz="Australia/Lord_Howe", capacity=2000),)
        now = _ms("2026-10-03 06:23", ZoneInfo("UTC"))
        assert _iso(next_boundary(sched, now_ms=now), ZoneInfo("UTC")).startswith(
            "2026-10-03T15:30"
        )

    def test_is_the_minimum_across_entries(self):
        sched = (
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
            ScheduleEntry(cron="* 7-8 * * *", tz="America/New_York", scale=0.8),
        )
        assert _iso(next_boundary(sched, now_ms=_ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T07:00"
        )

    def test_boundary_is_where_effective_params_actually_change(self):
        """The instant returned is a real change point, not merely a probe that matched."""
        now = _ms("2026-09-15 06:00")
        b = next_boundary(BUSINESS, now_ms=now)
        base = (1_000_000, 200_000, 60_000)
        assert effective_params(*base, BUSINESS, b - 60_000) == base
        assert effective_params(*base, BUSINESS, b) == (500_000, 100_000, 60_000)

    def test_reset_sched_is_accepted_and_ignored(self):
        """Surface-plan Task 2 makes it a boundary source; until then it must not bite.

        Pins the two-tuple signature so the unused parameter is not removed as
        dead code, which would turn that later change into a signature change
        rippling through `lease.py` and `processor.py`.
        """
        other = (ScheduleEntry(cron="0 * * * *", scale=0.5),)
        now = _ms("2026-09-15 06:00")
        assert next_boundary(BUSINESS, other, now_ms=now) == next_boundary(BUSINESS, now_ms=now)

    def test_now_ms_is_keyword_only(self):
        """The second *positional* slot belongs to `reset_sched` (#500).

        Without the marker this call silently binds a timestamp to a schedule
        tuple instead of raising.
        """
        with pytest.raises(TypeError, match="now_ms"):
            next_boundary(BUSINESS, _ms("2026-09-15 06:00"))


def _brute_force_boundary(sched: tuple[ScheduleEntry, ...], now_ms: int, horizon_ms: int):
    """Ground truth: walk every minute and report the first change in the active entry.

    Deliberately shares nothing with `next_boundary` but `parse_cron`/`matches` —
    no granularity choice, no step grid, no refinement.
    """
    parsed = [parse_cron(e.cron, e.tz) for e in sched]

    def active(t: int) -> int | None:
        return next((i for i, p in enumerate(parsed) if matches(p, t)), None)

    current = active(now_ms)
    probe = (now_ms // 60_000 + 1) * 60_000
    while probe <= now_ms + horizon_ms:
        if active(probe) != current:
            return probe
        probe += 60_000
    return None


class TestAgainstABruteForceMinuteScan:
    """The hand-written timezone cases above sample the failures; this sweeps them.

    A UTC-aligned coarse grid reports a late boundary whenever the zone's offset is
    not a whole number of steps, and skips a window outright when the clock makes a
    local unit shorter than the step (#540). These zones cover +05:30, +05:45,
    -03:30, +10:30 and +12:45, the -01:00 zone whose DST shift lands at local
    midnight and the +10:30 zone whose shift is half an hour, plus whole-hour zones
    as controls.
    """

    @pytest.mark.parametrize(
        "cron,tz",
        [
            ("* 9-17 * * MON-FRI", "America/New_York"),
            ("* 9-17 * * *", "Asia/Kolkata"),
            ("* 9-17 * * *", "Asia/Kathmandu"),
            ("* 0-6 * * *", "Australia/Lord_Howe"),
            ("* 2 * * *", "Australia/Lord_Howe"),
            ("0 0 * * *", "America/St_Johns"),
            ("*/15 * * * *", "UTC"),
            ("* * * * SAT,SUN", "America/New_York"),
            ("* * * * SAT,SUN", "Pacific/Chatham"),
            ("* * * * SUN", "Atlantic/Azores"),
            ("* * 1-7 * *", "Europe/Berlin"),
        ],
    )
    @pytest.mark.parametrize(
        "day", ["2026-09-15", "2026-09-19", "2026-10-01", "2027-03-13", "2027-03-22"]
    )
    def test_matches_ground_truth(self, cron, tz, day):
        sched = (ScheduleEntry(cron=cron, tz=tz, scale=0.5),)
        now = _ms(f"{day} 06:23", ZoneInfo("UTC"))
        horizon = 20 * _DAY_MS
        expected = _brute_force_boundary(sched, now, horizon)
        assert expected is not None, "test case has no boundary inside the brute horizon"
        assert next_boundary(sched, now_ms=now) == expected


class TestParseCacheIsHot:
    def test_repeated_boundary_calls_do_not_reparse(self):
        parse_cron.cache_clear()
        next_boundary(BUSINESS, now_ms=_ms("2026-09-15 06:00"))
        first = parse_cron.cache_info().misses
        next_boundary(BUSINESS, now_ms=_ms("2026-09-15 06:00"))
        assert parse_cron.cache_info().misses == first
