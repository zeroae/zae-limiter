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

from zae_limiter.schedule import matches, parse_cron

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
