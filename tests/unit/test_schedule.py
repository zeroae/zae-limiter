"""Tests for schedule parsing, validation and evaluation (#222 §3.1)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.schedule import ScheduleEntry, effective_params, parse_cron


class TestParseCron:
    def test_expands_fields_to_int_sets(self):
        p = parse_cron("* 9-17 * * MON-FRI", "America/New_York")
        assert p.hours == frozenset(range(9, 18))
        assert p.weekdays == frozenset({1, 2, 3, 4, 5})
        assert p.day_and is True

    def test_dom_and_dow_both_constrained_means_or(self):
        """`* * 13 * FRI` matches the 13th OR any Friday — the classic cron trap."""
        assert parse_cron("* * 13 * FRI", "UTC").day_and is False
        assert parse_cron("* * 13 * *", "UTC").day_and is True
        assert parse_cron("* * * * FRI", "UTC").day_and is True

    @pytest.mark.parametrize("expr", ["* * * * SUN", "* * * * 0", "* * * * 7"])
    def test_sunday_normalised_to_seven(self, expr):
        """cronsim gives SUN/0 -> {0} and 7 -> {7}; we normalise to isoweekday's 7."""
        assert 7 in parse_cron(expr, "UTC").weekdays

    @pytest.mark.parametrize("expr", ["* * * * SUN", "* * * * 0", "* * * * 7"])
    def test_sunday_leaves_no_zero_behind(self, expr):
        """A leftover 0 can never match isoweekday(); it would be dead weight."""
        assert 0 not in parse_cron(expr, "UTC").weekdays

    def test_unconstrained_weekday_covers_every_isoweekday(self):
        """cronsim's wildcard set is {0..7}; folding 0 into 7 must not drop a day."""
        assert parse_cron("* * 13 * *", "UTC").weekdays == frozenset(range(1, 8))

    @pytest.mark.parametrize("expr", ["* * L * *", "* * LW * *", "* * * * FRI#2", "* * * * 5L"])
    def test_rejects_extended_tokens(self, expr):
        """These parse without error and poison the sets with sentinels."""
        with pytest.raises(ValueError, match="not supported"):
            parse_cron(expr, "UTC")

    @pytest.mark.parametrize("expr", ["* 9-17 * *", "bogus * * * *", "* 99 * * *", "60 * * * *"])
    def test_rejects_malformed(self, expr):
        with pytest.raises(ValueError):
            parse_cron(expr, "UTC")

    def test_rejects_unknown_timezone(self):
        with pytest.raises(ValueError, match="timezone"):
            parse_cron("* * * * *", "Mars/Olympus_Mons")


class TestScheduleEntry:
    def test_scale_entry(self):
        e = ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5)
        assert e.scale == 0.5

    def test_absolute_entry(self):
        assert ScheduleEntry(cron="* 0-6 * * *", capacity=2000).capacity == 2000

    def test_rejects_both_kinds(self):
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="* * * * *", scale=0.5, capacity=100)

    def test_rejects_neither_kind(self):
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="* * * * *")

    @pytest.mark.parametrize("scale", [0, -1, -0.5])
    def test_rejects_non_positive_scale(self, scale):
        with pytest.raises(ValueError, match="scale"):
            ScheduleEntry(cron="* * * * *", scale=scale)

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"capacity": 0},
            {"capacity": -5},
            {"capacity": 10, "refill_amount": 0},
            {"capacity": 10, "refill_period_seconds": 0},
        ],
    )
    def test_rejects_non_positive_absolutes(self, kwargs):
        with pytest.raises(ValueError):
            ScheduleEntry(cron="* * * * *", **kwargs)

    @pytest.mark.parametrize("expr", ["* * L * *", "nonsense"])
    def test_rejects_unusable_cron(self, expr):
        """A schedule that stores must be a schedule that evaluates (§3.1)."""
        with pytest.raises(ValueError):
            ScheduleEntry(cron=expr, scale=0.5)

    def test_rejects_unknown_timezone(self):
        with pytest.raises(ValueError, match="timezone"):
            ScheduleEntry(cron="* * * * *", tz="Mars/Olympus_Mons", scale=0.5)

    def test_is_frozen_and_hashable(self):
        e = ScheduleEntry(cron="* * * * *", scale=0.5)
        with pytest.raises(Exception):
            e.cron = "x"  # type: ignore[misc]
        assert hash(e)


# Base params in milli-units, deliberately with capacity != refill_amount and a
# refill period that is not a multiple of either: every one of the three is then
# pinned independently, so an implementation that returns the wrong field (or
# fails to touch one at all) cannot be rescued by a coincidence in the fixture.
BASE = (1_000_000, 200_000, 60_000)  # 1000 token bucket, 200 tokens/min

