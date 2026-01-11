"""Tests for models."""

import pytest

from zae_limiter import Entity, Limit, LimitName, StackOptions


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
