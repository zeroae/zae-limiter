"""Tests for schedule parsing, validation and evaluation (#222 §3.1)."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter.schedule import (
    MAX_PERIOD_SECONDS,
    MAX_SCALE,
    MAX_STORED_MILLI,
    MAX_TOKENS,
    ScheduleEntry,
    decode,
    effective_params,
    encode,
    parse_cron,
)


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


class TestParseCronRejectsSixFields:
    """cronsim accepts a leading seconds field; nothing here is finer than a minute.

    Left unrejected, `ScheduleEntry(cron="30 5 9 * * *")` constructs, matches the
    whole of 09:05 rather than one second of it, and then has six fields to squeeze
    into the encoding's five slots.
    """

    @pytest.mark.parametrize("cron", ["30 5 9 * * *", "* * * * * *", "* * * *"])
    def test_rejects_non_five_field_expressions(self, cron):
        with pytest.raises(ValueError, match="5 fields"):
            parse_cron(cron, "UTC")

    def test_schedule_entry_rejects_them_too(self):
        with pytest.raises(ValueError, match="5 fields"):
            ScheduleEntry(cron="30 5 9 * * *", tz="UTC", scale=0.5)


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

    @pytest.mark.parametrize("scale", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_scale(self, scale):
        """NaN slips past `<= 0` (every NaN comparison is False) and `inf` is positive.

        Both then die much later, inside `encode` ("cannot convert float NaN to
        integer") or `effective_params`, naming no field (#564).
        """
        with pytest.raises(ValueError, match="scale must be a finite number"):
            ScheduleEntry(cron="* * * * *", scale=scale)

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_rejects_non_finite_absolutes(self, field, value):
        """Worse than `scale`: these encode *cleanly*, as the byte string `cnan`.

        Nothing raises at write time, so the corrupt value reaches the config item
        and every later `decode` of it fails (#564).
        """
        with pytest.raises(ValueError, match=f"{field} must be a finite number"):
            ScheduleEntry(cron="* * * * *", **{field: value})

    def test_finite_values_still_pass(self):
        """The guard must not narrow anything that was already valid."""
        e = ScheduleEntry(cron="* * * * *", scale=0.5)
        assert e.scale == 0.5
        e = ScheduleEntry(cron="* * * * *", capacity=10, refill_amount=5, refill_period_seconds=60)
        assert (e.capacity, e.refill_amount, e.refill_period_seconds) == (10, 5, 60)

    def test_non_finite_is_rejected_before_positivity(self):
        """Ordering matters: a NaN must not fall through to `scale must be positive`."""
        with pytest.raises(ValueError, match="finite"):
            ScheduleEntry(cron="* * * * *", scale=float("nan"))

    def test_a_huge_integer_capacity_is_not_swept_up_by_the_finiteness_guard(self):
        """`math.isfinite` casts to float, so a bare call would OverflowError here.

        Since #570 the value is rejected — but for its *magnitude*, by an `int`
        comparison, which is only reachable because the isinstance guard above
        still keeps `math.isfinite` away from it.
        """
        with pytest.raises(ValueError, match="capacity must be at most"):
            ScheduleEntry(cron="* * * * *", capacity=10**400)


class TestAbsolutesAreIntegers:
    """The three absolute fields are `int | None` and nothing enforced it (#569).

    A non-integral float passes #564's finiteness guard (`math.isfinite(1.5)` is
    True), encodes cleanly as the byte string `c1.5`, and then dies at `decode`'s
    bare `int(tokens["c"])` — the same "bytes no later read can decode" failure
    class, arriving through the type system rather than through finiteness.
    """

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    @pytest.mark.parametrize("value", [1.5, 0.5, 2.5])
    def test_rejects_a_non_integral_float(self, field, value):
        with pytest.raises(ValueError, match=f"{field} must be a whole number"):
            ScheduleEntry(cron="* * * * *", **{field: value})

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    def test_rejects_an_integral_float_too(self, field):
        """`2.0` is rejected rather than coerced, agreeing with `_coerce_int`.

        The field is declared `int`; accepting a float that happens to be whole
        would widen the documented contract, and would leave a YAML author with
        an arbitrary line to reason about (2.0 fine, 1.5 not).
        """
        with pytest.raises(ValueError, match=f"{field} must be a whole number"):
            ScheduleEntry(cron="* * * * *", **{field: 2.0})

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    @pytest.mark.parametrize("value", [True, False])
    def test_rejects_a_boolean(self, field, value):
        """`bool` is an `int` subclass, so a bare `isinstance(x, int)` admits it.

        `ScheduleEntry(capacity=True)` encoded as `cTrue`, which `decode` cannot
        read either.
        """
        with pytest.raises(ValueError, match=f"{field} must be a whole number"):
            ScheduleEntry(cron="* * * * *", **{field: value})

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    def test_rejects_a_string(self, field):
        """Previously a `TypeError` from `value <= 0`, naming no field."""
        with pytest.raises(ValueError, match=f"{field} must be a whole number"):
            ScheduleEntry(cron="* * * * *", **{field: "5"})

    def test_integers_still_pass(self):
        e = ScheduleEntry(cron="* * * * *", capacity=10, refill_amount=5, refill_period_seconds=60)
        assert (e.capacity, e.refill_amount, e.refill_period_seconds) == (10, 5, 60)

    def test_scale_is_still_a_float_field(self):
        """`scale` is a float by design; the integer rule is only for the absolutes."""
        assert ScheduleEntry(cron="* * * * *", scale=0.5).scale == 0.5
        assert ScheduleEntry(cron="* * * * *", scale=2).scale == 2

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    def test_non_finite_still_reports_finiteness_not_integrality(self, field):
        """Ordering: #564's message is the specific one, so it stays first."""
        with pytest.raises(ValueError, match=f"{field} must be a finite number"):
            ScheduleEntry(cron="* * * * *", **{field: float("nan")})

    @pytest.mark.parametrize("value", [1.5, 2.0, True, "1.5"])
    def test_agrees_with_the_cloudformation_boundary(self, value):
        """#561 already rejected these at `Custom::ZaeLimiterLimits`; the direct
        API and the YAML manifest did not. The two boundaries now agree."""
        from zae_limiter_provisioner.handler import _coerce_int

        with pytest.raises(ValueError):
            _coerce_int(value, "Capacity")
        with pytest.raises(ValueError):
            ScheduleEntry(cron="* * * * *", capacity=value)

    def test_the_one_deliberate_divergence_is_the_cfn_string(self):
        """`_coerce_int` parses `"5"` because CloudFormation delivers every
        property as a string. That concession belongs to that boundary only —
        the Python field is `int`, so a string is not an integer here."""
        from zae_limiter_provisioner.handler import _coerce_int

        assert _coerce_int("5", "Capacity") == 5
        with pytest.raises(ValueError, match="whole number"):
            ScheduleEntry(cron="* * * * *", capacity="5")

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    @pytest.mark.parametrize("value", [1, 7, 1000, 10**9, 1.5, 2.0, True, False, 0, -1, "5"])
    def test_every_constructible_entry_stays_in_integer_milli_units(self, field, value):
        """`entry_params` multiplies the absolute by 1000 with no int conversion,
        so a float field produced a float milli-unit (`2500.0`) in the in-memory
        path before storage came into it at all. Constructibility is the gate."""
        try:
            entry = ScheduleEntry(cron="* * * * *", **{field: value})
        except ValueError:
            return  # rejected at the gate; nothing downstream ever sees it
        cp, ra, rp = effective_params(1_000, 1_000, 60_000, (entry,), 1_768_000_000_000)
        assert all(isinstance(v, int) and not isinstance(v, bool) for v in (cp, ra, rp))

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

    def test_scaling_a_quota_does_not_invent_a_drip(self):
        """A quota has `refill_amount = 0` by ADR-137; the floor must not raise it.

        Flooring the *scaled* rate at 1 gives a quota a phantom 1-millitoken drip
        (#556) — a rate the limit is defined not to have. The floor is conditioned
        on the base rate instead.
        """
        sched = (ScheduleEntry(cron="* * * * *", scale=0.5),)
        assert effective_params(10_000_000, 0, 60_000, sched, TUE_1400) == (5_000_000, 0, 60_000)

    @pytest.mark.parametrize("scale", [0.5, 2.0, 0.0000001])
    def test_a_quota_stays_a_quota_at_every_scale(self, scale):
        """Including a boost window and a scale small enough to floor a live rate."""
        sched = (ScheduleEntry(cron="* * * * *", scale=scale),)
        assert effective_params(10_000_000, 0, 60_000, sched, TUE_1400)[1] == 0

    def test_the_floor_still_protects_a_limit_that_really_drips(self):
        """The #556 guard must key on the base rate, not on "is the result zero?".

        Keying on the result would drop a live drip to zero whenever the scale
        truncated it away, which is the thing the floor exists to prevent.
        """
        sched = (ScheduleEntry(cron="* * * * *", scale=0.0000001),)
        assert effective_params(10_000_000, 1, 60_000, sched, TUE_1400)[1] == 1

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


class TestScheduleEntryReset:
    """A reset entry names an instant; it overrides no parameters (§3.6).

    Kept at the ``ScheduleEntry`` level so this module stays free of any
    ``models`` import, mirroring ``schedule.py``'s own one-way dependency.
    The ``Limit`` half lives in ``test_models.py``.
    """

    def test_carries_cron_and_tz_only(self):
        e = ScheduleEntry.reset("0 0 * * *", "America/New_York")
        assert (e.cron, e.tz) == ("0 0 * * *", "America/New_York")
        assert (e.scale, e.capacity, e.refill_amount, e.refill_period_seconds) == (
            None,
            None,
            None,
            None,
        )
        assert e._reset is True

    def test_timezone_defaults_to_utc(self):
        assert ScheduleEntry.reset("0 0 * * *").tz == "UTC"

    def test_an_ordinary_entry_is_not_a_reset(self):
        """The flag is opt-in, so every merged call site keeps its meaning."""
        assert ScheduleEntry(cron="* * * * *", scale=0.5)._reset is False

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"scale": 0.5},
            {"capacity": 100},
            {"refill_amount": 10},
            {"refill_period_seconds": 30},
        ],
    )
    def test_rejects_a_modifier_on_a_reset_entry(self, kwargs):
        """A modifier on a reset is a category error, not a harmless extra."""
        with pytest.raises(ValueError, match="reset"):
            ScheduleEntry(cron="0 0 * * *", _reset=True, **kwargs)

    def test_names_every_modifier_it_rejected(self):
        with pytest.raises(ValueError, match=r"capacity.*scale|scale.*capacity"):
            ScheduleEntry(cron="0 0 * * *", _reset=True, scale=0.5, capacity=100)

    def test_a_bare_entry_is_still_invalid_for_the_params_tuple(self):
        """The same cron is legal as a reset and illegal as a param override."""
        with pytest.raises(ValueError, match="exactly one"):
            ScheduleEntry(cron="0 0 * * *")

    @pytest.mark.parametrize("expr", ["nonsense", "* * L * *", "0 0 0 * * *"])
    def test_a_reset_still_validates_its_cron(self, expr):
        """The reset branch must not short-circuit past ``parse_cron``: a
        schedule that stores must be a schedule that evaluates (§3.1), and
        six fields are rejected exactly as they are for a param entry."""
        with pytest.raises(ValueError):
            ScheduleEntry.reset(expr)

    def test_a_reset_still_validates_its_timezone(self):
        with pytest.raises(ValueError, match="timezone"):
            ScheduleEntry.reset("0 0 * * *", "Mars/Olympus_Mons")

    def test_is_frozen_and_hashable(self):
        e = ScheduleEntry.reset("0 0 * * *")
        with pytest.raises(Exception):
            e.cron = "x"  # type: ignore[misc]
        assert hash(e)


