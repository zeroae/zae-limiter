"""Tests for next_boundary (#222 §3.2)."""

from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.bucket import calculate_retry_after
from zae_limiter.schedule import (
    ScheduleEntry,
    effective_params,
    matches,
    next_boundary,
    next_reset_edge,
    parse_cron,
    prev_reset_edge,
    retry_after_with_schedule,
)

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")

_DAY_MS = 86_400_000


def _ms(s: str, tz: ZoneInfo = NY) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=tz).timestamp() * 1000)


def _iso(ms: int, tz: ZoneInfo = NY) -> str:
    return datetime.fromtimestamp(ms / 1000, tz).isoformat()


BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
IST_BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="Asia/Kolkata", scale=0.5),)


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

    def test_now_ms_is_keyword_only(self):
        """The second *positional* slot belongs to `reset_sched` (#500).

        Without the marker this call silently binds a timestamp to a schedule
        tuple instead of raising.
        """
        with pytest.raises(TypeError, match="now_ms"):
            next_boundary(BUSINESS, _ms("2026-09-15 06:00"))


DAILY = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)


class TestPrevResetEdge:
    def test_finds_the_most_recent_midnight(self):
        assert _iso(prev_reset_edge(DAILY, _ms("2026-09-15 09:00"))).startswith("2026-09-15T00:00")

    def test_an_idle_bucket_sees_the_missed_edge(self):
        """Idle 18:00 -> 09:00: the edge is in the past and must still be found."""
        rf = _ms("2026-09-14 18:00")
        assert prev_reset_edge(DAILY, _ms("2026-09-15 09:00")) > rf

    def test_two_missed_edges_report_only_the_latest(self):
        """Applying a reset is idempotent, so one edge is enough."""
        assert _iso(prev_reset_edge(DAILY, _ms("2026-09-16 09:00"))).startswith("2026-09-16T00:00")

    def test_no_edge_since_the_last_refill(self):
        rf = _ms("2026-09-15 01:00")
        assert prev_reset_edge(DAILY, _ms("2026-09-15 09:00")) <= rf

    def test_an_out_of_reach_expression_resets_nothing(self):
        """No edge within the cap, so nothing to apply.

        The plan and design §3.6 both name `0 0 30 2 *` (February 30th) here, but
        cronsim rejects it outright — `ScheduleEntry` never constructs, so the
        case is unreachable. `0 0 29 2 *` is the constructible equivalent: it
        matches only on a leap-year February 29th, which is outside the seven-day
        minute-granularity cap on all but one day in roughly 1,461.
        """
        out_of_reach = (ScheduleEntry.reset(cron="0 0 29 2 *"),)
        assert prev_reset_edge(out_of_reach, _ms("2026-09-15 09:00")) is None

    def test_empty_reset_schedule(self):
        assert prev_reset_edge((), _ms("2026-09-15 09:00")) is None

    def test_standing_exactly_on_the_edge_reports_that_edge(self):
        """The contract is "at or before", not "strictly before".

        The materialising pass compares the edge against `rf`, so an edge dropped
        for arriving exactly on it would be seen by the *next* pass instead —
        harmless for a daily reset, and a silently lost window for a fine one.
        """
        midnight = _ms("2026-09-15 00:00")
        assert prev_reset_edge(DAILY, midnight) == midnight

    def test_reports_the_opening_of_a_window_not_its_latest_matching_minute(self):
        """`* 0 * * *` matches all sixty minutes of hour zero; the edge is 00:00.

        An implementation that stops at the latest *matching* minute answers 00:45
        here, which would re-fire the reset on every request inside the window.
        """
        hourly = (ScheduleEntry.reset(cron="* 0 * * *", tz="America/New_York"),)
        assert _iso(prev_reset_edge(hourly, _ms("2026-09-15 00:45"))).startswith("2026-09-15T00:00")

    def test_reports_the_opening_from_outside_the_window_too(self):
        """The 00:45 case above answers from inside; this one walks back into it.

        From 05:00 the backwards scan steps over four non-matching local hours,
        finds hour zero, and must resolve it to that hour's *last* minute (00:59)
        before the second search turns it into the 00:00 edge. Returning the
        probe itself — 00:00 — happens to give the same final answer here and a
        wrong one for any window whose opening is not also a probe.
        """
        hourly = (ScheduleEntry.reset(cron="* 0 * * *", tz="America/New_York"),)
        assert _iso(prev_reset_edge(hourly, _ms("2026-09-15 05:00"))).startswith("2026-09-15T00:00")

    def test_a_match_sitting_on_the_horizon_is_not_an_edge(self):
        """An edge needs a non-matching minute before it, and the cap hides that one.

        `0 0 15 9 *` matches once a year. Seven days later — exactly the
        minute-granularity cap — the match is the oldest instant the scan may
        look at, so whether it *opened* there is unknowable and None is the only
        honest answer. Claiming an edge at the horizon would re-fire the reset on
        every pass for a bucket that has been idle exactly that long.
        """
        annual = (ScheduleEntry.reset(cron="0 0 15 9 *", tz="America/New_York"),)
        assert prev_reset_edge(annual, _ms("2026-09-22 00:00")) is None

    def test_an_always_matching_expression_has_no_edge(self):
        """No transition into matching anywhere in the horizon, so nothing rises."""
        always = (ScheduleEntry.reset(cron="* * * * *"),)
        assert prev_reset_edge(always, _ms("2026-09-15 09:00")) is None

    @pytest.mark.parametrize("order", [(0, 1), (1, 0)])
    def test_is_the_maximum_across_entries_not_the_first_match(self, order):
        """Reset entries are independent instants, not first-match-wins overrides.

        Midnight and noon both have an edge before 15:00; the pass needs the noon
        one. Both orderings are asserted, so returning the first entry with an
        edge cannot pass by luck.
        """
        entries = (
            ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),
            ScheduleEntry.reset(cron="0 12 * * *", tz="America/New_York"),
        )
        sched = tuple(entries[i] for i in order)
        assert _iso(prev_reset_edge(sched, _ms("2026-09-15 15:00"))).startswith("2026-09-15T12:00")

    def test_entries_may_disagree_about_the_timezone(self):
        """Tokyo midnight is 11:00 the previous day in New York, so it wins at 14:00 NY."""
        sched = (
            ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),
            ScheduleEntry.reset(cron="0 0 * * *", tz="Asia/Tokyo"),
        )
        assert _iso(prev_reset_edge(sched, _ms("2026-09-15 14:00"))).startswith("2026-09-15T11:00")

    def test_each_entry_keeps_its_own_horizon(self):
        """A coarse entry's edge must not be hidden by a fine neighbour's shorter cap.

        `* 0 1 * *` scans at hour granularity and looks back 31 days; `0 0 29 2 *`
        scans at minute granularity and looks back 7. Resolving one granularity
        for the whole tuple picks the finer, and the monthly edge nineteen days
        back disappears.
        """
        sched = (
            ScheduleEntry.reset(cron="0 0 29 2 *"),
            ScheduleEntry.reset(cron="* 0 1 * *", tz="America/New_York"),
        )
        assert _iso(prev_reset_edge(sched, _ms("2026-09-20 09:00"))).startswith("2026-09-01T00:00")

    def test_a_skipped_local_hour_produces_no_edge_that_day(self):
        """`30 2 * * *` does not fire on a spring-forward day; it never is 02:30.

        A reset edge is a fact about the wall clock, so the most recent edge on
        2027-03-14 is the *previous* day's. croniter's `get_prev` disagrees and
        reschedules the skipped fire to 03:00, which is a scheduler's job and not
        a matcher's — the reason that expression is excluded from the reset half
        of the croniter oracle in test_schedule_oracle.py.
        """
        sched = (ScheduleEntry.reset(cron="30 2 * * *", tz="America/New_York"),)
        assert _iso(prev_reset_edge(sched, _ms("2027-03-14 04:14"))).startswith("2027-03-13T02:30")

    def test_the_edge_follows_local_midnight_across_dst(self):
        """Spring forward moves midnight New York from 05:00Z to 04:00Z."""
        before = prev_reset_edge(DAILY, _ms("2027-03-12 09:00"))
        after = prev_reset_edge(DAILY, _ms("2027-03-16 09:00"))
        assert datetime.fromtimestamp(before / 1000, UTC).hour == 5  # EST
        assert datetime.fromtimestamp(after / 1000, UTC).hour == 4  # EDT


