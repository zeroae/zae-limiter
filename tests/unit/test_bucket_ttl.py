"""Bucket TTL horizons for dripping limits and quotas (#271, #296, #532).

`schema.calculate_bucket_ttl_seconds()` used to divide unconditionally by
`refill_amount`, which ADR-137 fixes at zero for every quota. These tests pin
the replacement rule (time-to-fill for a limit that drips, the reset *period*
for a quota, a single `max` across both) and the ADR-136 boundary that decides
which buckets reach the formula at all.
"""

from datetime import timedelta

import pytest

from zae_limiter import RateLimiter, schema
from zae_limiter.models import Limit
from zae_limiter.repository import Repository
from zae_limiter.schedule import ScheduleEntry

DAY = 86_400
HOUR = 3_600
WEEK = 7 * DAY
MONTH = 31 * DAY
YEAR = 366 * DAY


def _unrecoverable_limit() -> Limit:
    """A limit that neither drips nor resets, built past `__post_init__`.

    ADR-137 makes this unconstructible through every public path, so the only
    way to exercise the defensive branch is to bypass validation the way
    `Limit._carrier` does.
    """
    obj = object.__new__(Limit)
    for field, value in (
        ("name", "broken"),
        ("capacity", 100),
        ("refill_amount", 0),
        ("refill_period_seconds", 60),
        ("schedule", ()),
        ("reset_schedule", ()),
        ("reset_after", None),
    ):
        object.__setattr__(obj, field, value)
    return obj


class TestQuotaTtlHorizon:
    """A quota's horizon is its reset period, not time-to-fill (#532)."""

    def test_daily_quota_no_longer_raises_zero_division(self):
        # The #532 reproducer verbatim.
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert schema.calculate_bucket_ttl(0, [quota], 7) == DAY * 7

    @pytest.mark.parametrize(
        ("cron", "period"),
        [
            ("* * * * *", 60),  # every minute
            ("0 * * * *", HOUR),  # hourly
            ("0 0 * * *", DAY),  # daily at midnight
            ("30 6 * * *", DAY),  # daily, off-midnight
            ("0 0 * * MON", WEEK),  # weekly
            ("0 0 1 * *", MONTH),  # monthly
            ("0 0 1 1 *", YEAR),  # annually
        ],
    )
    def test_horizon_tracks_the_reset_cadence(self, cron, period):
        # The whole point of the rule: a monthly quota must not get a daily
        # bucket TTL. A constant, or the multiplier alone, fails every row.
        quota = Limit.quota("q", 1_000, cron=cron)
        assert schema.calculate_bucket_ttl_seconds([quota], 7) == period * 7

    def test_horizon_scales_with_the_multiplier(self):
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert schema.calculate_bucket_ttl_seconds([quota], 1) == DAY
        assert schema.calculate_bucket_ttl_seconds([quota], 14) == DAY * 14

    def test_horizon_ignores_capacity(self):
        # Time-to-fill scales with capacity; a reset period does not. A "reuse
        # the old formula with refill_amount substituted" fix fails here.
        small = Limit.quota("q", 10, cron="0 0 * * *")
        huge = Limit.quota("q", 10_000_000, cron="0 0 * * *")
        assert schema.calculate_bucket_ttl_seconds([small], 7) == DAY * 7
        assert schema.calculate_bucket_ttl_seconds([huge], 7) == DAY * 7

    def test_horizon_ignores_the_inert_refill_period(self):
        # `Limit.quota` parks `refill_period_seconds` at the inert
        # `_QUOTA_REFILL_PERIOD_SECONDS = 1`. Multiplying by it would give 7.
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert quota.refill_period_seconds == 1
        assert schema.calculate_bucket_ttl_seconds([quota], 7) == DAY * 7

    def test_tightest_reset_entry_wins(self):
        # The balance is restored by whichever entry fires first, so the
        # sharpest correct upper bound is the smallest cycle, not the largest.
        quota = Limit.quota("q", 1_000, cron="0 0 1 * *").with_reset_schedule(
            (
                ScheduleEntry.reset(cron="0 0 1 * *"),  # monthly
                ScheduleEntry.reset(cron="0 0 * * *"),  # daily
            )
        )
        assert schema.calculate_bucket_ttl_seconds([quota], 7) == DAY * 7

    def test_timezone_does_not_change_the_period(self):
        utc = Limit.quota("q", 1_000, cron="0 0 * * *")
        kolkata = Limit.quota("q", 1_000, cron="0 0 * * *", tz="Asia/Kolkata")
        assert schema.calculate_bucket_ttl_seconds([utc], 7) == schema.calculate_bucket_ttl_seconds(
            [kolkata], 7
        )


