"""Exceptions for zae-limiter."""

import time
from typing import TYPE_CHECKING, Any

from .schedule import next_reset_edge

if TYPE_CHECKING:
    from .models import LimitStatus


# ---------------------------------------------------------------------------
# Base Exception
# ---------------------------------------------------------------------------


class ZAELimiterError(Exception):
    """
    Base exception for all zae-limiter errors.

    All exceptions raised by this library inherit from this class,
    allowing callers to catch all library-specific errors with a single
    except clause.
    """

    pass


# ---------------------------------------------------------------------------
# Category Exceptions
# ---------------------------------------------------------------------------


class RateLimitError(ZAELimiterError):
    """
    Base exception for rate limit-related errors.

    This includes errors raised when rate limits are exceeded or when
    a lease operation is attempted after the lease has exited.
    """

    pass


class InfrastructureError(ZAELimiterError):
    """
    Base exception for infrastructure-related errors.

    This includes errors related to CloudFormation stacks, DynamoDB tables,
    and Lambda functions.
    """

    pass


class EntityError(ZAELimiterError):
    """
    Base exception for entity-related errors.

    This includes errors when creating, reading, or deleting entities
    in the rate limiter.
    """

    pass


class VersionError(ZAELimiterError):
    """
    Base exception for version-related errors.

    This includes errors related to schema versioning, migrations,
    and compatibility between client and infrastructure versions.
    """

    pass


# ---------------------------------------------------------------------------
# Rate Limit Exceptions
# ---------------------------------------------------------------------------


