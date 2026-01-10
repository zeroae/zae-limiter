"""Exceptions for zae-limiter."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import LimitStatus


class RateLimitError(Exception):
    """Base exception for rate limiting errors."""

    pass


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


class RateLimiterUnavailable(RateLimitError):  # noqa: N818
    """
    Raised when DynamoDB is unavailable and failure_mode=FAIL_CLOSED.

    This indicates a transient infrastructure issue, not a rate limit.
    """

    def __init__(self, message: str, cause: Exception | None = None) -> None:
        self.cause = cause
        super().__init__(message)


class EntityNotFoundError(RateLimitError):
    """Raised when an entity is not found."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(f"Entity not found: {entity_id}")


class EntityExistsError(RateLimitError):
    """Raised when trying to create an entity that already exists."""

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        super().__init__(f"Entity already exists: {entity_id}")
