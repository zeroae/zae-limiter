"""The reset scan's horizon reaches a coarse reset's own cycle (#574).

`_granularity` picks the probe *step* from the finest cron field any entry
constrains, and every practical reset pattern pins the minute — so before this
was split, every reset was scanned with a seven-day horizon regardless of how
far apart its edges actually are. A monthly `0 0 1 * *` reported "no edge" for
roughly 24 days out of every 30, and a yearly `0 0 1 1 *` reported
`retry_after_seconds == 0.0` — "retry immediately" against a limit that cannot
admit anything for months.

Both surfaces are pinned here, at every period from six-hourly to annual:

* `next_reset_edge`, which `RateLimitExceeded.as_dict()` serialises as
  `resets_at_ms` (#545), and
* `retry_after_seconds`, through the real rejection path (`bucket.try_consume`)
  and the real display path (`bucket.calculate_time_until_available`).
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from zae_limiter import schedule
from zae_limiter.bucket import calculate_time_until_available, try_consume
from zae_limiter.models import BucketState, Limit
from zae_limiter.schedule import (
    ScheduleEntry,
    next_reset_edge,
    prev_reset_edge,
    retry_after_with_schedule,
)

# Mid-September: inside every period below, and far enough from January that the
# annual case is well past the old 56-day walk budget (8 windows x 7-day cap).
NOW = int(datetime(2026, 9, 15, 12, 0, tzinfo=UTC).timestamp() * 1000)

# (label, cron, the next edge after NOW, in UTC)
PERIODS = [
    ("six-hourly", "0 */6 * * *", "2026-09-15T18:00:00+00:00"),
    ("daily", "0 0 * * *", "2026-09-16T00:00:00+00:00"),
    ("weekly", "0 0 * * 1", "2026-09-21T00:00:00+00:00"),
    ("monthly", "0 0 1 * *", "2026-10-01T00:00:00+00:00"),
    ("quarterly", "0 0 1 1,4,7,10 *", "2026-10-01T00:00:00+00:00"),
    ("yearly", "0 0 1 1 *", "2027-01-01T00:00:00+00:00"),
]

_IDS = [p[0] for p in PERIODS]


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _quota_state(limit: Limit, now_ms: int, tokens_milli: int = 0) -> BucketState:
    """An exhausted bucket carrying ``limit``'s reset schedule."""
    return BucketState(
        entity_id="u1",
        resource="api",
        limit_name=limit.name,
        tokens_milli=tokens_milli,
        last_refill_ms=now_ms,
        capacity_milli=limit.capacity * 1000,
        refill_amount_milli=limit.refill_amount * 1000,
        refill_period_ms=limit.refill_period_seconds * 1000,
        reset_sched=limit.reset_schedule,
    )


class TestNextResetEdgeReachesItsOwnCycle:
    """Surface 1 — `resets_at_ms` in a 429 body (#545)."""

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_finds_the_edge(self, label, cron, expected):
        sched = (ScheduleEntry.reset(cron=cron),)
        assert _iso(next_reset_edge(sched, now_ms=NOW)) == expected

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_the_edge_is_never_none(self, label, cron, expected):
        """`None` is the documented "this expression never matches" reading.

        Routine `resets_at_ms: null` is what made the field #545 added useless
        exactly where a client most needs it — a coarse quota.
        """
        assert next_reset_edge((ScheduleEntry.reset(cron=cron),), now_ms=NOW) is not None

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_the_backward_twin_finds_a_missed_edge_too(self, label, cron, expected):
        """`prev_reset_edge` is the half the materialising pass asks (§3.6).

        Scanned from one minute past the edge this test's forward twin found, so
        the answer is that same edge. A horizon shorter than the entry's cycle
        loses it, and a quota whose bucket was idle across the edge then never
        resets at all.
        """
        sched = (ScheduleEntry.reset(cron=cron),)
        edge = next_reset_edge(sched, now_ms=NOW)
        assert edge is not None
        # A whole cycle later, so the scan has to reach back past its old cap.
        later = edge + 60 * 86_400_000
        assert prev_reset_edge(sched, later) is not None