class RateLimitExceeded(RateLimitError):  # noqa: N818
    """
    Raised when one or more rate limits would be exceeded.

    Provides full visibility into ALL limits that were checked,
    both passed and failed, to help callers understand the full picture.

    Attributes:
        statuses: Status of ALL limits checked (both passed and failed)
        violations: Only the limits that were exceeded
        passed: Only the limits that passed
        retry_after_seconds: Time until ALL requested capacity is available
        primary_violation: The violation with longest retry time (bottleneck)
    """

    def __init__(self, statuses: list["LimitStatus"]) -> None:
        self.statuses = statuses
        self.violations = [s for s in statuses if s.exceeded]
        self.passed = [s for s in statuses if not s.exceeded]

        if not self.violations:
            raise ValueError("RateLimitExceeded requires at least one violation")

        self.primary_violation = max(self.violations, key=lambda v: v.retry_after_seconds)
        self.retry_after_seconds = self.primary_violation.retry_after_seconds

        super().__init__(self._format_message())

    def _format_message(self) -> str:
        v = self.primary_violation
        exceeded_names = ", ".join(s.limit_name for s in self.violations)
        return (
            f"Rate limit exceeded for {v.entity_id}/{v.resource}: "
            f"[{exceeded_names}]. "
            f"Retry after {self.retry_after_seconds:.1f}s"
        )

    @staticmethod
    def _limit_shape(status: "LimitStatus", now_ms: int) -> dict[str, Any]:
        """The fields describing *how this limit recovers*, by its kind (#545).

        Two shapes, tagged by ``kind``, because a quota and a rate limit
        recover by different mechanisms and there is no honest set of fields
        that covers both:

        * ``"rate"`` drips, so ``refill_amount`` per
          ``refill_period_seconds`` is the whole answer.
        * ``"quota"`` does **not** drip (ADR-137): ``refill_amount`` is fixed
          at 0 and ``refill_period_seconds`` is ``_QUOTA_REFILL_PERIOD_SECONDS``,
          an inert placeholder kept only because the field is validated
          positive. Serialising those two is worse than serialising nothing —
          a client dividing one by the other computes "0 tokens per second,
          never recovers", which is false about a limit that comes back whole
          at its next reset edge. They are therefore **omitted**, and the fact
          a client actually needs is emitted instead: ``resets_at_ms``, the
          absolute epoch-millisecond instant the allowance returns.

        ``kind`` is emitted on **both** shapes so the distinction is read
        directly rather than inferred from ``refill_amount == 0`` — which is
        not a safe inference in either direction. A dripping limit's share can
        floor to zero without being a quota (#475), and #556 gives a quota
        inside a ``scale`` window a phantom 1-millitoken drip. ``kind`` is
        derived from :attr:`Limit.is_quota`, the structural predicate, which
        both of those carve-outs already respect.

        An absolute instant rather than the cron string the CLI shows: an HTTP
        client should not need a cron parser to schedule a retry, and being
        absolute it is self-interpreting — no companion ``checked_at_ms`` is
        required to read it. ``None`` when ``schedule.next_reset_edge`` finds
        no edge inside its scan horizon; the key stays present so a quota
        entry's shape does not vary with the calendar.

        Takes the **status** rather than the limit because a duration window's
        instant (``ws + reset_after``, ADR-139) is anchored on the bucket item,
        not in the config: it is computed where the bucket was read and
        carried on :attr:`LimitStatus.resets_at_ms`, a constant with no scan.
        A calendar edge still falls back to the cron walk, which is the only
        source for it.
        """
        limit = status.limit
        if not limit.is_quota:
            return {
                "kind": "rate",
                "capacity": limit.capacity,
                "refill_amount": limit.refill_amount,
                "refill_period_seconds": limit.refill_period_seconds,
            }
        resets_at_ms = status.resets_at_ms
        if resets_at_ms is None and limit.reset_schedule:
            resets_at_ms = next_reset_edge(limit.reset_schedule, now_ms=now_ms)
        return {"kind": "quota", "capacity": limit.capacity, "resets_at_ms": resets_at_ms}

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize for JSON API responses.

        Returns a dictionary suitable for returning in a 429 response body.

        Per-limit entries carry a ``kind`` of ``"rate"`` or ``"quota"`` and,
        for a quota, ``resets_at_ms`` in place of the drip fields — see
        :meth:`_limit_shape`.
        """
        # One clock reading for the whole body, so two quotas on the same
        # rejection cannot report reset instants scanned from different
        # instants. Read here rather than at construction time: this is the
        # only consumer, and rejections are a hot path that must not pay for a
        # cron scan it may never serialize.
        now_ms = int(time.time() * 1000)
        return {
            "error": "rate_limit_exceeded",
            "message": str(self),
            "retry_after_seconds": self.retry_after_seconds,
            "retry_after_ms": int(self.retry_after_seconds * 1000),
            "limits": [
                {
                    "entity_id": s.entity_id,
                    "resource": s.resource,
                    "limit_name": s.limit_name,
                    **self._limit_shape(s, now_ms),
                    "available": s.available,
                    "requested": s.requested,
                    "exceeded": s.exceeded,
                    "retry_after_seconds": s.retry_after_seconds,
                }
                for s in self.statuses
            ],
        }

    @property
    def retry_after_header(self) -> str:
        """Value for HTTP Retry-After header (integer seconds)."""
        return str(int(self.retry_after_seconds) + 1)  # round up


class LeaseExpiredError(RateLimitError):
    """Raised when a lease operation is attempted after the lease has exited."""

    def __init__(self) -> None:
        super().__init__("Lease is no longer active")


class RateLimiterUnavailable(InfrastructureError):  # noqa: N818
    """
    Raised when DynamoDB is unavailable and on_unavailable=OnUnavailable.BLOCK.

    This indicates a transient infrastructure issue, not a rate limit.
    When using OnUnavailable.BLOCK (the default), your application should
    be prepared to catch this exception and handle degraded mode gracefully.

    Attributes:
        cause: The underlying exception that caused the unavailability
        stack_name: The stack/table that was being accessed
        entity_id: The entity being rate limited (if applicable)
        resource: The resource being rate limited (if applicable)
    """

    def __init__(
        self,
        message: str,
        cause: Exception | None = None,
        *,
        stack_name: str | None = None,
        entity_id: str | None = None,
        resource: str | None = None,
    ) -> None:
        self.cause = cause
        self.stack_name = stack_name
        self.entity_id = entity_id
        self.resource = resource
        super().__init__(self._format_message(message))

    def _format_message(self, message: str) -> str:
        parts = [message]
        context = []
        if self.stack_name:
            context.append(f"stack={self.stack_name}")
        if self.entity_id:
            context.append(f"entity={self.entity_id}")
        if self.resource:
            context.append(f"resource={self.resource}")
        if context:
            parts.append(f"[{', '.join(context)}]")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Entity Exceptions
# ---------------------------------------------------------------------------


class EntityNotFoundError(EntityError):
    """Raised when an entity is not found."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(f"Entity not found: {entity_id}")