# 2026-09-15 is a Tuesday. 14:00 New York == 18:00 UTC, 03:00 New York == 07:00 UTC,
# so both instants also separate "used entry.tz" from "hardcoded UTC".
TUE_1400 = int(datetime(2026, 9, 15, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
TUE_0300 = int(datetime(2026, 9, 15, 3, 0, tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)


class TestEffectiveParams:
    def test_no_schedule_returns_base(self):
        assert effective_params(*BASE, (), TUE_1400) == BASE

    def test_no_match_returns_base(self):
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        assert effective_params(*BASE, sched, TUE_1400) == BASE

    def test_scale_halves_capacity_and_refill_together(self):
        """Time-to-fill must be preserved: halving only capacity would double refill speed."""
        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
        cp, ra, rp = effective_params(*BASE, sched, TUE_1400)
        # Exact values, not just the ratio: with cp != ra in BASE, scaling only one
        # of them (or neither) cannot satisfy both halves of this assertion.
        assert (cp, ra, rp) == (500_000, 100_000, 60_000)
        assert cp / ra == BASE[0] / BASE[1]

    def test_scale_above_one_raises_both(self):
        """`scale` is a multiplier, not a discount — a boost window must work too."""
        sched = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=2.0),)
        assert effective_params(*BASE, sched, TUE_1400) == (2_000_000, 400_000, 60_000)

    def test_scale_truncates_rather_than_rounding_up(self):
        """Rounding a limit up would admit more than the window allows."""
        sched = (ScheduleEntry(cron="* * * * *", scale=0.5),)
        assert effective_params(1001, 999, 60_000, sched, TUE_1400) == (500, 499, 60_000)

    def test_scale_floors_to_at_least_one_millitoken(self):
        """A tiny scale must not produce a zero capacity, which is unadmittable."""
        sched = (ScheduleEntry(cron="* * * * *", scale=0.0000001),)
        # Exact, not `>= 1`: `>= 1` is also satisfied by returning the base untouched.
        assert effective_params(1000, 500, 60_000, sched, TUE_1400) == (1, 1, 60_000)

    def test_absolute_capacity_only_overrides_capacity(self):
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", capacity=2000),)
        assert effective_params(*BASE, sched, TUE_0300) == (2_000_000, 200_000, 60_000)

    def test_absolute_refill_amount_only_overrides_refill_amount(self):
        sched = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", refill_amount=500),)
        assert effective_params(*BASE, sched, TUE_0300) == (1_000_000, 500_000, 60_000)

    def test_absolute_refill_period_only_overrides_period(self):
        sched = (
            ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", refill_period_seconds=30),
        )
        assert effective_params(*BASE, sched, TUE_0300) == (1_000_000, 200_000, 30_000)

    def test_absolute_fields_override_together(self):
        sched = (
            ScheduleEntry(
                cron="* 0-6 * * *",
                tz="America/New_York",
                capacity=2000,
                refill_amount=500,
                refill_period_seconds=30,
            ),
        )
        assert effective_params(*BASE, sched, TUE_0300) == (2_000_000, 500_000, 30_000)

    def test_first_matching_entry_wins(self):
        sched = (
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
            ScheduleEntry(cron="* * * * *", tz="America/New_York", scale=0.1),
        )
        assert effective_params(*BASE, sched, TUE_1400) == (500_000, 100_000, 60_000)

    def test_first_matching_entry_wins_even_when_it_is_the_larger_limit(self):
        """The same pair reversed. Without this, "pick the max" also passes the test above."""
        sched = (
            ScheduleEntry(cron="* * * * *", tz="America/New_York", scale=0.1),
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
        )
        assert effective_params(*BASE, sched, TUE_1400) == (100_000, 20_000, 60_000)

    def test_a_non_matching_first_entry_does_not_stop_the_search(self):
        """A miss must `continue`, not return the base."""
        sched = (
            ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.1),
            ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),
        )
        assert effective_params(*BASE, sched, TUE_1400) == (500_000, 100_000, 60_000)

    @pytest.mark.parametrize(
        ("tz", "expected"),
        [
            ("America/New_York", (500_000, 100_000, 60_000)),  # 14:00 local -> matches
            ("UTC", BASE),  # same instant is 18:00 UTC -> no match
        ],
    )
    def test_each_entry_matches_in_its_own_timezone(self, tz, expected):
        """Pins that `entry.tz` reaches `parse_cron` rather than a hardcoded zone."""
        sched = (ScheduleEntry(cron="* 14 * * *", tz=tz, scale=0.5),)
        assert effective_params(*BASE, sched, TUE_1400) == expected
