"""Exceptions for zae-limiter."""

from typing import TYPE_CHECKING, Any

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
    the rate limiter service is unavailable.
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

    def as_dict(self) -> dict[str, Any]:
        """
        Serialize for JSON API responses.

        Returns a dictionary suitable for returning in a 429 response body.
        """
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
                    "capacity": s.limit.capacity,
                    "burst": s.limit.burst,
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


class BucketNotFoundError(RateLimitError):
    """
    Raised when a bucket is not found.

    This typically means the entity hasn't consumed any rate limit capacity
    for the specified resource/limit combination yet.

    Attributes:
        entity_id: The entity ID
        resource: The resource name
        limit_name: The limit name
    """

    def __init__(self, entity_id: str, resource: str, limit_name: str) -> None:
        self.entity_id = entity_id
        self.resource = resource
        self.limit_name = limit_name
        super().__init__(
            f"Bucket not found: entity={entity_id}, resource={resource}, limit={limit_name}"
        )


class RateLimiterUnavailable(RateLimitError):  # noqa: N818
    """
    Raised when DynamoDB is unavailable and failure_mode=FAIL_CLOSED.

    This indicates a transient infrastructure issue, not a rate limit.

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
        # For backwards compatibility, table_name is same as stack_name
        self.table_name = stack_name
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


class StackCreationError(InfrastructureError):
    """Raised when CloudFormation stack creation fails."""

    def __init__(
        self, stack_name: str, reason: str, events: list[dict[str, Any]] | None = None
    ) -> None:
        self.stack_name = stack_name
        self.reason = reason
        self.events = events or []
        super().__init__(f"Stack {stack_name} creation failed: {reason}")


class StackAlreadyExistsError(StackCreationError):
    """Raised when stack already exists (informational)."""

    pass


class InfrastructureNotFoundError(InfrastructureError):
    """
    Raised when expected infrastructure doesn't exist.

    This typically means the CloudFormation stack or DynamoDB table
    hasn't been deployed yet.
    """

    def __init__(self, stack_name: str) -> None:
        self.stack_name = stack_name
        # Stack name and table name are always identical with ZAEL- prefix
        self.table_name = stack_name
        msg = f"Infrastructure not found for stack '{stack_name}'"
        msg += ". Run 'zae-limiter deploy' or use stack_options=StackOptions()."
        super().__init__(msg)


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