class TestNextBoundarySpansBothTuples:
    def test_a_reset_edge_is_a_boundary(self):
        # From 22:00 the next param change is 09:00, but the reset fires at 00:00.
        assert _iso(next_boundary(BUSINESS, DAILY, now_ms=_ms("2026-09-15 22:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_a_nearer_param_change_still_wins(self):
        """The other direction: the minimum is genuinely a minimum, not a preference."""
        assert _iso(next_boundary(BUSINESS, DAILY, now_ms=_ms("2026-09-15 06:00"))).startswith(
            "2026-09-15T09:00"
        )

    def test_reset_only_schedule_still_produces_boundaries(self):
        assert _iso(next_boundary((), DAILY, now_ms=_ms("2026-09-15 09:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_neither_tuple_means_no_boundary(self):
        assert next_boundary((), (), now_ms=_ms("2026-09-15 09:00")) is None

    def test_an_empty_reset_tuple_changes_nothing(self):
        now = _ms("2026-09-15 06:00")
        assert next_boundary(BUSINESS, (), now_ms=now) == next_boundary(BUSINESS, now_ms=now)

    def test_standing_on_a_reset_edge_returns_the_next_one(self):
        """Strictly after `now`, or `vu` expires the instant it is written."""
        assert _iso(next_boundary((), DAILY, now_ms=_ms("2026-09-15 00:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_standing_inside_a_reset_window_skips_past_it(self):
        """`* 0 * * *` matches all sixty minutes; the next *edge* is tomorrow's 00:00.

        The current window has to be left before looking for an edge, because the
        next matching minute from inside one is 00:31 — no rising edge at all. A
        `vu` of 00:31 would re-materialise the bucket every minute until 01:00 and
        apply no reset, since `prev_reset_edge` correctly reports 00:00 each time.
        """
        hourly = (ScheduleEntry.reset(cron="* 0 * * *", tz="America/New_York"),)
        assert _iso(next_boundary((), hourly, now_ms=_ms("2026-09-15 00:30"))).startswith(
            "2026-09-16T00:00"
        )

    def test_a_reset_that_never_stops_matching_caps_too(self):
        """`* * * * *` has no rising edge because it has no falling one either.

        The forward scan has to leave the current window before an edge can
        exist, and here it never does. Capping keeps the bucket materialising
        once a horizon rather than pinning `vu` to nothing at all.
        """
        always = (ScheduleEntry.reset(cron="* * * * *"),)
        now = _ms("2026-09-15 09:00")
        assert next_boundary((), always, now_ms=now) == now + 366 * _DAY_MS

    def test_an_out_of_reach_reset_caps_rather_than_vanishing(self):
        """A yearly reset cannot be seen seven days out, and must not report nothing.

        None would leave `vu` unset and the fast path spending pre-reset tokens
        forever, because only a materialising pass runs the backwards scan that
        would find the edge. The cap forces one pass per horizon instead.
        """
        yearly = (ScheduleEntry.reset(cron="0 0 1 1 *", tz="America/New_York"),)
        now = _ms("2026-09-15 09:00")
        assert next_boundary((), yearly, now_ms=now) == now + 7 * _DAY_MS


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


class TestNextResetEdge:
    def test_finds_the_next_midnight(self):
        assert _iso(next_reset_edge(DAILY, now_ms=_ms("2026-09-15 09:00"))).startswith(
            "2026-09-16T00:00"
        )

    def test_standing_on_an_edge_returns_the_following_one(self):
        """Strictly after `now`, for the same reason `next_boundary` is: an
        answer at `now` makes a wait of zero look like a wait until a reset."""
        assert _iso(next_reset_edge(DAILY, now_ms=_ms("2026-09-16 00:00"))).startswith(
            "2026-09-17T00:00"
        )

    def test_a_never_matching_expression_has_no_edge(self):
        """`0 0 29 2 *` — a leap day, out of reach of the minute-granularity
        scan almost always. (§3.6 names February 30th; cronsim rejects it.)"""
        never = (ScheduleEntry.reset(cron="0 0 29 2 *"),)
        assert next_reset_edge(never, now_ms=_ms("2026-09-15 09:00")) is None

    def test_empty_reset_schedule(self):
        assert next_reset_edge((), now_ms=_ms("2026-09-15 09:00")) is None

    def test_reports_none_where_the_internal_twin_reports_the_cap(self):
        """Discriminates this against `_next_reset_edge`, which caps rather
        than reporting None. The cap is right for a `vu` stamp and wrong for a
        wait, which would quote a countdown to an instant where nothing
        happens."""
        from zae_limiter.schedule import _next_reset_edge

        never = (ScheduleEntry.reset(cron="0 0 29 2 *"),)
        now = _ms("2026-09-15 09:00")
        assert next_reset_edge(never, now_ms=now) is None
        assert _next_reset_edge(never, now) == now + 7 * _DAY_MS

    def test_takes_the_earliest_across_entries(self):
        """The mirror of `prev_reset_edge` taking the latest: looking forward,
        the first edge to fire is the one that matters."""
        pair = (
            ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),
            ScheduleEntry.reset(cron="0 12 * * *", tz="America/New_York"),
        )
        assert _iso(next_reset_edge(pair, now_ms=_ms("2026-09-15 09:00"))).startswith(
            "2026-09-15T12:00"
        )


class TestBoundaryAwareRetryAfter:
    BASE = dict(cp_milli=1_000_000, ra_milli=1_000_000, rp_ms=60_000)

    def test_the_specs_worked_example(self):
        """Flat says 30.001 s; the truth is 50.001 s — 10 s yielding 166_666
        millitokens, then 333_334 remaining at half rate."""
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 08:59:50"),
        )
        assert got == pytest.approx(50.001, abs=0.002)

    def test_the_flat_estimate_is_the_wrong_answer(self):
        """Discriminates the test above against an implementation that walks
        but never applies the window's effective rate."""
        flat = calculate_retry_after(500_000, 1_000_000, 60_000)
        assert flat == pytest.approx(30.001, abs=0.002)

    def test_a_non_whole_hour_offset_is_handled(self):
        """Asia/Kolkata is +05:30, so a 09:00 local edge is 03:30Z — half a
        step off the hourly probe grid. This is the case core plan Task 4's
        two-phase refinement exists for; America/New_York cannot tell a correct
        scan from a late one, because its offset is a whole number of hours."""
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=IST_BUSINESS,
            now_ms=_ms("2026-09-15 08:59:50", IST),
        )
        assert got == pytest.approx(50.001, abs=0.002)

    def test_a_boundary_that_raises_the_limit_shortens_the_wait(self):
        """The over-reporting direction. From 08:59:50 the base 1000/min needs
        30 s; the 09:00 window doubles the rate, so 10 s of base refill leaves
        333_334 to clear at 2000/min = 10.000 s. Total 20.001 s."""
        doubling = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=2.0),)
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=doubling,
            now_ms=_ms("2026-09-15 08:59:50"),
        )
        assert got == pytest.approx(20.001, abs=0.002)

    def test_a_reset_edge_dominates(self):
        """A daily quota's answer is 'at midnight', and it is the *only*
        answer: ADR-137 gives a reset-carrying limit `ra_milli == 0`, so there
        is no drip to fall back on. Before #530 this reported 0.0 — 'retry
        immediately', an hour early and repeatedly."""
        got = retry_after_with_schedule(
            deficit_milli=5_000_000,
            cp_milli=10_000_000,
            ra_milli=0,  # ADR-137: a limit drips or resets, never both
            rp_ms=86_400_000,
            sched=(),
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert got == pytest.approx(3600.001, abs=0.002)

    def test_a_quota_with_no_deficit_does_not_report_its_next_reset(self):
        """Discriminates the test above against "a quota always returns its
        next edge"."""
        got = retry_after_with_schedule(
            deficit_milli=0,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=86_400_000,
            sched=(),
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert got == 0.0

    def test_a_zero_rate_with_no_reset_is_still_no_wait(self):
        """The other half of the discrimination: the edge, not the zero rate,
        is what produces a non-zero answer above. Unreachable through a
        constructible limit — it guards a corrupt stored item."""
        got = retry_after_with_schedule(
            deficit_milli=5_000_000,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=86_400_000,
            sched=(),
            reset_sched=(),
            now_ms=_ms("2026-09-15 23:00"),
        )
        assert got == 0.0

    def test_a_quota_inside_a_scale_window_still_reports_its_edge(self):
        """Both tuples live at once, and the walk must not depend on #556.

        `effective_params` floors a scaled rate at 1 milli-unit, so a quota
        inside a `scale` window arrives in the walk with `eff_ra == 1` — a
        phantom drip, not the zero the rate gate looks for. The positive-rate
        branch has to consult the edge too, or this reports a 24-day countdown
        (5_000_000 millitokens at 1 per day) instead of one hour.
        """
        evening = (ScheduleEntry(cron="* 22-23 * * *", tz="America/New_York", scale=0.5),)
        with patch("zae_limiter.schedule.next_boundary", wraps=next_boundary) as spy:
            got = retry_after_with_schedule(
                deficit_milli=5_000_000,
                cp_milli=10_000_000,
                ra_milli=0,
                rp_ms=86_400_000,
                sched=evening,
                reset_sched=DAILY,
                now_ms=_ms("2026-09-15 23:00"),
            )
        assert got == pytest.approx(3600.001, abs=0.002)
        # The walk must *return* on the edge, not merely arrive at the same
        # number by exhausting its budget and falling back. Deleting the
        # positive-rate branch's edge check still gives 3600.001 — via the
        # fallback — after eight windows of cron scanning on a rejection path.
        assert spy.call_count == 1

    def test_a_zero_rate_window_steps_to_its_boundary_rather_than_reporting_it(self):
        """A boundary is not an answer; nothing happens at one except a change
        of rate. The same quota asked at noon, outside its `scale` window, must
        report midnight (12 h) and not the 22:00 boundary it steps over (10 h).
        """
        evening = (ScheduleEntry(cron="* 22-23 * * *", tz="America/New_York", scale=0.5),)
        with patch("zae_limiter.schedule.next_boundary", wraps=next_boundary) as spy:
            got = retry_after_with_schedule(
                deficit_milli=5_000_000,
                cp_milli=10_000_000,
                ra_milli=0,
                rp_ms=86_400_000,
                sched=evening,
                reset_sched=DAILY,
                now_ms=_ms("2026-09-15 12:00"),
            )
        assert got == pytest.approx(43_200.001, abs=0.002)
        # One step over the boundary, then the edge — not eight and a fallback.
        assert spy.call_count == 2

    def test_a_sub_token_deficit_on_a_scaled_quota_still_reports_the_edge(self):
        """The #556 failure the existing tests could not see.

        The tests above use a deficit large enough that even the phantom
        1-millitoken drip took longer than the reset edge, so the edge won
        either way and the bug stayed invisible. Here it does not: `rp_ms` is
        the 1 s `Limit.quota` actually stores (`_QUOTA_REFILL_PERIOD_SECONDS`,
        inert while `ra` is 0), so the phantom drip clears a 1-millitoken
        deficit in a single period and the walk reported **1.001 s** — "retry in
        one second" for a daily quota that does not come back until midnight.

        A quota that is *always* inside its `scale` window, so there is no
        boundary to step over and nothing but the rate can produce the answer.
        """
        always = (ScheduleEntry(cron="* * * * *", tz="America/New_York", scale=0.5),)
        got = retry_after_with_schedule(
            deficit_milli=1,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=1_000,
            sched=always,
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 12:00"),
        )
        assert got == pytest.approx(43_200.001, abs=0.002)

    def test_a_parameter_boundary_before_the_reset_edge_is_walked_through(self):
        """The other both-tuples ordering: the parameter window closes at 23:00
        and the reset fires at 00:00, so the walk must step over the boundary
        and still land on the edge rather than stopping at the boundary."""
        early_evening = (ScheduleEntry(cron="* 20-22 * * *", tz="America/New_York", scale=0.5),)
        got = retry_after_with_schedule(
            deficit_milli=5_000_000,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=86_400_000,
            sched=early_evening,
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 22:30"),
        )
        # 22:30 -> 00:00 is 90 minutes, across the 23:00 window close.
        assert got == pytest.approx(5400.001, abs=0.002)

    def test_unscheduled_matches_calculate_retry_after_exactly(self):
        """Not 'approximately' — the unscheduled path must be the identical
        integer arithmetic, or every existing retry assertion in the suite
        drifts by a millisecond."""
        got = retry_after_with_schedule(
            deficit_milli=500_000, **self.BASE, sched=(), now_ms=_ms("2026-09-15 10:00")
        )
        assert got == calculate_retry_after(500_000, 1_000_000, 60_000)

    def test_a_cleared_deficit_is_no_wait(self):
        got = retry_after_with_schedule(
            deficit_milli=0, **self.BASE, sched=BUSINESS, now_ms=_ms("2026-09-15 10:00")
        )
        assert got == 0.0

    def test_the_shard_share_is_applied_inside_each_window(self):
        """Scale first, then divide (Global Constraints). Half of 1000/min
        across 2 shards is 250/min, so 500 tokens take 120 s, not 60."""
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 10:00"),
            shard_count=2,
        )
        assert got == pytest.approx(120.001, abs=0.002)

    def test_dividing_before_scaling_would_be_a_different_answer(self):
        """Discriminates the test above: 60 s is scale-only, 120 s is both."""
        scale_only = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 10:00"),
        )
        assert scale_only == pytest.approx(60.001, abs=0.002)

    def test_a_share_that_floors_to_zero_falls_back_to_the_undivided_rate(self):
        """Same rule as `BucketState.retry_refill_amount_milli` (#475): a share
        of zero has no finite wait, so report the undivided *scheduled* rate
        rather than dividing by zero or returning 0.0."""
        got = retry_after_with_schedule(
            deficit_milli=500,
            cp_milli=1_000,
            ra_milli=1_000,
            rp_ms=60_000,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 10:00"),
            shard_count=1024,
        )
        assert got == pytest.approx(calculate_retry_after(500, 500, 60_000), abs=0.002)

    def test_the_floored_fallback_is_the_scheduled_rate_not_the_base(self):
        """Discriminates the test above: the base rate would be 1_000, which
        halves the wait to something nothing in the system refills at."""
        got = retry_after_with_schedule(
            deficit_milli=500,
            cp_milli=1_000,
            ra_milli=1_000,
            rp_ms=60_000,
            sched=BUSINESS,
            now_ms=_ms("2026-09-15 10:00"),
            shard_count=1024,
        )
        assert got != pytest.approx(calculate_retry_after(500, 1_000, 60_000), abs=0.002)

    def test_falls_back_to_the_flat_estimate_past_the_walk_cap(self):
        """A schedule that alternates every minute against a deficit that takes
        an hour exhausts the 8-window budget. The fallback must be the flat
        estimate, not a partial walk reported as if it were complete."""
        alternating = (ScheduleEntry(cron="*/2 * * * *", tz="UTC", scale=0.001),)
        got = retry_after_with_schedule(
            deficit_milli=10_000_000,
            **self.BASE,
            sched=alternating,
            now_ms=_ms("2026-09-15 10:00:00"),
        )
        assert got == calculate_retry_after(10_000_000, 1_000_000, 60_000)

    def test_the_walk_cap_is_honoured_rather_than_looping(self):
        """Pins the budget itself: nine windows must not be walked. Verified by
        counting boundary lookups rather than by timing."""
        alternating = (ScheduleEntry(cron="*/2 * * * *", tz="UTC", scale=0.001),)
        with patch("zae_limiter.schedule.next_boundary", wraps=next_boundary) as spy:
            retry_after_with_schedule(
                deficit_milli=10_000_000,
                **self.BASE,
                sched=alternating,
                now_ms=_ms("2026-09-15 10:00:00"),
            )
        assert spy.call_count == 8

    def test_the_cap_fallback_still_carries_the_reset_edge(self):
        """The `max_windows` fallback is exactly where a quota that outran the
        walk lands (#530). An inlined copy that stopped at the rate arithmetic
        would reintroduce the 0.0 this function exists to remove."""
        got = retry_after_with_schedule(
            deficit_milli=5_000_000,
            cp_milli=10_000_000,
            ra_milli=0,
            rp_ms=86_400_000,
            sched=(),
            reset_sched=DAILY,
            now_ms=_ms("2026-09-15 23:00"),
            max_windows=0,  # forces the fallback without a pathological schedule
        )
        assert got == pytest.approx(3600.001, abs=0.002)

    def test_a_zero_shard_count_does_not_divide_by_zero(self):
        got = retry_after_with_schedule(
            deficit_milli=500_000,
            **self.BASE,
            sched=(),
            now_ms=_ms("2026-09-15 10:00"),
            shard_count=0,
        )
        assert got == calculate_retry_after(500_000, 1_000_000, 60_000)
