"""Core models for zae-limiter."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Limit:
    """
    Token bucket rate limit configuration.

    Refill rate is stored as a fraction (refill_amount / refill_period_seconds)
    to avoid floating point precision issues.

    Attributes:
        name: Unique identifier for this limit type (e.g., "rpm", "tpm")
        capacity: Max tokens that refill over the period (sustained rate)
        burst: Max tokens in bucket (>= capacity, allows bursting)
        refill_amount: Numerator of refill rate
        refill_period_seconds: Denominator of refill rate
    """

    name: str
    capacity: int
    burst: int
    refill_amount: int
    refill_period_seconds: int

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError("capacity must be positive")
        if self.burst < self.capacity:
            raise ValueError("burst must be >= capacity")
        if self.refill_amount <= 0:
            raise ValueError("refill_amount must be positive")
        if self.refill_period_seconds <= 0:
            raise ValueError("refill_period_seconds must be positive")

    @classmethod
    def per_second(
        cls,
        name: str,
        capacity: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills `capacity` tokens per second."""
        return cls(
            name=name,
            capacity=capacity,
            burst=burst if burst is not None else capacity,
            refill_amount=capacity,
            refill_period_seconds=1,
        )

    @classmethod
    def per_minute(
        cls,
        name: str,
        capacity: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills `capacity` tokens per minute."""
        return cls(
            name=name,
            capacity=capacity,
            burst=burst if burst is not None else capacity,
            refill_amount=capacity,
            refill_period_seconds=60,
        )

    @classmethod
    def per_hour(
        cls,
        name: str,
        capacity: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills `capacity` tokens per hour."""
        return cls(
            name=name,
            capacity=capacity,
            burst=burst if burst is not None else capacity,
            refill_amount=capacity,
            refill_period_seconds=3600,
        )

    @classmethod
    def per_day(
        cls,
        name: str,
        capacity: int,
        burst: int | None = None,
    ) -> "Limit":
        """Create a limit that refills `capacity` tokens per day."""
        return cls(
            name=name,
            capacity=capacity,
            burst=burst if burst is not None else capacity,
            refill_amount=capacity,
            refill_period_seconds=86400,
        )

    @classmethod
    def custom(
        cls,
        name: str,
        capacity: int,
        refill_amount: int,
        refill_period_seconds: int,
        burst: int | None = None,
    ) -> "Limit":
        """
        Create a custom limit with explicit refill rate.

        Example: Sustain 100/sec with burst of 1000
            Limit.custom("requests", capacity=100, refill_amount=100,
                        refill_period_seconds=1, burst=1000)
        """
        return cls(
            name=name,
            capacity=capacity,
            burst=burst if burst is not None else capacity,
            refill_amount=refill_amount,
            refill_period_seconds=refill_period_seconds,
        )

    @property
    def refill_rate(self) -> float:
        """Tokens per second (for display/debugging)."""
        return self.refill_amount / self.refill_period_seconds

    def to_dict(self) -> dict[str, str | int]:
        """Serialize to dictionary for storage."""
        return {
            "name": self.name,
            "capacity": self.capacity,
            "burst": self.burst,
            "refill_amount": self.refill_amount,
            "refill_period_seconds": self.refill_period_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Limit":
        """Deserialize from dictionary."""
        return cls(
            name=data["name"],
            capacity=data["capacity"],
            burst=data["burst"],
            refill_amount=data["refill_amount"],
            refill_period_seconds=data["refill_period_seconds"],
        )


@dataclass
class Entity:
    """
    An entity that can have rate limits applied.

    Entities can be parents (projects) or children (API keys).
    Children have a parent_id reference.
    """

    id: str
    name: str | None = None
    parent_id: str | None = None
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


@dataclass
class BucketState:
    """
    Internal state of a token bucket.

    All token values are stored in millitokens (x1000) for precision.
    """

    entity_id: str
    resource: str
    limit_name: str
    tokens_milli: int  # current tokens (in millitokens)
    last_refill_ms: int  # epoch milliseconds
    capacity_milli: int  # max sustained (in millitokens)
    burst_milli: int  # max burst (in millitokens)
    refill_amount_milli: int  # refill numerator (in millitokens)
    refill_period_ms: int  # refill denominator (in milliseconds)

    @property
    def tokens(self) -> int:
        """Current tokens (not millitokens)."""
        return self.tokens_milli // 1000

    @property
    def capacity(self) -> int:
        """Capacity (not millitokens)."""
        return self.capacity_milli // 1000

    @property
    def burst(self) -> int:
        """Burst (not millitokens)."""
        return self.burst_milli // 1000

    @classmethod
    def from_limit(
        cls,
        entity_id: str,
        resource: str,
        limit: Limit,
        now_ms: int,
    ) -> "BucketState":
        """Create a new bucket at full capacity from a Limit."""
        return cls(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit.name,
            tokens_milli=limit.burst * 1000,  # start at burst capacity
            last_refill_ms=now_ms,
            capacity_milli=limit.capacity * 1000,
            burst_milli=limit.burst * 1000,
            refill_amount_milli=limit.refill_amount * 1000,
            refill_period_ms=limit.refill_period_seconds * 1000,
        )


@dataclass
class UsageSnapshot:
    """Aggregated usage for a time window."""

    entity_id: str
    resource: str
    window_start: str  # ISO timestamp
    window_end: str  # ISO timestamp
    window_type: str  # "hourly", "daily"
    counters: dict[str, int]  # limit_name -> total consumed
    total_events: int


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