class EntityExistsError(EntityError):
    """Raised when trying to create an entity that already exists."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(f"Entity already exists: {entity_id}")


# ---------------------------------------------------------------------------
# Infrastructure Exceptions
# ---------------------------------------------------------------------------


class StackOperationError(InfrastructureError):
    """Raised when a CloudFormation stack operation fails."""

    def __init__(
        self, stack_name: str, reason: str, events: list[dict[str, Any]] | None = None
    ) -> None:
        self.stack_name = stack_name
        self.reason = reason
        self.events = events or []
        super().__init__(f"Stack '{stack_name}' operation failed: {reason}")


class StackAlreadyExistsError(InfrastructureError):
    """Raised when a stack already exists."""

    def __init__(self, stack_name: str) -> None:
        self.stack_name = stack_name
        super().__init__(f"Stack '{stack_name}' already exists")


class InfrastructureNotFoundError(InfrastructureError):
    """
    Raised when expected infrastructure doesn't exist.

    This typically means the CloudFormation stack or DynamoDB table
    hasn't been deployed yet.
    """

    def __init__(self, stack_name: str) -> None:
        self.stack_name = stack_name
        msg = f"Infrastructure not found for stack '{stack_name}'"
        msg += ". Run 'zae-limiter deploy' or use stack_options=StackOptions()."
        super().__init__(msg)


class NamespaceNotFoundError(InfrastructureError):
    """Raised when a namespace is not found in the registry."""

    def __init__(self, namespace_name: str) -> None:
        self.namespace_name = namespace_name
        super().__init__(
            f"Namespace '{namespace_name}' not found. Register it first or check for typos."
        )


class NamespaceStateError(InfrastructureError):
    """Raised when a namespace operation is invalid for the current state.

    Examples: recovering an active namespace, purging an active namespace,
    recovering a namespace whose name was re-registered.

    Attributes:
        namespace_name: The namespace name (if known).
        state: The current state that caused the conflict (e.g. "active", "purging").
    """

    def __init__(self, message: str, *, namespace_name: str = "", state: str = "") -> None:
        self.namespace_name = namespace_name
        self.state = state
        super().__init__(message)


# ---------------------------------------------------------------------------
# Version Exceptions
# ---------------------------------------------------------------------------


class VersionMismatchError(VersionError):
    """
    Raised when client and infrastructure versions are incompatible.

    This error indicates that the client library version doesn't match
    the deployed infrastructure and auto-update is disabled or failed.
    """

    def __init__(
        self,
        client_version: str,
        schema_version: str,
        lambda_version: str | None,
        message: str,
        can_auto_update: bool = False,
    ) -> None:
        self.client_version = client_version
        self.schema_version = schema_version
        self.lambda_version = lambda_version
        self.can_auto_update = can_auto_update
        super().__init__(self._format_message(message))

    def _format_message(self, message: str) -> str:
        return (
            f"Version mismatch: client={self.client_version}, "
            f"schema={self.schema_version}, "
            f"lambda={self.lambda_version or 'unknown'}. {message}"
        )


class IncompatibleSchemaError(VersionError):
    """
    Raised when schema version requires manual migration.

    This indicates a major version difference that cannot be
    automatically reconciled.
    """

    def __init__(
        self,
        client_version: str,
        schema_version: str,
        message: str,
        migration_guide_url: str | None = None,
    ) -> None:
        self.client_version = client_version
        self.schema_version = schema_version
        self.migration_guide_url = migration_guide_url
        msg = (
            f"Incompatible schema: client {client_version} is not compatible "
            f"with schema {schema_version}. {message}"
        )
        if migration_guide_url:
            msg += f" See: {migration_guide_url}"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# Validation Exceptions
# ---------------------------------------------------------------------------


class ValidationError(ZAELimiterError):
    """
    Base exception for input validation errors.

    This includes errors raised when invalid input is provided to
    models, such as invalid entity IDs, limit names, or resource names.

    Attributes:
        field: The name of the field that failed validation
        value: The invalid value (truncated if too long)
        reason: Human-readable explanation of why validation failed
    """

    def __init__(self, field: str, value: str, reason: str) -> None:
        self.field = field
        self.value = value[:50] + "..." if len(value) > 50 else value
        self.reason = reason
        super().__init__(f"Invalid {field}: {reason}")


class InvalidIdentifierError(ValidationError):
    """
    Raised when an identifier (entity_id, parent_id) is invalid.

    Identifiers must:
    - Not be empty
    - Not contain the '#' character (used as key delimiter)
    - Not exceed 256 characters
    - Start with alphanumeric character
    - Contain only: alphanumeric, underscore, hyphen, dot, colon, @
    """

    pass


class InvalidNameError(ValidationError):
    """
    Raised when a name (limit_name, resource) is invalid.

    Names must:
    - Not be empty
    - Start with a letter
    - Contain only alphanumeric characters, underscores, hyphens, and dots
    - Not exceed 64 characters
    - Not contain the '#' character
    """

    pass


# ---------------------------------------------------------------------------
# Configuration State Exceptions
# ---------------------------------------------------------------------------


class FanoutIncomplete(ZAELimiterError):  # noqa: N818
    """
    Raised when a config fan-out to bucket items fails partway through.

    Two fan-outs raise this: the disable/enable stamp (ADR-125) and the
    limit-param sync that follows ``set_limits``/``delete_limits`` (#468,
    #487). Both share the same hazard.

    The config write lands before the fan-out begins, so a failure here
    leaves the table half-applied: the config item carries the new value,
    some bucket items match and the rest still hold the old one. Stale
    buckets keep passing the speculative fast path, which tests
    ``attribute_not_exists(#disabled)`` and the bucket's own stored
    ``cp``/``ra`` and never re-reads config.

    Nothing reconciles this on its own. Buckets backed by entity-level
    custom limits carry no TTL, so the drift persists until the same
    command is run again — which is safe to do, since both the config write
    and each bucket write are idempotent.

    Attributes:
        stamped: Bucket items successfully written before the failure
        resource: Resource being changed, if scoped to one
        entity_id: Entity being changed, if scoped to one
        cause: The underlying exception that stopped the fan-out
    """

    def __init__(
        self,
        stamped: int,
        cause: Exception,
        *,
        resource: str | None = None,
        entity_id: str | None = None,
        action: str = "stamping",
    ) -> None:
        self.stamped = stamped
        self.resource = resource
        self.entity_id = entity_id
        self.cause = cause

        target = (
            f"entity '{entity_id}'"
            if entity_id is not None and resource is None
            else f"entity '{entity_id}' resource '{resource}'"
            if entity_id is not None
            else f"resource '{resource}'"
        )
        super().__init__(
            f"Fan-out for {target} stopped after {action} {stamped} bucket(s): {cause}. "
            f"Config was written, so the change is partially applied — re-run the same "
            f"command to reconcile the remaining buckets."
        )


class ResourceDisabled(ZAELimiterError):  # noqa: N818
    """
    Raised when a resource is disabled for the requesting entity.

    This is a configuration state, not a throttling signal: it does not
    inherit from RateLimitError and carries no retry hint, because retrying
    will not help. Map it to 403, not 429.

    The resolved value comes from the first level that sets ``disabled``
    explicitly, walking entity -> entity default -> resource (ADR-125).

    Attributes:
        entity_id: Entity that attempted the acquire
        resource: Resource that is disabled
        level: Config level that decided it ("entity", "entity_default",
            "resource", or "bucket" when the decision came from the
            denormalized bucket attribute on the fast path)
    """

    def __init__(self, entity_id: str, resource: str, level: str) -> None:
        self.entity_id = entity_id
        self.resource = resource
        self.level = level
        super().__init__(
            f"Resource '{resource}' is disabled for entity '{entity_id}' "
            f"(disabled at {level} level)"
        )
