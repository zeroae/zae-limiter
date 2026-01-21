"""Core models for zae-limiter."""

import re
from dataclasses import dataclass, field
from typing import Any

from .exceptions import InvalidIdentifierError, InvalidNameError

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
# Used for limit names (rpm, tpm) and resources (api, gpt-3.5-turbo)
NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]*$")

# The '#' character is used as a key delimiter in DynamoDB and must be forbidden
FORBIDDEN_CHAR = "#"


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

    if not NAME_PATTERN.match(value):
        raise InvalidNameError(
            field_name,
            value,
            "must start with a letter and contain only alphanumeric, "
            "underscore, hyphen, or dot characters",
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


# ---------------------------------------------------------------------------
# Limit Configuration
# ---------------------------------------------------------------------------


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
        validate_name(self.name, "name")
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

    Note: This model does not validate in __post_init__ to support DynamoDB
    deserialization and avoid performance overhead. Validation is performed
    in Repository.create_entity() at the API boundary.
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
    capacity_milli: int  # max sustained (in millitokens)
    burst_milli: int  # max burst (in millitokens)
    refill_amount_milli: int  # refill numerator (in millitokens)
    refill_period_ms: int  # refill denominator (in milliseconds)
    # Net consumption counter (millitokens). Stored as FLAT top-level attribute
    # (not in nested data.M) to enable atomic ADD operations. See issue #179.
    # None means counter not yet initialized (old bucket).
    total_consumed_milli: int | None = None

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
        """
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
            total_consumed_milli=0,  # initialize counter for new buckets
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
        stack_name: Full CloudFormation stack name (e.g., "ZAEL-my-app")
        user_name: User-friendly name without prefix (e.g., "my-app")
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


@dataclass(frozen=True)
class StackOptions:
    """
    Configuration options for CloudFormation stack creation and updates.

    When passed to RateLimiter constructor, triggers automatic stack creation.
    When None is passed (default), no stack creation is attempted.

    Attributes:
        snapshot_windows: Comma-separated list of snapshot windows (e.g., "hourly,daily")
        retention_days: Number of days to retain usage snapshots
        enable_aggregator: Deploy Lambda aggregator for usage snapshots
        pitr_recovery_days: Point-in-Time Recovery period (1-35, None for AWS default)
        log_retention_days: CloudWatch log retention period in days (must be valid CloudWatch value)
        lambda_timeout: Lambda timeout in seconds (1-900)
        lambda_memory: Lambda memory size in MB (128-3008)
        enable_alarms: Deploy CloudWatch alarms for monitoring
        alarm_sns_topic: SNS topic ARN for alarm notifications
        lambda_duration_threshold_pct: Duration alarm threshold as percentage of timeout (1-100)
        permission_boundary: IAM permission boundary (policy name or full ARN)
        role_name_format: Format template for role name, {} = default role name
        enable_audit_archival: Archive expired audit events to S3 via TTL
        audit_archive_glacier_days: Days before transitioning archives to Glacier IR (1-3650)
        enable_tracing: Enable AWS X-Ray tracing for Lambda aggregator
        create_iam_roles: Create App/Admin/ReadOnly IAM roles for application access
    """

    snapshot_windows: str = "hourly,daily"
    retention_days: int = 90
    enable_aggregator: bool = True
    pitr_recovery_days: int | None = None
    log_retention_days: int = 30
    lambda_timeout: int = 60
    lambda_memory: int = 256
    enable_alarms: bool = True
    alarm_sns_topic: str | None = None
    lambda_duration_threshold_pct: int = 80
    permission_boundary: str | None = None
    role_name_format: str | None = None
    enable_audit_archival: bool = True
    audit_archive_glacier_days: int = 90
    enable_tracing: bool = False
    create_iam_roles: bool = True

    def __post_init__(self) -> None:
        """Validate options."""
        if not (1 <= self.lambda_timeout <= 900):
            raise ValueError("lambda_timeout must be between 1 and 900")
        if not (128 <= self.lambda_memory <= 3008):
            raise ValueError("lambda_memory must be between 128 and 3008")
        if not (1 <= self.lambda_duration_threshold_pct <= 100):
            raise ValueError("lambda_duration_threshold_pct must be between 1 and 100")
        if self.pitr_recovery_days is not None and not (1 <= self.pitr_recovery_days <= 35):
            raise ValueError("pitr_recovery_days must be between 1 and 35")
        if self.retention_days <= 0:
            raise ValueError("retention_days must be positive")
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
            # We can't fully validate without stack_name, but we can check the format length
            format_len = len(self.role_name_format) - 2  # subtract {}
            if format_len > 40:  # leave room for stack_name-aggregator-role
                raise ValueError(
                    "role_name_format template is too long, resulting role name "
                    "may exceed IAM 64 character limit"
                )
        # Validate audit archival options
        if not (1 <= self.audit_archive_glacier_days <= 3650):
            raise ValueError("audit_archive_glacier_days must be between 1 and 3650")

    def get_role_name(self, stack_name: str) -> str | None:
        """
        Get the final role name for a given stack name.

        Args:
            stack_name: Full stack name (with ZAEL- prefix)

        Returns:
            Final role name, or None to use CloudFormation default
        """
        if self.role_name_format is None:
            return None
        default_role = f"{stack_name}-aggregator-role"
        return self.role_name_format.replace("{}", default_role)

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
            "retention_days": str(self.retention_days),
            "enable_aggregator": "true" if self.enable_aggregator else "false",
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
        if self.role_name_format and stack_name:
            role_name = self.get_role_name(stack_name)
            if role_name:
                params["role_name"] = role_name
        # Audit archival parameters
        params["enable_audit_archival"] = "true" if self.enable_audit_archival else "false"
        params["audit_archive_glacier_days"] = str(self.audit_archive_glacier_days)
        # Extract base name (without ZAEL- prefix) for S3 bucket naming
        # S3 bucket names must be lowercase, so we derive from the stack name
        if stack_name:
            # Stack name format: ZAEL-{base_name}
            base_name = stack_name.replace("ZAEL-", "").lower()
            params["base_name"] = base_name
        return params


@dataclass
class Status:
    """
    Comprehensive status of a RateLimiter instance.

    Consolidates connectivity, infrastructure, identity, versions, and table
    metrics into a single status object. Returned by ``RateLimiter.get_status()``
    and ``SyncRateLimiter.get_status()``.

    Example:
        Check if infrastructure is ready::

            status = await limiter.get_status()
            if status.available and status.stack_status == "CREATE_COMPLETE":
                print(f"Ready! Latency: {status.latency_ms}ms")
            else:
                print(f"Not ready: {status.stack_status}")

    Attributes:
        available: Whether DynamoDB is reachable and responding
        latency_ms: Round-trip latency in milliseconds (None if unavailable)
        stack_status: CloudFormation stack status (e.g., 'CREATE_COMPLETE')
        table_status: DynamoDB table status (e.g., 'ACTIVE')
        aggregator_enabled: Whether Lambda aggregator is deployed
        name: Resource name (with ZAEL- prefix)
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
