"""Tests for exception classes."""

import pytest

from zae_limiter.exceptions import (
    EntityError,
    EntityExistsError,
    EntityNotFoundError,
    IncompatibleSchemaError,
    InfrastructureError,
    InfrastructureNotFoundError,
    RateLimitError,
    RateLimiterUnavailable,
    RateLimitExceeded,
    StackAlreadyExistsError,
    StackCreationError,
    VersionError,
    VersionMismatchError,
    ZAELimiterError,
)
from zae_limiter.models import Limit, LimitStatus


class TestRateLimitExceeded:
    """Tests for RateLimitExceeded exception."""

    def _make_status(
        self,
        limit_name: str = "rpm",
        available: int = 0,
        requested: int = 10,
        exceeded: bool = True,
        retry_after: float = 5.0,
        entity_id: str = "entity-1",
        resource: str = "api",
    ) -> LimitStatus:
        """Helper to create a LimitStatus."""
        return LimitStatus(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit_name,
            limit=Limit.per_minute(limit_name, 100),
            available=available,
            requested=requested,
            exceeded=exceeded,
            retry_after_seconds=retry_after,
        )

    def test_empty_statuses_raises_value_error(self) -> None:
        """RateLimitExceeded requires at least one violation."""
        with pytest.raises(ValueError, match="requires at least one violation"):
            RateLimitExceeded([])

    def test_no_violations_raises_value_error(self) -> None:
        """RateLimitExceeded raises if all statuses passed."""
        passed_status = self._make_status(exceeded=False, retry_after=0)
        with pytest.raises(ValueError, match="requires at least one violation"):
            RateLimitExceeded([passed_status])

    def test_single_violation(self) -> None:
        """Single violation sets properties correctly."""
        status = self._make_status(retry_after=10.0)
        exc = RateLimitExceeded([status])

        assert len(exc.statuses) == 1
        assert len(exc.violations) == 1
        assert len(exc.passed) == 0
        assert exc.primary_violation is status
        assert exc.retry_after_seconds == 10.0

    def test_multiple_violations_primary_is_longest(self) -> None:
        """Primary violation is the one with longest retry_after."""
        status1 = self._make_status(limit_name="rpm", retry_after=5.0)
        status2 = self._make_status(limit_name="tpm", retry_after=15.0)
        status3 = self._make_status(limit_name="rpd", retry_after=10.0)

        exc = RateLimitExceeded([status1, status2, status3])

        assert exc.primary_violation is status2
        assert exc.retry_after_seconds == 15.0
        assert len(exc.violations) == 3

    def test_mixed_passed_and_violated(self) -> None:
        """Correctly separates passed and violated limits."""
        violated = self._make_status(limit_name="rpm", exceeded=True, retry_after=5.0)
        passed = self._make_status(limit_name="tpm", exceeded=False, retry_after=0.0)

        exc = RateLimitExceeded([violated, passed])

        assert len(exc.statuses) == 2
        assert len(exc.violations) == 1
        assert len(exc.passed) == 1
        assert violated in exc.violations
        assert passed in exc.passed

    def test_format_message(self) -> None:
        """Message includes entity, resource, and exceeded limit names."""
        status = self._make_status(
            entity_id="test-entity",
            resource="gpt-4",
            limit_name="tpm",
            retry_after=12.5,
        )
        exc = RateLimitExceeded([status])

        msg = str(exc)
        assert "test-entity" in msg
        assert "gpt-4" in msg
        assert "tpm" in msg
        assert "12.5s" in msg

    def test_format_message_multiple_violations(self) -> None:
        """Message lists all exceeded limit names."""
        status1 = self._make_status(limit_name="rpm", retry_after=5.0)
        status2 = self._make_status(limit_name="tpm", retry_after=10.0)

        exc = RateLimitExceeded([status1, status2])

        msg = str(exc)
        assert "rpm" in msg
        assert "tpm" in msg

    def test_as_dict_structure(self) -> None:
        """as_dict returns correct structure."""
        status = self._make_status(
            entity_id="entity-1",
            resource="api",
            limit_name="rpm",
            available=5,
            requested=10,
            retry_after=7.5,
        )
        exc = RateLimitExceeded([status])

        d = exc.as_dict()

        assert d["error"] == "rate_limit_exceeded"
        assert "message" in d
        assert d["retry_after_seconds"] == 7.5
        assert d["retry_after_ms"] == 7500
        assert len(d["limits"]) == 1

        limit_info = d["limits"][0]
        assert limit_info["entity_id"] == "entity-1"
        assert limit_info["resource"] == "api"
        assert limit_info["limit_name"] == "rpm"
        assert limit_info["available"] == 5
        assert limit_info["requested"] == 10
        assert limit_info["exceeded"] is True
        assert limit_info["retry_after_seconds"] == 7.5
        assert limit_info["capacity"] == 100
        assert limit_info["burst"] == 100

    def test_retry_after_header_rounds_up(self) -> None:
        """retry_after_header rounds up fractional seconds."""
        status = self._make_status(retry_after=1.1)
        exc = RateLimitExceeded([status])
        assert exc.retry_after_header == "2"

        status = self._make_status(retry_after=1.9)
        exc = RateLimitExceeded([status])
        assert exc.retry_after_header == "2"

        status = self._make_status(retry_after=2.0)
        exc = RateLimitExceeded([status])
        assert exc.retry_after_header == "3"

    def test_retry_after_header_zero(self) -> None:
        """retry_after_header handles near-zero values."""
        status = self._make_status(retry_after=0.1)
        exc = RateLimitExceeded([status])
        assert exc.retry_after_header == "1"

    def test_inherits_from_rate_limit_error(self) -> None:
        """RateLimitExceeded is a RateLimitError."""
        status = self._make_status()
        exc = RateLimitExceeded([status])
        assert isinstance(exc, RateLimitError)