class TestDurationWindowTtlHorizon:
    """A `reset_after` quota's horizon is the window itself (ADR-139).

    The other spelling of the reset half of ADR-137: no `reset_schedule`
    means the pre-existing `_recovery_seconds` branch (`min()` over
    `limit.reset_schedule`) had nothing to scan and raised
    `ValueError: min() iterable argument is empty` for every session quota
    that reached a resource- or system-level TTL calculation — reachable as
    soon as the provisioner's `_decode_limits` (or a direct `set_limits()`
    call at a level that expires) started round-tripping `reset_after`.
    """

    def test_recovery_horizon_of_a_duration_quota_is_its_window(self):
        # `_recovery_seconds` exactly, not the multiplied TTL: pins that the
        # window is the horizon with no rounding-up ladder and no clock,
        # unlike the calendar branch which rounds a monthly pattern to 31 days.
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        assert schema._recovery_seconds(window) == 18_000.0

    def test_the_window_length_is_the_horizon(self):
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        assert schema.calculate_bucket_ttl_seconds([window], 7) == 5 * HOUR * 7

    def test_horizon_ignores_capacity(self):
        small = Limit.quota("q", 10, reset_after=timedelta(minutes=1))
        huge = Limit.quota("q", 10_000_000, reset_after=timedelta(minutes=1))
        assert schema.calculate_bucket_ttl_seconds([small], 7) == 60 * 7
        assert schema.calculate_bucket_ttl_seconds([huge], 7) == 60 * 7

    def test_mixed_item_max_spans_a_drip_and_a_duration_quota(self):
        drip = Limit.per_minute("rpm", 100)  # 60s
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))  # 18000s
        assert schema.calculate_bucket_ttl_seconds([drip, window], 7) == 5 * HOUR * 7

    def test_a_calendar_quota_beside_a_duration_quota_takes_the_slower(self):
        calendar = Limit.quota("rpd", 10_000, cron="0 0 * * *")  # DAY
        window = Limit.quota("session", 10_000, reset_after=timedelta(hours=1))  # HOUR
        assert schema.calculate_bucket_ttl_seconds([calendar, window], 7) == DAY * 7

    def test_mixed_item_takes_the_max_across_both_quota_spellings(self):
        # Three shapes at once: a drip (6000s time-to-fill), a duration-window
        # quota (18000s = reset_after itself), and a calendar quota (86400s =
        # the daily reset period). The daily quota wins.
        slow = Limit.custom("slow", capacity=1000, refill_amount=10, refill_period_seconds=60)
        session = Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        daily = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert schema.calculate_bucket_ttl_seconds([slow, session, daily], multiplier=1) == 86_400


class TestMixedBucketTakesTheMax:
    """One `max` spans both shapes on a composite item (ADR-114)."""

    def test_quota_outlasts_a_fast_drip(self):
        drip = Limit.per_minute("rpm", 100)  # time-to-fill 60s
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")  # period 86400s
        assert schema.calculate_bucket_ttl_seconds([drip], 7) == 420
        assert schema.calculate_bucket_ttl_seconds([drip, quota], 7) == DAY * 7

    def test_slow_drip_outlasts_a_quota(self):
        # The direction that kills "just always use the quota branch": a
        # capacity=1000/refill=10-per-minute limit takes 6000s to refill,
        # which is longer than an hourly quota's 3600s period.
        slow = Limit.custom("t", capacity=1000, refill_amount=10, refill_period_seconds=60)
        quota = Limit.quota("q", 1_000, cron="0 * * * *")
        assert schema.calculate_bucket_ttl_seconds([slow], 7) == 42_000
        assert schema.calculate_bucket_ttl_seconds([quota], 7) == HOUR * 7
        assert schema.calculate_bucket_ttl_seconds([slow, quota], 7) == 42_000

    def test_result_is_order_independent(self):
        drip = Limit.per_minute("rpm", 100)
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert schema.calculate_bucket_ttl_seconds([drip, quota], 7) == (
            schema.calculate_bucket_ttl_seconds([quota, drip], 7)
        )


