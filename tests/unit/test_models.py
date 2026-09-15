"""Tests for models."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from zae_limiter import (
    AuditAction,
    AuditEvent,
    Entity,
    InvalidIdentifierError,
    InvalidNameError,
    Limit,
    LimiterInfo,
    LimitName,
    StackOptions,
    ValidationError,
    models,
)
from zae_limiter.models import BucketState, LimitStatus
from zae_limiter.schedule import ScheduleEntry

_NY = ZoneInfo("America/New_York")

#: Halve every parameter during New York business hours (#222 §1.1).
BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)

#: Tuesday 2026-09-15, inside and outside ``BUSINESS``. Local wall clock, not
#: UTC: the window's edges fall on New York minutes (#222 §3.2).
TUE_1400 = int(datetime(2026, 9, 15, 14, tzinfo=_NY).timestamp() * 1000)
TUE_0300 = int(datetime(2026, 9, 15, 3, tzinfo=_NY).timestamp() * 1000)

#: Back to the full daily quota at New York midnight (#222 §3.6). Same zone as
#: ``BUSINESS`` so the two tuples can be combined without anticipating the
#: item-level ``sched_tz`` guard.
DAILY_RESET = (ScheduleEntry.reset("0 0 * * *", "America/New_York"),)


def _state(
    *,
    capacity_milli: int = 1_000_000,
    refill_amount_milli: int = 1_000_000,
    refill_period_ms: int = 60_000,
    shard_count: int = 1,
    sched: tuple[ScheduleEntry, ...] = (),
    reset_sched: tuple[ScheduleEntry, ...] = (),
) -> BucketState:
    """A bucket item's stored state: base parameters, never the effective ones."""
    return BucketState(
        entity_id="e1",
        resource="gpt-4",
        limit_name="rpm",
        tokens_milli=0,
        last_refill_ms=1000,
        capacity_milli=capacity_milli,
        refill_amount_milli=refill_amount_milli,
        refill_period_ms=refill_period_ms,
        shard_count=shard_count,
        sched=sched,
        reset_sched=reset_sched,
    )


