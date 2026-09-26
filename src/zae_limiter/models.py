"""Core models for zae-limiter."""

import re
import warnings
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Literal

from .exceptions import InvalidIdentifierError, InvalidNameError
from .schedule import (
    MAX_PERIOD_SECONDS,
    MAX_TOKENS,
    ScheduleEntry,
    effective_params,
)

# ---------------------------------------------------------------------------
# Validation Constants
# ---------------------------------------------------------------------------

# Maximum lengths for validated fields
MAX_IDENTIFIER_LENGTH = 256  # entity_id, parent_id
MAX_NAME_LENGTH = 64  # limit_name, resource

# Identifiers: alphanumeric start, then alphanumeric + _ - . : @
# Supports UUIDs, API keys (sk-proj-xxx), email-like formats
IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-:@]*$")

# Names: letter start, then alphanumeric + _ - .
# Used for limit names (rpm, tpm)
NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]*$")

# Resources: letter start, then alphanumeric + _ - . / :
# Allows provider/model grouping (openai/gpt-4, anthropic/claude-3) and
# colon-separated tags (llama3:8b, anthropic.claude-v2:1)
RESOURCE_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-/:]*$")

# The '#' character is used as a key delimiter in DynamoDB and must be forbidden
FORBIDDEN_CHAR = "#"

# Reserved limit names (internal infrastructure limits, not user-configurable)
RESERVED_LIMIT_NAMES = frozenset({"wcu"})


# ---------------------------------------------------------------------------
# Validation Functions
# ---------------------------------------------------------------------------


def validate_identifier(value: str, field_name: str) -> None:
    """
    Validate an identifier (entity_id, parent_id).

    Args:
        value: The identifier value to validate
        field_name: Name of the field (for error messages)

    Raises:
        InvalidIdentifierError: If validation fails
    """
    if not value:
        raise InvalidIdentifierError(field_name, value, "cannot be empty")

    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise InvalidIdentifierError(
            field_name, value, f"exceeds maximum length of {MAX_IDENTIFIER_LENGTH}"
        )

    if FORBIDDEN_CHAR in value:
        raise InvalidIdentifierError(
            field_name, value, f"cannot contain '{FORBIDDEN_CHAR}' (reserved delimiter)"
        )

    if not IDENTIFIER_PATTERN.match(value):
        raise InvalidIdentifierError(
            field_name,
            value,
            "must start with alphanumeric and contain only alphanumeric, "
            "underscore, hyphen, dot, colon, or @ characters",
        )


def validate_name(value: str, field_name: str) -> None:
    """
    Validate a name (limit_name, resource).

    Args:
        value: The name value to validate
        field_name: Name of the field (for error messages)

    Raises:
        InvalidNameError: If validation fails
    """
    if not value:
        raise InvalidNameError(field_name, value, "cannot be empty")

    if len(value) > MAX_NAME_LENGTH:
        raise InvalidNameError(field_name, value, f"exceeds maximum length of {MAX_NAME_LENGTH}")

    if FORBIDDEN_CHAR in value:
        raise InvalidNameError(
            field_name, value, f"cannot contain '{FORBIDDEN_CHAR}' (reserved delimiter)"
        )

    if value in RESERVED_LIMIT_NAMES:
        raise InvalidNameError(field_name, value, f"'{value}' is a reserved limit name")

    if not NAME_PATTERN.match(value):
        raise InvalidNameError(
            field_name,
            value,
            "must start with a letter and contain only alphanumeric, "
            "underscore, hyphen, or dot characters",
        )


def validate_resource(value: str, field_name: str = "resource") -> None:
    """
    Validate a resource name.

    Resource names allow "/" for provider/model grouping (e.g., openai/gpt-4)
    and ":" for tag/version suffixes (e.g., llama3:8b, anthropic.claude-v2:1).

    Args:
        value: The resource name to validate
        field_name: Name of the field (for error messages)

    Raises:
        InvalidNameError: If validation fails
    """
    if not value:
        raise InvalidNameError(field_name, value, "cannot be empty")

    if len(value) > MAX_NAME_LENGTH:
        raise InvalidNameError(field_name, value, f"exceeds maximum length of {MAX_NAME_LENGTH}")

    if FORBIDDEN_CHAR in value:
        raise InvalidNameError(
            field_name, value, f"cannot contain '{FORBIDDEN_CHAR}' (reserved delimiter)"
        )

    if not RESOURCE_PATTERN.match(value):
        raise InvalidNameError(
            field_name,
            value,
            "must start with a letter and contain only alphanumeric, "
            "underscore, hyphen, dot, slash, or colon characters",
        )


# ---------------------------------------------------------------------------
# Backend Capabilities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendCapabilities:
    """
    Declares which extended features a backend supports.

    Used by RateLimiter to gracefully degrade when features are unavailable.
    Backend implementations should return an instance from their `capabilities`
    property.

    See ADR-109 for the capability matrix across backends.
    """

    supports_audit_logging: bool = False
    """Whether the backend supports audit event storage and retrieval."""

    supports_usage_snapshots: bool = False
    """Whether the backend supports usage snapshot aggregation."""

    supports_infrastructure_management: bool = False
    """Whether the backend supports declarative infrastructure (e.g., CloudFormation)."""

    supports_change_streams: bool = False
    """Whether the backend supports real-time change notifications."""

    supports_batch_operations: bool = False
    """Whether the backend supports batch_get_buckets() for optimized reads."""


#: Valid values for the ``on_unavailable`` system config attribute.
OnUnavailableAction = Literal["allow", "block"]

# ---------------------------------------------------------------------------
# Limit Configuration
# ---------------------------------------------------------------------------


#: Denominator stored on a quota, whose numerator is zero (ADR-137).
#:
#: ``refill_period_seconds`` is validated ``> 0`` and a rate of zero has no
#: meaningful denominator, so the field has to hold *something*. ``1`` reads as
#: "0 per second", which is what the limit does; ``86_400`` would read as a
#: daily rate, which is what it emphatically does not do. Deliberately not a
#: ``quota()`` keyword — a knob that changes nothing is worse than a constant.
_QUOTA_REFILL_PERIOD_SECONDS = 1


def is_accrual_rate(refill_amount_milli: int) -> bool:
    """Does this refill rate actually add tokens?

    The **temporal** half of ADR-137's "this limit has no refill", in its
    primitive form: a rate that has already been resolved — scaled by whatever
    schedule window is in force, and divided by ``shard_count``.
    :meth:`BucketState.accrues` is the same question bound to a bucket and a
    clock reading; this is what a free function holding only the rate can ask.

    Deliberately **not** the same question as :attr:`Limit.is_quota`, and the
    reason the two are separate predicates. A resolved rate reaches zero two
    ways:

    * a **quota** never drips at all, at any instant (``refill_amount = 0``
      paired with a ``reset_schedule``); and
    * a limit that *does* drip can be accruing nothing at this instant, when
      its share floors away — ``ra_milli // shard_count`` is 0 for a
      1-token/minute limit spread over 32 shards, and a ``scale`` window
      shrinks the numerator before the shard split reaches it.

    Only the first has a ``reset_schedule`` to be recognised by, so code that
    divides by a rate, or computes a wait from one, must ask *this* rather than
    test for a quota: a quota-only special case is silently wrong for the
    second case, which predates ADR-137 (#475, GHSA-76rv) and has no marker on
    the limit at all.
    """
    return refill_amount_milli > 0


def new_shard_starting_tokens_milli(
    share_milli: int,
    reclaimed_milli: int | None,
    *,
    is_quota: bool,
) -> int:
    """What a shard coming into existence right now may start with (#587).

    A shard is created by the client slow path (ADR-133) and by the
    aggregator's propagation clone, and until #587 both minted it a fresh
    ``capacity // shard_count``. For a **dripping** limit that is sound: the
    stored ``ra`` is undivided, each shard refills at ``ra // shard_count``, so
    the ceilings across all shards still sum to the configured capacity and a
    new shard starting full is a one-off burst of at most one ``time_to_fill``
    — which token-bucket semantics already permit. That path is unchanged, and
    ``is_quota=False`` returns ``share_milli`` verbatim.

    A **quota** has no rate at all (ADR-137), so there is nothing for a fresh
    share to amortise against and no pass that takes it back before the next
    reset edge. Minting one hands the entity allowance it never earned: the
    issue measured a 1000-a-day quota admitting 3496 inside a single frozen
    period, 3.5x, by walking ``shard_count`` 1 -> 32.

    So a quota's new shard is given a **transfer, never a mint**. Before it is
    created, every shard that already exists is clamped to the freshly shrunken
    per-shard ceiling — the same ``min(capacity, tokens)`` that
    ``bucket.refill_bucket`` would apply on their next materialising pass (#496
    / #222 §3.3), just taken now instead of eventually — and the new shard
    starts with what that reclaimed, capped at its own share:

    * an entity holding a **full** quota when it doubles gets a full new share,
      precisely paid for by the clamp on the shard it split from — the behaviour
      before #587, which was right for this case and is why the bug hid;
    * an entity that has **spent** its quota gets nothing, because there is no
      surplus to move. That is the fix; and
    * everything in between conserves exactly, and both shards keep tokens, so
      ADR-134's random re-draw still finds them.

    Doing the clamp *eagerly* is what makes the conservation hold at the instant
    of the doubling rather than eventually. Leaving it to the siblings' next
    pass reopens the same hole in transient form: the speculative fast path is a
    pure ``ADD`` with no ceiling arithmetic, so an unclamped sibling will happily
    spend the surplus that has just been granted to the new shard as well. No
    token is destroyed that was not already doomed — the clamp takes exactly
    this much whenever it next runs — so a reclaim followed by an acquire that
    is then rejected leaves the entity no worse off.

    Zero-filling instead would also never over-admit, but it is not neutral: a
    new shard that can admit nothing takes its share of the draws and rejects
    them, the successful writes pile back onto the one shard that has tokens,
    that shard trips ``wcu`` again, and the count runs away to
    ``MAX_SHARD_COUNT`` with the whole balance clamped onto a single
    ``C // 32``. Redistribution — deducting a blind ``old_share / 2`` from each
    existing shard at the doubling — does not fix the over-admission at all: on
    a spent quota the deduction lands as debt that nothing ever repays, while
    the new shard's share is immediately spendable, so the entity still gains
    ``C/2`` per doubling. Conserving the *sum* of the balances is not the same
    as conserving what can be **spent**.

    Args:
        share_milli: This shard's ceiling — the capacity in force now,
            divided by ``shard_count`` (``BucketState.effective_capacity_milli``).
        reclaimed_milli: Millitokens taken off the existing shards of this
            (entity, resource, limit) by the eager clamp. ``None`` means no
            shard exists to reclaim from — nothing has been materialised for
            this limit, so there is no spend to conserve against and the share
            is granted in full.
        is_quota: :attr:`Limit.is_quota` — the **structural** predicate. Not
            :meth:`BucketState.accrues`, which is also true of a dripping limit
            whose share has floored to zero; starving that limit's new shard
            would be wrong, since it does recover.

    Returns:
        Starting balance in millitokens, never negative and never above
        ``share_milli``.
    """
    if not is_quota or reclaimed_milli is None:
        return share_milli
    return max(0, min(share_milli, reclaimed_milli))