class TestRateLimiterUnavailable:
    """Tests for RateLimiterUnavailable exception."""

    def test_message_only(self) -> None:
        """Can create with just a message."""
        exc = RateLimiterUnavailable("DynamoDB timeout")
        assert str(exc) == "DynamoDB timeout"
        assert exc.cause is None
        assert exc.table_name is None
        assert exc.entity_id is None
        assert exc.resource is None

    def test_with_cause(self) -> None:
        """Can create with cause exception."""
        cause = ConnectionError("network failure")
        exc = RateLimiterUnavailable("DynamoDB unavailable", cause=cause)
        assert exc.cause is cause
        assert str(exc) == "DynamoDB unavailable"

    def test_with_full_context(self) -> None:
        """Can create with all context fields."""
        cause = ConnectionError("network failure")
        exc = RateLimiterUnavailable(
            "DynamoDB unavailable",
            cause=cause,
            stack_name="rate-limits",
            entity_id="entity-1",
            resource="gpt-4",
        )
        assert exc.stack_name == "rate-limits"
        assert exc.table_name == "rate-limits"  # Backwards compatibility
        assert exc.entity_id == "entity-1"
        assert exc.resource == "gpt-4"
        msg = str(exc)
        assert "stack=rate-limits" in msg
        assert "entity=entity-1" in msg
        assert "resource=gpt-4" in msg

    def test_with_partial_context(self) -> None:
        """Context fields are optional."""
        exc = RateLimiterUnavailable(
            "DynamoDB timeout",
            stack_name="my-table",
        )
        assert exc.stack_name == "my-table"
        assert exc.table_name == "my-table"  # Backwards compatibility
        assert exc.entity_id is None
        assert exc.resource is None
        assert "stack=my-table" in str(exc)
        assert "entity=" not in str(exc)
        assert "resource=" not in str(exc)

    def test_inherits_from_infrastructure_error(self) -> None:
        """RateLimiterUnavailable is an InfrastructureError (not RateLimitError)."""
        exc = RateLimiterUnavailable("test")
        assert isinstance(exc, InfrastructureError)
        assert not isinstance(exc, RateLimitError)