class TestDrippingLimitsUnchanged:
    """ADR-136's time-to-fill formula is untouched for anything that drips."""

    def test_per_minute_limit(self):
        assert schema.calculate_bucket_ttl_seconds([Limit.per_minute("rpm", 100)], 7) == 420

    def test_slow_refill_limit(self):
        slow = Limit.custom("t", capacity=1000, refill_amount=10, refill_period_seconds=60)
        assert schema.calculate_bucket_ttl_seconds([slow], 7) == 42_000

    def test_multiplier_zero_disables_ttl(self):
        assert schema.calculate_bucket_ttl_seconds([Limit.per_minute("rpm", 100)], 0) is None

    def test_empty_limits_returns_none(self):
        assert schema.calculate_bucket_ttl_seconds([], 7) is None

    def test_multiplier_zero_short_circuits_before_any_horizon(self):
        # The early return must come first, or the defensive raise below would
        # fire for a caller that asked for no TTL at all.
        assert schema.calculate_bucket_ttl_seconds([_unrecoverable_limit()], 0) is None

    def test_calculate_bucket_ttl_adds_now(self):
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert schema.calculate_bucket_ttl(5_000, [quota], 7) == 5 + DAY * 7


class TestAbsoluteScheduleWindows:
    """A window that overrides parameters outright lengthens the horizon (#557).

    `_recovery_seconds` read a dripping limit's time-to-fill off its **base**
    parameters, so an absolute `ScheduleEntry` that lowers `refill_amount`
    (or raises `capacity`, or stretches `refill_period_seconds`) recovered far
    more slowly inside its window than the TTL allowed for. The bucket was
    swept while still in debt and recreated at full capacity — the
    over-admission ADR-136 exempts custom-configured buckets from TTL to avoid.

    The horizon is the worst case across the base parameters **and** every
    window, since the function holds no clock and cannot know which window the
    expiry will land in. Rounding up is the safe direction (see
    `calculate_bucket_ttl_seconds`).
    """

    def test_night_window_governs_the_horizon(self):
        # The #557 reproducer verbatim: 60 tokens at 60/min fills in 60s, but
        # the night window drips 1/min, so it needs 3600s.
        limit = Limit.per_minute("rpm", 60).with_schedule(
            (ScheduleEntry(cron="0 0-6 * * *", refill_amount=1),)
        )
        assert schema.calculate_bucket_ttl_seconds([limit], 7) == 3_600 * 7  # 25_200

    def test_unscheduled_limit_is_untouched(self):
        # The overwhelming majority. A schedule-shaped fix must cost them nothing.
        assert schema.calculate_bucket_ttl_seconds([Limit.per_minute("rpm", 60)], 7) == 420

    def test_scale_window_preserves_time_to_fill(self):
        # §1.1 scales capacity and refill together, so the horizon is unchanged.
        # This is the row that proves the walk is not merely "always bigger".
        scaled = Limit.per_minute("rpm", 60).with_schedule(
            (ScheduleEntry(cron="0 0-6 * * *", scale=0.5),)
        )
        assert schema.calculate_bucket_ttl_seconds([scaled], 7) == 420

    def test_absolute_capacity_raise_lengthens_the_horizon(self):
        # Ten times the ceiling at the same drip is ten times the time-to-fill.
        limit = Limit.per_minute("rpm", 60).with_schedule(
            (ScheduleEntry(cron="0 9-17 * * *", capacity=600),)
        )
        assert schema.calculate_bucket_ttl_seconds([limit], 7) == 600 * 7

    def test_absolute_period_stretch_lengthens_the_horizon(self):
        # The third absolute field: same capacity and amount, slower clock.
        limit = Limit.per_minute("rpm", 60).with_schedule(
            (ScheduleEntry(cron="0 9-17 * * *", refill_period_seconds=600),)
        )
        assert schema.calculate_bucket_ttl_seconds([limit], 7) == 600 * 7

    def test_a_faster_window_never_shortens_the_horizon(self):
        # The base still applies outside every window, so the max spans both.
        # A "use the window instead of the base" fix fails here.
        limit = Limit.per_minute("rpm", 60).with_schedule(
            (ScheduleEntry(cron="0 9-17 * * *", refill_amount=600),)
        )
        assert schema.calculate_bucket_ttl_seconds([limit], 7) == 420

    def test_widest_window_wins_across_several_entries(self):
        limit = Limit.per_minute("rpm", 60).with_schedule(
            (
                ScheduleEntry(cron="0 9-17 * * *", refill_amount=600),  # 6s
                ScheduleEntry(cron="0 0-6 * * *", refill_amount=1),  # 3600s
                ScheduleEntry(cron="0 7-8 * * *", refill_amount=6),  # 600s
            )
        )
        assert schema.calculate_bucket_ttl_seconds([limit], 7) == 3_600 * 7

    def test_scheduled_quota_still_uses_its_reset_period(self):
        # A quota may carry a parameter schedule: it sets the ceiling the reset
        # restores to. `is_quota` is structural, so the reset period governs and
        # the window walk is never reached — dividing by the quota's zero rate
        # would be #532 all over again.
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *").with_schedule(
            (ScheduleEntry(cron="0 0-6 * * *", scale=0.5),)
        )
        assert quota.refill_amount == 0
        assert schema.calculate_bucket_ttl_seconds([quota], 7) == DAY * 7

    def test_scheduled_quota_with_an_absolute_window_is_not_a_zero_division(self):
        # The nastier half: an absolute entry could hand the walk a rate the
        # quota does not have. The reset still bounds recovery from above.
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *").with_schedule(
            (ScheduleEntry(cron="0 0-6 * * *", capacity=500_000),)
        )
        assert schema.calculate_bucket_ttl_seconds([quota], 7) == DAY * 7

    def test_mixed_item_max_spans_a_scheduled_drip_and_a_quota(self):
        # ADR-114 composite item: the `max` must still see both shapes, with
        # the scheduled window supplying the dripping limit's horizon.
        scheduled = Limit.per_minute("rpm", 60).with_schedule(
            (ScheduleEntry(cron="0 0-6 * * *", refill_amount=1),)
        )  # 3600s
        hourly = Limit.quota("q", 1_000, cron="0 * * * *")  # 3600s
        daily = Limit.quota("rpd", 10_000, cron="0 0 * * *")  # 86400s
        assert schema.calculate_bucket_ttl_seconds([scheduled, hourly], 7) == 3_600 * 7
        assert schema.calculate_bucket_ttl_seconds([scheduled, daily], 7) == DAY * 7

    def test_a_tiny_scale_cannot_collapse_the_rate_to_zero(self):
        # `effective_params` floors a live rate at 1 milli-unit, so the walk
        # must floor identically rather than divide by a truncated zero.
        limit = Limit.custom("t", capacity=1, refill_amount=1, refill_period_seconds=60)
        tiny = limit.with_schedule((ScheduleEntry(cron="0 0-6 * * *", scale=0.0001),))
        assert schema.calculate_bucket_ttl_seconds([tiny], 7) == 420