class TestQuotaRetryAfterReachesItsOwnCycle:
    """Surface 2 — `retry_after_seconds` on the rejection and display paths."""

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_rejection_path_reports_the_wait_to_the_edge(self, label, cron, expected):
        limit = Limit.quota("rpq", 10_000, cron=cron)
        state = _quota_state(limit, NOW)
        result = try_consume(state, 1, NOW)
        assert not result.success
        edge = int(datetime.fromisoformat(expected).timestamp() * 1000)
        assert result.retry_after_seconds == pytest.approx((edge - NOW + 1) / 1000.0)

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_display_path_agrees_with_the_rejection_path(self, label, cron, expected):
        limit = Limit.quota("rpq", 10_000, cron=cron)
        state = _quota_state(limit, NOW)
        edge = int(datetime.fromisoformat(expected).timestamp() * 1000)
        assert calculate_time_until_available(state, 1, NOW) == pytest.approx(
            (edge - NOW + 1) / 1000.0
        )

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_never_reports_retry_immediately(self, label, cron, expected):
        """The user-visible harm: a 429 telling a client to retry now.

        A client honouring `retry_after_seconds == 0.0` goes into a hot loop
        against a limit that will reject every attempt for months.
        """
        limit = Limit.quota("rpq", 10_000, cron=cron)
        assert try_consume(_quota_state(limit, NOW), 1, NOW).retry_after_seconds > 0.0

    def test_an_annual_quota_is_right_in_every_month(self):
        """Sampled through a whole year, not just at one instant.

        The annual case used to answer `0.0` from January through October and be
        correct only in November and December, where the next January 1st fell
        inside the 56-day walk budget.
        """
        limit = Limit.quota("rpq", 10_000, cron="0 0 1 1 *")
        for month in range(1, 13):
            now = int(datetime(2026, month, 15, 12, 0, tzinfo=UTC).timestamp() * 1000)
            edge = int(datetime(2027, 1, 1, tzinfo=UTC).timestamp() * 1000)
            got = try_consume(_quota_state(limit, now), 1, now).retry_after_seconds
            assert got == pytest.approx((edge - now + 1) / 1000.0), f"month {month}"


class TestTheFiftySixDayCliff:
    """The old walk budget was 8 windows x a 7-day cap; 57 days answered 0.0.

    Pinned so a future regression in either the horizon or `max_windows` is
    caught at the exact instant the old one broke, rather than only by the
    period cases above.
    """

    @pytest.mark.parametrize("days_out", [54, 55, 56, 57, 58, 120, 300])
    def test_an_edge_beyond_the_old_budget_is_still_found(self, days_out):
        # `0 0 D M *` fires once a year, so the only edge in reach is the one
        # placed exactly `days_out` days ahead.
        target = datetime(2026, 9, 15, 12, 0, tzinfo=UTC) + timedelta(days=days_out)
        target = target.replace(hour=0, minute=0)
        cron = f"0 0 {target.day} {target.month} *"
        sched = (ScheduleEntry.reset(cron=cron),)

        edge = next_reset_edge(sched, now_ms=NOW)
        assert edge == int(target.timestamp() * 1000)

        wait = retry_after_with_schedule(1000, 10_000_000, 0, 1000, (), sched, now_ms=NOW)
        assert wait == pytest.approx((edge - NOW + 1) / 1000.0)