class TestEntityNotFoundError:
    """Tests for EntityNotFoundError."""

    def test_entity_id_stored(self) -> None:
        """entity_id is stored as attribute."""
        exc = EntityNotFoundError("test-entity-123")
        assert exc.entity_id == "test-entity-123"

    def test_message_format(self) -> None:
        """Message includes entity_id."""
        exc = EntityNotFoundError("my-entity")
        assert "my-entity" in str(exc)
        assert "not found" in str(exc).lower()

    def test_inherits_from_entity_error(self) -> None:
        """EntityNotFoundError is an EntityError."""
        exc = EntityNotFoundError("test")
        assert isinstance(exc, EntityError)
        assert isinstance(exc, ZAELimiterError)


class TestEntityExistsError:
    """Tests for EntityExistsError."""

    def test_entity_id_stored(self) -> None:
        """entity_id is stored as attribute."""
        exc = EntityExistsError("existing-entity")
        assert exc.entity_id == "existing-entity"

    def test_message_format(self) -> None:
        """Message includes entity_id."""
        exc = EntityExistsError("dup-entity")
        assert "dup-entity" in str(exc)
        assert "already exists" in str(exc).lower()

    def test_inherits_from_entity_error(self) -> None:
        """EntityExistsError is an EntityError."""
        exc = EntityExistsError("test")
        assert isinstance(exc, EntityError)
        assert isinstance(exc, ZAELimiterError)


class TestStackCreationError:
    """Tests for StackCreationError."""

    def test_attributes_stored(self) -> None:
        """All attributes are stored correctly."""
        events = [{"event": "CREATE_FAILED"}]
        exc = StackCreationError("my-stack", "timeout", events=events)

        assert exc.stack_name == "my-stack"
        assert exc.reason == "timeout"
        assert exc.events == events

    def test_events_default_empty(self) -> None:
        """Events defaults to empty list."""
        exc = StackCreationError("stack", "reason")
        assert exc.events == []

    def test_message_format(self) -> None:
        """Message includes stack name and reason."""
        exc = StackCreationError("test-stack", "permission denied")
        msg = str(exc)
        assert "test-stack" in msg
        assert "permission denied" in msg

    def test_inherits_from_infrastructure_error(self) -> None:
        """StackCreationError is an InfrastructureError."""
        exc = StackCreationError("test-stack", "test reason")
        assert isinstance(exc, InfrastructureError)
        assert isinstance(exc, ZAELimiterError)


class TestStackAlreadyExistsError:
    """Tests for StackAlreadyExistsError."""

    def test_inherits_from_stack_creation_error(self) -> None:
        """StackAlreadyExistsError extends StackCreationError."""
        exc = StackAlreadyExistsError("existing-stack", "already exists")
        assert isinstance(exc, StackCreationError)
        assert exc.stack_name == "existing-stack"


class TestVersionMismatchError:
    """Tests for VersionMismatchError."""

    def test_attributes_stored(self) -> None:
        """All attributes are stored correctly."""
        exc = VersionMismatchError(
            client_version="1.0.0",
            schema_version="0.9.0",
            lambda_version="0.9.0",
            message="Upgrade required",
            can_auto_update=True,
        )

        assert exc.client_version == "1.0.0"
        assert exc.schema_version == "0.9.0"
        assert exc.lambda_version == "0.9.0"
        assert exc.can_auto_update is True

    def test_message_includes_versions(self) -> None:
        """Message includes all version info."""
        exc = VersionMismatchError(
            client_version="2.0.0",
            schema_version="1.0.0",
            lambda_version="1.0.0",
            message="Please upgrade",
        )

        msg = str(exc)
        assert "2.0.0" in msg
        assert "1.0.0" in msg
        assert "Please upgrade" in msg

    def test_lambda_version_none_shows_unknown(self) -> None:
        """When lambda_version is None, shows 'unknown'."""
        exc = VersionMismatchError(
            client_version="1.0.0",
            schema_version="0.9.0",
            lambda_version=None,
            message="test",
        )

        msg = str(exc)
        assert "unknown" in msg

    def test_inherits_from_version_error(self) -> None:
        """VersionMismatchError is a VersionError."""
        exc = VersionMismatchError("1.0", "0.9", None, "test")
        assert isinstance(exc, VersionError)
        assert isinstance(exc, ZAELimiterError)


