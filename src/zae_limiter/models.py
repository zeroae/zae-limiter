"""Core models for zae-limiter."""

import re
import warnings
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from .exceptions import InvalidIdentifierError, InvalidNameError
from .schedule import ScheduleEntry, effective_params

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
    """
    zones = {limit.schedule[0].tz for limit in limits if limit.schedule}
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
        refill_amount: Numerator of refill rate
        refill_period_seconds: Denominator of refill rate
        schedule: Time windows in which different parameters apply (#222).
            The fields above stay the *base* parameters forever; the schedule
            is applied on top of them at read time by
            ``schedule.effective_params()``.
    """

    name: str
    capacity: int
    refill_amount: int
    refill_period_seconds: int
    schedule: tuple[ScheduleEntry, ...] = ()

    def __post_init__(self) -> None:
        validate_name(self.name, "name")
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.refill_amount <= 0:
            raise ValueError("refill_amount must be positive")
        if self.refill_period_seconds <= 0:
            raise ValueError("refill_period_seconds must be positive")
        if self.schedule:
            zones = {entry.tz for entry in self.schedule}
            if len(zones) > 1:
                raise ValueError(
                    f"all schedule entries on one limit must share a timezone, got "
                    f"{sorted(zones)}. The timezone is stored once per item as "
                    f"`sched_tz`, not per entry."
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
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Limit":
        """Deserialize from dictionary."""
        return cls(
            name=data["name"],
            capacity=data["capacity"],
            refill_amount=data["refill_amount"],
            refill_period_seconds=data["refill_period_seconds"],
            schedule=tuple(ScheduleEntry(**entry) for entry in data.get("schedule", ())),
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
        """
        return cls(
            name=state.limit_name,
            capacity=max(1, state.capacity_milli // 1000),
            refill_amount=max(1, state.refill_amount_milli // 1000),
            refill_period_seconds=max(1, state.refill_period_ms // 1000),
            schedule=state.sched,
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

        Shares are floored to one whole token because ``Limit`` is whole-token
        and must stay constructible; ``schema.MAX_SHARD_COUNT`` bounds how
        small a real share can get, and a ``0.5x`` window on a share of one
        would otherwise raise from inside a rejection path. Surfacing an
        unadmittable request as an event or metric is tracked in #475.
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
            refill_amount=max(1, (ra_milli // divisor) // 1000),
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
        """
        share = self.effective_refill_amount_milli(now_ms)
        if share:
            return share
        _cp, ra, _rp = self._scheduled_params(now_ms)
        return ra

    @classmethod
    def from_limit(
        cls,
        entity_id: str,
        resource: str,
        limit: Limit,
        now_ms: int,
        shard_count: int = 1,
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
        """
        capacity_milli = limit.capacity * 1000
        return cls(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit.name,
            tokens_milli=capacity_milli // shard_count,  # start at full (per-shard) capacity
            last_refill_ms=now_ms,
            capacity_milli=capacity_milli,
            refill_amount_milli=limit.refill_amount * 1000,
            refill_period_ms=limit.refill_period_seconds * 1000,
            total_consumed_milli=0,  # initialize counter for new buckets
            shard_count=shard_count,
        )


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
ROLE_COMPONENTS = ("aggr", "app", "admin", "read")

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
