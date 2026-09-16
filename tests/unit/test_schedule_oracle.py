"""Our matcher must agree with croniter across a year including both DST switches.

croniter is a dev-only dependency used purely as an oracle. Its own
`match()` re-parses the expression on every call (362 us measured), which is
why it is unusable in the scan itself — but that cost is irrelevant here.
"""

import random
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

from zae_limiter.schedule import (
    ScheduleEntry,
    matches,
    next_reset_edge,
    parse_cron,
    prev_reset_edge,
)

NY = ZoneInfo("America/New_York")

EXPRESSIONS = [
    "* 9-17 * * MON-FRI",
    "*/15 0-6,22-23 * * *",
    "* * 13 * FRI",
    "0 0 * * SUN",
    "0 0 * * 7",
    "30 2 * * *",
    "* * 1 JAN,JUL *",
    "*/5 9-17/2 1-7 * MON",
]


def _sample_instants():
    """Random instants across 2027 plus dense coverage of both DST switches."""
    random.seed(7)
    lo = int(datetime(2027, 1, 1, tzinfo=NY).timestamp())
    hi = int(datetime(2028, 1, 1, tzinfo=NY).timestamp())
    samples = [random.randrange(lo, hi, 60) for _ in range(1500)]
    for switch in (datetime(2027, 3, 14, tzinfo=NY), datetime(2027, 11, 7, tzinfo=NY)):
        base = int(switch.timestamp())
        samples += list(range(base, base + 3 * 86400, 60))[::7]
    return samples


@pytest.mark.parametrize("expr", EXPRESSIONS)
def test_agrees_with_croniter(expr):
    parsed = parse_cron(expr, "America/New_York")
    for ts in _sample_instants():
        dt = datetime.fromtimestamp(ts, NY)
        assert matches(parsed, ts * 1000) == croniter.match(expr, dt), (
            f"{expr} disagreed at {dt.isoformat()}"
        )


def test_oracle_comparison_count():
    """Pin the sample size: a silently shrunk sweep proves much less (§3.1)."""
    assert len(_sample_instants()) * len(EXPRESSIONS) == 21888


def test_dst_window_edge_holds_local_and_moves_in_utc():
    """9am New York stays 9am local across spring-forward; its UTC instant shifts."""
    parsed = parse_cron("* 9-17 * * MON-FRI", "America/New_York")
    before = int(datetime(2027, 3, 12, 9, 0, tzinfo=NY).timestamp())  # Fri, EST
    after = int(datetime(2027, 3, 16, 9, 0, tzinfo=NY).timestamp())  # Tue, EDT
    assert matches(parsed, before * 1000) and matches(parsed, after * 1000)
    assert datetime.fromtimestamp(before, ZoneInfo("UTC")).hour == 14
    assert datetime.fromtimestamp(after, ZoneInfo("UTC")).hour == 13


@pytest.mark.parametrize(
    "day,expected_minutes",
    [
        ("2027-03-14", 1380),  # spring forward: a 23-hour day
        ("2026-11-01", 1500),  # fall back: a 25-hour day
        ("2026-09-15", 1440),  # ordinary
    ],
)
def test_no_minute_skipped_or_doubled(day, expected_minutes):
    start = int(datetime.fromisoformat(f"{day} 00:00").replace(tzinfo=NY).timestamp())
    end = int(datetime.fromisoformat(f"{day} 23:59").replace(tzinfo=NY).timestamp()) + 60
    assert (end - start) // 60 == expected_minutes


# `reset_schedule` entries are edge-triggered, so the previous edge is croniter's
# previous *fire time* — but only for expressions whose matching minutes are
# isolated. `* 0 * * *` matches sixty consecutive minutes and croniter's
# `get_prev` reports the last of them, where the edge is the first; these five
# are all single-minute patterns, so the two coincide and croniter is an oracle.
# Each also has a maximum gap under the seven-day minute-granularity cap, so
# `prev_reset_edge` can always reach the edge.
#
# `30 2 * * *` is deliberately absent, though it is in EXPRESSIONS above. croniter
# agrees with `matches` that no instant on a spring-forward day is 02:30 local —
# but its `get_prev` is a *scheduler* API and reschedules the skipped fire to
# 03:00, where a reset edge is a fact about the wall clock and simply does not
# occur that day. The two disagree by design, so croniter is an oracle for
# matching and not for edge finding in the skipped hour. Our semantics are pinned
# directly in test_schedule_boundary.py instead.
RESET_EXPRESSIONS = [
    "0 0 * * *",
    "*/15 * * * *",
    "0 */6 * * *",
    "0 0 * * MON,THU",
]