class TestUnrecoverableLimit:
    """A limit that neither drips nor resets reports itself, not ZeroDivision."""

    def test_raises_value_error_naming_the_limit(self):
        with pytest.raises(ValueError, match="neither drips.*nor resets"):
            schema.calculate_bucket_ttl_seconds([_unrecoverable_limit()], 7)

    def test_is_not_a_zero_division_error(self):
        with pytest.raises(ValueError) as exc:
            schema.calculate_bucket_ttl_seconds([_unrecoverable_limit()], 7)
        assert not isinstance(exc.value, ZeroDivisionError)
        assert "broken" in str(exc.value)

    def test_public_constructors_reject_it(self):
        # The branch above is defensive only because ADR-137 closes every
        # public path to it. If this ever stops raising, the branch becomes
        # live behaviour and needs a decision, not a guard.
        with pytest.raises(ValueError, match="refill_amount=0"):
            Limit.custom("x", capacity=100, refill_amount=0, refill_period_seconds=60)


@pytest.fixture
async def ttl_repo(mock_dynamodb):
    """Moto-backed repository for bucket-TTL write-path tests."""
    repo = Repository(name="test-bucket-ttl", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    # What open() writes: without it a reset_after write is refused (#638).
    await repo._initialize_version_record()
    yield repo
    await repo.close()


async def _bucket_item(repo: Repository, entity_id: str, resource: str) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response.get("Item", {})


async def _acquire_once(repo: Repository, entity_id: str, resource: str, limit_name: str) -> None:
    limiter = RateLimiter(repository=repo)
    async with limiter:
        async with limiter.acquire(entity_id, resource, consume={limit_name: 1}):
            pass


@pytest.mark.asyncio
class TestQuotaReachesTheWritePaths:
    """The three call sites #532 named, exercised through the public API."""

    async def test_resource_level_quota_writes_a_ttl(self, ttl_repo):
        # lease.py `_commit_initial` — the public reproducer from #532.
        await ttl_repo.set_resource_defaults("gpt-4", [Limit.quota("rpd", 1000, cron="0 0 * * *")])
        await _acquire_once(ttl_repo, "u-res", "gpt-4", "rpd")

        item = await _bucket_item(ttl_repo, "u-res", "gpt-4")
        assert "ttl" in item
        # Sensible, not merely present: one daily reset period × the default
        # multiplier, anchored on the item's own refill stamp.
        rf_seconds = int(item["rf"]["N"]) // 1000
        assert int(item["ttl"]["N"]) == rf_seconds + DAY * 7

    async def test_system_level_quota_writes_a_ttl(self, ttl_repo):
        await ttl_repo.set_system_defaults([Limit.quota("rpd", 1000, cron="0 0 1 * *")])
        await _acquire_once(ttl_repo, "u-sys", "api", "rpd")

        item = await _bucket_item(ttl_repo, "u-sys", "api")
        rf_seconds = int(item["rf"]["N"]) // 1000
        # Monthly, so the horizon must be a month — not the daily value above.
        assert int(item["ttl"]["N"]) == rf_seconds + MONTH * 7

    async def test_entity_level_quota_writes_no_ttl(self, ttl_repo):
        # ADR-136 unchanged: entity config means the bucket persists, so the
        # formula is never reached for it.
        await ttl_repo.set_resource_defaults("gpt-4", [Limit.quota("rpd", 1000, cron="0 0 * * *")])
        await ttl_repo.set_limits(
            "u-ent", [Limit.quota("rpd", 50, cron="0 0 * * *")], resource="gpt-4"
        )
        await _acquire_once(ttl_repo, "u-ent", "gpt-4", "rpd")

        assert "ttl" not in await _bucket_item(ttl_repo, "u-ent", "gpt-4")

    async def test_entity_wide_default_quota_writes_no_ttl(self, ttl_repo):
        # The `entity_default` half of ADR-136 (#489).
        await ttl_repo.set_limits("u-def", [Limit.quota("rpd", 50, cron="0 0 * * *")])
        await _acquire_once(ttl_repo, "u-def", "gpt-4", "rpd")

        assert "ttl" not in await _bucket_item(ttl_repo, "u-def", "gpt-4")

    async def test_sync_bucket_params_stamps_a_quota_ttl(self, ttl_repo):
        # repository.py `_sync_bucket_params`, reached by
        # `RateLimiter.delete_limits` -> `reconcile_bucket_to_defaults`: the
        # bucket drops from entity config back onto a resource-level quota, so
        # the TTL is *added* by the fan-out rather than by the acquire.
        await ttl_repo.set_resource_defaults("gpt-4", [Limit.quota("rpd", 1000, cron="0 0 * * *")])
        await ttl_repo.set_limits("u-sync", [Limit.per_minute("rpm", 60)], resource="gpt-4")
        await _acquire_once(ttl_repo, "u-sync", "gpt-4", "rpm")
        assert "ttl" not in await _bucket_item(ttl_repo, "u-sync", "gpt-4")

        limiter = RateLimiter(repository=ttl_repo)
        async with limiter:
            await limiter.delete_limits("u-sync", resource="gpt-4")

        item = await _bucket_item(ttl_repo, "u-sync", "gpt-4")
        assert "ttl" in item
        assert "b_rpm_cp" not in item  # the stale entity limit was removed
        # The fan-out stamps from its own clock, so bound rather than pin it.
        now_seconds = ttl_repo._now_ms() // 1000
        assert now_seconds + DAY * 7 - 60 <= int(item["ttl"]["N"]) <= now_seconds + DAY * 7 + 60

    async def test_sync_bucket_params_stamps_a_duration_window_ttl(self, ttl_repo):
        # Task 9 (#626): the param sync's TTL branch reconstructs a `Limit`
        # from the resolved config, and a session quota round-trips through
        # `reset_after` rather than `reset_schedule` — the #532-shaped crash
        # this pins is `ValueError: min() iterable argument is empty` out of
        # `schema._recovery_seconds`, not `ZeroDivisionError`.
        window = Limit.quota("session", 1000, reset_after=timedelta(hours=1))
        await ttl_repo.set_resource_defaults("gpt-4", [window])
        await ttl_repo.set_limits("u-sync2", [Limit.per_minute("rpm", 60)], resource="gpt-4")
        await _acquire_once(ttl_repo, "u-sync2", "gpt-4", "rpm")
        assert "ttl" not in await _bucket_item(ttl_repo, "u-sync2", "gpt-4")

        limiter = RateLimiter(repository=ttl_repo)
        async with limiter:
            await limiter.delete_limits("u-sync2", resource="gpt-4")

        item = await _bucket_item(ttl_repo, "u-sync2", "gpt-4")
        assert "ttl" in item
        now_seconds = ttl_repo._now_ms() // 1000
        assert now_seconds + HOUR * 7 - 60 <= int(item["ttl"]["N"]) <= now_seconds + HOUR * 7 + 60