class TestLimit:
    """Tests for Limit model."""

    def test_per_minute(self):
        """Test per_minute factory."""
        limit = Limit.per_minute("rpm", 100)
        assert limit.name == "rpm"
        assert limit.capacity == 100
        assert limit.refill_amount == 100
        assert limit.refill_period_seconds == 60

    def test_per_hour(self):
        """Test per_hour factory."""
        limit = Limit.per_hour("rph", 1000)
        assert limit.refill_period_seconds == 3600

    def test_per_day(self):
        """Test per_day factory."""
        limit = Limit.per_day("rpd", 10000)
        assert limit.refill_period_seconds == 86400

    def test_per_second(self):
        """Test per_second factory."""
        limit = Limit.per_second("rps", 10)
        assert limit.refill_period_seconds == 1

    def test_custom(self):
        """Test custom limit configuration."""
        limit = Limit.custom(
            name="custom",
            capacity=100,
            refill_amount=50,
            refill_period_seconds=30,
        )
        assert limit.capacity == 100
        assert limit.refill_amount == 50
        assert limit.refill_period_seconds == 30

    def test_refill_rate_property(self):
        """Test refill_rate calculation."""
        limit = Limit.per_minute("rpm", 60)
        assert limit.refill_rate == 1.0  # 1 token per second

    def test_invalid_capacity(self):
        """Test validation of capacity."""
        with pytest.raises(ValueError, match="capacity must be positive"):
            Limit.per_minute("rpm", 0)

    def test_invalid_refill_amount(self):
        """A zero rate is only valid with a reset schedule (ADR-137).

        The message names the pairing, not the field: a caller who wanted a
        calendar allowance has to be pointed at ``Limit.quota``, not told a
        number is out of range.
        """
        with pytest.raises(ValueError, match="reset_schedule"):
            Limit.custom("rpm", capacity=100, refill_amount=0, refill_period_seconds=60)

    def test_invalid_refill_amount_negative(self):
        """Negative is still simply out of range — ADR-137 widened `> 0` to
        `>= 0`, not to "anything"."""
        with pytest.raises(ValueError, match="refill_amount must not be negative"):
            Limit.custom("rpm", capacity=100, refill_amount=-1, refill_period_seconds=60)

    def test_invalid_refill_period_seconds(self):
        """Test validation of refill_period_seconds must be positive."""
        with pytest.raises(ValueError, match="refill_period_seconds must be positive"):
            Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=0)

    def test_invalid_refill_period_seconds_negative(self):
        """Test validation of negative refill_period_seconds."""
        with pytest.raises(ValueError, match="refill_period_seconds must be positive"):
            Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=-1)

    def test_to_dict_from_dict(self):
        """Test serialization round-trip."""
        limit = Limit.per_minute("rpm", 100)
        data = limit.to_dict()
        restored = Limit.from_dict(data)
        assert restored == limit

    def test_frozen(self):
        """Test that Limit is immutable."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(AttributeError):
            limit.capacity = 200

    def test_from_bucket_state(self):
        """Test reconstructing Limit from BucketState."""
        state = BucketState(
            entity_id="e1",
            resource="gpt-4",
            limit_name="rpm",
            tokens_milli=50_000,
            last_refill_ms=1000,
            capacity_milli=100_000,
            refill_amount_milli=100_000,
            refill_period_ms=60_000,
        )
        limit = Limit.from_bucket_state(state)
        assert limit.name == "rpm"
        assert limit.capacity == 100
        assert limit.refill_amount == 100
        assert limit.refill_period_seconds == 60

    def test_from_bucket_state_returns_the_undivided_base(self):
        """Bucket items store the undivided base on every shard, and that is
        what comes back. Narrowing to the shard is ``per_shard``'s job, and
        doing it here as well narrowed twice: ``LeaseEntry.limit`` built on the
        fast path was already divided, and the lease divided it again for every
        status it reported, so a 4-shard bucket quoted ``capacity // 16``."""
        state = _state(shard_count=4)
        limit = Limit.from_bucket_state(state)
        assert (limit.capacity, limit.refill_amount) == (1000, 1000)
        assert limit.refill_period_seconds == 60

    def test_from_bucket_state_carries_the_buckets_schedule(self):
        """The fast path never reads config (#222 §2.1), so the bucket item's
        own ``sched`` is the only schedule a status built from it can quote."""
        state = _state(sched=BUSINESS)
        assert Limit.from_bucket_state(state).schedule == BUSINESS

    def test_from_bucket_state_reconstructs_a_quota_as_a_quota(self):
        """The `max(1, ...)` rate floor and `reset_schedule` move together: a
        quota bucket reconstructed with a floored `refill_amount=1` and no
        reset advertises a one-token drip ADR-137 says does not exist, beside a
        `retry_after_seconds` computed from the calendar edge."""
        state = _state(refill_amount_milli=0, reset_sched=DAILY_RESET)
        limit = Limit.from_bucket_state(state)
        assert limit.refill_amount == 0
        assert limit.reset_schedule == DAILY_RESET
        assert limit.is_quota

    def test_a_quota_survives_per_shard_with_its_reset(self):
        """The narrowing applied on top of it must not reintroduce the drip."""
        state = _state(refill_amount_milli=0, reset_sched=DAILY_RESET, shard_count=4)
        narrowed = Limit.from_bucket_state(state).per_shard(4, TUE_1400)
        assert narrowed.refill_amount == 0
        assert narrowed.reset_schedule == DAILY_RESET

    def test_a_reset_beside_a_positive_rate_keeps_the_old_reading(self):
        """Unconstructible under ADR-137, so it means a corrupt item. Carrying
        the tuple would make `Limit.__post_init__` *raise* from inside a
        rejection path; the floor is kept and the tuple dropped instead."""
        state = _state(refill_amount_milli=1_000_000, reset_sched=DAILY_RESET)
        limit = Limit.from_bucket_state(state)
        assert limit.refill_amount == 1000
        assert limit.reset_schedule == ()

    def test_a_zero_rate_with_no_reset_still_floors(self):
        """The other half: absent a reset there is nothing to make it a legal
        quota, so the floor still applies rather than raising."""
        state = _state(refill_amount_milli=0)
        assert Limit.from_bucket_state(state).refill_amount == 1

    def test_from_bucket_state_clamps_a_sub_token_base(self):
        """``Limit`` validates ``capacity > 0`` and this runs on the rejection
        path, where raising would mask the real error."""
        state = _state(capacity_milli=500, refill_amount_milli=400, refill_period_ms=500)
        limit = Limit.from_bucket_state(state)
        assert (limit.capacity, limit.refill_amount, limit.refill_period_seconds) == (1, 1, 1)

    def test_per_shard_is_identity_for_an_unscheduled_unsharded_limit(self):
        limit = Limit.per_minute("rpm", 100)
        assert limit.per_shard(1, TUE_1400) is limit

    def test_per_shard_divides_capacity_and_refill(self):
        limit = Limit.per_minute("rpm", 1000, burst=2000)
        shard = limit.per_shard(4, TUE_1400)
        assert (shard.capacity, shard.refill_amount) == (500, 250)
        assert (shard.name, shard.refill_period_seconds) == ("rpm", 60)
        assert (limit.capacity, limit.refill_amount) == (2000, 1000), "must not mutate"

    def test_per_shard_floors_a_sub_token_share_to_one(self):
        """``Limit`` is whole-token and validates ``capacity > 0``, so a share
        below one token clamps rather than raising while building a status."""
        shard = Limit.custom("rpd", 5, refill_amount=1, refill_period_seconds=60).per_shard(
            32, TUE_1400
        )
        assert (shard.capacity, shard.refill_amount) == (1, 1)

    def test_per_shard_tolerates_a_zero_or_negative_shard_count(self):
        """A scheduled limit skips the ``shard_count <= 1`` early return, so a
        malformed count reaches the division. Clamp rather than raise
        ``ZeroDivisionError`` from inside a rejection path."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        assert limit.per_shard(0, TUE_1400).capacity == 500
        assert limit.per_shard(-1, TUE_1400).capacity == 500


class TestQuotaFactory:
    """`Limit.quota` is the only shape ADR-137 leaves standing (#222 §3.6).

    A limit drips **or** resets, never both and never neither, so the amount
    and the reset have to arrive in the same call. Every chained spelling dies
    on its intermediate value: `per_day(...).with_reset_schedule(...)` is a
    rate alongside a reset, and `custom(refill_amount=0, ...)` is a zero rate
    with no reset. `__post_init__` runs at construction, so neither survives
    long enough to be repaired.
    """

    def test_quota_sets_the_zero_refill_and_the_reset_together(self):
        q = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert q.capacity == 10_000
        assert q.refill_amount == 0  # ADR-137: a quota does not drip
        assert q.reset_schedule[0].cron == "0 0 * * *"
        assert q.reset_schedule[0].tz == "America/New_York"

    def test_quota_carries_no_parameter_schedule(self):
        """The reset tuple is populated; the parameter tuple is not."""
        assert Limit.quota("rpd", 10_000, cron="0 0 * * *").schedule == ()

    def test_quota_defaults_to_utc(self):
        assert Limit.quota("rpd", 10_000, cron="0 0 * * *").reset_schedule[0].tz == "UTC"

    def test_quota_stores_the_inert_period_constant(self):
        """`refill_period_seconds` is validated positive and a zero rate has no
        meaningful denominator, so the field holds a documented constant that
        reads as "0 per second" rather than a daily rate the limit does not
        have. It is not a `quota()` keyword: a knob that changes nothing is
        worse than a constant."""
        q = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        assert q.refill_period_seconds == models._QUOTA_REFILL_PERIOD_SECONDS
        assert q.refill_rate == 0.0

    def test_quota_validates_its_cron(self):
        """A schedule that stores must be a schedule that evaluates (§3.1)."""
        with pytest.raises(ValueError):
            Limit.quota("rpd", 10_000, cron="nonsense")

    def test_quota_validates_its_timezone(self):
        with pytest.raises(ValueError, match="timezone"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="Mars/Olympus_Mons")

    def test_quota_validates_its_name(self):
        with pytest.raises(InvalidNameError):
            Limit.quota("not a name", 10_000, cron="0 0 * * *")

    def test_quota_validates_its_amount(self):
        """`amount` is the capacity, and a zero ceiling is unadmittable."""
        with pytest.raises(ValueError, match="capacity must be positive"):
            Limit.quota("rpd", 0, cron="0 0 * * *")

    def test_a_zero_refill_without_a_reset_is_rejected(self):
        """ADR-137: never neither — the bucket could never recover."""
        with pytest.raises(ValueError, match="reset_schedule"):
            Limit.custom("rpd", capacity=10_000, refill_amount=0, refill_period_seconds=86_400)

    def test_a_positive_rate_alongside_a_reset_is_rejected(self):
        """ADR-137: never both — the drip returns the allowance a second time."""
        with pytest.raises(ValueError, match="refill_amount"):
            Limit.per_day("rpd", 10_000).with_reset_schedule(
                (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
            )

    def test_the_rejection_names_the_factory_that_works(self):
        """Both halves of the pairing rule point at `Limit.quota`, because a
        caller who hit either one wanted a calendar allowance and there is
        exactly one way to spell it."""
        with pytest.raises(ValueError, match=r"Limit\.quota"):
            Limit.custom("rpd", capacity=10_000, refill_amount=0, refill_period_seconds=86_400)
        with pytest.raises(ValueError, match=r"Limit\.quota"):
            Limit.per_day("rpd", 10_000).with_reset_schedule(DAILY_RESET)

    def test_per_day_stays_a_drip(self):
        """`per_day` is a *rate* and must not grow a reset parameter: 10,000 a
        day at ~7 a minute is a different product from 10,000 at midnight."""
        rate = Limit.per_day("rpd", 10_000)
        assert (rate.refill_amount, rate.refill_period_seconds) == (10_000, 86400)
        assert rate.reset_schedule == ()

    def test_a_quota_may_carry_a_parameter_schedule(self):
        """`with_schedule` never touches `refill_amount`, so the intermediate
        value is already a legal quota. Only the *reset* has to arrive with the
        amount."""
        q = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        assert q.schedule == BUSINESS
        assert q.reset_schedule == DAILY_RESET
        assert q.refill_amount == 0


class TestResetSchedule:
    """`reset_schedule` is a second, independent tuple on `Limit` (#222 §3.6).

    A reset entry names a calendar instant at which the balance goes back to
    the effective capacity. It overrides no parameters, which is exactly why it
    cannot live in `schedule`: that tuple is resolved first-match-wins, so an
    entry supplying nothing would win its window and shadow everything below.
    """

    def test_defaults_to_empty(self):
        """Every dripping limit in existence today has no reset schedule."""
        assert Limit.per_day("rpd", 10_000).reset_schedule == ()

    def test_with_reset_schedule_replaces_one_reset_with_another(self):
        """The surviving use: swap a quota's schedule, never build one."""
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        weekly = (ScheduleEntry.reset("0 0 * * SUN", "America/New_York"),)
        assert quota.with_reset_schedule(weekly).reset_schedule == weekly
        assert quota.reset_schedule == DAILY_RESET, "must not mutate the base"

    def test_with_reset_schedule_leaves_the_parameter_schedule_alone(self):
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        weekly = (ScheduleEntry.reset("0 0 * * SUN", "America/New_York"),)
        assert quota.with_reset_schedule(weekly).schedule == BUSINESS

    def test_clearing_a_quotas_reset_is_rejected(self):
        """`with_reset_schedule(())` on a zero-refill limit leaves a bucket that
        can never recover, so ADR-137 makes it unconstructible rather than
        silently restoring a drip."""
        with pytest.raises(ValueError, match="reset_schedule"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *").with_reset_schedule(())

    def test_attaching_a_reset_to_a_dripping_limit_is_rejected(self):
        """The chained form the pre-ADR-137 API was built around. It is not
        verbose, it is impossible — hence the factory."""
        with pytest.raises(ValueError, match="never both"):
            Limit.per_minute("rpm", 1000).with_reset_schedule(DAILY_RESET)

    def test_an_empty_reset_tuple_on_a_dripping_limit_is_a_no_op(self):
        """Only *clearing an existing* reset is rejected. A drip that never had
        one is already legal and stays legal, so the guard cannot be written as
        "reset_schedule may not be empty"."""
        assert Limit.per_minute("rpm", 1000).with_reset_schedule(()).reset_schedule == ()

    def test_carries_many_entries_in_order(self):
        """Nothing here collapses to a single entry: the tuple is ordered and
        every entry is a separate edge (a midnight reset and a Sunday-noon
        top-up are two resets, not one)."""
        entries = (
            ScheduleEntry.reset("0 0 * * *", "America/New_York"),
            ScheduleEntry.reset("0 12 * * SUN", "America/New_York"),
            ScheduleEntry.reset("30 2 1 JAN,JUL *", "America/New_York"),
        )
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert quota.with_reset_schedule(entries).reset_schedule == entries

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"scale": 0.5},
            {"capacity": 100},
            {"refill_amount": 10},
            {"refill_period_seconds": 30},
        ],
    )
    def test_rejects_a_parameter_entry_in_the_reset_tuple(self, kwargs):
        """A reset overrides no parameters; a modifier on one is a category
        error, and storage (Task 4) has nowhere to put it.

        The entry under test is a *param* entry handed to the reset tuple,
        which is the only way to construct one — `ScheduleEntry.reset()` takes
        no modifiers at all.
        """
        entry = ScheduleEntry(cron="0 0 * * *", **kwargs)
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *")
        with pytest.raises(ValueError, match="reset"):
            quota.with_reset_schedule((entry,))

    def test_rejects_a_reset_entry_in_the_parameter_tuple(self):
        """The dangerous direction, and the one the plan does not name.
        `effective_params` is first-match-wins: a reset entry matches its
        window, supplies no override, returns the base, and silently shadows
        every entry below it for that minute."""
        with pytest.raises(ValueError, match="reset"):
            Limit.per_minute("rpm", 1000).with_schedule(
                (ScheduleEntry.reset("0 0 * * *"), *BUSINESS)
            )

    def test_a_misplaced_entry_is_diagnosed_as_misplaced_not_as_the_pairing(self):
        """Ordering pin. A dripping limit handed a *param* entry in the reset
        tuple violates both rules at once; the structural one is the more
        specific diagnosis and must win, or the operator is told to use
        `Limit.quota` when the real problem is an entry in the wrong tuple."""
        entry = ScheduleEntry(cron="0 0 * * *", scale=0.5)
        with pytest.raises(ValueError, match="takes reset entries only"):
            Limit.per_day("rpd", 10_000).with_reset_schedule((entry,))

    def test_validates_every_reset_entry_not_just_the_first(self):
        """A check written against `reset_schedule[0]` passes this suite
        everywhere else and lets the second entry through."""
        mixed = (ScheduleEntry.reset("0 0 * * *"), ScheduleEntry(cron="0 12 * * *", scale=0.5))
        with pytest.raises(ValueError, match="reset"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *").with_reset_schedule(mixed)

    def test_validates_every_parameter_entry_not_just_the_first(self):
        mixed = (*BUSINESS, ScheduleEntry.reset("0 0 * * *", "America/New_York"))
        with pytest.raises(ValueError, match="reset"):
            Limit.per_minute("rpm", 1000).with_schedule(mixed)

    def test_counts_the_misplaced_entries_in_the_message(self):
        """Two of three, not "an entry" — the operator has to find them."""
        mixed = (
            ScheduleEntry(cron="0 1 * * *", scale=0.5),
            ScheduleEntry.reset("0 0 * * *"),
            ScheduleEntry(cron="0 2 * * *", capacity=5),
        )
        with pytest.raises(ValueError, match="2 of 3"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *").with_reset_schedule(mixed)

    def test_both_tuples_coexist_on_one_limit(self):
        """The parameter schedule sets the ceiling, the reset schedule sets the
        balance to it; a quota may carry both."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        assert limit.schedule == BUSINESS
        assert limit.reset_schedule == DAILY_RESET

    def test_the_reserved_wcu_carrier_has_no_reset_schedule(self):
        """`_carrier` bypasses `__init__`, so it sets both tuples explicitly
        rather than leaning on a class-level default a future
        `field(default_factory=...)` would remove."""
        carrier = Limit._carrier(_state())
        assert carrier.reset_schedule == ()
        assert carrier.schedule == ()


class TestResetScheduleSurvivesNarrowing:
    """`per_shard` clears `schedule` and keeps `reset_schedule`, deliberately.

    `schedule` is cleared because `per_shard` has just applied it and leaving it
    attached invites a second application. Nothing applies `reset_schedule`
    here, so there is nothing to apply twice — and it is the only thing that
    tells a reported status the balance returns in a lump at a calendar
    instant rather than dripping back at `refill_amount` (surface Task 5).
    """

    def test_per_shard_keeps_it_while_dropping_the_parameter_schedule(self):
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        shard = limit.per_shard(4, TUE_1400)
        assert shard.reset_schedule == DAILY_RESET
        assert shard.schedule == ()

    def test_per_shard_still_scales_and_divides_with_both_tuples_set(self):
        """The reset tuple must not perturb the parameter arithmetic: 10,000
        halved by BUSINESS, then quartered by the shard count."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        assert limit.per_shard(4, TUE_1400).capacity == 1250
        assert limit.per_shard(4, TUE_0300).capacity == 2500

    def test_a_reset_only_limit_keeps_it_through_the_division(self):
        """No parameter schedule, so this takes the `replace` path only because
        of the shard count — the branch where a `reset_schedule=()` slip would
        hide."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        shard = limit.per_shard(4, TUE_1400)
        assert shard.reset_schedule == DAILY_RESET
        assert shard.capacity == 2500

    def test_a_reset_only_limit_takes_the_identity_return_when_unsharded(self):
        """A reset schedule alone changes no parameter, so it must not cost the
        unsharded fast return."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert limit.per_shard(1, TUE_1400) is limit

    def test_a_quotas_rate_stays_zero_through_the_division(self):
        """The `max(1, ...)` share floor must not fire on a zero rate (ADR-137).

        Flooring it would invent a drip nobody configured *and* make the result
        unconstructible, since a positive rate alongside the `reset_schedule`
        this method carries through is exactly what validation rejects — so the
        slip raises from inside a rejection path rather than returning a wrong
        number.
        """
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert limit.per_shard(4, TUE_1400).refill_amount == 0

    def test_a_scaled_quotas_rate_stays_zero(self):
        """`effective_params` floors a scaled refill at one *milli*-unit, so the
        guard has to test the base rate rather than the scheduled one. No drip,
        scaled by anything, is still no drip."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        assert limit.per_shard(4, TUE_1400).refill_amount == 0
        assert limit.per_shard(1, TUE_1400).refill_amount == 0

    def test_a_dripping_limit_still_floors_its_share_at_one(self):
        """The zero case is carved out; the floor it was carved out of stays."""
        limit = Limit.per_minute("rpm", 2)
        assert limit.per_shard(32, TUE_1400).refill_amount == 1


class TestResetScheduleSerialisation:
    """`to_dict()` feeds the audit event `details` for all three setters.

    A field it drops makes the audit record for "attached a daily quota reset"
    byte-identical to the record for attaching nothing — the defect core plan
    Task 8 found for `schedule` and fixed there.
    """

    def test_to_dict_emits_standard_cron_under_its_own_key(self):
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert limit.to_dict()["reset_schedule"] == [
            {"cron": "0 0 * * *", "tz": "America/New_York"}
        ]

    def test_to_dict_omits_an_absent_reset_schedule(self):
        """Every existing audit payload stays byte-identical."""
        assert "reset_schedule" not in Limit.per_day("rpd", 10_000).to_dict()

    def test_to_dict_keeps_the_two_tuples_apart(self):
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        payload = limit.to_dict()
        assert payload["schedule"] == [
            {"cron": "* 9-17 * * MON-FRI", "tz": "America/New_York", "scale": 0.5}
        ]
        assert payload["reset_schedule"] == [{"cron": "0 0 * * *", "tz": "America/New_York"}]

    def test_to_dict_emits_every_entry(self):
        entries = (
            ScheduleEntry.reset("0 0 * * *", "America/New_York"),
            ScheduleEntry.reset("0 12 * * SUN", "America/New_York"),
        )
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        payload = quota.with_reset_schedule(entries).to_dict()
        assert [e["cron"] for e in payload["reset_schedule"]] == ["0 0 * * *", "0 12 * * SUN"]

    def test_to_dict_emits_the_zero_rate(self):
        """`refill_amount` is unconditional, so ADR-137's zero reaches storage
        as a zero rather than being omitted and defaulted back to a drip."""
        payload = Limit.quota("rpd", 10_000, cron="0 0 * * *").to_dict()
        assert payload["refill_amount"] == 0

    def test_round_trips_through_from_dict(self):
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert Limit.from_dict(limit.to_dict()) == limit

    def test_from_dict_rebuilds_the_rate_and_the_reset_together(self):
        """ADR-137 makes the round trip atomic: a `from_dict` that restored the
        zero rate but dropped the reset (or the reverse) would not merely
        compare unequal, it would raise."""
        payload = Limit.quota("rpd", 10_000, cron="0 0 * * *").to_dict()
        restored = Limit.from_dict(payload)
        assert (restored.refill_amount, len(restored.reset_schedule)) == (0, 1)

        with pytest.raises(ValueError, match="reset_schedule"):
            Limit.from_dict({**payload, "reset_schedule": []})
        with pytest.raises(ValueError, match="never both"):
            Limit.from_dict({**payload, "refill_amount": 1})

    def test_round_trips_both_tuples_together(self):
        """`from_dict` must build the reset entries through
        `ScheduleEntry.reset`: the ordinary constructor requires exactly one
        modifier and a reset entry has none, so a single shared code path
        raises rather than round-tripping."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        restored = Limit.from_dict(limit.to_dict())
        assert restored == limit
        assert restored.schedule == BUSINESS
        assert restored.reset_schedule == DAILY_RESET

    def test_restored_entries_are_still_reset_entries(self):
        """Equality already covers this, but only because `_reset` is a field.
        Pinned separately so it survives that field becoming non-comparing."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        restored = Limit.from_dict(limit.to_dict())
        assert all(entry._reset for entry in restored.reset_schedule)
        assert restored.with_reset_schedule(restored.reset_schedule).reset_schedule == DAILY_RESET

    def test_survives_a_json_round_trip(self):
        """Audit `details` is serialised to JSON, so anything that is not a
        plain type never reaches the stored event."""
        limit = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")
        assert Limit.from_dict(json.loads(json.dumps(limit.to_dict()))) == limit


class TestScheduledStatusCapacity:
    """A rejection must quote the capacity that actually rejected it (#222 §3.5).

    ``LimitStatus.limit`` is built by ``Limit.per_shard`` on every path that
    reports one, so that is where both narrowings live: the schedule in force
    and the shard's share.
    """

    def test_quotes_the_scheduled_capacity_inside_the_window(self):
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        assert limit.per_shard(1, TUE_1400).capacity == 500
        assert limit.per_shard(1, TUE_0300).capacity == 1000

    def test_scales_the_refill_with_the_capacity(self):
        """Scaling both preserves time-to-fill (#222 §1.1); scaling only the
        capacity would silently make a window's bucket fill twice as fast."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        inside = limit.per_shard(1, TUE_1400)
        assert (inside.capacity, inside.refill_amount) == (500, 500)
        assert inside.refill_period_seconds == 60

    def test_scales_and_divides_together(self):
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        assert limit.per_shard(4, TUE_1400).capacity == 125
        assert limit.per_shard(4, TUE_0300).capacity == 250

    def test_scale_applies_before_shard_division(self):
        """Scale-then-divide, never divide-then-scale (#222 Global Constraints).

        Most ``(capacity, shards, scale)`` triples land both orders on the same
        integer, so this one is chosen rather than guessed. At 1099 tokens over
        32 shards at ``0.99x``:

        * scale first: ``int(1_099_000 * .99) = 1_088_010``; ``// 32 = 34_000``;
          ``// 1000 = 34``
        * divide first: ``1_099_000 // 32 = 34_343``;
          ``int(34_343 * .99) = 33_999``; ``// 1000 = 33``

        The bucket itself refills against
        ``BucketState.effective_capacity_milli``, which scales first, so 33
        would be a status that disagrees with the gate that produced it.
        """
        limit = Limit.per_minute("rpm", 1099).with_schedule(
            (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.99),)
        )
        shard = limit.per_shard(32, TUE_1400)
        assert shard.capacity == 34
        assert shard.refill_amount == 34

    def test_matches_what_the_bucket_enforces(self):
        """The status and the gate must agree to the token: ``try_consume``
        admits against ``effective_capacity_milli``, which floors in
        milli-units, so ``per_shard`` has to floor there too."""
        limit = Limit.per_minute("rpm", 1099).with_schedule(
            (ScheduleEntry(cron="* * * * *", tz="UTC", scale=0.99),)
        )
        state = _state(
            capacity_milli=1_099_000,
            refill_amount_milli=1_099_000,
            shard_count=32,
            sched=limit.schedule,
        )
        assert limit.per_shard(32, TUE_1400).capacity == (
            state.effective_capacity_milli(TUE_1400) // 1000
        )
        assert limit.per_shard(32, TUE_1400).refill_amount == (
            state.effective_refill_amount_milli(TUE_1400) // 1000
        )

    def test_an_unscheduled_limit_is_completely_unaffected(self):
        """Every bucket in existence today is unscheduled."""
        limit = Limit.per_minute("rpm", 1000, burst=2000)
        assert limit.per_shard(1, TUE_1400) is limit
        assert limit.per_shard(1, TUE_0300) is limit
        shard = limit.per_shard(4, TUE_1400)
        assert (shard.capacity, shard.refill_amount, shard.refill_period_seconds) == (500, 250, 60)
        assert shard == limit.per_shard(4, TUE_0300)

    def test_a_scheduled_limit_scales_even_when_unsharded(self):
        """The ``shard_count <= 1`` fast return must not skip the schedule."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        assert limit.per_shard(1, TUE_1400) is not limit
        assert limit.per_shard(1, TUE_1400).capacity == 500

    def test_sub_token_share_clamps_to_one(self):
        """A ``0.5x`` window on a share that is already one token floors to
        zero, and ``Limit`` validates ``capacity > 0`` — raising here would
        raise from inside the rejection path."""
        limit = Limit.custom("rpd", 32, refill_amount=32, refill_period_seconds=60).with_schedule(
            BUSINESS
        )
        assert limit.per_shard(32, TUE_0300).capacity == 1
        assert limit.per_shard(32, TUE_1400).capacity == 1

    def test_an_absolute_window_overrides_the_period(self):
        """``ScheduleEntry`` can set ``refill_period_seconds`` outright, so the
        reported rate must follow the window's denominator, not the base one."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(
            (
                ScheduleEntry(
                    cron="* * * * *",
                    tz="UTC",
                    capacity=2000,
                    refill_amount=400,
                    refill_period_seconds=10,
                ),
            )
        )
        shard = limit.per_shard(4, TUE_1400)
        assert (shard.capacity, shard.refill_amount) == (500, 100)
        assert shard.refill_period_seconds == 10

    def test_the_period_is_never_divided_by_shard_count(self):
        """Shards split the numerator; every shard refills on the same clock."""
        limit = Limit.per_hour("rph", 1000).with_schedule(BUSINESS)
        assert limit.per_shard(32, TUE_1400).refill_period_seconds == 3600
        assert Limit.per_hour("rph", 1000).per_shard(32, TUE_1400).refill_period_seconds == 3600

    def test_the_materialised_limit_carries_no_schedule(self):
        """It is a point-in-time value. Leaving the schedule attached invites a
        second application and reads as "500, which halves to 250"."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        assert limit.per_shard(4, TUE_1400).schedule == ()
        assert limit.schedule == BUSINESS, "must not mutate"

    def test_narrowing_is_idempotent_once_materialised(self):
        """``LeaseEntry.limit`` is narrowed for every status a lease reports, so
        a second narrowing of an already-materialised limit must not re-scale."""
        limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        once = limit.per_shard(1, TUE_1400)
        assert once.per_shard(1, TUE_1400) is once

    def test_a_bucket_state_limit_narrows_exactly_once(self):
        """``LeaseEntry.limit`` comes from ``from_bucket_state`` on the fast path
        and from the resolved config on the slow path, and the lease narrows
        whichever it got. Both provenances must land on the same number."""
        state = _state(shard_count=4, sched=BUSINESS)
        config_limit = Limit.per_minute("rpm", 1000).with_schedule(BUSINESS)
        assert Limit.from_bucket_state(state) == config_limit
        assert (
            Limit.from_bucket_state(state).per_shard(state.shard_count, TUE_1400).capacity
            == config_limit.per_shard(state.shard_count, TUE_1400).capacity
            == 125
        )


#: Shrink the rate far enough that a single shard's share rounds away entirely.
#: ``effective_params`` floors a scaled value at one *milli*-token, so this is
#: as close to "stops dripping" as a parameter schedule can get — and it is a
#: dripping limit throughout, with no ``reset_schedule`` anywhere.
TINY = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.001),)


class TestZeroRefillPredicates:
    """Two predicates for "this limit has no refill", deliberately distinct.

    ADR-137 made ``refill_amount = 0`` the normal state of a quota, so the bare
    ``refill_amount <= 0`` tests scattered through the codebase became a named
    concept — but only after separating two questions a single check conflates:

    * **structural**, :attr:`Limit.is_quota`: does this limit recover at a
      reset edge rather than by dripping? A property of the configuration,
      identical at every instant.
    * **temporal**, :meth:`BucketState.accrues`: is this shard gaining tokens
      *at this instant*? False for a quota, and **also** false for a limit that
      does drip whose scheduled per-shard share has floored to zero.

    The second case predates ADR-137 (#475, GHSA-76rv) and carries no
    ``reset_schedule`` to be recognised by, so a quota-only special case is
    silently wrong there. That is what these tests pin.
    """

    # --- structural: `Limit.is_quota` ------------------------------------

    def test_a_quota_is_one_and_a_dripping_limit_is_not(self):
        assert Limit.quota("rpd", 10_000, cron="0 0 * * *").is_quota is True
        assert Limit.per_minute("rpm", 100).is_quota is False

    def test_it_does_not_move_with_the_clock(self):
        """Structural means structural. ``BUSINESS`` halves every parameter at
        ``TUE_1400`` and nothing at ``TUE_0300``; neither answer may follow."""
        quota = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York").with_schedule(
            BUSINESS
        )
        drip = Limit.per_minute("rpm", 100).with_schedule(BUSINESS)
        for now in (TUE_1400, TUE_0300):
            assert quota.per_shard(4, now).is_quota is True
            assert drip.per_shard(4, now).is_quota is False

    def test_a_dripping_limit_narrowed_to_its_floor_is_still_not_a_quota(self):
        """A reported share floors at one whole token, so the narrowed limit
        never *looks* like a quota — while the bucket underneath it may be
        accruing nothing at all. The temporal tests below take that up."""
        narrowed = Limit.per_minute("rpm", 1).per_shard(32, TUE_1400)
        assert narrowed.is_quota is False
        assert narrowed.refill_amount == 1

    # --- temporal: `BucketState.accrues` ---------------------------------

    def test_an_ordinary_bucket_accrues(self):
        assert _state().accrues(TUE_1400) is True

    def test_a_quota_bucket_never_accrues(self):
        """First source of a zero effective rate: ``ra`` is 0 on the item, so
        the answer is False at every instant rather than inside a window."""
        quota = _state(refill_amount_milli=0)
        assert quota.accrues(TUE_1400) is False
        assert quota.accrues(TUE_0300) is False

    def test_a_dripping_bucket_stops_accruing_inside_a_shrinking_window(self):
        """Second source, and the reason this is not :attr:`Limit.is_quota`.

        ``int(1_000 * 0.001)`` floors to one millitoken, which ``// 8`` takes to
        zero — inside the window only. Same bucket, two instants, two answers,
        so an implementation that ignores ``now_ms`` (or that asks whether the
        limit is a quota) cannot pass this.
        """
        state = _state(refill_amount_milli=1_000, shard_count=8, sched=TINY)
        assert state.effective_refill_amount_milli(TUE_1400) == 0
        assert state.accrues(TUE_1400) is False
        assert state.effective_refill_amount_milli(TUE_0300) == 125
        assert state.accrues(TUE_0300) is True

    def test_a_share_can_floor_away_with_no_schedule_at_all(self):
        """The same second source without any window: a slow limit split very
        wide. Nothing here carries a ``reset_schedule``, so there is nothing a
        structural check could even look at."""
        assert _state(refill_amount_milli=1_000, shard_count=1024).accrues(TUE_1400) is False
        assert _state(refill_amount_milli=1_000, shard_count=2).accrues(TUE_1400) is True

    def test_the_two_predicates_disagree_on_a_floored_share(self):
        """The crux. The limit is not a quota *and* the bucket is not accruing,
        so collapsing the pair into one check gets this row wrong whichever way
        it collapses."""
        limit = Limit.custom("rpm", capacity=5, refill_amount=1, refill_period_seconds=60)
        state = _state(capacity_milli=5_000, refill_amount_milli=1_000, shard_count=1024)
        assert limit.is_quota is False
        assert state.accrues(TUE_1400) is False

    # --- the primitive both are built on ----------------------------------

    def test_is_accrual_rate_is_strictly_positive(self):
        assert models.is_accrual_rate(1) is True
        assert models.is_accrual_rate(0) is False
        # A corrupt stored item, not a constructible limit — but the callers
        # divide, so "not positive" is the question, not "not zero".
        assert models.is_accrual_rate(-1) is False

    def test_a_quotas_retry_rate_falls_through_to_zero(self):
        """``retry_refill_amount_milli`` falls back to the *undivided* rate for
        a floored share, and a quota's undivided rate is 0 too — which is what
        routes ``calculate_retry_after`` to the reset edge instead of a rate
        (#530). Pinned because the fallback is what keeps a floored share from
        ever reaching that branch."""
        assert _state(refill_amount_milli=0).retry_refill_amount_milli(TUE_1400) == 0
        assert (
            _state(refill_amount_milli=1_000, shard_count=1024).retry_refill_amount_milli(TUE_1400)
            == 1_000
        )


class TestEntity:
    """Tests for Entity model."""

    def test_parent_entity(self):
        """Test parent entity (no parent_id)."""
        entity = Entity(id="proj-1", name="Project 1")
        assert entity.is_parent is True
        assert entity.is_child is False

    def test_child_entity(self):
        """Test child entity (has parent_id)."""
        entity = Entity(id="key-1", name="Key 1", parent_id="proj-1")
        assert entity.is_parent is False
        assert entity.is_child is True

    def test_default_metadata(self):
        """Test default metadata is empty dict."""
        entity = Entity(id="test")
        assert entity.metadata == {}


class TestLimitName:
    """Tests for LimitName constants."""

    def test_constants(self):
        """Test limit name constants."""
        assert LimitName.RPM == "rpm"
        assert LimitName.TPM == "tpm"
        assert LimitName.RPH == "rph"
        assert LimitName.TPH == "tph"


class TestStackOptions:
    """Tests for StackOptions model."""

    def test_default_values(self):
        """Test default values match expected defaults."""
        opts = StackOptions()
        assert opts.snapshot_windows == "hourly,daily"
        assert opts.usage_retention_days == 90
        assert opts.enable_aggregator is True
        assert opts.enable_provisioner is True
        assert opts.pitr_recovery_days is None
        assert opts.log_retention_days == 30
        assert opts.lambda_timeout == 60
        assert opts.lambda_memory == 256
        assert opts.enable_alarms is True
        assert opts.alarm_sns_topic is None
        assert opts.lambda_duration_threshold_pct == 80
        assert opts.permission_boundary is None
        assert opts.role_name_format is None
        assert opts.create_iam_roles is False  # Policies by default, roles opt-in

    def test_custom_values(self):
        """Test custom values are preserved."""
        opts = StackOptions(
            snapshot_windows="hourly",
            usage_retention_days=30,
            lambda_timeout=120,
            lambda_memory=512,
            enable_aggregator=False,
        )
        assert opts.snapshot_windows == "hourly"
        assert opts.usage_retention_days == 30
        assert opts.lambda_timeout == 120
        assert opts.lambda_memory == 512
        assert opts.enable_aggregator is False

    def test_invalid_lambda_timeout_too_high(self):
        """Test validation of lambda_timeout upper bound."""
        with pytest.raises(ValueError, match="lambda_timeout must be between 1 and 900"):
            StackOptions(lambda_timeout=1000)

    def test_invalid_lambda_timeout_too_low(self):
        """Test validation of lambda_timeout lower bound."""
        with pytest.raises(ValueError, match="lambda_timeout must be between 1 and 900"):
            StackOptions(lambda_timeout=0)

    def test_invalid_lambda_memory_too_low(self):
        """Test validation of lambda_memory lower bound."""
        with pytest.raises(ValueError, match="lambda_memory must be between 128 and 3008"):
            StackOptions(lambda_memory=100)

    def test_invalid_lambda_memory_too_high(self):
        """Test validation of lambda_memory upper bound."""
        with pytest.raises(ValueError, match="lambda_memory must be between 128 and 3008"):
            StackOptions(lambda_memory=4000)

    def test_invalid_duration_threshold_pct(self):
        """Test validation of lambda_duration_threshold_pct range."""
        with pytest.raises(
            ValueError, match="lambda_duration_threshold_pct must be between 1 and 100"
        ):
            StackOptions(lambda_duration_threshold_pct=0)

    def test_invalid_pitr_recovery_days(self):
        """Test validation of pitr_recovery_days range."""
        with pytest.raises(ValueError, match="pitr_recovery_days must be between 1 and 35"):
            StackOptions(pitr_recovery_days=40)

    def test_invalid_usage_retention_days(self):
        """Test validation of usage_retention_days must be positive."""
        with pytest.raises(ValueError, match="usage_retention_days must be positive"):
            StackOptions(usage_retention_days=0)

    def test_invalid_audit_retention_days(self):
        """Test validation of audit_retention_days must be positive."""
        with pytest.raises(ValueError, match="audit_retention_days must be positive"):
            StackOptions(audit_retention_days=0)

    def test_invalid_audit_retention_days_negative(self):
        """Test validation of negative audit_retention_days."""
        with pytest.raises(ValueError, match="audit_retention_days must be positive"):
            StackOptions(audit_retention_days=-1)

    def test_default_audit_retention_days(self):
        """Test default audit_retention_days is 90."""
        opts = StackOptions()
        assert opts.audit_retention_days == 90

    def test_custom_audit_retention_days(self):
        """Test custom audit_retention_days value."""
        opts = StackOptions(audit_retention_days=30)
        assert opts.audit_retention_days == 30

    def test_invalid_log_retention_days(self):
        """Test validation of log_retention_days must be valid CloudWatch value."""
        with pytest.raises(ValueError, match="log_retention_days must be one of"):
            StackOptions(log_retention_days=15)  # 15 is not a valid CloudWatch value

    def test_valid_log_retention_days(self):
        """Test that valid log_retention_days values are accepted."""
        # Test a few valid values
        valid_values = [1, 3, 5, 7, 14, 30, 60, 90, 365]
        for value in valid_values:
            opts = StackOptions(log_retention_days=value)
            assert opts.log_retention_days == value

    def test_to_parameters(self):
        """Test conversion to stack parameters dict."""
        opts = StackOptions(
            lambda_timeout=60,
            lambda_duration_threshold_pct=80,
        )
        params = opts.to_parameters()

        # Check duration threshold is computed correctly: 60 * 1000 * 0.8 = 48000
        assert params["lambda_duration_threshold"] == "48000"
        assert params["lambda_timeout"] == "60"
        assert params["enable_aggregator"] == "true"
        assert params["enable_provisioner"] == "true"
        assert params["enable_alarms"] == "true"

    def test_to_parameters_provisioner_disabled(self):
        """Test enable_provisioner=False maps to the CFN parameter."""
        opts = StackOptions(enable_provisioner=False)
        params = opts.to_parameters()

        assert params["enable_provisioner"] == "false"

    def test_deploys_lambda_properties_default(self):
        """Both Lambdas are created under the default (create_iam=True) options."""
        opts = StackOptions()

        assert opts.deploys_aggregator_lambda is True
        assert opts.deploys_provisioner_lambda is True

    def test_deploys_lambda_properties_without_iam(self):
        """create_iam=False leaves neither Lambda a role, so neither is created."""
        opts = StackOptions(create_iam=False)

        assert opts.deploys_aggregator_lambda is False
        assert opts.deploys_provisioner_lambda is False

    def test_deploys_aggregator_lambda_with_external_role(self):
        """An external role revives the aggregator under --no-iam, not the provisioner."""
        opts = StackOptions(
            create_iam=False,
            aggregator_role_arn="arn:aws:iam::123456789012:role/external",
        )

        assert opts.deploys_aggregator_lambda is True
        assert opts.deploys_provisioner_lambda is False

    def test_deploys_lambda_properties_when_disabled(self):
        """Explicitly disabling a component wins over IAM being available."""
        opts = StackOptions(enable_aggregator=False, enable_provisioner=False)

        assert opts.deploys_aggregator_lambda is False
        assert opts.deploys_provisioner_lambda is False

    def test_to_parameters_with_optional_fields(self):
        """Test to_parameters with optional fields set."""
        opts = StackOptions(
            pitr_recovery_days=7,
            alarm_sns_topic="arn:aws:sns:us-east-1:123456789012:alerts",
        )
        params = opts.to_parameters()

        assert params["pitr_recovery_days"] == "7"
        assert params["alarm_sns_topic_arn"] == "arn:aws:sns:us-east-1:123456789012:alerts"

    def test_to_parameters_without_optional_fields(self):
        """Test to_parameters without optional fields."""
        opts = StackOptions()
        params = opts.to_parameters()

        # Optional fields should not be in params when None
        assert "pitr_recovery_days" not in params
        assert "alarm_sns_topic_arn" not in params

    def test_frozen(self):
        """Test that StackOptions is immutable."""
        opts = StackOptions()
        with pytest.raises(AttributeError):
            opts.lambda_timeout = 120

    # -------------------------------------------------------------------------
    # Permission Boundary Tests
    # -------------------------------------------------------------------------

    def test_permission_boundary_policy_name(self):
        """Test permission_boundary with just policy name."""
        opts = StackOptions(permission_boundary="MyBoundary")
        params = opts.to_parameters()
        assert params["permission_boundary"] == "MyBoundary"

    def test_permission_boundary_full_arn(self):
        """Test permission_boundary with full ARN."""
        arn = "arn:aws:iam::123456789012:policy/MyBoundary"
        opts = StackOptions(permission_boundary=arn)
        params = opts.to_parameters()
        assert params["permission_boundary"] == arn

    def test_permission_boundary_aws_managed_policy(self):
        """Test permission_boundary with AWS managed policy ARN."""
        arn = "arn:aws:iam::aws:policy/PowerUserAccess"
        opts = StackOptions(permission_boundary=arn)
        params = opts.to_parameters()
        assert params["permission_boundary"] == arn

    def test_permission_boundary_not_in_params_when_none(self):
        """Test permission_boundary is not in params when None."""
        opts = StackOptions()
        params = opts.to_parameters()
        assert "permission_boundary" not in params

    # -------------------------------------------------------------------------
    # Role Name Format Tests (Updated for component-based naming, ADR-116)
    # -------------------------------------------------------------------------

    def test_role_name_format_valid_prefix(self):
        """Test role_name_format with prefix pattern."""
        opts = StackOptions(role_name_format="app-{}")
        assert opts.get_role_name("mytable", "aggr") == "app-mytable-aggr"
        assert opts.get_role_name("mytable", "app") == "app-mytable-app"
        assert opts.get_role_name("mytable", "admin") == "app-mytable-admin"
        assert opts.get_role_name("mytable", "read") == "app-mytable-read"

    def test_role_name_format_prefix_suffix(self):
        """Test role_name_format with both prefix and suffix."""
        opts = StackOptions(role_name_format="pb-{}-PowerUser")
        assert opts.get_role_name("mytable", "aggr") == "pb-mytable-aggr-PowerUser"
        assert opts.get_role_name("mytable", "app") == "pb-mytable-app-PowerUser"

    def test_role_name_format_suffix_only(self):
        """Test role_name_format with suffix only."""
        opts = StackOptions(role_name_format="{}-prod")
        assert opts.get_role_name("mytable", "aggr") == "mytable-aggr-prod"

    def test_role_name_format_no_placeholder(self):
        """Test role_name_format without placeholder raises ValueError."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(role_name_format="my-custom-role")

    def test_role_name_format_multiple_placeholders(self):
        """Test role_name_format with multiple placeholders raises ValueError."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(role_name_format="app-{}-{}-role")

    def test_role_name_format_max_length_55(self):
        """Test role_name_format template up to 55 chars is valid."""
        # 55 chars total: 52 'a's + "-{}" (3 chars) = 55
        template = "a" * 52 + "-{}"  # 55 total
        opts = StackOptions(role_name_format=template)
        assert opts.role_name_format == template

    def test_role_name_format_56_chars_rejected(self):
        """Test role_name_format over 55 chars is rejected."""
        # 56 chars total: 53 'a's + "-{}" (3 chars) = 56
        template = "a" * 53 + "-{}"  # 56 total
        with pytest.raises(ValueError, match="too long"):
            StackOptions(role_name_format=template)

    def test_get_role_name_returns_none_when_format_not_set(self):
        """Test get_role_name returns None when role_name_format is None."""
        opts = StackOptions()
        assert opts.get_role_name("mytable", "aggr") is None

    def test_to_parameters_generates_four_role_names(self):
        """Test to_parameters generates separate role name params for each component."""
        opts = StackOptions(role_name_format="app-{}")
        params = opts.to_parameters(stack_name="mystack")

        assert params["aggregator_role_name"] == "app-mystack-aggr"
        assert params["app_role_name"] == "app-mystack-app"
        assert params["admin_role_name"] == "app-mystack-admin"
        assert params["readonly_role_name"] == "app-mystack-read"
        # Old single role_name param should not be present
        assert "role_name" not in params

    def test_to_parameters_no_role_names_when_format_none(self):
        """Test to_parameters omits role names when format is None."""
        opts = StackOptions()
        params = opts.to_parameters(stack_name="mystack")

        assert "aggregator_role_name" not in params
        assert "app_role_name" not in params
        assert "admin_role_name" not in params
        assert "readonly_role_name" not in params

    def test_role_names_not_in_params_without_stack_name(self):
        """Test role names are not in params when stack_name is not provided."""
        opts = StackOptions(role_name_format="app-{}")
        params = opts.to_parameters()
        assert "aggregator_role_name" not in params
        assert "app_role_name" not in params
        assert "admin_role_name" not in params
        assert "readonly_role_name" not in params

    # -------------------------------------------------------------------------
    # IAM Roles Tests (Issue #132)
    # -------------------------------------------------------------------------

    def test_create_iam_roles_default_false(self):
        """Test create_iam_roles defaults to False (policies always created)."""
        opts = StackOptions()
        assert opts.create_iam_roles is False

    def test_create_iam_roles_can_be_enabled(self):
        """Test create_iam_roles can be set to True."""
        opts = StackOptions(create_iam_roles=True)
        assert opts.create_iam_roles is True

    def test_create_iam_roles_in_to_parameters_enabled(self):
        """Test enable_iam_roles is in params when create_iam_roles is True."""
        opts = StackOptions(create_iam_roles=True)
        params = opts.to_parameters()
        assert params["enable_iam_roles"] == "true"

    def test_create_iam_roles_in_to_parameters_disabled(self):
        """Test enable_iam_roles is in params when create_iam_roles is False."""
        opts = StackOptions(create_iam_roles=False)
        params = opts.to_parameters()
        assert params["enable_iam_roles"] == "false"

    # -------------------------------------------------------------------------
    # ROLE_COMPONENTS Constant Tests (ADR-116)
    # -------------------------------------------------------------------------

    def test_role_components_constant_exists(self):
        """Test ROLE_COMPONENTS constant is defined."""
        from zae_limiter.models import ROLE_COMPONENTS

        assert ROLE_COMPONENTS == ("aggr", "app", "admin", "read")

    def test_role_components_max_length_invariant(self):
        """Test all role components are <= 8 characters (ADR-116 invariant)."""
        from zae_limiter.models import ROLE_COMPONENTS

        for component in ROLE_COMPONENTS:
            assert len(component) <= 8, f"Component '{component}' exceeds 8 chars"

    def test_get_role_name_requires_component(self):
        """Test get_role_name requires component parameter."""
        opts = StackOptions(role_name_format="app-{}")
        # New signature: get_role_name(stack_name, component)
        result = opts.get_role_name("mystack", "aggr")
        assert result == "app-mystack-aggr"

    def test_get_role_name_with_different_components(self):
        """Test get_role_name works with all component types."""
        opts = StackOptions(role_name_format="pb-{}")
        assert opts.get_role_name("mystack", "aggr") == "pb-mystack-aggr"
        assert opts.get_role_name("mystack", "app") == "pb-mystack-app"
        assert opts.get_role_name("mystack", "admin") == "pb-mystack-admin"
        assert opts.get_role_name("mystack", "read") == "pb-mystack-read"

    def test_get_role_name_validates_length(self):
        """Test get_role_name raises ValidationError when exceeding 64 chars."""
        # Format with long prefix that will exceed 64 chars with a long stack name
        opts = StackOptions(role_name_format="very-long-prefix-{}")
        # 19 (prefix) + 50 (stack) + 1 (-) + 5 (admin) = 75 chars > 64
        long_stack = "a" * 50
        with pytest.raises(ValidationError) as exc_info:
            opts.get_role_name(long_stack, "admin")
        assert "exceeds IAM 64-character limit" in str(exc_info.value)

    def test_get_role_name_returns_none_when_format_not_set_with_component(self):
        """Test get_role_name returns None when role_name_format is None."""
        opts = StackOptions()
        assert opts.get_role_name("mytable", "aggr") is None

    # -------------------------------------------------------------------------
    # Policy Name Format Tests
    # -------------------------------------------------------------------------

    def test_policy_name_format_valid_prefix(self):
        """Test valid policy_name_format with prefix."""
        opts = StackOptions(policy_name_format="pb-{}")
        assert opts.policy_name_format == "pb-{}"

    def test_policy_name_format_no_placeholder(self):
        """Test policy_name_format with no placeholder fails."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(policy_name_format="no-placeholder")

    def test_policy_name_format_multiple_placeholders(self):
        """Test policy_name_format with multiple placeholders fails."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(policy_name_format="{}-{}")

    def test_policy_name_format_too_long(self):
        """Test policy_name_format exceeding 120 chars fails.

        Longest component is ns-full (7 chars): 128 - 7 - 1 = 120.
        """
        long_format = "x" * 119 + "{}"  # 121 chars total
        with pytest.raises(ValueError, match="too long"):
            StackOptions(policy_name_format=long_format)

    def test_policy_name_format_max_length_120(self):
        """Test policy_name_format at exactly 120 chars succeeds."""
        max_format = "x" * 118 + "{}"  # exactly 120 chars
        opts = StackOptions(policy_name_format=max_format)
        assert opts.policy_name_format == max_format

    def test_get_policy_name_returns_none_when_format_not_set(self):
        """Test get_policy_name returns None when policy_name_format is None."""
        opts = StackOptions()
        assert opts.get_policy_name("mystack", "app") is None

    def test_get_policy_name_with_format(self):
        """Test get_policy_name returns formatted name."""
        opts = StackOptions(policy_name_format="pb-{}")
        result = opts.get_policy_name("mystack", "app")
        assert result == "pb-mystack-app"

    def test_get_policy_name_validates_length(self):
        """Test get_policy_name raises ValidationError for names > 128 chars."""
        opts = StackOptions(policy_name_format="prefix-{}-suffix")
        long_stack = "x" * 120  # Will exceed 128 chars
        with pytest.raises(ValidationError) as exc_info:
            opts.get_policy_name(long_stack, "admin")
        assert "exceeds IAM 128-character limit" in str(exc_info.value)

    def test_to_parameters_generates_policy_names(self):
        """Test to_parameters generates policy name params when format is set."""
        opts = StackOptions(policy_name_format="pb-{}")
        params = opts.to_parameters(stack_name="mystack")
        # Table-level policies
        assert params["acquire_only_policy_name"] == "pb-mystack-acq"
        assert params["full_access_policy_name"] == "pb-mystack-full"
        assert params["readonly_policy_name"] == "pb-mystack-read"
        # Namespace-scoped policies
        assert params["namespace_acquire_policy_name"] == "pb-mystack-ns-acq"
        assert params["namespace_full_access_policy_name"] == "pb-mystack-ns-full"
        assert params["namespace_readonly_policy_name"] == "pb-mystack-ns-read"

    def test_to_parameters_no_policy_names_when_format_none(self):
        """Test to_parameters excludes policy names when format is None."""
        opts = StackOptions()
        params = opts.to_parameters(stack_name="mystack")
        assert "acquire_only_policy_name" not in params
        assert "full_access_policy_name" not in params
        assert "readonly_policy_name" not in params
        assert "namespace_acquire_policy_name" not in params
        assert "namespace_full_access_policy_name" not in params
        assert "namespace_readonly_policy_name" not in params

    # -------------------------------------------------------------------------
    # create_iam and aggregator_role_arn Tests
    # -------------------------------------------------------------------------

    def test_create_iam_default_true(self):
        """Test create_iam defaults to True."""
        opts = StackOptions()
        assert opts.create_iam is True

    def test_create_iam_false_valid(self):
        """Test create_iam can be set to False."""
        opts = StackOptions(create_iam=False)
        assert opts.create_iam is False

    def test_create_iam_false_with_create_iam_roles_raises(self):
        """Test create_iam=False with create_iam_roles=True raises ValueError."""
        with pytest.raises(
            ValueError, match="create_iam_roles=True cannot be used with create_iam=False"
        ):
            StackOptions(create_iam=False, create_iam_roles=True)

    def test_aggregator_role_arn_default_none(self):
        """Test aggregator_role_arn defaults to None."""
        opts = StackOptions()
        assert opts.aggregator_role_arn is None

    def test_aggregator_role_arn_valid(self):
        """Test valid aggregator_role_arn is accepted."""
        valid_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        assert opts.aggregator_role_arn == valid_arn

    def test_aggregator_role_arn_valid_govcloud(self):
        """Test valid GovCloud aggregator_role_arn is accepted."""
        valid_arn = "arn:aws-us-gov:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        assert opts.aggregator_role_arn == valid_arn

    def test_aggregator_role_arn_valid_china(self):
        """Test valid China region aggregator_role_arn is accepted."""
        valid_arn = "arn:aws-cn:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        assert opts.aggregator_role_arn == valid_arn

    def test_aggregator_role_arn_invalid_raises(self):
        """Test invalid aggregator_role_arn raises ValueError."""
        invalid_arns = [
            "not-an-arn",
            "arn:aws:iam::12345:role/TooShort",  # Account ID too short
            "arn:aws:iam::1234567890123:role/TooLong",  # Account ID too long
            "arn:aws:s3:::bucket",  # Wrong service
            "arn:aws:iam::123456789012:user/NotARole",  # User not role
        ]
        for invalid_arn in invalid_arns:
            with pytest.raises(
                ValueError, match="aggregator_role_arn must be a valid IAM role ARN"
            ):
                StackOptions(aggregator_role_arn=invalid_arn)

    def test_to_parameters_includes_enable_iam_true(self):
        """Test to_parameters includes enable_iam when True."""
        opts = StackOptions(create_iam=True)
        params = opts.to_parameters()
        assert params["enable_iam"] == "true"

    def test_to_parameters_includes_enable_iam_false(self):
        """Test to_parameters includes enable_iam when False."""
        opts = StackOptions(create_iam=False)
        params = opts.to_parameters()
        assert params["enable_iam"] == "false"

    def test_to_parameters_includes_aggregator_role_arn(self):
        """Test to_parameters includes aggregator_role_arn when set."""
        valid_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        params = opts.to_parameters()
        assert params["aggregator_role_arn"] == valid_arn

    def test_to_parameters_excludes_aggregator_role_arn_when_none(self):
        """Test to_parameters excludes aggregator_role_arn when None."""
        opts = StackOptions()
        params = opts.to_parameters()
        assert "aggregator_role_arn" not in params

    def test_create_iam_false_with_aggregator_role_arn_valid(self):
        """Test create_iam=False with aggregator_role_arn is valid."""
        valid_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(create_iam=False, aggregator_role_arn=valid_arn)
        assert opts.create_iam is False
        assert opts.aggregator_role_arn == valid_arn


class TestInputValidation:
    """Tests for input validation security (issue #48)."""

    # -------------------------------------------------------------------------
    # Exception Hierarchy Tests
    # -------------------------------------------------------------------------

    def test_validation_error_inherits_from_base(self):
        """Test ValidationError is in the exception hierarchy."""
        from zae_limiter import ZAELimiterError

        assert issubclass(ValidationError, ZAELimiterError)
        assert issubclass(InvalidIdentifierError, ValidationError)
        assert issubclass(InvalidNameError, ValidationError)

    def test_validation_error_attributes(self):
        """Test ValidationError contains field, value, reason."""
        err = ValidationError("test_field", "bad_value", "test reason")
        assert err.field == "test_field"
        assert err.value == "bad_value"
        assert err.reason == "test reason"
        assert "test_field" in str(err)
        assert "test reason" in str(err)

    def test_validation_error_truncates_long_values(self):
        """Test that long values are truncated in error."""
        long_value = "x" * 100
        err = ValidationError("field", long_value, "too long")
        assert len(err.value) <= 53  # 50 + "..."
        assert err.value.endswith("...")

    # -------------------------------------------------------------------------
    # Limit Name Validation Tests
    # -------------------------------------------------------------------------

    def test_limit_name_valid(self):
        """Test valid limit names are accepted."""
        valid_names = [
            "rpm",
            "tpm",
            "requests",
            "tokens_per_minute",
            "rate-limit",
            "gpt-3.5",  # dots allowed
        ]
        for name in valid_names:
            limit = Limit.per_minute(name, 100)
            assert limit.name == name

    def test_limit_name_rejects_hash(self):
        """Test limit name with # is rejected."""
        with pytest.raises(InvalidNameError) as exc_info:
            Limit.per_minute("rpm#evil", 100)
        assert exc_info.value.field == "name"
        assert "#" in exc_info.value.reason

    def test_limit_name_rejects_empty(self):
        """Test empty limit name is rejected."""
        with pytest.raises(InvalidNameError) as exc_info:
            Limit.per_minute("", 100)
        assert exc_info.value.field == "name"
        assert "empty" in exc_info.value.reason

    def test_limit_name_rejects_too_long(self):
        """Test limit name exceeding max length is rejected."""
        with pytest.raises(InvalidNameError) as exc_info:
            Limit.per_minute("a" * 100, 100)
        assert exc_info.value.field == "name"
        assert "length" in exc_info.value.reason

    def test_limit_name_must_start_with_letter(self):
        """Test limit name starting with number is rejected."""
        with pytest.raises(InvalidNameError) as exc_info:
            Limit.per_minute("123rpm", 100)
        assert exc_info.value.field == "name"
        assert "letter" in exc_info.value.reason

    def test_limit_name_rejects_special_chars(self):
        """Test limit name with special characters is rejected."""
        # Note: dots are allowed (e.g., gpt-3.5)
        invalid_names = ["rpm@test", "rpm:test", "rpm/test", "rpm test"]
        for name in invalid_names:
            with pytest.raises(InvalidNameError):
                Limit.per_minute(name, 100)

    def test_limit_name_rejects_slash(self):
        """Test limit name with / is rejected (slash only allowed for resources)."""
        with pytest.raises(InvalidNameError) as exc_info:
            Limit.per_minute("rpm/tpm", 100)
        assert exc_info.value.field == "name"
        assert "slash" not in exc_info.value.reason  # slash not in allowed chars

    def test_validate_name_rejects_reserved_wcu(self):
        """Test that 'wcu' is rejected as a reserved limit name."""
        with pytest.raises(InvalidNameError, match="reserved"):
            Limit.per_minute("wcu", 100)

    def test_validate_name_allows_normal_names(self):
        """Test that normal limit names still work."""
        from zae_limiter.models import validate_name

        validate_name("rpm", "limit_name")  # should not raise
        validate_name("tpm", "limit_name")  # should not raise

    # -------------------------------------------------------------------------
    # Resource Name Validation Tests
    # -------------------------------------------------------------------------

    def test_resource_name_valid(self):
        """Test valid resource names are accepted."""
        from zae_limiter.models import validate_resource

        valid_names = [
            "api",
            "gpt-4",
            "gpt-3.5-turbo",
            "openai/gpt-4",  # provider/model grouping
            "anthropic/claude-3",
            "anthropic/claude-3/opus",  # nested paths
            "a/b/c/d",  # deep nesting
        ]
        for name in valid_names:
            validate_resource(name)  # Should not raise

    def test_resource_name_with_trailing_slash(self):
        """Test resource name with trailing slash is valid."""
        from zae_limiter.models import validate_resource

        validate_resource("openai/")  # Should not raise

    def test_resource_name_allows_colon(self):
        """Test resource names with colons are accepted (issue #434)."""
        from zae_limiter.models import validate_resource

        valid_names = [
            "llama3:8b",
            "mistral:7b-instruct",
            "anthropic.claude-v2:1",
            "openai/gpt-4:latest",  # colon combined with slash grouping
        ]
        for name in valid_names:
            validate_resource(name)  # Should not raise

    def test_resource_name_rejects_leading_slash(self):
        """Test resource name starting with / is rejected (must start with letter)."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("/gpt-4")
        assert exc_info.value.field == "resource"
        assert "letter" in exc_info.value.reason

    def test_resource_name_rejects_hash(self):
        """Test resource name with # is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("openai#gpt-4")
        assert exc_info.value.field == "resource"
        assert "#" in exc_info.value.reason

    def test_resource_name_rejects_empty(self):
        """Test empty resource name is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("")
        assert exc_info.value.field == "resource"
        assert "empty" in exc_info.value.reason

    def test_resource_name_rejects_too_long(self):
        """Test resource name exceeding max length is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("a" * 100)
        assert exc_info.value.field == "resource"
        assert "length" in exc_info.value.reason

    def test_resource_name_must_start_with_letter(self):
        """Test resource name starting with number is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("123api")
        assert exc_info.value.field == "resource"
        assert "letter" in exc_info.value.reason

    # -------------------------------------------------------------------------
    # Entity Tests (internal model - no __post_init__ validation)
    # -------------------------------------------------------------------------

    def test_entity_valid(self):
        """Test valid Entity is created."""
        entity = Entity(id="user-123", name="Test User")
        assert entity.id == "user-123"

    def test_entity_allows_any_values_direct_construction(self):
        """Test Entity allows any values when constructed directly.

        Entity is used for DynamoDB deserialization. Validation is performed
        in Repository.create_entity() instead of __post_init__ to support
        reading existing data and avoid performance overhead.
        """
        # This should NOT raise - direct construction bypasses validation
        entity = Entity(
            id="user#123",  # Would be invalid in Repository.create_entity
            parent_id="",  # Empty - from DynamoDB deserialization
        )
        assert entity.id == "user#123"

    def test_entity_with_parent(self):
        """Test Entity with parent_id."""
        entity = Entity(id="child-1", parent_id="parent-123")
        assert entity.parent_id == "parent-123"
        assert entity.is_child is True
        assert entity.is_parent is False

    def test_entity_without_parent(self):
        """Test Entity without parent_id (root entity)."""
        entity = Entity(id="root-1", parent_id=None)
        assert entity.parent_id is None
        assert entity.is_parent is True
        assert entity.is_child is False

    # -------------------------------------------------------------------------
    # BucketState Tests (internal model - no __post_init__ validation)
    # -------------------------------------------------------------------------

    def test_bucket_state_valid(self):
        """Test valid BucketState is created."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.entity_id == "user-123"

    def test_bucket_state_allows_any_values_direct_construction(self):
        """Test BucketState allows any values when constructed directly.

        BucketState is an internal model used for DynamoDB deserialization.
        Validation is performed in from_limit() instead of __post_init__
        to support reading existing data and avoid performance overhead.
        """
        # This should NOT raise - direct construction bypasses validation
        bucket = BucketState(
            entity_id="user#123",  # Would be invalid in from_limit
            resource="",  # Empty - from DynamoDB deserialization
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.entity_id == "user#123"

    # -------------------------------------------------------------------------
    # LimitStatus Tests (internal model - no validation)
    # -------------------------------------------------------------------------

    def test_limit_status_valid(self):
        """Test valid LimitStatus is created."""
        limit = Limit.per_minute("rpm", 100)
        status = LimitStatus(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            limit=limit,
            available=50,
            requested=10,
            exceeded=False,
            retry_after_seconds=0,
        )
        assert status.entity_id == "user-123"

    def test_limit_status_is_internal_model_no_validation(self):
        """Test LimitStatus is an internal model without validation.

        LimitStatus is created internally by the limiter from already-validated
        inputs. No validation is performed to avoid performance overhead during
        rate limiting operations.
        """
        limit = Limit.per_minute("rpm", 100)
        # This should NOT raise - LimitStatus doesn't validate
        status = LimitStatus(
            entity_id="user#123",  # Would be invalid if validated
            resource="123api",  # Would be invalid if validated
            limit_name="rpm",
            limit=limit,
            available=50,
            requested=10,
            exceeded=False,
            retry_after_seconds=0,
        )
        assert status.entity_id == "user#123"

    # -------------------------------------------------------------------------
    # BucketState.from_limit Tests (internal factory - no validation)
    # -------------------------------------------------------------------------

    def test_bucket_state_from_limit_is_internal_no_validation(self):
        """Test BucketState.from_limit is internal and does not validate.

        Validation of entity_id and resource is performed at the API boundary
        (RateLimiter public methods), not in this internal factory method.
        """
        limit = Limit.per_minute("rpm", 100)
        # This should NOT raise - from_limit is internal and trusts its caller
        bucket = BucketState.from_limit(
            entity_id="user#123",  # Would be invalid at API boundary
            resource="api#v2",  # Would be invalid at API boundary
            limit=limit,
            now_ms=1000000,
        )
        assert bucket.entity_id == "user#123"
        assert bucket.resource == "api#v2"

    def test_bucket_state_from_limit_valid(self):
        """Test BucketState.from_limit creates bucket correctly."""
        limit = Limit.per_minute("rpm", 100)
        bucket = BucketState.from_limit(
            entity_id="user-123",
            resource="gpt-3.5-turbo",
            limit=limit,
            now_ms=1000000,
        )
        assert bucket.entity_id == "user-123"
        assert bucket.resource == "gpt-3.5-turbo"
        assert bucket.limit_name == "rpm"

    # -------------------------------------------------------------------------
    # Catching ValidationError as Category
    # -------------------------------------------------------------------------

    def test_can_catch_validation_error_category(self):
        """Test that ValidationError can catch all validation errors."""
        # InvalidNameError (via Limit)
        with pytest.raises(ValidationError):
            Limit.per_minute("rpm#test", 100)


class TestBucketStateProperties:
    """Tests for BucketState property accessors."""

    def test_tokens_property(self):
        """Test tokens property converts millitokens to tokens."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=150500,  # 150.5 tokens
            last_refill_ms=1000000,
            capacity_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.tokens == 150  # truncates to 150

    def test_tokens_property_exact(self):
        """Test tokens property with exact token value."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,  # exactly 100 tokens
            last_refill_ms=1000000,
            capacity_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.tokens == 100

    def test_capacity_property(self):
        """Test capacity property converts millitokens to tokens."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=250000,  # 250 tokens
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.capacity == 250


class TestLimitStatusDeficit:
    """Tests for LimitStatus deficit property."""

    def test_deficit_when_exceeded(self):
        """Test deficit calculation when limit is exceeded."""
        limit = Limit.per_minute("rpm", 100)
        status = LimitStatus(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            limit=limit,
            available=30,
            requested=50,
            exceeded=True,
            retry_after_seconds=12.0,
        )
        assert status.deficit == 20  # 50 - 30

    def test_deficit_when_not_exceeded(self):
        """Test deficit is 0 when limit is not exceeded."""
        limit = Limit.per_minute("rpm", 100)
        status = LimitStatus(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            limit=limit,
            available=100,
            requested=50,
            exceeded=False,
            retry_after_seconds=0,
        )
        assert status.deficit == 0


class TestAuditEvent:
    """Tests for AuditEvent model."""

    def test_to_dict_minimal(self):
        """Test to_dict with minimal required fields."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.ENTITY_CREATED,
            entity_id="user-123",
        )
        result = event.to_dict()
        assert result == {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "entity_created",
            "entity_id": "user-123",
        }

    def test_to_dict_with_principal(self):
        """Test to_dict includes principal when set."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_SET,
            entity_id="user-123",
            principal="admin@example.com",
        )
        result = event.to_dict()
        assert result["principal"] == "admin@example.com"

    def test_to_dict_with_resource(self):
        """Test to_dict includes resource when set."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_SET,
            entity_id="user-123",
            resource="gpt-4",
        )
        result = event.to_dict()
        assert result["resource"] == "gpt-4"

    def test_to_dict_with_details(self):
        """Test to_dict includes details when non-empty."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_SET,
            entity_id="user-123",
            details={"limits": ["rpm", "tpm"]},
        )
        result = event.to_dict()
        assert result["details"] == {"limits": ["rpm", "tpm"]}

    def test_to_dict_excludes_empty_details(self):
        """Test to_dict excludes details when empty dict."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.ENTITY_CREATED,
            entity_id="user-123",
            details={},
        )
        result = event.to_dict()
        assert "details" not in result

    def test_to_dict_full(self):
        """Test to_dict with all fields populated."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_DELETED,
            entity_id="user-123",
            principal="system",
            resource="gpt-4",
            details={"reason": "quota exceeded"},
        )
        result = event.to_dict()
        assert result == {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "limits_deleted",
            "entity_id": "user-123",
            "principal": "system",
            "resource": "gpt-4",
            "details": {"reason": "quota exceeded"},
        }

    def test_from_dict_minimal(self):
        """Test from_dict with minimal required fields."""
        data = {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "entity_created",
            "entity_id": "user-123",
        }
        event = AuditEvent.from_dict(data)
        assert event.event_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assert event.timestamp == "2024-01-15T10:30:00Z"
        assert event.action == "entity_created"
        assert event.entity_id == "user-123"
        assert event.principal is None
        assert event.resource is None
        assert event.details == {}

    def test_from_dict_full(self):
        """Test from_dict with all fields populated."""
        data = {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "limits_set",
            "entity_id": "user-123",
            "principal": "admin@example.com",
            "resource": "gpt-4",
            "details": {"limits": ["rpm", "tpm"]},
        }
        event = AuditEvent.from_dict(data)
        assert event.principal == "admin@example.com"
        assert event.resource == "gpt-4"
        assert event.details == {"limits": ["rpm", "tpm"]}

    def test_to_dict_from_dict_roundtrip(self):
        """Test serialization round-trip."""
        original = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.ENTITY_DELETED,
            entity_id="user-123",
            principal="admin",
            resource="api",
            details={"reason": "test"},
        )
        data = original.to_dict()
        restored = AuditEvent.from_dict(data)
        assert restored.event_id == original.event_id
        assert restored.timestamp == original.timestamp
        assert restored.action == original.action
        assert restored.entity_id == original.entity_id
        assert restored.principal == original.principal
        assert restored.resource == original.resource
        assert restored.details == original.details


class TestAuditAction:
    """Tests for AuditAction constants."""

    def test_action_constants(self):
        """Test audit action constant values."""
        assert AuditAction.ENTITY_CREATED == "entity_created"
        assert AuditAction.ENTITY_DELETED == "entity_deleted"
        assert AuditAction.LIMITS_SET == "limits_set"
        assert AuditAction.LIMITS_DELETED == "limits_deleted"


class TestLimiterInfo:
    """Tests for LimiterInfo model."""

    def test_basic_construction(self):
        """Test basic LimiterInfo construction."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.stack_name == "my-app"
        assert info.user_name == "my-app"
        assert info.region == "us-east-1"
        assert info.stack_status == "CREATE_COMPLETE"
        assert info.creation_time == "2024-01-15T10:30:00Z"
        assert info.last_updated_time is None
        assert info.version is None
        assert info.lambda_version is None
        assert info.schema_version is None

    def test_construction_with_all_fields(self):
        """Test LimiterInfo with all optional fields."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
            last_updated_time="2024-01-16T14:00:00Z",
            version="0.5.0",
            lambda_version="0.5.0",
            schema_version="1.0.0",
        )
        assert info.last_updated_time == "2024-01-16T14:00:00Z"
        assert info.version == "0.5.0"
        assert info.lambda_version == "0.5.0"
        assert info.schema_version == "1.0.0"

    def test_is_healthy_create_complete(self):
        """Test is_healthy returns True for CREATE_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is True

    def test_is_healthy_update_complete(self):
        """Test is_healthy returns True for UPDATE_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is True

    def test_is_healthy_false_for_in_progress(self):
        """Test is_healthy returns False for in-progress states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is False

    def test_is_healthy_false_for_failed(self):
        """Test is_healthy returns False for failed states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is False

    def test_is_in_progress_create(self):
        """Test is_in_progress for CREATE_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_update(self):
        """Test is_in_progress for UPDATE_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_delete(self):
        """Test is_in_progress for DELETE_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="DELETE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_rollback(self):
        """Test is_in_progress for ROLLBACK_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_ROLLBACK_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_false_for_complete(self):
        """Test is_in_progress returns False for complete states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is False

    def test_is_failed_create_failed(self):
        """Test is_failed for CREATE_FAILED."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_update_failed(self):
        """Test is_failed for UPDATE_FAILED."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_rollback_complete(self):
        """Test is_failed for ROLLBACK_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="ROLLBACK_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_update_rollback_complete(self):
        """Test is_failed for UPDATE_ROLLBACK_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_ROLLBACK_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_rollback_failed(self):
        """Test is_failed for ROLLBACK_FAILED."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="ROLLBACK_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_false_for_complete(self):
        """Test is_failed returns False for healthy complete states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is False

    def test_is_failed_false_for_in_progress(self):
        """Test is_failed returns False for in-progress states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is False

    def test_frozen(self):
        """Test that LimiterInfo is immutable."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        with pytest.raises(AttributeError):
            info.stack_name = "other"

    def test_states_are_mutually_exclusive(self):
        """Test that at most one of is_healthy/is_in_progress/is_failed is True."""
        # Healthy state
        healthy = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert healthy.is_healthy is True
        assert healthy.is_in_progress is False
        assert healthy.is_failed is False

        # In-progress state
        in_progress = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="UPDATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert in_progress.is_healthy is False
        assert in_progress.is_in_progress is True
        assert in_progress.is_failed is False

        # Failed state
        failed = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="CREATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert failed.is_healthy is False
        assert failed.is_in_progress is False
        assert failed.is_failed is True

    def test_rollback_in_progress_is_both_in_progress_and_failed(self):
        """Test ROLLBACK_IN_PROGRESS matches both is_in_progress and is_failed."""
        # This is a special case: rollback is both in-progress AND indicates failure
        info = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="ROLLBACK_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is False
        assert info.is_in_progress is True
        assert info.is_failed is True  # ROLLBACK contains "ROLLBACK"


class TestResetScheduleAndTimezoneHoisting:
    """The item-level `sched_tz` covers both tuples (#222 §4.1, surface Task 4).

    `Limit.to_dict`/`from_dict` already carried `reset_schedule` (#531); what
    this class pins is the *storage* consequence — one timezone attribute per
    item, and a limit carrying only a reset must still vote on it.
    """

    QUOTA = Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="America/New_York")

    def test_to_dict_emits_standard_cron(self):
        """Audit events are one of §4's standard-cron boundaries, and
        `to_dict()` is what all three setters put in the event `details`."""
        assert self.QUOTA.to_dict()["reset_schedule"] == [
            {"cron": "0 0 * * *", "tz": "America/New_York"}
        ]

    def test_to_dict_omits_an_absent_reset_schedule(self):
        assert "reset_schedule" not in Limit.per_day("rpd", 10_000).to_dict()

    def test_from_dict_restores_it(self):
        assert Limit.from_dict(self.QUOTA.to_dict()) == self.QUOTA

    def test_rejects_a_timezone_disagreement_across_the_two_tuples(self):
        """Without this guard one of the two is silently reinterpreted in the
        other's zone on the way back out of storage.

        The disagreement is introduced by `with_schedule` on an existing quota,
        not by `with_reset_schedule` on a drip: under ADR-137 the latter raises
        about `refill_amount` first and this test would pass for the wrong
        reason — `match="timezone"` is what keeps that honest.
        """
        with pytest.raises(ValueError, match="timezone"):
            Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="UTC").with_schedule(
                (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
            )

    def test_agreeing_tuples_construct(self):
        """The guard must not reject the legal shape §1.7 allows: a quota may
        carry a parameter schedule as well, in the same zone."""
        quota = self.QUOTA.with_schedule(
            (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        )
        assert quota.schedule and quota.reset_schedule

    def test_a_reset_only_limit_votes_on_the_hoisted_timezone(self):
        """A quota has no parameter schedule unless one is chained on, so a
        helper voting on `limit.schedule[0].tz` alone returns None, `sched_tz`
        is never written, and the stored reset decodes as UTC — a daily New
        York quota resetting at 19:00 local, forever, with no error."""
        assert models.hoisted_schedule_timezone([self.QUOTA]) == "America/New_York"

    def test_two_limits_disagreeing_across_different_tuples_are_rejected(self):
        """Each limit is individually legal; the *item* they share is not."""
        scheduled = Limit.per_minute("rpm", 1000).with_schedule(
            (ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5),)
        )
        with pytest.raises(ValueError, match="share a timezone"):
            models.hoisted_schedule_timezone([scheduled, self.QUOTA])

    def test_unscheduled_limits_still_do_not_vote(self):
        assert models.hoisted_schedule_timezone([Limit.per_minute("rpm", 1000)]) is None


class TestBucketStateCarriesTheResetSchedule:
    def test_from_limit_carries_it(self):
        """`build_composite_create` stamps `rsched` off the state, so a state
        built without it creates a quota bucket that never resets."""
        state = BucketState.from_limit(
            "e1", "gpt-4", Limit.quota("rpd", 10_000, cron="0 0 * * *", tz="UTC"), 0
        )
        assert state.reset_sched == (ScheduleEntry.reset("0 0 * * *", "UTC"),)

    def test_from_limit_leaves_a_dripping_limit_without_one(self):
        state = BucketState.from_limit("e1", "gpt-4", Limit.per_minute("rpm", 100), 0)
        assert state.reset_sched == ()

    def test_the_wcu_carrier_never_resets(self):
        """`wcu` is the per-partition write ceiling, not a user limit. An
        item-level `rsched` applies to every limit on the item by default, so
        the carrier must set the field explicitly rather than inherit."""
        state = BucketState.from_limit("e1", "gpt-4", Limit.per_minute("rpm", 100), 0)
        assert Limit._carrier(state).reset_schedule == ()