def _reset_sample_instants():
    """Sixty random instants in 2027 plus a dense walk over both DST switches.

    Far smaller than the matcher sweep above on purpose: each assertion runs a
    backwards scan of up to four days at minute resolution, where `matches` runs
    once.
    """
    random.seed(11)
    lo = int(datetime(2027, 1, 1, tzinfo=NY).timestamp())
    hi = int(datetime(2028, 1, 1, tzinfo=NY).timestamp())
    samples = [random.randrange(lo, hi, 60) for _ in range(60)]
    for switch in (datetime(2027, 3, 14, tzinfo=NY), datetime(2027, 11, 7, tzinfo=NY)):
        base = int(switch.timestamp())
        samples += list(range(base, base + 86400, 60))[::97]
    return samples


@pytest.mark.parametrize("expr", RESET_EXPRESSIONS)
def test_prev_reset_edge_agrees_with_croniter(expr):
    sched = (ScheduleEntry.reset(cron=expr, tz="America/New_York"),)
    for ts in _reset_sample_instants():
        dt = datetime.fromtimestamp(ts, NY)
        if croniter.match(expr, dt):
            expected = dt.replace(second=0, microsecond=0)
        else:
            expected = croniter(expr, dt).get_prev(datetime)
        got = prev_reset_edge(sched, ts * 1000)
        assert got is not None, f"{expr} found no edge at {dt.isoformat()}"
        assert datetime.fromtimestamp(got / 1000, NY) == expected, (
            f"{expr} disagreed at {dt.isoformat()}"
        )


def test_reset_oracle_comparison_count():
    """Pin the sample size, as the matcher sweep above does."""
    assert len(_reset_sample_instants()) * len(RESET_EXPRESSIONS) == 360


# Coarse quota periods, whose edges are further apart than the seven-day cap the
# reset scanners used before #574 — so none of these could be oracle-tested at
# all until the horizon became the entry's own cycle. Single-minute patterns
# again, so croniter's fire times *are* the edges.
COARSE_RESET_EXPRESSIONS = [
    "0 0 1 * *",  # monthly
    "0 0 1 1,4,7,10 *",  # quarterly
    "0 0 1 1 *",  # annual
    "0 0 1 7 *",  # annual, mid-year, so both directions cross a year end
]


def _coarse_sample_instants():
    """Twenty-four instants spread over two years, one per month.

    Deliberately not the dense sweep above: each assertion here runs a scan of
    up to 366 days, and one per month of a two-year span already exercises every
    position within each period's cycle.
    """
    return [
        int(datetime(year, month, 15, 12, 0, tzinfo=NY).timestamp())
        for year in (2027, 2028)
        for month in range(1, 13)
    ]


@pytest.mark.parametrize("expr", COARSE_RESET_EXPRESSIONS)
def test_coarse_prev_reset_edge_agrees_with_croniter(expr):
    sched = (ScheduleEntry.reset(cron=expr, tz="America/New_York"),)
    for ts in _coarse_sample_instants():
        dt = datetime.fromtimestamp(ts, NY)
        expected = croniter(expr, dt).get_prev(datetime)
        got = prev_reset_edge(sched, ts * 1000)
        assert got is not None, f"{expr} found no edge before {dt.isoformat()}"
        assert datetime.fromtimestamp(got / 1000, NY) == expected, (
            f"{expr} disagreed looking back from {dt.isoformat()}"
        )


@pytest.mark.parametrize("expr", RESET_EXPRESSIONS + COARSE_RESET_EXPRESSIONS)
def test_next_reset_edge_agrees_with_croniter(expr):
    """The forward twin, which had no oracle test at all before #574.

    It is the one `RateLimitExceeded.as_dict()` serialises as `resets_at_ms`
    (#545) and the one a quota's `retry_after_seconds` is measured to, so a
    silent disagreement with a real cron implementation shows up in 429 bodies.
    """
    sched = (ScheduleEntry.reset(cron=expr, tz="America/New_York"),)
    for ts in _coarse_sample_instants():
        dt = datetime.fromtimestamp(ts, NY)
        expected = croniter(expr, dt).get_next(datetime)
        got = next_reset_edge(sched, now_ms=ts * 1000)
        assert got is not None, f"{expr} found no edge after {dt.isoformat()}"
        assert datetime.fromtimestamp(got / 1000, NY) == expected, (
            f"{expr} disagreed looking forward from {dt.isoformat()}"
        )