def _schedule_entry_to_dict(entry: ScheduleEntry) -> dict[str, Any]:
    """One schedule entry as a plain dict, emitting only the fields that are set.

    Keeps ``Limit.from_dict(limit.to_dict()) == limit`` — an entry sets exactly
    one of ``scale`` or the absolute fields, so writing the unset ones as
    ``None`` would round-trip into a ``ScheduleEntry`` that validates the same
    but compares unequal on nothing, while writing them all would just be noise.
    """
    result: dict[str, Any] = {"cron": entry.cron, "tz": entry.tz}
    for name in ("scale", "capacity", "refill_amount", "refill_period_seconds"):
        value = getattr(entry, name)
        if value is not None:
            result[name] = value
    return result


def hoisted_schedule_timezone(limits: list["Limit"]) -> str | None:
    """The one timezone shared by every scheduled limit destined for one item.

    Schedules are stored per limit (``l_{name}_sched``) but the timezone is
    stored **once per item** (``sched_tz``, #222 §4.1), so a config item cannot
    represent two. ``Limit.__post_init__`` only ever sees one limit's entries;
    nothing below it notices that a *second* limit on the same item arrived with
    a different zone, and the item-level write is last-one-wins — which would
    silently reinterpret the first limit's schedule in the second's timezone.

    Returns ``None`` when no limit on the item carries a schedule; limits
    without one do not vote.

    **Both tuples vote** (#222 §4.1). A limit carrying only a
    ``reset_schedule`` — which is every quota, and quotas have no parameter
    schedule unless one is chained on — would otherwise not vote at all:
    ``sched_tz`` would come back ``None``, be omitted from the item, and the
    stored reset would decode as UTC on the way back out. A daily quota
    configured for ``America/New_York`` would then reset at 19:00 local.
    """
    zones = {entry.tz for limit in limits for entry in (*limit.schedule, *limit.reset_schedule)}
    if len(zones) > 1:
        raise ValueError(
            f"all scheduled limits on one config item must share a timezone, got "
            f"{sorted(zones)}. The timezone is stored once per item as `sched_tz`, "
            f"not per limit."
        )
    return zones.pop() if zones else None


