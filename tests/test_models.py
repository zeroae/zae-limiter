"""Tests for models."""

import pytest

from zae_limiter import (
    Entity,
    InvalidIdentifierError,
    InvalidNameError,
    Limit,
    LimitName,
    StackOptions,
    ValidationError,
)
from zae_limiter.models import BucketState, LimitStatus


class TestLimit:
    """Tests for Limit model."""

    def test_per_minute(self):
        """Test per_minute factory."""
        limit = Limit.per_minute("rpm", 100)
        assert limit.name == "rpm"
        assert limit.capacity == 100
        assert limit.burst == 100
        assert limit.refill_amount == 100
        assert limit.refill_period_seconds == 60

    def test_per_minute_with_burst(self):
        """Test per_minute with custom burst."""
        limit = Limit.per_minute("rpm", 100, burst=150)
        assert limit.capacity == 100
        assert limit.burst == 150

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
            burst=200,
        )
        assert limit.capacity == 100
        assert limit.burst == 200
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

    def test_invalid_burst(self):
        """Test validation of burst < capacity."""
        with pytest.raises(ValueError, match="burst must be >= capacity"):
            Limit.per_minute("rpm", 100, burst=50)

    def test_to_dict_from_dict(self):
        """Test serialization round-trip."""
        limit = Limit.per_minute("rpm", 100, burst=150)
        data = limit.to_dict()
        restored = Limit.from_dict(data)
        assert restored == limit

    def test_frozen(self):
        """Test that Limit is immutable."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(AttributeError):
            limit.capacity = 200


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
        assert opts.retention_days == 90
        assert opts.enable_aggregator is True
        assert opts.pitr_recovery_days is None
        assert opts.log_retention_days == 30
        assert opts.lambda_timeout == 60
        assert opts.lambda_memory == 256
        assert opts.enable_alarms is True
        assert opts.alarm_sns_topic is None
        assert opts.lambda_duration_threshold_pct == 80
        assert opts.stack_name is None

    def test_custom_values(self):
        """Test custom values are preserved."""
        opts = StackOptions(
            snapshot_windows="hourly",
            retention_days=30,
            lambda_timeout=120,
            lambda_memory=512,
            enable_aggregator=False,
        )
        assert opts.snapshot_windows == "hourly"
        assert opts.retention_days == 30
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

    def test_invalid_retention_days(self):
        """Test validation of retention_days must be positive."""
        with pytest.raises(ValueError, match="retention_days must be positive"):
            StackOptions(retention_days=0)

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
        assert params["enable_alarms"] == "true"

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

    # -------------------------------------------------------------------------
    # Entity ID Validation Tests
    # -------------------------------------------------------------------------

    def test_entity_id_valid(self):
        """Test valid entity IDs are accepted."""
        valid_ids = [
            "user123",
            "550e8400-e29b-41d4-a716-446655440000",  # UUID
            "sk-proj-abc123_xyz",  # API key format
            "user@example.com",  # Email-like
            "org:team:user",  # Colon-separated
            "a",  # Single char
        ]
        for entity_id in valid_ids:
            entity = Entity(id=entity_id)
            assert entity.id == entity_id

    def test_entity_id_rejects_hash(self):
        """Test entity ID with # is rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            Entity(id="user#123")
        assert exc_info.value.field == "id"
        assert "#" in exc_info.value.reason

    def test_entity_id_rejects_empty(self):
        """Test empty entity ID is rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            Entity(id="")
        assert exc_info.value.field == "id"
        assert "empty" in exc_info.value.reason

    def test_entity_id_rejects_too_long(self):
        """Test entity ID exceeding max length is rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            Entity(id="a" * 300)
        assert exc_info.value.field == "id"
        assert "length" in exc_info.value.reason

    def test_entity_id_must_start_alphanumeric(self):
        """Test entity ID must start with alphanumeric."""
        invalid_starts = ["_user", "-user", ".user", "@user", ":user"]
        for entity_id in invalid_starts:
            with pytest.raises(InvalidIdentifierError):
                Entity(id=entity_id)

    def test_entity_id_rejects_special_chars(self):
        """Test entity ID with invalid special characters is rejected."""
        invalid_ids = ["user/path", "user\\path", "user\nid", "user\tid", "user id"]
        for entity_id in invalid_ids:
            with pytest.raises(InvalidIdentifierError):
                Entity(id=entity_id)

    # -------------------------------------------------------------------------
    # Parent ID Validation Tests
    # -------------------------------------------------------------------------

    def test_parent_id_valid(self):
        """Test valid parent IDs are accepted."""
        entity = Entity(id="child-1", parent_id="parent-123")
        assert entity.parent_id == "parent-123"

    def test_parent_id_none_is_valid(self):
        """Test None parent_id is valid (root entity)."""
        entity = Entity(id="root-1", parent_id=None)
        assert entity.parent_id is None
        assert entity.is_parent is True

    def test_parent_id_rejects_hash(self):
        """Test parent ID with # is rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            Entity(id="child-1", parent_id="parent#123")
        assert exc_info.value.field == "parent_id"

    def test_parent_id_rejects_empty(self):
        """Test empty parent ID is rejected."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            Entity(id="child-1", parent_id="")
        assert exc_info.value.field == "parent_id"

    # -------------------------------------------------------------------------
    # BucketState Validation Tests
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
            burst_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.entity_id == "user-123"

    def test_bucket_state_rejects_invalid_entity_id(self):
        """Test BucketState rejects invalid entity_id."""
        with pytest.raises(InvalidIdentifierError) as exc_info:
            BucketState(
                entity_id="user#123",
                resource="api",
                limit_name="rpm",
                tokens_milli=100000,
                last_refill_ms=1000000,
                capacity_milli=100000,
                burst_milli=100000,
                refill_amount_milli=100000,
                refill_period_ms=60000,
            )
        assert exc_info.value.field == "entity_id"

    def test_bucket_state_rejects_invalid_resource(self):
        """Test BucketState rejects invalid resource name."""
        with pytest.raises(InvalidNameError) as exc_info:
            BucketState(
                entity_id="user-123",
                resource="api#v2",
                limit_name="rpm",
                tokens_milli=100000,
                last_refill_ms=1000000,
                capacity_milli=100000,
                burst_milli=100000,
                refill_amount_milli=100000,
                refill_period_ms=60000,
            )
        assert exc_info.value.field == "resource"

    def test_bucket_state_rejects_invalid_limit_name(self):
        """Test BucketState rejects invalid limit_name."""
        with pytest.raises(InvalidNameError) as exc_info:
            BucketState(
                entity_id="user-123",
                resource="api",
                limit_name="rpm#hack",
                tokens_milli=100000,
                last_refill_ms=1000000,
                capacity_milli=100000,
                burst_milli=100000,
                refill_amount_milli=100000,
                refill_period_ms=60000,
            )
        assert exc_info.value.field == "limit_name"

    # -------------------------------------------------------------------------
    # LimitStatus Validation Tests
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

    def test_limit_status_rejects_invalid_entity_id(self):
        """Test LimitStatus rejects invalid entity_id."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(InvalidIdentifierError) as exc_info:
            LimitStatus(
                entity_id="user#123",
                resource="api",
                limit_name="rpm",
                limit=limit,
                available=50,
                requested=10,
                exceeded=False,
                retry_after_seconds=0,
            )
        assert exc_info.value.field == "entity_id"

    def test_limit_status_rejects_invalid_resource(self):
        """Test LimitStatus rejects invalid resource."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(InvalidNameError) as exc_info:
            LimitStatus(
                entity_id="user-123",
                resource="123api",  # must start with letter
                limit_name="rpm",
                limit=limit,
                available=50,
                requested=10,
                exceeded=False,
                retry_after_seconds=0,
            )
        assert exc_info.value.field == "resource"

    # -------------------------------------------------------------------------
    # BucketState.from_limit Validation Tests
    # -------------------------------------------------------------------------

    def test_bucket_state_from_limit_validates_entity_id(self):
        """Test BucketState.from_limit validates entity_id."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(InvalidIdentifierError):
            BucketState.from_limit(
                entity_id="user#123",
                resource="api",
                limit=limit,
                now_ms=1000000,
            )

    def test_bucket_state_from_limit_validates_resource(self):
        """Test BucketState.from_limit validates resource."""
        limit = Limit.per_minute("rpm", 100)
        with pytest.raises(InvalidNameError):
            BucketState.from_limit(
                entity_id="user-123",
                resource="api#v2",
                limit=limit,
                now_ms=1000000,
            )

    # -------------------------------------------------------------------------
    # Catching ValidationError as Category
    # -------------------------------------------------------------------------

    def test_can_catch_validation_error_category(self):
        """Test that ValidationError can catch all validation errors."""
        # InvalidIdentifierError
        with pytest.raises(ValidationError):
            Entity(id="user#123")

        # InvalidNameError
        with pytest.raises(ValidationError):
            Limit.per_minute("rpm#test", 100)