class TestIncompatibleSchemaError:
    """Tests for IncompatibleSchemaError."""

    def test_attributes_stored(self) -> None:
        """All attributes are stored correctly."""
        exc = IncompatibleSchemaError(
            client_version="2.0.0",
            schema_version="1.0.0",
            message="Major version mismatch",
            migration_guide_url="https://example.com/migrate",
        )

        assert exc.client_version == "2.0.0"
        assert exc.schema_version == "1.0.0"
        assert exc.migration_guide_url == "https://example.com/migrate"

    def test_message_without_url(self) -> None:
        """Message format when no migration URL."""
        exc = IncompatibleSchemaError(
            client_version="2.0.0",
            schema_version="1.0.0",
            message="Breaking change",
        )

        msg = str(exc)
        assert "2.0.0" in msg
        assert "1.0.0" in msg
        assert "Breaking change" in msg
        assert "See:" not in msg

    def test_message_with_url(self) -> None:
        """Message includes migration URL when provided."""
        exc = IncompatibleSchemaError(
            client_version="2.0.0",
            schema_version="1.0.0",
            message="Migrate now",
            migration_guide_url="https://docs.example.com/v2",
        )

        msg = str(exc)
        assert "https://docs.example.com/v2" in msg
        assert "See:" in msg

    def test_inherits_from_version_error(self) -> None:
        """IncompatibleSchemaError is a VersionError."""
        exc = IncompatibleSchemaError("2.0", "1.0", "test")
        assert isinstance(exc, VersionError)
        assert isinstance(exc, ZAELimiterError)


class TestInfrastructureNotFoundError:
    """Tests for InfrastructureNotFoundError."""

    def test_attributes_stored(self) -> None:
        """All attributes are stored correctly."""
        exc = InfrastructureNotFoundError(stack_name="rate-limits")

        assert exc.stack_name == "rate-limits"
        # table_name is same as stack_name for backwards compatibility
        assert exc.table_name == "rate-limits"

    def test_message_format(self) -> None:
        """Message includes stack name and deploy hint."""
        exc = InfrastructureNotFoundError(stack_name="my-table")

        msg = str(exc)
        assert "my-table" in msg
        assert "zae-limiter deploy" in msg

    def test_inherits_from_infrastructure_error(self) -> None:
        """InfrastructureNotFoundError is an InfrastructureError."""
        exc = InfrastructureNotFoundError("my-table")
        assert isinstance(exc, InfrastructureError)
        assert isinstance(exc, ZAELimiterError)


class TestLimitStatusDeficit:
    """Tests for LimitStatus.deficit property."""

    def test_deficit_when_exceeded(self) -> None:
        """Deficit is requested - available when exceeded."""
        status = LimitStatus(
            entity_id="e1",
            resource="api",
            limit_name="rpm",
            limit=Limit.per_minute("rpm", 100),
            available=30,
            requested=50,
            exceeded=True,
            retry_after_seconds=5.0,
        )
        assert status.deficit == 20

    def test_deficit_zero_when_not_exceeded(self) -> None:
        """Deficit is 0 when not exceeded."""
        status = LimitStatus(
            entity_id="e1",
            resource="api",
            limit_name="rpm",
            limit=Limit.per_minute("rpm", 100),
            available=100,
            requested=50,
            exceeded=False,
            retry_after_seconds=0.0,
        )
        assert status.deficit == 0

    def test_deficit_with_negative_available(self) -> None:
        """Deficit calculated correctly with negative available."""
        status = LimitStatus(
            entity_id="e1",
            resource="api",
            limit_name="rpm",
            limit=Limit.per_minute("rpm", 100),
            available=-10,
            requested=5,
            exceeded=True,
            retry_after_seconds=10.0,
        )
        assert status.deficit == 15  # 5 - (-10) = 15