@dataclass(frozen=True)
class Limit:
    """
    Token bucket rate limit configuration.

    Refill rate is stored as a fraction (refill_amount / refill_period_seconds)
    to avoid floating point precision issues.

    Attributes:
        name: Unique identifier for this limit type (e.g., "rpm", "tpm")
        capacity: Max tokens in the bucket (ceiling)
        refill_amount: Numerator of refill rate. ``0`` means the limit does not
            drip at all, and is valid **only** alongside a non-empty
            ``reset_schedule`` (ADR-137).
        refill_period_seconds: Denominator of refill rate. Inert while
            ``refill_amount`` is 0.
        schedule: Time windows in which different parameters apply (#222).
            The fields above stay the *base* parameters forever; the schedule
            is applied on top of them at read time by
            ``schedule.effective_params()``.
        reset_schedule: Calendar instants at which the balance goes back to the
            effective capacity in one lump (#222 §3.6). A second, independent
            tuple rather than a third kind of entry in ``schedule``, because
            ``schedule`` is resolved first-match-wins and an entry overriding
            no parameters would win its window and supply nothing. Entries are
            built with ``ScheduleEntry.reset()``.

    A limit **drips or resets, never both and never neither** (ADR-137): a
    positive ``refill_amount`` alongside a ``reset_schedule`` would return the
    allowance a second time over the period, and a zero one without a reset
    leaves a bucket that can never recover. Both are rejected at construction,
    so the amount and the reset have to arrive in the same call — which is what
    :meth:`quota` is for. A quota may still carry a *parameter* schedule
    (``Limit.quota(...).with_schedule(...)``): that one sets the ceiling the
    reset restores to, and never touches ``refill_amount``.

    The reset window is a **fixed calendar window** — every entity on one
    schedule resets at the same wall-clock instant (ADR-138).
    """

    name: str
    capacity: int
    refill_amount: int
    refill_period_seconds: int
    schedule: tuple[ScheduleEntry, ...] = ()
    reset_schedule: tuple[ScheduleEntry, ...] = ()
    # A window anchored to the entity's own first use, rather than to the wall
    # clock (ADR-139). The alternative spelling of `reset_schedule`, never a
    # companion to it: a limit has one recovery mechanism (ADR-137). A
    # `timedelta` rather than a bare integer because the field name carries no
    # unit, so the type has to — `reset_after=timedelta(hours=5)` is
    # self-documenting where `reset_after=18000` is a puzzle. Storage,
    # manifests and CloudFormation all spell it `..._seconds` and take an int,
    # because a bare scalar there cannot carry a type.
    reset_after: timedelta | None = None

    def __post_init__(self) -> None:
        validate_name(self.name, "name")
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_amount < 0:
            raise ValueError("refill_amount must not be negative")
        if self.refill_period_seconds <= 0:
            raise ValueError("refill_period_seconds must be positive")
        # Upper bounds, for the same reason `ScheduleEntry` has them (#570).
        # Bounding `scale` alone proves nothing: the quantity that has to stay
        # inside DynamoDB's 38 significant digits is the *product*
        # `capacity x 1000 x scale`, so the base has to be bounded too or the
        # overflow threshold stays data-dependent — the same entry raising on a
        # large `tpm` and returning cleanly on a small `rpm`. The ceilings and
        # their derivation live in `schedule.py`, beside the one hard limit they
        # respect, because that module may not import this one.
        for field_name, value, bound in (
            ("capacity", self.capacity, MAX_TOKENS),
            ("refill_amount", self.refill_amount, MAX_TOKENS),
            ("refill_period_seconds", self.refill_period_seconds, MAX_PERIOD_SECONDS),
        ):
            if value > bound:
                raise ValueError(
                    f"{field_name} must be at most {bound}, got {value!r}. Above this "
                    f"the limit cannot be stored exactly as a DynamoDB Number, and a "
                    f"schedule applied on top of it overflows (#570)."
                )
        # The two tuples are validated by opposite rules and neither is a
        # superset of the other, so an entry in the wrong one is checked here
        # rather than left to whatever reads it. A reset entry in `schedule`
        # is the dangerous direction: `effective_params` is first-match-wins,
        # so it would match its window, supply no override, return the base,
        # and silently shadow every entry below it.
        misplaced_reset = [entry for entry in self.schedule if entry._reset]
        if misplaced_reset:
            raise ValueError(
                f"`schedule` takes parameter entries only; {len(misplaced_reset)} of "
                f"{len(self.schedule)} came from `ScheduleEntry.reset()`. Pass them to "
                f"`with_reset_schedule()` instead — an entry that overrides no parameters "
                f"would win its window under first-match-wins and shadow the entries below it."
            )
        misplaced_param = [entry for entry in self.reset_schedule if not entry._reset]
        if misplaced_param:
            raise ValueError(
                f"`reset_schedule` takes reset entries only, built with "
                f"`ScheduleEntry.reset(cron, tz)`; {len(misplaced_param)} of "
                f"{len(self.reset_schedule)} carry a parameter modifier. A reset names the "
                f"instant the balance goes back to the effective capacity; it overrides "
                f"no parameters."
            )
        # A duration has to be expressible in the storage unit, which is whole
        # seconds (`l_{name}_rsa` / `b_{name}_rsa`). Rejecting here rather
        # than truncating is the same call #569 made for the schedule
        # absolutes: a silently-truncated window is a limit that resets at a
        # time the operator never wrote.
        if self.reset_after is not None:
            total = self.reset_after.total_seconds()
            if total <= 0 or total != int(total):
                raise ValueError(
                    f"reset_after must be a positive whole number of seconds, got "
                    f"{self.reset_after!r} ({total}s). The window length is stored in "
                    f"seconds, so a fraction of one cannot be represented."
                )
            # The upper half of the positivity test, ordered after it as #570
            # orders its own bounds: every duration on a limit shares one
            # ceiling, so a window cannot outlive what `refill_period_seconds`
            # is allowed to be.
            if total > MAX_PERIOD_SECONDS:
                raise ValueError(
                    f"reset_after must be at most {MAX_PERIOD_SECONDS} seconds, got "
                    f"{self.reset_after!r} ({int(total)}s). Every duration on a limit "
                    f"shares this ceiling (#570)."
                )
        # ADR-139: `reset_after` and `reset_schedule` are two spellings of the
        # reset half, not two mechanisms that compose. A limit that both reset
        # at midnight and rolled five hours from first use would restore its
        # allowance twice over some periods and once over others, with no
        # reading of "the allowance" faithful to either.
        if self.reset_after is not None and self.reset_schedule:
            raise ValueError(
                "a limit has one recovery mechanism: `reset_after` names a window "
                "anchored to the entity's own first use and `reset_schedule` names "
                "fixed calendar instants, so they are alternatives rather than "
                "companions (ADR-137, ADR-139). Pass one of `cron=` or "
                "`reset_after=` to Limit.quota()."
            )
        # ADR-137: a limit drips or resets, never both and never neither. The
        # two fields can no longer be validated independently, so the message
        # has to explain the pairing rather than the field. Checked *after* the
        # structural checks above: an entry in the wrong tuple is a more
        # specific diagnosis than the pairing it happens to violate.
        if self.refill_amount == 0 and not self.reset_schedule and self.reset_after is None:
            raise ValueError(
                "refill_amount=0 means the limit does not drip, which is only valid "
                "with a reset_schedule; otherwise the bucket can never recover. "
                "Use Limit.quota(name, amount, cron=...) or "
                "Limit.quota(name, amount, reset_after=...) (ADR-137, ADR-139)."
            )
        if self.refill_amount > 0 and (self.reset_schedule or self.reset_after is not None):
            raise ValueError(
                "a limit drips or resets, never both: a positive refill_amount "
                "alongside a reset_schedule grants roughly twice the intended "
                "allowance per period. Use Limit.quota(...) (ADR-137)."
            )
        # Across BOTH tuples, not within each (#222 §4.1). `sched_tz` is one
        # item-level attribute shared by the parameter schedule and the reset
        # schedule, so a limit whose `schedule` is America/New_York and whose
        # `reset_schedule` is UTC has nowhere to put the second zone: it would
        # serialise, and come back with one of the two silently reinterpreted
        # in the other's zone, forever and with no error anywhere.
        if self.schedule or self.reset_schedule:
            zones = {entry.tz for entry in (*self.schedule, *self.reset_schedule)}
            if len(zones) > 1:
                raise ValueError(
                    f"all schedule entries on one limit must share a timezone, got "
                    f"{sorted(zones)}. The timezone is stored once per item as "
                    f"`sched_tz` and covers the parameter schedule and the reset "
                    f"schedule together, not per entry and not per tuple."
                )

    @classmethod
    def per_second(
        cls,
        name: str,
        rate: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills ``rate`` tokens per second.

        Args:
            name: Limit name (e.g., "rps")
            rate: Sustained tokens per second (also the refill amount)
            burst: Optional burst ceiling. When set, ``capacity`` is
                ``burst`` and ``refill_amount`` is ``rate``, allowing
                temporary spikes above the sustained rate.
        """
        capacity = burst if burst is not None else rate
        return cls(
            name=name,
            capacity=capacity,
            refill_amount=rate,
            refill_period_seconds=1,
        )

    @classmethod
    def per_minute(
        cls,
        name: str,
        rate: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills ``rate`` tokens per minute.

        Args:
            name: Limit name (e.g., "rpm", "tpm")
            rate: Sustained tokens per minute (also the refill amount)
            burst: Optional burst ceiling. When set, ``capacity`` is
                ``burst`` and ``refill_amount`` is ``rate``, allowing
                temporary spikes above the sustained rate.
        """
        capacity = burst if burst is not None else rate
        return cls(
            name=name,
            capacity=capacity,
            refill_amount=rate,
            refill_period_seconds=60,
        )

    @classmethod
    def per_hour(
        cls,
        name: str,
        rate: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills ``rate`` tokens per hour.

        Args:
            name: Limit name (e.g., "rph")
            rate: Sustained tokens per hour (also the refill amount)
            burst: Optional burst ceiling. When set, ``capacity`` is
                ``burst`` and ``refill_amount`` is ``rate``, allowing
                temporary spikes above the sustained rate.
        """
        capacity = burst if burst is not None else rate
        return cls(
            name=name,
            capacity=capacity,
            refill_amount=rate,
            refill_period_seconds=3600,
        )

    @classmethod
    def per_day(
        cls,
        name: str,
        rate: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills ``rate`` tokens per day.

        Args:
            name: Limit name (e.g., "rpd")
            rate: Sustained tokens per day (also the refill amount)
            burst: Optional burst ceiling. When set, ``capacity`` is
                ``burst`` and ``refill_amount`` is ``rate``, allowing
                temporary spikes above the sustained rate.
        """
        capacity = burst if burst is not None else rate
        return cls(
            name=name,
            capacity=capacity,
            refill_amount=rate,
            refill_period_seconds=86400,
        )

    @classmethod
    def quota(
        cls,
        name: str,
        amount: int,
        *,
        cron: str | None = None,
        tz: str = "UTC",
        reset_after: timedelta | None = None,
    ) -> "Limit":
        """An allowance of ``amount`` per window, restored in one lump.

        Two window shapes, and **exactly one** of them per limit:

        ``cron`` gives a **fixed calendar window** — every entity on this
        schedule resets at the same wall-clock instant, in ``tz`` (ADR-138).
        That is what a billing period needs: "10,000 per calendar month" is a
        statement about the calendar, not about the caller.

        ``reset_after`` gives a **window anchored to the entity's own first
        use** (ADR-139): five hours from when *you* started, not from midnight.
        That is what a session cap needs. The window is idle-restarting — go
        quiet past its end and the next call opens a fresh one. ``tz`` is
        meaningless here and is ignored.

        Either way the limit does not drip: the balance is *set* to the
        capacity when the window opens and does not recover in between
        (ADR-137). The amount and the reset have to arrive together, which is
        why this factory exists — the intermediate value in any two-step
        spelling is either a drip with a reset or a zero rate with none, and
        ``Limit`` rejects both.

        Args:
            name: Limit name (e.g. "rpd", "session")
            amount: The whole allowance for one window (also the ceiling)
            cron: Standard 5-field cron naming the instant the window opens.
                Mutually exclusive with ``reset_after``.
            tz: IANA timezone ``cron`` is read in. Ignored with ``reset_after``.
            reset_after: Window length, anchored to first use. Mutually
                exclusive with ``cron``.

        Example: 10,000 a day, back to 10,000 at New York midnight
            Limit.quota("rpd", 10_000, cron="0 0 * * *",
                        tz="America/New_York")

        Example: 10,000 a session, five hours from your own first call
            Limit.quota("session", 10_000, reset_after=timedelta(hours=5))
        """
        if (cron is None) == (reset_after is None):
            raise ValueError(
                "Limit.quota() takes exactly one of `cron` or `reset_after`: a "
                "calendar window resets every entity at the same instant (ADR-138) "
                "and a duration window resets each entity relative to its own first "
                "use (ADR-139), and a limit has one recovery mechanism (ADR-137)."
            )
        return cls(
            name=name,
            capacity=amount,
            refill_amount=0,
            refill_period_seconds=_QUOTA_REFILL_PERIOD_SECONDS,
            reset_schedule=((ScheduleEntry.reset(cron=cron, tz=tz),) if cron is not None else ()),
            reset_after=reset_after,
        )

    @classmethod
    def custom(
        cls,
        name: str,
        capacity: int,
        refill_amount: int,
        refill_period_seconds: int,
    ) -> "Limit":
        """
        Create a custom limit with explicit refill rate.

        Example: Allow ceiling of 1000 tokens, sustained at 100/sec
            Limit.custom("requests", capacity=1000, refill_amount=100,
                        refill_period_seconds=1)
        """
        return cls(
            name=name,
            capacity=capacity,
            refill_amount=refill_amount,
            refill_period_seconds=refill_period_seconds,
        )

    @property
    def is_quota(self) -> bool:
        """Does this limit recover at a reset edge rather than by dripping?

        The **structural** half of ADR-137's "this limit has no refill": a
        property of the configuration and of nothing else. True for a quota at
        every instant, false for a dripping limit at every instant — including
        one whose effective rate happens to be zero right now.

        Derived from ``reset_schedule`` rather than from ``refill_amount == 0``
        because the reset is what the limit *has*; the zero rate is the
        consequence ``__post_init__`` pairs with it, and the pairing is what
        makes the two spellings interchangeable for any constructible limit.
        ``reset_schedule`` is also the half that survives
        :meth:`per_shard`, which must return a quota whose rate is still zero.

        Since ADR-139 there are two spellings of the reset half — a cron and a
        duration — and this is true of both. It stays structural: a property
        of the configuration and of nothing else, so ``per_shard`` and
        ``from_bucket_state`` can rely on it without a clock.

        Consumers are the ones that must treat a quota as a different *kind* of
        thing no matter what the clock says: display (``10,000 per day``, never
        ``0/sec``), validation, documentation. Anything asking "is this
        accruing right now" wants :meth:`BucketState.accrues` instead — a quota
        is only one of the two ways to get a zero rate, and the other one
        carries no ``reset_schedule`` to be found by.
        """
        return bool(self.reset_schedule) or self.reset_after is not None

    @property
    def reset_after_seconds(self) -> int | None:
        """:attr:`reset_after` in the unit everything below the API uses.

        Storage (``l_{name}_rsa``, ``b_{name}_rsa``), the manifest
        (``reset_after_seconds``) and CloudFormation (``ResetAfterSeconds``)
        all carry whole seconds, because a bare scalar cannot carry a type.
        ``__post_init__`` has already rejected anything that is not a positive
        whole number of them, so this cannot lose information.
        """
        return None if self.reset_after is None else int(self.reset_after.total_seconds())

    @property
    def refill_rate(self) -> float:
        """Tokens per second (for display/debugging)."""
        return self.refill_amount / self.refill_period_seconds

    def with_schedule(self, schedule: tuple[ScheduleEntry, ...]) -> "Limit":
        """This limit with a schedule attached (#222 §1.1).

        Returns a new instance; ``Limit`` is frozen and the factory methods
        (``per_minute``, ``per_hour``, ...) do not take a schedule. Validation
        runs through ``__post_init__``, so a schedule whose entries disagree on
        timezone is rejected here rather than at the DynamoDB write.

        Pass ``()`` to clear a schedule: storage is override-not-merge, so a
        limit with no schedule has no schedule.
        """
        return replace(self, schedule=schedule)

    def with_reset_schedule(self, reset_schedule: tuple[ScheduleEntry, ...]) -> "Limit":
        """Swap one quota's reset schedule for another (#222 §3.6).

        A *replacement* operator on a limit that is already a quota, not the way
        a quota is built — use :meth:`quota` for that. ADR-137 leaves nothing
        else standing: this method returns a limit whose ``refill_amount`` is
        whatever it already was, so calling it on a dripping limit produces a
        rate alongside a reset and raises, and calling it with ``()`` on a quota
        produces a zero rate with no reset and raises too. Both are rejected
        here rather than silently repaired, because either repair would throw
        away a number the caller explicitly passed.

        Entries must come from :meth:`ScheduleEntry.reset`; a parameter entry is
        rejected rather than silently stored, since the two mean different
        things to every reader downstream.

        Independent of :meth:`with_schedule`, which a quota may also carry: the
        parameter schedule decides what the ceiling is, the reset schedule
        decides when the balance returns to it.
        """
        return replace(self, reset_schedule=reset_schedule)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for storage."""
        result: dict[str, Any] = {
            "name": self.name,
            "capacity": self.capacity,
            "refill_amount": self.refill_amount,
            "refill_period_seconds": self.refill_period_seconds,
        }
        # Standard 5-field cron at every boundary of the system, including
        # audit events — the compact form is purely a storage encoding
        # (#222 §4). Omitted when empty so existing payloads are unchanged.
        if self.schedule:
            result["schedule"] = [_schedule_entry_to_dict(e) for e in self.schedule]
        # Same reasoning, separate key: a reset schedule that vanished here
        # would make the audit record for "attached a daily quota reset"
        # byte-identical to the record for attaching nothing.
        if self.reset_schedule:
            result["reset_schedule"] = [_schedule_entry_to_dict(e) for e in self.reset_schedule]
        # Whole seconds at every boundary below the API, for the reason in
        # `reset_after_seconds`. Omitted when unset so existing payloads —
        # audit events included — are byte-identical.
        if self.reset_after is not None:
            result["reset_after_seconds"] = self.reset_after_seconds
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Limit":
        """Deserialize from dictionary."""
        reset_after_seconds = data.get("reset_after_seconds")
        return cls(
            name=data["name"],
            capacity=data["capacity"],
            refill_amount=data["refill_amount"],
            refill_period_seconds=data["refill_period_seconds"],
            schedule=tuple(ScheduleEntry(**entry) for entry in data.get("schedule", ())),
            # `ScheduleEntry.reset`, never `ScheduleEntry(...)`: a reset entry
            # carries no modifier and the ordinary constructor requires
            # exactly one.
            reset_schedule=tuple(
                ScheduleEntry.reset(**entry) for entry in data.get("reset_schedule", ())
            ),
            reset_after=(
                timedelta(seconds=reset_after_seconds) if reset_after_seconds is not None else None
            ),
        )

    @classmethod
    def from_bucket_state(cls, state: "BucketState") -> "Limit":
        """The *base* limit a bucket item encodes, schedule included.

        Bucket items store the undivided, unscaled base parameters forever
        (#222 §2.1) alongside the schedule that applies on top of them, so
        what comes back here is the configured limit — not the view one shard
        has of it right now. Narrowing to a shard and to the window in force
        is :meth:`per_shard`, which does both together; calling it on this
        result divides and scales exactly once.

        This deliberately does **not** pre-divide. The result is what
        ``LeaseEntry.limit`` carries, and the slow path already puts the
        undivided config limit there; a pre-divided one would be narrowed a
        second time when the lease builds a status from it.

        ``reset_schedule`` and the ``max(1, ...)`` rate floor move **together**,
        because neither is correct without the other: the floor turns a quota's
        zero rate into one token, and a positive rate alongside a reset is
        precisely what ADR-137 rejects, so carrying the tuple on its own would
        start *raising* from inside a rejection path rather than round-tripping.
        Carried together, a quota bucket reconstructs as the quota it is —
        ``refill_amount=0``, reset intact — instead of advertising a phantom
        one-token drip beside a ``retry_after_seconds`` computed from the
        calendar edge.

        The pairing is decided by the **stored** shape, not by either half
        alone. Since ADR-139 a quota item can carry either spelling of the
        reset — ``reset_sched`` (a cron) or ``reset_after_seconds`` (a
        duration) — so both are consulted, and two shapes are corrupt rather
        than legally constructible. Neither may reach the constructor call
        below, which would raise from inside a rejection path instead of
        round-tripping — the same discipline the positive-rate case already
        followed, now stated for both:

        * **A reset beside a positive rate** (``refill_amount_milli > 0``)
          is unconstructible under ADR-137: it keeps the rate floor and drops
          *both* reset tuples, preserving the pre-#222 reading — the same
          call ``bucket.calculate_retry_after``'s last branch makes. A
          duration window read back as a dripping limit would advertise a
          phantom one-token drip beside a ``retry_after_seconds`` computed
          from a rate that does not exist.
        * **Both reset tuples at once** beside a zero rate is unconstructible
          under ADR-139 (§Negatives: a limit resets one way, never both).
          ``reset_sched`` — the pre-ADR-139 reading — wins and
          ``reset_after_seconds`` is dropped, so the bucket still reconstructs
          as the quota it is rather than raising two-recovery-mechanisms from
          inside a rejection path.

        Note that ``state.reset_sched`` and ``state.reset_after_seconds`` are
        populated by the slow path (from the resolved config) and by
        ``BucketState.from_limit``, but **not yet** by
        ``_deserialize_composite_bucket`` — so for a bucket read back off the
        item this is still the old behaviour exactly, and becomes live with no
        further edit once that deserialiser decodes ``rsched`` / ``rsa``.
        """
        is_quota = state.refill_amount_milli == 0 and (
            bool(state.reset_sched) or state.reset_after_seconds is not None
        )
        # `reset_sched` wins when a corrupt item carries both spellings at
        # once (see above) — never pass both to the constructor.
        reset_after = None
        if is_quota and not state.reset_sched and state.reset_after_seconds is not None:
            reset_after = timedelta(seconds=state.reset_after_seconds)
        return cls(
            name=state.limit_name,
            capacity=max(1, state.capacity_milli // 1000),
            refill_amount=0 if is_quota else max(1, state.refill_amount_milli // 1000),
            refill_period_seconds=max(1, state.refill_period_ms // 1000),
            schedule=state.sched,
            reset_schedule=state.reset_sched if is_quota else (),
            reset_after=reset_after,
        )

    def per_shard(self, shard_count: int, now_ms: int) -> "Limit":
        """This limit as a single shard of ``shard_count`` sees it at ``now_ms``.

        Two narrowings happen here, and the order matters (#222 §2.1): the
        schedule in force at ``now_ms`` scales the **undivided** parameters,
        and only then does the shard take its share — ``capacity //
        shard_count``, ``refill_amount // shard_count`` (GHSA-76rv). Dividing
        first and scaling second is a different integer, and it is the wrong
        one: the bucket itself refills against
        ``BucketState.effective_capacity_milli``, which scales first.

        Both narrowings belong together because both feed ``LimitStatus`` and
        neither may be applied twice. A status reported from one shard has to
        quote the share, or it promises a capacity no shard can serve and, for
        a request larger than the share, a ``retry_after_seconds`` that never
        pays off (#475). A status reported inside a ``0.5x`` window has to
        quote the scaled value for the same reason.

        ``refill_period_seconds`` follows the window — an absolute schedule
        entry may override it — but is never divided: shards split the
        numerator and every shard refills on the same clock.

        The result carries no schedule. It is a point-in-time materialisation,
        and leaving the schedule attached would invite a second application
        and read to a caller as "500, which halves to 250".

        ``reset_schedule`` **is** carried through, for the mirror-image reason:
        nothing here applies it, so there is nothing to apply twice, and it is
        the only thing that tells a reported status the balance returns in a
        lump at a calendar instant rather than dripping back at
        ``refill_amount``. Dropping it would leave a daily-quota rejection
        quoting a wait computed from the drip alone.

        ``reset_after`` is carried through for the identical reason, and the
        ``is_quota`` carve-out on the rate floor already covers it because that
        predicate is structural and asks both spellings.

        Shares are floored to one whole token because ``Limit`` is whole-token
        and must stay constructible; ``schema.MAX_SHARD_COUNT`` bounds how
        small a real share can get, and a ``0.5x`` window on a share of one
        would otherwise raise from inside a rejection path. Surfacing an
        unadmittable request as an event or metric is tracked in #475.

        That floor is **not** applied to a quota's ``refill_amount``, which is
        zero and must stay zero (ADR-137). Flooring it to one would both invent
        a drip the operator never configured and make the result
        unconstructible, since a positive rate alongside the ``reset_schedule``
        this carries through is exactly what validation rejects. The carve-out
        asks :attr:`is_quota`, the **structural** predicate, deliberately: no
        drip, scaled by any window and divided by any shard count, is still no
        drip, so the question must not depend on ``now_ms``. The temporal
        predicate would be the wrong one here — it is also true of a dripping
        limit whose share has floored away, and zeroing *that* limit's rate
        would report a recovering limit as one that never recovers.
        """
        if shard_count <= 1 and not self.schedule:
            return self
        # Milli-units, so this floors exactly where `BucketState` does: a
        # status built from a config `Limit` and one built from the bucket
        # item must agree to the token.
        cp_milli, ra_milli, rp_ms = effective_params(
            self.capacity * 1000,
            self.refill_amount * 1000,
            self.refill_period_seconds * 1000,
            self.schedule,
            now_ms,
        )
        divisor = max(1, shard_count)
        return replace(
            self,
            capacity=max(1, (cp_milli // divisor) // 1000),
            refill_amount=(0 if self.is_quota else max(1, (ra_milli // divisor) // 1000)),
            refill_period_seconds=max(1, rp_ms // 1000),
            schedule=(),
        )

    @classmethod
    def _carrier(cls, state: "BucketState") -> "Limit":
        """Limit view of a reserved infrastructure bucket such as ``wcu``.

        The slow path carries the ``wcu`` bucket through a lease so its refill
        is written back with the other limits (ADR-133); it is never declared,
        never gated and never reported. The reserved name is rejected by the
        public constructors on purpose, so this bypasses validation.
        """
        obj = object.__new__(cls)
        object.__setattr__(obj, "name", state.limit_name)
        object.__setattr__(obj, "capacity", max(1, state.capacity_milli // 1000))
        object.__setattr__(obj, "refill_amount", max(1, state.refill_amount_milli // 1000))
        object.__setattr__(obj, "refill_period_seconds", max(1, state.refill_period_ms // 1000))
        # `wcu` is never scheduled — it tracks partition write pressure, not a
        # user limit. Set explicitly rather than leaning on the class-level
        # dataclass default, which a future `field(default_factory=...)` would
        # remove out from under this constructor.
        object.__setattr__(obj, "schedule", ())
        object.__setattr__(obj, "reset_schedule", ())
        object.__setattr__(obj, "reset_after", None)
        return obj


@dataclass
class Entity:
    """
    An entity that can have rate limits applied.

    Entities can be parents (projects) or children (API keys).
    Children have a parent_id reference.

    Note: This model does not validate in __post_init__ to support DynamoDB
    deserialization and avoid performance overhead. Validation is performed
    in Repository.create_entity() at the API boundary.
    """

    id: str
    name: str | None = None
    parent_id: str | None = None
    cascade: bool = False
    metadata: dict[str, str] = field(default_factory=dict)
    created_at: str | None = None

    @property
    def is_parent(self) -> bool:
        """True if this entity has no parent (is a root/project)."""
        return self.parent_id is None

    @property
    def is_child(self) -> bool:
        """True if this entity has a parent."""
        return self.parent_id is not None


@dataclass
class LimitStatus:
    """
    Status of a specific limit check.

    Returned in RateLimitExceeded to provide full visibility into
    all limits that were checked.

    Note: This is an internal model created by the limiter from validated
    inputs. No validation is performed here to avoid performance overhead.
    """

    entity_id: str
    resource: str
    limit_name: str
    limit: Limit
    available: int  # current available (can be negative)
    requested: int  # amount requested
    exceeded: bool  # True if this limit was exceeded
    retry_after_seconds: float  # time until `requested` is available (0 if not exceeded)
    # The absolute instant this limit's allowance returns, epoch ms, or None:
    # for a rate limit, for a calendar quota (whose edge is recoverable from
    # the clock plus the cron, so `RateLimitExceeded.as_dict` scans for it),
    # and for a duration quota with no live window.
    #
    # It has to live here rather than being derived from `limit` because a
    # duration window's anchor (`ws`) is on the BUCKET, not in the config
    # (ADR-139). A per-entity window is recoverable from nothing but the item.
    #
    # Populated at all four construction sites — `bucket.declared_statuses`,
    # `RateLimiter._admit_limit`, `lease._build_retry_failure_statuses` and
    # `RateLimiter.check_availability` — the same four #222 §7 wired the
    # boundary-aware `retry_after_seconds` at.
    resets_at_ms: int | None = None

    @property
    def deficit(self) -> int:
        """How many tokens short we are (0 if not exceeded)."""
        return max(0, self.requested - self.available)


@dataclass(frozen=True)
class Availability:
    """
    Combined result of a non-consuming capacity check (issue #472).

    Answers both "how much is left?" and "how long until I can proceed?" for
    **every** resolved limit, from a single config resolution and a single
    bucket read taken at one instant. Returned by
    :meth:`RateLimiter.check_availability`.

    The per-limit detail lives in :attr:`statuses`, reusing the same
    :class:`LimitStatus` that :class:`~zae_limiter.exceptions.RateLimitExceeded`
    carries, so a caller that renders a rejection and a caller that renders a
    dashboard read the same shape. Everything else on this class is derived
    from those statuses, which is what makes the numbers consistent: they all
    come from one snapshot rather than from separate reads at separate
    instants.

    One difference from the statuses in ``RateLimitExceeded``: those report a
    single shard's **share** of the limit (``Limit.per_shard()``, #475),
    because a rejection happened on one shard. These report the entity-wide
    total summed across every shard, against the undivided configured
    :class:`Limit` — which is the number a user's "N remaining" display means.

    Note: This is an internal model created by the limiter from validated
    inputs. No validation is performed here to avoid performance overhead.
    """

    entity_id: str
    resource: str
    # Epoch milliseconds the snapshot was taken at. Every number below is
    # relative to this instant, so a client can tick a countdown locally.
    checked_at_ms: int
    # One entry per resolved limit, in resolution order
    statuses: list[LimitStatus]

    def status(self, limit_name: str) -> LimitStatus | None:
        """The status for one limit, or None if it was not resolved."""
        return next((s for s in self.statuses if s.limit_name == limit_name), None)

    @property
    def limits(self) -> list[Limit]:
        """The resolved limits this snapshot was taken against."""
        return [status.limit for status in self.statuses]

    @property
    def available(self) -> dict[str, int]:
        """limit_name -> currently available tokens (negative if in debt)."""
        return {status.limit_name: status.available for status in self.statuses}

    @property
    def needed(self) -> dict[str, int]:
        """limit_name -> amount the caller asked about.

        Empty when none was given. Keys that named no resolved limit are not
        echoed back, since nothing was evaluated for them.
        """
        return {s.limit_name: s.requested for s in self.statuses if s.requested > 0}

    @property
    def retry_after_seconds(self) -> float:
        """Seconds until every needed amount is available (0.0 if already so)."""
        return max((status.retry_after_seconds for status in self.statuses), default=0.0)

    @property
    def allowed(self) -> bool:
        """True if every needed amount is currently available."""
        return not self.exceeded

    @property
    def exceeded(self) -> list[str]:
        """Names of the limits that are short of the needed amount."""
        return [status.limit_name for status in self.statuses if status.exceeded]

    @property
    def deficit(self) -> dict[str, int]:
        """How many tokens short each exceeded limit is (exceeded limits only)."""
        return {s.limit_name: s.deficit for s in self.statuses if s.exceeded}


@dataclass
class BucketState:
    """
    Internal state of a token bucket.

    All token values are stored in millitokens (x1000) for precision.

    Note: This is an internal model. Validation is performed in from_limit()
    for user-provided inputs, not in __post_init__ to support DynamoDB
    deserialization and avoid performance overhead on frequent operations.
    """

    entity_id: str
    resource: str
    limit_name: str
    tokens_milli: int  # current tokens (in millitokens)
    last_refill_ms: int  # epoch milliseconds
    capacity_milli: int  # max tokens / ceiling (in millitokens)
    refill_amount_milli: int  # refill numerator (in millitokens)
    refill_period_ms: int  # refill denominator (in milliseconds)
    # Net consumption counter (millitokens). Stored as FLAT top-level attribute
    # (not in nested data.M) to enable atomic ADD operations. See issue #179.
    # None means counter not yet initialized (old bucket).
    total_consumed_milli: int | None = None
    # Number of shards this bucket is split across (GHSA-76rv, ADR-133). The
    # stored cp/ra stay undivided; refill math must use the effective
    # per-shard share below or every shard refills to the full capacity and
    # the entity admits shard_count x capacity. The reserved `wcu` limit is
    # per-partition and is never divided (its shard_count stays 1).
    shard_count: int = 1
    # Compact-encoded schedule decoded from the bucket item (#222 §4.1). Empty
    # for an unscheduled bucket, which is the overwhelming majority — the
    # effective methods below then return the stored values unchanged.
    sched: tuple[ScheduleEntry, ...] = ()
    # The reset schedule the item carries (#222 §3.6), decoded from `rsched` /
    # `b_{name}_rsched`. Nothing here applies it — the edge detection is the
    # materialising pass's job — but `build_composite_create` stamps it onto a
    # newly created bucket from here, exactly as it does `sched`. Like `sched`,
    # it is populated by `from_limit` only: `_deserialize_composite_bucket`
    # reads the base params and neither tuple (surface plan Task 5).
    reset_sched: tuple[ScheduleEntry, ...] = ()

    # Start of the current duration window, epoch ms (ADR-139). `None` means
    # the window has not started — which for a client-created bucket never
    # happens, because a bucket is created BY a use, but which a shard stamped
    # before the limit gained its window can carry until the next fan-out.
    #
    # Entity-wide, replicated verbatim to every shard: only the balance is
    # divided. Each shard resets itself when it observes `ws > rf`, the same
    # rule `RateLimiter._apply_reset_edge` uses for a cron, so the rollover
    # fan-out moves this scalar and never `tk`.
    window_start_ms: int | None = None
    # The window's length in seconds, denormalised from config so a
    # materialiser needs no config read — the aggregator reads the item and
    # nothing else. Never divided by `shard_count`.
    reset_after_seconds: int | None = None

    @property
    def tokens(self) -> int:
        """Current tokens (not millitokens)."""
        return self.tokens_milli // 1000

    @property
    def capacity(self) -> int:
        """Capacity / ceiling (not millitokens)."""
        return self.capacity_milli // 1000

    def _scheduled_params(self, now_ms: int) -> tuple[int, int, int]:
        """The undivided (capacity, refill_amount, refill_period) at ``now_ms``.

        Returns the stored base unchanged when ``sched`` is empty, which is the
        overwhelming majority of buckets.
        """
        return effective_params(
            self.capacity_milli,
            self.refill_amount_milli,
            self.refill_period_ms,
            self.sched,
            now_ms,
        )

    def effective_capacity_milli(self, now_ms: int) -> int:
        """This shard's share of the capacity in force at ``now_ms``.

        Scale first, divide second (#222 §2.1): the schedule applies to the
        whole limit and the shards split the result. Dividing first floors
        against a smaller numerator and drifts below the intended share.
        """
        cp, _ra, _rp = self._scheduled_params(now_ms)
        return cp // self.shard_count

    def effective_refill_amount_milli(self, now_ms: int) -> int:
        """This shard's share of the refill in force at ``now_ms``.

        Scale first, divide second, exactly as ``effective_capacity_milli``.
        """
        _cp, ra, _rp = self._scheduled_params(now_ms)
        return ra // self.shard_count

    def effective_refill_period_ms(self, now_ms: int) -> int:
        """The refill denominator in force at ``now_ms``.

        A ``ScheduleEntry`` absolute override may replace
        ``refill_period_seconds``, so the period is time-dependent exactly as
        the capacity and the refill amount are. Unlike those two it is **not**
        divided by ``shard_count``: every shard refills on the same clock and
        only the numerator is split.
        """
        _cp, _ra, rp = self._scheduled_params(now_ms)
        return rp

    @property
    def window_end_ms(self) -> int | None:
        """When the current duration window closes, or ``None`` (ADR-139).

        Derived rather than stored, so the start and the end cannot disagree
        after a partial write, and because ``reset_after_seconds`` has to be on
        the item anyway for the next window's length.
        """
        if self.window_start_ms is None or self.reset_after_seconds is None:
            return None
        return self.window_start_ms + self.reset_after_seconds * 1000

    @property
    def window_rolled(self) -> bool:
        """Has a window opened that this shard's balance does not reflect yet?

        ``ws > rf`` (ADR-139): a shard whose window start is newer than its own
        last materialisation has not applied that window. This is the single
        statement of the rule — the slow-path roll
        (``RateLimiter._apply_window_roll``) and every read-only view of the
        balance ask it here rather than restating the comparison, so the two
        cannot drift (the #489 lesson).

        Strictly ``>``: the pass that applies a roll stamps ``rf`` at or after
        ``ws``, so ``>=`` would re-fire on every later request and refund
        everything spent since. ``False`` for a bucket with no window.
        """
        return self.window_start_ms is not None and self.window_start_ms > self.last_refill_ms

    def accrues(self, now_ms: int) -> bool:
        """Is this shard gaining tokens at ``now_ms``?

        The **temporal** half of ADR-137's "this limit has no refill", bound to
        a bucket and a clock: :func:`is_accrual_rate` applied to the rate this
        shard actually refills at right now, schedule and shard split included.

        False for two unrelated reasons, which is the whole point of keeping it
        apart from :attr:`Limit.is_quota`:

        * the limit is a **quota** and never drips (``ra`` is 0 on the item); or
        * the limit drips, but *this* shard's share of the rate in force floors
          to zero — a slow limit split many ways, or a ``scale`` window that
          shrank the numerator before ``// shard_count`` reached it.

        A caller that special-cases only the first gets the second wrong
        silently, because a sharded dripping limit carries nothing that marks
        it. Anything that divides by the refill rate, or computes a wait from
        it, must ask this question; anything that decides how to *describe* the
        limit — display, validation — wants :attr:`Limit.is_quota`, which does
        not move with the clock.

        Note what this does **not** say: a bucket that is not accruing may
        still recover, at the next ``reset_schedule`` edge for a quota, or on
        the next re-materialisation for a share that a widening window brings
        back above zero.
        """
        return is_accrual_rate(self.effective_refill_amount_milli(now_ms))

    def retry_refill_amount_milli(self, now_ms: int) -> int:
        """Refill rate to use for a "seconds until available" estimate.

        ``effective_refill_amount_milli`` floors to 0 for a slow refill on a
        heavily sharded bucket (1 token/minute at ``shard_count=1024``), and a
        rate of 0 has no finite wait at all. Fall back to the *undivided* rate
        rather than raise or invent infinity: the estimate is then optimistic
        by up to ``shard_count``, but it is finite and honest about the rate
        the limit itself refills at, and a retry draws a shard at random — so
        it may well land somewhere that admits. ``schema.MAX_SHARD_COUNT``
        bounds how far apart the two can get.

        The fallback is the *scheduled* undivided rate, not the stored base
        rate: during a ``scale`` window the base rate is a speed nothing in
        the system refills at, so quoting it would under-report the wait.

        A **quota** falls through to the same fallback and gets 0 back, because
        the undivided rate is 0 too — the estimate is then not a rate question
        at all, and ``bucket.calculate_retry_after`` answers it from the next
        reset edge instead (#530). That is why the guard here is
        :func:`is_accrual_rate` and not :attr:`Limit.is_quota`: it has to catch
        the floored share, which is not a quota and must not be treated as one.

        Since #222 §7 the retry estimate is computed by
        ``schedule.retry_after_with_schedule``, which re-derives this rule per
        window from the undivided base rather than calling this — it has to,
        because ``schedule`` may not import ``models`` (that one-way dependency
        is what lets both Lambdas vendor it). This stays the definition of the
        rule, and the walk's ``_rate`` helper names it.
        """
        _cp, ra, _rp = self._scheduled_params(now_ms)
        share = ra // self.shard_count
        if is_accrual_rate(share):
            return share
        return ra

    @classmethod
    def from_limit(
        cls,
        entity_id: str,
        resource: str,
        limit: Limit,
        now_ms: int,
        shard_count: int = 1,
        reclaimed_milli: int | None = None,
    ) -> "BucketState":
        """
        Create a new bucket at full capacity from a Limit.

        Note: This is an internal factory method. Validation of entity_id
        and resource is performed at the API boundary (RateLimiter public
        methods) before calling this method.

        Args:
            entity_id: Entity identifier (pre-validated by caller)
            resource: Resource name (pre-validated by caller)
            limit: Limit configuration (validated via __post_init__)
            now_ms: Current time in milliseconds
            shard_count: Shards the bucket is split across; a new shard
                starts at its effective share, ``capacity // shard_count``
            reclaimed_milli: Millitokens the caller's eager clamp took off
                the shards that already exist for this (entity, resource,
                limit). Only a **quota** reads it, and only to take a transfer
                instead of a mint (#587) — see
                :func:`new_shard_starting_tokens_milli`. ``None`` (the default,
                and every dripping limit) keeps the full share.
        """
        capacity_milli = limit.capacity * 1000
        state = cls(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit.name,
            tokens_milli=0,  # replaced below, once `sched` can be consulted
            last_refill_ms=now_ms,
            capacity_milli=capacity_milli,
            refill_amount_milli=limit.refill_amount * 1000,
            refill_period_ms=limit.refill_period_seconds * 1000,
            total_consumed_milli=0,  # initialize counter for new buckets
            shard_count=shard_count,
            # The stored cp/ra/rp stay the undivided base forever (#222 §2.1);
            # the schedule rides alongside so every reader can recompute the
            # effective params at its own instant.
            sched=limit.schedule,
            # Stamped onto the item beside `sched`: both refillers read the
            # schedules off the item and nothing else, so a bucket born
            # carrying `vu` but no `rsched` would never reset.
            reset_sched=limit.reset_schedule,
            # Stamped beside `rsched` and for the identical reason: both
            # refillers read the schedules off the item and nothing else, so a
            # bucket born carrying `vu` but no window is a bucket whose
            # `refill_amount` is 0 and which nothing ever resets.
            reset_after_seconds=limit.reset_after_seconds,
            # A bucket is created BY a use, so its window starts now. The one
            # caller that must override this is the shard-create path, which
            # inherits the entity's existing `ws` from a sibling (Task 8) —
            # a new shard joins the window in progress rather than opening one.
            window_start_ms=(now_ms if limit.reset_after is not None else None),
        )
        # Start at full capacity *as of now* — the scheduled share, not the
        # base one. A bucket born inside a `0.5x` window that started at the
        # base ceiling would hand out a full unscaled allowance before any
        # refiller trimmed it, which is exactly the window the schedule exists
        # to narrow.
        #
        # A quota being added to shards that already exist takes a transfer of
        # its siblings' surplus instead of a fresh share, because it has no
        # rate for a mint to amortise against (#587).
        state.tokens_milli = new_shard_starting_tokens_milli(
            state.effective_capacity_milli(now_ms),
            reclaimed_milli,
            is_quota=limit.is_quota,
        )
        return state


@dataclass
class UsageSnapshot:
    """
    Aggregated usage for a time window.

    Created by the aggregator Lambda from DynamoDB stream events.
    Tracks token consumption per limit type within a time window.

    Attributes:
        entity_id: Entity that consumed tokens
        resource: Resource being rate-limited (e.g., "gpt-4")
        window_start: ISO timestamp of window start (e.g., "2024-01-01T14:00:00Z")
        window_end: ISO timestamp of window end
        window_type: Window granularity ("hourly", "daily")
        counters: Consumption by limit name (e.g., {"tpm": 5000, "rpm": 10})
        total_events: Number of consumption events in this window
    """

    entity_id: str
    resource: str
    window_start: str  # ISO timestamp
    window_end: str  # ISO timestamp
    window_type: str  # "hourly", "daily"
    counters: dict[str, int]  # limit_name -> total consumed
    total_events: int


@dataclass
class UsageSummary:
    """
    Aggregated usage summary across multiple snapshots.

    Returned by `RateLimiter.get_usage_summary()` to provide
    total and average consumption statistics over a time range.

    Attributes:
        snapshot_count: Number of snapshots aggregated
        total: Sum of consumption by limit name (e.g., {"tpm": 50000, "rpm": 100})
        average: Average consumption per snapshot by limit name
        min_window_start: Earliest snapshot window start (ISO timestamp)
        max_window_start: Latest snapshot window start (ISO timestamp)

    Example:
        summary = await limiter.get_usage_summary(
            entity_id="user-123",
            resource="gpt-4",
            window_type="hourly",
        )
        print(f"Total tokens: {summary.total.get('tpm', 0)}")
        print(f"Average per hour: {summary.average.get('tpm', 0.0):.1f}")
    """

    snapshot_count: int
    total: dict[str, int]  # limit_name -> sum of consumption
    average: dict[str, float]  # limit_name -> average per snapshot
    min_window_start: str | None  # Earliest window (ISO timestamp)
    max_window_start: str | None  # Latest window (ISO timestamp)


@dataclass(frozen=True)
class LimiterInfo:
    """
    Information about a deployed rate limiter instance.

    Represents a CloudFormation stack discovered in a region via
    ``RateLimiter.list_deployed()`` or the ``zae-limiter list`` CLI command.
    This is a READ-ONLY model describing observed infrastructure state.

    Example:
        # Discover all limiters in us-east-1
        limiters = await RateLimiter.list_deployed(region="us-east-1")
        for limiter in limiters:
            if limiter.is_failed:
                print(f"⚠️  {limiter.user_name}: {limiter.stack_status}")

    Attributes:
        stack_name: Full CloudFormation stack name (e.g., "my-app")
        user_name: User-friendly name (e.g., "my-app")
        region: AWS region where the stack is deployed
        stack_status: CloudFormation stack status (e.g., "CREATE_COMPLETE")
        creation_time: ISO 8601 timestamp of stack creation
        last_updated_time: ISO 8601 timestamp of last update (None if never updated)
        version: Value of zae-limiter:version tag (client version at deployment)
        lambda_version: Value of zae-limiter:lambda-version tag
        schema_version: Value of zae-limiter:schema-version tag
    """

    # Identity
    stack_name: str
    user_name: str
    region: str

    # Status
    stack_status: str
    creation_time: str
    last_updated_time: str | None = None

    # Version info from CloudFormation tags
    version: str | None = None
    lambda_version: str | None = None
    schema_version: str | None = None

    # Stack type (e.g., "limiter", "load-test")
    stack_type: str | None = None

    @property
    def is_healthy(self) -> bool:
        """Stack is in a stable, operational state."""
        return self.stack_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE")

    @property
    def is_in_progress(self) -> bool:
        """Stack operation is in progress."""
        return "IN_PROGRESS" in self.stack_status

    @property
    def is_failed(self) -> bool:
        """Stack is in a failed or rollback state."""
        return any(x in self.stack_status for x in ("FAILED", "ROLLBACK"))


@dataclass
class ResourceCapacity:
    """Aggregated capacity info for a resource across entities."""

    resource: str
    limit_name: str
    total_capacity: int
    total_available: int
    utilization_pct: float
    entities: list["EntityCapacity"]


@dataclass
class EntityCapacity:
    """Capacity info for a single entity."""

    entity_id: str
    capacity: int
    available: int
    utilization_pct: float


class LimitName:
    """Common limit name constants."""

    RPM = "rpm"  # requests per minute
    RPH = "rph"  # requests per hour
    RPD = "rpd"  # requests per day
    TPM = "tpm"  # tokens per minute
    TPH = "tph"  # tokens per hour
    TPD = "tpd"  # tokens per day


# IAM role component suffixes (ADR-116)
# Invariant: all components must be <= 8 characters
ROLE_COMPONENTS = ("aggr", "app", "admin", "read", "prov")

# Valid CloudWatch Logs retention periods (in days)
# See: https://docs.aws.amazon.com/AmazonCloudWatchLogs/latest/APIReference/API_PutRetentionPolicy.html
VALID_LOG_RETENTION_DAYS = frozenset(
    {
        1,
        3,
        5,
        7,
        14,
        30,
        60,
        90,
        120,
        150,
        180,
        365,
        400,
        545,
        731,
        1096,
        1827,
        2192,
        2557,
        2922,
        3288,
        3653,
    }
)


# IAM Role ARN pattern for validation
IAM_ROLE_ARN_PATTERN = re.compile(r"^arn:(aws|aws-cn|aws-us-gov):iam::\d{12}:role/.+$")


@dataclass(frozen=True)
class StackOptions:
    """
    Configuration options for CloudFormation stack creation and updates.

    When passed to RateLimiter constructor, triggers automatic stack creation.
    When None is passed (default), no stack creation is attempted.

    Attributes:
        snapshot_windows: Comma-separated list of snapshot windows (e.g., "hourly,daily")
        usage_retention_days: Number of days to retain usage snapshots
        audit_retention_days: Number of days to retain audit records in DynamoDB
        enable_aggregator: Deploy Lambda aggregator for usage snapshots
        enable_provisioner: Deploy Lambda provisioner for declarative limits
        pitr_recovery_days: Point-in-Time Recovery period (1-35, None for AWS default)
        log_retention_days: CloudWatch log retention period in days (must be valid CloudWatch value)
        lambda_timeout: Lambda timeout in seconds (1-900)
        lambda_memory: Lambda memory size in MB (128-3008)
        enable_alarms: Deploy CloudWatch alarms for monitoring
        alarm_sns_topic: SNS topic ARN for alarm notifications
        lambda_duration_threshold_pct: Duration alarm threshold as percentage of timeout (1-100)
        permission_boundary: IAM permission boundary (policy name or full ARN)
        role_name_format: Format template for role name, {} = default role name
        policy_name_format: Format template for managed policy name, {} = default policy name
        enable_audit_archival: Archive expired audit events to S3 via TTL
        audit_archive_glacier_days: Days before transitioning archives to Glacier IR (1-3650)
        enable_tracing: Enable AWS X-Ray tracing for Lambda aggregator
        create_iam_roles: Create App/Admin/ReadOnly IAM roles (default: False).
            Managed policies are always created unless create_iam=False.
        create_iam: Create IAM resources (policies and roles). Set to False for
            restricted IAM environments (e.g., PowerUserAccess). When False,
            aggregator is disabled unless aggregator_role_arn is provided.
        aggregator_role_arn: ARN of an existing IAM role for the Lambda aggregator.
            Use this when deploying without iam:CreateRole permissions.
        enable_deletion_protection: Enable DynamoDB table deletion protection
        tags: User-defined tags to apply to the CloudFormation stack. Dict of key-value
            pairs. AWS tag constraints apply (max 50 total including managed tags,
            key 1-128 chars, value 0-256 chars). The ``aws:`` prefix is reserved.
    """

    snapshot_windows: str = "hourly,daily"
    usage_retention_days: int = 90
    audit_retention_days: int = 90
    enable_aggregator: bool = True
    enable_provisioner: bool = True
    pitr_recovery_days: int | None = None
    log_retention_days: int = 30
    lambda_timeout: int = 60
    lambda_memory: int = 256
    enable_alarms: bool = True
    alarm_sns_topic: str | None = None
    lambda_duration_threshold_pct: int = 80
    permission_boundary: str | None = None
    role_name_format: str | None = None
    policy_name_format: str | None = None
    enable_audit_archival: bool = True
    audit_archive_glacier_days: int = 90
    enable_tracing: bool = False
    create_iam_roles: bool = False
    create_iam: bool = True
    aggregator_role_arn: str | None = None
    enable_deletion_protection: bool = False
    tags: dict[str, str] | None = None

    def __post_init__(self) -> None:
        """Validate options and emit deprecation warning."""
        warnings.warn(
            "StackOptions is deprecated. Use Repository.builder(...) with "
            "fluent methods instead. This will be removed in v1.0.0.",
            DeprecationWarning,
            stacklevel=2,
        )
        if not (1 <= self.lambda_timeout <= 900):
            raise ValueError("lambda_timeout must be between 1 and 900")
        if not (128 <= self.lambda_memory <= 3008):
            raise ValueError("lambda_memory must be between 128 and 3008")
        if not (1 <= self.lambda_duration_threshold_pct <= 100):
            raise ValueError("lambda_duration_threshold_pct must be between 1 and 100")
        if self.pitr_recovery_days is not None and not (1 <= self.pitr_recovery_days <= 35):
            raise ValueError("pitr_recovery_days must be between 1 and 35")
        if self.usage_retention_days <= 0:
            raise ValueError("usage_retention_days must be positive")
        if self.audit_retention_days <= 0:
            raise ValueError("audit_retention_days must be positive")
        if self.log_retention_days not in VALID_LOG_RETENTION_DAYS:
            raise ValueError(
                f"log_retention_days must be one of {sorted(VALID_LOG_RETENTION_DAYS)}"
            )
        # Validate role_name_format contains exactly one {}
        if self.role_name_format is not None:
            placeholder_count = self.role_name_format.count("{}")
            if placeholder_count != 1:
                raise ValueError(
                    f"role_name_format must contain exactly one '{{}}' placeholder, "
                    f"found {placeholder_count}"
                )
            # Validate resulting name won't exceed IAM limits (64 chars)
            # Formula: 64 - max_component(8) - dash(1) = 55 max format length
            # See ADR-116 for role naming convention
            format_len = len(self.role_name_format)
            if format_len > 55:
                raise ValueError(
                    "role_name_format template is too long, resulting role name "
                    "may exceed IAM 64 character limit"
                )
        # Validate policy_name_format contains exactly one {}
        if self.policy_name_format is not None:
            placeholder_count = self.policy_name_format.count("{}")
            if placeholder_count != 1:
                raise ValueError(
                    f"policy_name_format must contain exactly one '{{}}' placeholder, "
                    f"found {placeholder_count}"
                )
            # IAM managed policy names max 128 chars
            # Policy components: ns-full is the longest at 7 chars
            # Formula: 128 - max_component(7) - dash(1) = 120 max format length
            format_len = len(self.policy_name_format)
            if format_len > 120:
                raise ValueError(
                    "policy_name_format template is too long, resulting policy name "
                    "may exceed IAM 128 character limit"
                )
        # Validate audit archival options
        if not (1 <= self.audit_archive_glacier_days <= 3650):
            raise ValueError("audit_archive_glacier_days must be between 1 and 3650")
        # Validate conflicting IAM flags
        if not self.create_iam and self.create_iam_roles:
            raise ValueError(
                "create_iam_roles=True cannot be used with create_iam=False. "
                "Roles require IAM permissions to create."
            )
        # Validate aggregator_role_arn format if provided
        if self.aggregator_role_arn is not None:
            if not IAM_ROLE_ARN_PATTERN.match(self.aggregator_role_arn):
                raise ValueError(
                    f"aggregator_role_arn must be a valid IAM role ARN, "
                    f"got: {self.aggregator_role_arn}"
                )
        # Validate user-defined tags
        if self.tags is not None:
            if len(self.tags) > 45:
                raise ValueError(
                    "tags exceeds maximum of 45 user-defined tags "
                    "(50 total including 5 managed tags)"
                )
            for key, value in self.tags.items():
                if key.startswith("aws:"):
                    raise ValueError(f"tag key '{key}' uses reserved 'aws:' prefix")
                if not (1 <= len(key) <= 128):
                    raise ValueError(f"tag key '{key}' must be 1-128 characters")
                if len(value) > 256:
                    raise ValueError(f"tag value for '{key}' exceeds 256 characters")

    def get_role_name(self, stack_name: str, component: str) -> str | None:
        """
        Get the final role name for a given stack name and component.

        Args:
            stack_name: Stack name
            component: Role component (aggr, app, admin, read)

        Returns:
            Final role name, or None if role_name_format not set

        Raises:
            ValidationError: If resulting name exceeds 64 characters
        """
        if self.role_name_format is None:
            return None
        role_name = self.role_name_format.replace("{}", f"{stack_name}-{component}")
        if len(role_name) > 64:
            format_overhead = len(self.role_name_format) - 2  # subtract {}
            max_stack_len = 64 - format_overhead - 1 - len(component)  # -1 for dash
            from .exceptions import ValidationError

            raise ValidationError(
                "role_name",
                role_name,
                f"exceeds IAM 64-character limit by {len(role_name) - 64} characters. "
                f"Shorten stack name to max {max_stack_len} characters with this format.",
            )
        return role_name

    def get_policy_name(self, stack_name: str, component: str) -> str | None:
        """
        Get the final policy name for a given stack name and component.

        Args:
            stack_name: Stack name
            component: Policy component (app, admin, read)

        Returns:
            Final policy name, or None if policy_name_format not set

        Raises:
            ValidationError: If resulting name exceeds 128 characters
        """
        if self.policy_name_format is None:
            return None
        policy_name = self.policy_name_format.replace("{}", f"{stack_name}-{component}")
        if len(policy_name) > 128:
            format_overhead = len(self.policy_name_format) - 2  # subtract {}
            max_stack_len = 128 - format_overhead - 1 - len(component)  # -1 for dash
            from .exceptions import ValidationError

            raise ValidationError(
                "policy_name",
                policy_name,
                f"exceeds IAM 128-character limit by {len(policy_name) - 128} characters. "
                f"Shorten stack name to max {max_stack_len} characters with this format.",
            )
        return policy_name

    @property
    def deploys_aggregator_lambda(self) -> bool:
        """Whether CloudFormation will create the aggregator Lambda function.

        Mirrors the template's ``DeployAggregatorLambda`` condition: the
        aggregator needs a role, so it is only created when IAM resources are
        enabled or an external role ARN was supplied.
        """
        return self.enable_aggregator and (self.create_iam or self.aggregator_role_arn is not None)

    @property
    def deploys_provisioner_lambda(self) -> bool:
        """Whether CloudFormation will create the limits provisioner Lambda function.

        Mirrors the template's ``DeployProvisionerLambda`` condition. The
        provisioner has no external-role escape hatch, so it requires
        ``create_iam``.
        """
        return self.enable_provisioner and self.create_iam

    def to_parameters(self, stack_name: str | None = None) -> dict[str, str]:
        """
        Convert to stack parameters dict for StackManager.

        Args:
            stack_name: Stack name for role_name_format substitution

        Returns:
            Dict with snake_case keys matching stack_manager parameter mapping.
        """
        lambda_duration_threshold_ms = int(
            self.lambda_timeout * 1000 * (self.lambda_duration_threshold_pct / 100)
        )
        params: dict[str, str] = {
            "snapshot_windows": self.snapshot_windows,
            "usage_retention_days": str(self.usage_retention_days),
            "enable_aggregator": "true" if self.enable_aggregator else "false",
            "enable_provisioner": "true" if self.enable_provisioner else "false",
            "log_retention_days": str(self.log_retention_days),
            "lambda_timeout": str(self.lambda_timeout),
            "lambda_memory_size": str(self.lambda_memory),
            "enable_alarms": "true" if self.enable_alarms else "false",
            "lambda_duration_threshold": str(lambda_duration_threshold_ms),
            "enable_tracing": "true" if self.enable_tracing else "false",
            "enable_iam_roles": "true" if self.create_iam_roles else "false",
        }
        if self.pitr_recovery_days is not None:
            params["pitr_recovery_days"] = str(self.pitr_recovery_days)
        if self.alarm_sns_topic:
            params["alarm_sns_topic_arn"] = self.alarm_sns_topic
        if self.permission_boundary:
            params["permission_boundary"] = self.permission_boundary
        # Generate 5 separate role name parameters (ADR-116)
        # get_role_name returns str when role_name_format is set (which we check above)
        if self.role_name_format and stack_name:
            # Export the format template for dependent stacks (e.g., stress test)
            params["role_name_format"] = self.role_name_format
            aggregator_role = self.get_role_name(stack_name, "aggr")
            app_role = self.get_role_name(stack_name, "app")
            admin_role = self.get_role_name(stack_name, "admin")
            readonly_role = self.get_role_name(stack_name, "read")
            provisioner_role = self.get_role_name(stack_name, "prov")
            # These assertions are guaranteed by the if condition above
            assert aggregator_role is not None
            assert app_role is not None
            assert admin_role is not None
            assert readonly_role is not None
            assert provisioner_role is not None
            params["aggregator_role_name"] = aggregator_role
            params["app_role_name"] = app_role
            params["admin_role_name"] = admin_role
            params["readonly_role_name"] = readonly_role
            params["provisioner_role_name"] = provisioner_role
        # Generate 3 table-level + 3 namespace-scoped policy name parameters
        if self.policy_name_format and stack_name:
            acquire_only_policy = self.get_policy_name(stack_name, "acq")
            full_access_policy = self.get_policy_name(stack_name, "full")
            readonly_policy = self.get_policy_name(stack_name, "read")
            assert acquire_only_policy is not None
            assert full_access_policy is not None
            assert readonly_policy is not None
            params["acquire_only_policy_name"] = acquire_only_policy
            params["full_access_policy_name"] = full_access_policy
            params["readonly_policy_name"] = readonly_policy
            # Namespace-scoped policies
            ns_acquire_policy = self.get_policy_name(stack_name, "ns-acq")
            ns_full_policy = self.get_policy_name(stack_name, "ns-full")
            ns_readonly_policy = self.get_policy_name(stack_name, "ns-read")
            assert ns_acquire_policy is not None
            assert ns_full_policy is not None
            assert ns_readonly_policy is not None
            params["namespace_acquire_policy_name"] = ns_acquire_policy
            params["namespace_full_access_policy_name"] = ns_full_policy
            params["namespace_readonly_policy_name"] = ns_readonly_policy
        # Audit archival parameters
        params["enable_audit_archival"] = "true" if self.enable_audit_archival else "false"
        params["audit_archive_glacier_days"] = str(self.audit_archive_glacier_days)
        # IAM resource creation controls
        params["enable_iam"] = "true" if self.create_iam else "false"
        if self.aggregator_role_arn:
            params["aggregator_role_arn"] = self.aggregator_role_arn
        # Deletion protection parameter
        params["enable_deletion_protection"] = (
            "true" if self.enable_deletion_protection else "false"
        )
        return params


@dataclass
class Status:
    """
    Comprehensive status of a rate limiter instance.

    Consolidates connectivity, infrastructure, identity, versions, and table
    metrics into a single status object. Used by the CLI ``status`` command.

    Attributes:
        available: Whether DynamoDB is reachable and responding
        latency_ms: Round-trip latency in milliseconds (None if unavailable)
        stack_status: CloudFormation stack status (e.g., 'CREATE_COMPLETE')
        table_status: DynamoDB table status (e.g., 'ACTIVE')
        aggregator_enabled: Whether Lambda aggregator is deployed
        name: Resource name
        region: AWS region (None if using default)
        schema_version: Deployed schema version
        lambda_version: Deployed Lambda version
        client_version: Current client library version
        table_item_count: Approximate item count in table
        table_size_bytes: Approximate table size in bytes
        app_role_arn: IAM role ARN for applications (None if roles disabled)
        admin_role_arn: IAM role ARN for administrators (None if roles disabled)
        readonly_role_arn: IAM role ARN for read-only access (None if roles disabled)
    """

    # Connectivity
    available: bool
    latency_ms: float | None

    # Infrastructure
    stack_status: str | None
    table_status: str | None
    aggregator_enabled: bool

    # Identity
    name: str
    region: str | None

    # Versions
    schema_version: str | None
    lambda_version: str | None
    client_version: str

    # Table metrics
    table_item_count: int | None
    table_size_bytes: int | None

    # IAM Roles (Issue #132)
    app_role_arn: str | None = None
    admin_role_arn: str | None = None
    readonly_role_arn: str | None = None


class AuditAction:
    """Audit action type constants."""

    ENTITY_CREATED = "entity_created"
    ENTITY_DELETED = "entity_deleted"
    LIMITS_SET = "limits_set"
    LIMITS_DELETED = "limits_deleted"


@dataclass
class AuditEvent:
    """
    Security audit event for tracking modifications.

    Audit events are logged for security-sensitive operations:
    - Entity creation and deletion
    - Limit configuration changes

    Attributes:
        event_id: Unique identifier for the event (timestamp-based)
        timestamp: ISO timestamp when the event occurred
        action: Type of action (see AuditAction constants)
        entity_id: ID of the entity affected
        principal: Caller identity who performed the action (optional)
        resource: Resource name for limit-related actions (optional)
        details: Additional action-specific details
    """

    event_id: str
    timestamp: str
    action: str
    entity_id: str
    principal: str | None = None
    resource: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary for storage."""
        result: dict[str, Any] = {
            "event_id": self.event_id,
            "timestamp": self.timestamp,
            "action": self.action,
            "entity_id": self.entity_id,
        }
        if self.principal is not None:
            result["principal"] = self.principal
        if self.resource is not None:
            result["resource"] = self.resource
        if self.details:
            result["details"] = self.details
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuditEvent":
        """Deserialize from dictionary."""
        return cls(
            event_id=data["event_id"],
            timestamp=data["timestamp"],
            action=data["action"],
            entity_id=data["entity_id"],
            principal=data.get("principal"),
            resource=data.get("resource"),
            details=data.get("details", {}),
        )