class TestScanCost:
    """The horizon grew ~52x; the scan must not.

    Skipping whole non-matching local days and hours bounds a forward search at
    (days in horizon) + 24 + 60 probes, against the (days x 1440) a flat
    minute-granularity walk costs. Asserted as wall-clock rather than a probe
    count because this runs on the rejection path, where the previous annual
    scan cost ~450 ms.
    """

    @pytest.mark.parametrize("label,cron,expected", PERIODS, ids=_IDS)
    def test_the_scan_is_fast_enough_for_the_rejection_path(self, label, cron, expected):
        import time

        sched = (ScheduleEntry.reset(cron=cron),)
        start = time.perf_counter()
        for _ in range(5):
            next_reset_edge(sched, now_ms=NOW)
        elapsed_ms = (time.perf_counter() - start) * 1000 / 5
        assert elapsed_ms < 25.0, f"{label}: {elapsed_ms:.1f} ms per scan"


# Both DST switches, plus one instant per month, in a zone that has them.
_NY = ZoneInfo("America/New_York")
_SKIP_CRONS = [
    "0 0 * * *",  # hour-skip on every probe
    "30 2 * * *",  # the local hour that does not exist at spring-forward
    "0 0 * * MON,THU",  # day-skip by weekday
    "* 1-3 * * *",  # a multi-hour window straddling both switches
    "0 0 1 * *",  # day-skip by day-of-month
    "0 0 29 2 *",  # day-skip by month, matching once in four years
    "* * * * *",  # constrains nothing, so nothing may be skipped
]


def _dst_probe_instants():
    out = []
    for switch in (datetime(2027, 3, 13, tzinfo=_NY), datetime(2027, 11, 6, tzinfo=_NY)):
        base = int(switch.timestamp())
        out += list(range(base, base + 3 * 86_400, 149 * 60))
    out += [int(datetime(2027, m, 7, 5, 13, tzinfo=_NY).timestamp()) for m in range(1, 13)]
    return out


def _flat_walk(parsed, want, start_ms, horizon_ms, step):
    """The exhaustive minute-by-minute walk the skipping scan replaces."""
    t = -(-start_ms // 60_000) * 60_000 if step > 0 else (start_ms // 60_000) * 60_000
    while (t <= horizon_ms) if step > 0 else (t >= horizon_ms):
        if schedule.matches(parsed, t) == want:
            return t
        t += step
    return None


@pytest.mark.parametrize("cron", _SKIP_CRONS)
def test_skipping_agrees_with_an_exhaustive_minute_walk(cron):
    """The skip is an optimisation, so it must change no answer at all.

    `_unreachable_block` lets a search for a *match* step over whole local days
    and hours the date and hour fields rule out — which is what keeps #574's
    widened horizon affordable, and the one part of it that could silently jump
    an edge. Pinned against the flat walk it replaces, in a zone with DST, over
    three days around each 2027 switch (`30 2 * * *` never occurs on the
    spring-forward day) plus one instant a month.

    A three-day window either side, so the brute-force side stays tractable;
    the horizon itself is covered by the croniter oracle in
    `test_schedule_oracle.py`.
    """
    parsed = schedule.parse_cron(cron, "America/New_York")
    unit, _cap = schedule._reset_scan(parsed)
    window = 3 * 86_400_000

    for ts in _dst_probe_instants():
        now = ts * 1000
        when = datetime.fromtimestamp(ts, _NY).isoformat()
        for want in (True, False):
            assert schedule._earliest_where(parsed, want, now, now + window, unit) == _flat_walk(
                parsed, want, now, now + window, 60_000
            ), f"{cron} forward want={want} at {when}"

            got = schedule._latest_where(parsed, want, now, now - window, unit)
            expected = _flat_walk(parsed, want, now, now - window, -60_000)
            # `_latest_where` reports the last minute of the found unit, which
            # for a coarse step is later than the first matching probe; both
            # readings must agree that it *is* a minute with the wanted state.
            assert (got is None) == (expected is None), f"{cron} back want={want} at {when}"
            if got is not None:
                assert schedule.matches(parsed, got) == want, (
                    f"{cron} back want={want} at {when} returned a wrong-state minute"
                )
                assert got == expected, f"{cron} back want={want} at {when}"