class TestMagnitudeBounds:
    """A finite but astronomical modifier is rejected at construction (#570).

    #564 closed the non-finite hole and #569 the non-integral one; neither
    reaches this third member of the family. ``math.isfinite(1e300)`` is True
    and ``1e300 > 0``, so the value clears ``ScheduleEntry`` untouched and dies
    somewhere else entirely — ``OverflowError`` inside ``encode`` above
    ~``1.8e305``, ``OverflowError`` inside ``effective_params`` (on the acquire
    slow path) from ``1e303``, and, quietest of all, nowhere at all below
    ``1e33``, where it merely encodes a 35-character token and enforces a limit
    nobody meant.
    """

    @pytest.mark.parametrize("scale", [1e7, 1e30, 1e33, 1e120, 1e302, 1e305, 1e306])
    def test_rejects_an_astronomical_scale(self, scale):
        with pytest.raises(ValueError, match="scale must be at most"):
            ScheduleEntry(cron="* * * * *", scale=scale)

    def test_the_message_names_the_field_the_value_and_the_bound(self):
        with pytest.raises(ValueError) as exc:
            ScheduleEntry(cron="* * * * *", scale=1e300)
        message = str(exc.value)
        assert "scale" in message
        assert "1e+300" in message
        assert str(MAX_SCALE) in message

    @pytest.mark.parametrize("scale", [1e3, 1e30, 1e33, 1e120, 1e302, 1e305, 1e306])
    def test_every_swept_scale_either_fails_or_round_trips(self, scale):
        """The acceptance sweep: construction rejects it, or storage survives it.

        The band that raises *nowhere* is the point — `1e30` encodes, decodes
        and enforces, so "does Python raise" was never the right test.
        """
        try:
            entry = ScheduleEntry(cron="* * * * *", scale=scale)
        except ValueError:
            return
        compact, tz = encode((entry,))
        assert decode(compact, tz or "UTC")

    @pytest.mark.parametrize(
        ("field", "bound"),
        [
            ("capacity", MAX_TOKENS),
            ("refill_amount", MAX_TOKENS),
            ("refill_period_seconds", MAX_PERIOD_SECONDS),
        ],
    )
    def test_rejects_an_astronomical_absolute(self, field, bound):
        """The absolutes are Python ints, so they never saturate to ``inf`` —
        they produce an unbounded digit string in `sched` and a `decimal`
        rejection from inside boto3 at write time instead."""
        with pytest.raises(ValueError, match=f"{field} must be at most"):
            ScheduleEntry(cron="* * * * *", **{field: bound + 1})

    @pytest.mark.parametrize(
        ("field", "bound"),
        [
            ("scale", MAX_SCALE),
            ("capacity", MAX_TOKENS),
            ("refill_amount", MAX_TOKENS),
            ("refill_period_seconds", MAX_PERIOD_SECONDS),
        ],
    )
    def test_the_bound_itself_is_accepted(self, field, bound):
        """Inclusive: the constant names the largest *accepted* value."""
        assert getattr(ScheduleEntry(cron="* * * * *", **{field: bound}), field) == bound

    def test_non_finite_is_reported_before_magnitude(self):
        with pytest.raises(ValueError, match="scale must be a finite number"):
            ScheduleEntry(cron="* * * * *", scale=float("inf"))

    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period_seconds"])
    def test_non_integral_is_reported_before_magnitude(self, field):
        with pytest.raises(ValueError, match=f"{field} must be a whole number"):
            ScheduleEntry(cron="* * * * *", **{field: 1e300})

    def test_negative_is_reported_as_positivity_not_magnitude(self):
        """Ordering: the two ends of one range check, and the low end is first
        so a negative value keeps the message #564/#569 callers already match."""
        with pytest.raises(ValueError, match="scale must be positive"):
            ScheduleEntry(cron="* * * * *", scale=-1e300)

    def test_effective_params_cannot_overflow_for_any_constructible_entry(self):
        """The property that makes the acquire path safe by construction.

        Taken against the largest base DynamoDB can store at all, which is
        further than any `Limit` this build will now construct.
        """
        largest_storable_base = 10**38 - 1
        for entry in (
            ScheduleEntry(cron="* * * * *", scale=MAX_SCALE),
            ScheduleEntry(cron="* * * * *", capacity=MAX_TOKENS),
            ScheduleEntry(cron="* * * * *", refill_amount=MAX_TOKENS),
            ScheduleEntry(cron="* * * * *", refill_period_seconds=MAX_PERIOD_SECONDS),
        ):
            effective_params(
                largest_storable_base,
                largest_storable_base,
                MAX_PERIOD_SECONDS * 1000,
                (entry,),
                0,
            )

    def test_the_scaled_result_of_a_permitted_base_is_storable(self):
        """DynamoDB Numbers carry 38 significant digits and boto3 raises from the
        `decimal` context rather than sending the request, so the real boundary
        is the serializer, not a Python exception."""
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        base_milli = MAX_TOKENS * 1000
        for entry in (
            ScheduleEntry(cron="* * * * *", scale=MAX_SCALE),
            ScheduleEntry(cron="* * * * *", capacity=MAX_TOKENS, refill_amount=MAX_TOKENS),
        ):
            cp, ra, rp = effective_params(
                base_milli, base_milli, MAX_PERIOD_SECONDS * 1000, (entry,), 0
            )
            for value in (cp, ra, rp):
                serializer.serialize(value)

    def test_the_derived_ceiling_is_where_boto3_actually_stops(self):
        """Pins the derivation itself: `MAX_STORED_MILLI` is not a preference."""
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        serializer.serialize(MAX_STORED_MILLI)
        with pytest.raises(Exception):
            serializer.serialize(MAX_STORED_MILLI + 1)

    def test_the_worst_constructible_product_stays_inside_the_ceiling(self):
        """The per-field ceilings are chosen so no product of them can reach it."""
        assert MAX_TOKENS * 1000 * MAX_SCALE <= MAX_STORED_MILLI


class TestMagnitudeBoundsAtTheManifestAndCloudFormationEntrances:
    """#570 must be reported before anything is written, at every entrance."""

    def test_the_yaml_manifest_reports_it_as_a_schedule_entry_error(self):
        from zae_limiter_provisioner.manifest import _parse_entries

        with pytest.raises(ValueError, match=r"schedule\[0\]: scale must be at most"):
            _parse_entries([{"cron": "* * * * *", "scale": 1e300}], key="schedule", reset=False)

    def test_the_cloudformation_entrance_rejects_a_stringified_scale(self):
        """`_coerce_float` (#561) checks type and finiteness, never magnitude, so
        the bound has to be the one `ScheduleEntry` applies underneath it."""
        from zae_limiter_provisioner.handler import _coerce_float

        assert _coerce_float("1e300", "Scale") == 1e300
        with pytest.raises(ValueError, match="scale must be at most"):
            ScheduleEntry(cron="* * * * *", scale=_coerce_float("1e300", "Scale"))
