"""Tests for models."""

import pytest

from zae_limiter import (
    AuditAction,
    AuditEvent,
    Entity,
    InvalidIdentifierError,
    InvalidNameError,
    Limit,
    LimiterInfo,
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

    def test_invalid_refill_amount(self):
        """Test validation of refill_amount must be positive."""
        with pytest.raises(ValueError, match="refill_amount must be positive"):
            Limit.custom("rpm", capacity=100, refill_amount=0, refill_period_seconds=60)

    def test_invalid_refill_amount_negative(self):
        """Test validation of negative refill_amount."""
        with pytest.raises(ValueError, match="refill_amount must be positive"):
            Limit.custom("rpm", capacity=100, refill_amount=-1, refill_period_seconds=60)

    def test_invalid_refill_period_seconds(self):
        """Test validation of refill_period_seconds must be positive."""
        with pytest.raises(ValueError, match="refill_period_seconds must be positive"):
            Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=0)

    def test_invalid_refill_period_seconds_negative(self):
        """Test validation of negative refill_period_seconds."""
        with pytest.raises(ValueError, match="refill_period_seconds must be positive"):
            Limit.custom("rpm", capacity=100, refill_amount=100, refill_period_seconds=-1)

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
        assert opts.permission_boundary is None
        assert opts.role_name_format is None
        assert opts.create_iam_roles is False  # Policies by default, roles opt-in

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

    # -------------------------------------------------------------------------
    # Permission Boundary Tests
    # -------------------------------------------------------------------------

    def test_permission_boundary_policy_name(self):
        """Test permission_boundary with just policy name."""
        opts = StackOptions(permission_boundary="MyBoundary")
        params = opts.to_parameters()
        assert params["permission_boundary"] == "MyBoundary"

    def test_permission_boundary_full_arn(self):
        """Test permission_boundary with full ARN."""
        arn = "arn:aws:iam::123456789012:policy/MyBoundary"
        opts = StackOptions(permission_boundary=arn)
        params = opts.to_parameters()
        assert params["permission_boundary"] == arn

    def test_permission_boundary_aws_managed_policy(self):
        """Test permission_boundary with AWS managed policy ARN."""
        arn = "arn:aws:iam::aws:policy/PowerUserAccess"
        opts = StackOptions(permission_boundary=arn)
        params = opts.to_parameters()
        assert params["permission_boundary"] == arn

    def test_permission_boundary_not_in_params_when_none(self):
        """Test permission_boundary is not in params when None."""
        opts = StackOptions()
        params = opts.to_parameters()
        assert "permission_boundary" not in params

    # -------------------------------------------------------------------------
    # Role Name Format Tests (Updated for component-based naming, ADR-116)
    # -------------------------------------------------------------------------

    def test_role_name_format_valid_prefix(self):
        """Test role_name_format with prefix pattern."""
        opts = StackOptions(role_name_format="app-{}")
        assert opts.get_role_name("mytable", "aggr") == "app-mytable-aggr"
        assert opts.get_role_name("mytable", "app") == "app-mytable-app"
        assert opts.get_role_name("mytable", "admin") == "app-mytable-admin"
        assert opts.get_role_name("mytable", "read") == "app-mytable-read"

    def test_role_name_format_prefix_suffix(self):
        """Test role_name_format with both prefix and suffix."""
        opts = StackOptions(role_name_format="pb-{}-PowerUser")
        assert opts.get_role_name("mytable", "aggr") == "pb-mytable-aggr-PowerUser"
        assert opts.get_role_name("mytable", "app") == "pb-mytable-app-PowerUser"

    def test_role_name_format_suffix_only(self):
        """Test role_name_format with suffix only."""
        opts = StackOptions(role_name_format="{}-prod")
        assert opts.get_role_name("mytable", "aggr") == "mytable-aggr-prod"

    def test_role_name_format_no_placeholder(self):
        """Test role_name_format without placeholder raises ValueError."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(role_name_format="my-custom-role")

    def test_role_name_format_multiple_placeholders(self):
        """Test role_name_format with multiple placeholders raises ValueError."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(role_name_format="app-{}-{}-role")

    def test_role_name_format_max_length_55(self):
        """Test role_name_format template up to 55 chars is valid."""
        # 55 chars total: 52 'a's + "-{}" (3 chars) = 55
        template = "a" * 52 + "-{}"  # 55 total
        opts = StackOptions(role_name_format=template)
        assert opts.role_name_format == template

    def test_role_name_format_56_chars_rejected(self):
        """Test role_name_format over 55 chars is rejected."""
        # 56 chars total: 53 'a's + "-{}" (3 chars) = 56
        template = "a" * 53 + "-{}"  # 56 total
        with pytest.raises(ValueError, match="too long"):
            StackOptions(role_name_format=template)

    def test_get_role_name_returns_none_when_format_not_set(self):
        """Test get_role_name returns None when role_name_format is None."""
        opts = StackOptions()
        assert opts.get_role_name("mytable", "aggr") is None

    def test_to_parameters_generates_four_role_names(self):
        """Test to_parameters generates separate role name params for each component."""
        opts = StackOptions(role_name_format="app-{}")
        params = opts.to_parameters(stack_name="mystack")

        assert params["aggregator_role_name"] == "app-mystack-aggr"
        assert params["app_role_name"] == "app-mystack-app"
        assert params["admin_role_name"] == "app-mystack-admin"
        assert params["readonly_role_name"] == "app-mystack-read"
        # Old single role_name param should not be present
        assert "role_name" not in params

    def test_to_parameters_no_role_names_when_format_none(self):
        """Test to_parameters omits role names when format is None."""
        opts = StackOptions()
        params = opts.to_parameters(stack_name="mystack")

        assert "aggregator_role_name" not in params
        assert "app_role_name" not in params
        assert "admin_role_name" not in params
        assert "readonly_role_name" not in params

    def test_role_names_not_in_params_without_stack_name(self):
        """Test role names are not in params when stack_name is not provided."""
        opts = StackOptions(role_name_format="app-{}")
        params = opts.to_parameters()
        assert "aggregator_role_name" not in params
        assert "app_role_name" not in params
        assert "admin_role_name" not in params
        assert "readonly_role_name" not in params

    # -------------------------------------------------------------------------
    # IAM Roles Tests (Issue #132)
    # -------------------------------------------------------------------------

    def test_create_iam_roles_default_false(self):
        """Test create_iam_roles defaults to False (policies always created)."""
        opts = StackOptions()
        assert opts.create_iam_roles is False

    def test_create_iam_roles_can_be_enabled(self):
        """Test create_iam_roles can be set to True."""
        opts = StackOptions(create_iam_roles=True)
        assert opts.create_iam_roles is True

    def test_create_iam_roles_in_to_parameters_enabled(self):
        """Test enable_iam_roles is in params when create_iam_roles is True."""
        opts = StackOptions(create_iam_roles=True)
        params = opts.to_parameters()
        assert params["enable_iam_roles"] == "true"

    def test_create_iam_roles_in_to_parameters_disabled(self):
        """Test enable_iam_roles is in params when create_iam_roles is False."""
        opts = StackOptions(create_iam_roles=False)
        params = opts.to_parameters()
        assert params["enable_iam_roles"] == "false"

    # -------------------------------------------------------------------------
    # ROLE_COMPONENTS Constant Tests (ADR-116)
    # -------------------------------------------------------------------------

    def test_role_components_constant_exists(self):
        """Test ROLE_COMPONENTS constant is defined."""
        from zae_limiter.models import ROLE_COMPONENTS

        assert ROLE_COMPONENTS == ("aggr", "app", "admin", "read")

    def test_role_components_max_length_invariant(self):
        """Test all role components are <= 8 characters (ADR-116 invariant)."""
        from zae_limiter.models import ROLE_COMPONENTS

        for component in ROLE_COMPONENTS:
            assert len(component) <= 8, f"Component '{component}' exceeds 8 chars"

    def test_get_role_name_requires_component(self):
        """Test get_role_name requires component parameter."""
        opts = StackOptions(role_name_format="app-{}")
        # New signature: get_role_name(stack_name, component)
        result = opts.get_role_name("mystack", "aggr")
        assert result == "app-mystack-aggr"

    def test_get_role_name_with_different_components(self):
        """Test get_role_name works with all component types."""
        opts = StackOptions(role_name_format="pb-{}")
        assert opts.get_role_name("mystack", "aggr") == "pb-mystack-aggr"
        assert opts.get_role_name("mystack", "app") == "pb-mystack-app"
        assert opts.get_role_name("mystack", "admin") == "pb-mystack-admin"
        assert opts.get_role_name("mystack", "read") == "pb-mystack-read"

    def test_get_role_name_validates_length(self):
        """Test get_role_name raises ValidationError when exceeding 64 chars."""
        # Format with long prefix that will exceed 64 chars with a long stack name
        opts = StackOptions(role_name_format="very-long-prefix-{}")
        # 19 (prefix) + 50 (stack) + 1 (-) + 5 (admin) = 75 chars > 64
        long_stack = "a" * 50
        with pytest.raises(ValidationError) as exc_info:
            opts.get_role_name(long_stack, "admin")
        assert "exceeds IAM 64-character limit" in str(exc_info.value)

    def test_get_role_name_returns_none_when_format_not_set_with_component(self):
        """Test get_role_name returns None when role_name_format is None."""
        opts = StackOptions()
        assert opts.get_role_name("mytable", "aggr") is None

    # -------------------------------------------------------------------------
    # Policy Name Format Tests
    # -------------------------------------------------------------------------

    def test_policy_name_format_valid_prefix(self):
        """Test valid policy_name_format with prefix."""
        opts = StackOptions(policy_name_format="pb-{}")
        assert opts.policy_name_format == "pb-{}"

    def test_policy_name_format_no_placeholder(self):
        """Test policy_name_format with no placeholder fails."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(policy_name_format="no-placeholder")

    def test_policy_name_format_multiple_placeholders(self):
        """Test policy_name_format with multiple placeholders fails."""
        with pytest.raises(ValueError, match="exactly one"):
            StackOptions(policy_name_format="{}-{}")

    def test_policy_name_format_too_long(self):
        """Test policy_name_format exceeding 122 chars fails."""
        long_format = "x" * 121 + "{}"  # 123 chars total
        with pytest.raises(ValueError, match="too long"):
            StackOptions(policy_name_format=long_format)

    def test_policy_name_format_max_length_122(self):
        """Test policy_name_format at exactly 122 chars succeeds."""
        max_format = "x" * 120 + "{}"  # exactly 122 chars
        opts = StackOptions(policy_name_format=max_format)
        assert opts.policy_name_format == max_format

    def test_get_policy_name_returns_none_when_format_not_set(self):
        """Test get_policy_name returns None when policy_name_format is None."""
        opts = StackOptions()
        assert opts.get_policy_name("mystack", "app") is None

    def test_get_policy_name_with_format(self):
        """Test get_policy_name returns formatted name."""
        opts = StackOptions(policy_name_format="pb-{}")
        result = opts.get_policy_name("mystack", "app")
        assert result == "pb-mystack-app"

    def test_get_policy_name_validates_length(self):
        """Test get_policy_name raises ValidationError for names > 128 chars."""
        opts = StackOptions(policy_name_format="prefix-{}-suffix")
        long_stack = "x" * 120  # Will exceed 128 chars
        with pytest.raises(ValidationError) as exc_info:
            opts.get_policy_name(long_stack, "admin")
        assert "exceeds IAM 128-character limit" in str(exc_info.value)

    def test_to_parameters_generates_policy_names(self):
        """Test to_parameters generates policy name params when format is set."""
        opts = StackOptions(policy_name_format="pb-{}")
        params = opts.to_parameters(stack_name="mystack")
        assert params["app_policy_name"] == "pb-mystack-app"
        assert params["admin_policy_name"] == "pb-mystack-admin"
        assert params["readonly_policy_name"] == "pb-mystack-read"

    def test_to_parameters_no_policy_names_when_format_none(self):
        """Test to_parameters excludes policy names when format is None."""
        opts = StackOptions()
        params = opts.to_parameters(stack_name="mystack")
        assert "app_policy_name" not in params
        assert "admin_policy_name" not in params
        assert "readonly_policy_name" not in params

    # -------------------------------------------------------------------------
    # create_iam and aggregator_role_arn Tests
    # -------------------------------------------------------------------------

    def test_create_iam_default_true(self):
        """Test create_iam defaults to True."""
        opts = StackOptions()
        assert opts.create_iam is True

    def test_create_iam_false_valid(self):
        """Test create_iam can be set to False."""
        opts = StackOptions(create_iam=False)
        assert opts.create_iam is False

    def test_create_iam_false_with_create_iam_roles_raises(self):
        """Test create_iam=False with create_iam_roles=True raises ValueError."""
        with pytest.raises(
            ValueError, match="create_iam_roles=True cannot be used with create_iam=False"
        ):
            StackOptions(create_iam=False, create_iam_roles=True)

    def test_aggregator_role_arn_default_none(self):
        """Test aggregator_role_arn defaults to None."""
        opts = StackOptions()
        assert opts.aggregator_role_arn is None

    def test_aggregator_role_arn_valid(self):
        """Test valid aggregator_role_arn is accepted."""
        valid_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        assert opts.aggregator_role_arn == valid_arn

    def test_aggregator_role_arn_valid_govcloud(self):
        """Test valid GovCloud aggregator_role_arn is accepted."""
        valid_arn = "arn:aws-us-gov:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        assert opts.aggregator_role_arn == valid_arn

    def test_aggregator_role_arn_valid_china(self):
        """Test valid China region aggregator_role_arn is accepted."""
        valid_arn = "arn:aws-cn:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        assert opts.aggregator_role_arn == valid_arn

    def test_aggregator_role_arn_invalid_raises(self):
        """Test invalid aggregator_role_arn raises ValueError."""
        invalid_arns = [
            "not-an-arn",
            "arn:aws:iam::12345:role/TooShort",  # Account ID too short
            "arn:aws:iam::1234567890123:role/TooLong",  # Account ID too long
            "arn:aws:s3:::bucket",  # Wrong service
            "arn:aws:iam::123456789012:user/NotARole",  # User not role
        ]
        for invalid_arn in invalid_arns:
            with pytest.raises(
                ValueError, match="aggregator_role_arn must be a valid IAM role ARN"
            ):
                StackOptions(aggregator_role_arn=invalid_arn)

    def test_to_parameters_includes_enable_iam_true(self):
        """Test to_parameters includes enable_iam when True."""
        opts = StackOptions(create_iam=True)
        params = opts.to_parameters()
        assert params["enable_iam"] == "true"

    def test_to_parameters_includes_enable_iam_false(self):
        """Test to_parameters includes enable_iam when False."""
        opts = StackOptions(create_iam=False)
        params = opts.to_parameters()
        assert params["enable_iam"] == "false"

    def test_to_parameters_includes_aggregator_role_arn(self):
        """Test to_parameters includes aggregator_role_arn when set."""
        valid_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(aggregator_role_arn=valid_arn)
        params = opts.to_parameters()
        assert params["aggregator_role_arn"] == valid_arn

    def test_to_parameters_excludes_aggregator_role_arn_when_none(self):
        """Test to_parameters excludes aggregator_role_arn when None."""
        opts = StackOptions()
        params = opts.to_parameters()
        assert "aggregator_role_arn" not in params

    def test_create_iam_false_with_aggregator_role_arn_valid(self):
        """Test create_iam=False with aggregator_role_arn is valid."""
        valid_arn = "arn:aws:iam::123456789012:role/MyLambdaRole"
        opts = StackOptions(create_iam=False, aggregator_role_arn=valid_arn)
        assert opts.create_iam is False
        assert opts.aggregator_role_arn == valid_arn


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

    def test_limit_name_rejects_slash(self):
        """Test limit name with / is rejected (slash only allowed for resources)."""
        with pytest.raises(InvalidNameError) as exc_info:
            Limit.per_minute("rpm/tpm", 100)
        assert exc_info.value.field == "name"
        assert "slash" not in exc_info.value.reason  # slash not in allowed chars

    # -------------------------------------------------------------------------
    # Resource Name Validation Tests
    # -------------------------------------------------------------------------

    def test_resource_name_valid(self):
        """Test valid resource names are accepted."""
        from zae_limiter.models import validate_resource

        valid_names = [
            "api",
            "gpt-4",
            "gpt-3.5-turbo",
            "openai/gpt-4",  # provider/model grouping
            "anthropic/claude-3",
            "anthropic/claude-3/opus",  # nested paths
            "a/b/c/d",  # deep nesting
        ]
        for name in valid_names:
            validate_resource(name)  # Should not raise

    def test_resource_name_with_trailing_slash(self):
        """Test resource name with trailing slash is valid."""
        from zae_limiter.models import validate_resource

        validate_resource("openai/")  # Should not raise

    def test_resource_name_rejects_leading_slash(self):
        """Test resource name starting with / is rejected (must start with letter)."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("/gpt-4")
        assert exc_info.value.field == "resource"
        assert "letter" in exc_info.value.reason

    def test_resource_name_rejects_hash(self):
        """Test resource name with # is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("openai#gpt-4")
        assert exc_info.value.field == "resource"
        assert "#" in exc_info.value.reason

    def test_resource_name_rejects_empty(self):
        """Test empty resource name is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("")
        assert exc_info.value.field == "resource"
        assert "empty" in exc_info.value.reason

    def test_resource_name_rejects_too_long(self):
        """Test resource name exceeding max length is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("a" * 100)
        assert exc_info.value.field == "resource"
        assert "length" in exc_info.value.reason

    def test_resource_name_must_start_with_letter(self):
        """Test resource name starting with number is rejected."""
        from zae_limiter.models import validate_resource

        with pytest.raises(InvalidNameError) as exc_info:
            validate_resource("123api")
        assert exc_info.value.field == "resource"
        assert "letter" in exc_info.value.reason

    # -------------------------------------------------------------------------
    # Entity Tests (internal model - no __post_init__ validation)
    # -------------------------------------------------------------------------

    def test_entity_valid(self):
        """Test valid Entity is created."""
        entity = Entity(id="user-123", name="Test User")
        assert entity.id == "user-123"

    def test_entity_allows_any_values_direct_construction(self):
        """Test Entity allows any values when constructed directly.

        Entity is used for DynamoDB deserialization. Validation is performed
        in Repository.create_entity() instead of __post_init__ to support
        reading existing data and avoid performance overhead.
        """
        # This should NOT raise - direct construction bypasses validation
        entity = Entity(
            id="user#123",  # Would be invalid in Repository.create_entity
            parent_id="",  # Empty - from DynamoDB deserialization
        )
        assert entity.id == "user#123"

    def test_entity_with_parent(self):
        """Test Entity with parent_id."""
        entity = Entity(id="child-1", parent_id="parent-123")
        assert entity.parent_id == "parent-123"
        assert entity.is_child is True
        assert entity.is_parent is False

    def test_entity_without_parent(self):
        """Test Entity without parent_id (root entity)."""
        entity = Entity(id="root-1", parent_id=None)
        assert entity.parent_id is None
        assert entity.is_parent is True
        assert entity.is_child is False

    # -------------------------------------------------------------------------
    # BucketState Tests (internal model - no __post_init__ validation)
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

    def test_bucket_state_allows_any_values_direct_construction(self):
        """Test BucketState allows any values when constructed directly.

        BucketState is an internal model used for DynamoDB deserialization.
        Validation is performed in from_limit() instead of __post_init__
        to support reading existing data and avoid performance overhead.
        """
        # This should NOT raise - direct construction bypasses validation
        bucket = BucketState(
            entity_id="user#123",  # Would be invalid in from_limit
            resource="",  # Empty - from DynamoDB deserialization
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=100000,
            burst_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.entity_id == "user#123"

    # -------------------------------------------------------------------------
    # LimitStatus Tests (internal model - no validation)
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

    def test_limit_status_is_internal_model_no_validation(self):
        """Test LimitStatus is an internal model without validation.

        LimitStatus is created internally by the limiter from already-validated
        inputs. No validation is performed to avoid performance overhead during
        rate limiting operations.
        """
        limit = Limit.per_minute("rpm", 100)
        # This should NOT raise - LimitStatus doesn't validate
        status = LimitStatus(
            entity_id="user#123",  # Would be invalid if validated
            resource="123api",  # Would be invalid if validated
            limit_name="rpm",
            limit=limit,
            available=50,
            requested=10,
            exceeded=False,
            retry_after_seconds=0,
        )
        assert status.entity_id == "user#123"

    # -------------------------------------------------------------------------
    # BucketState.from_limit Tests (internal factory - no validation)
    # -------------------------------------------------------------------------

    def test_bucket_state_from_limit_is_internal_no_validation(self):
        """Test BucketState.from_limit is internal and does not validate.

        Validation of entity_id and resource is performed at the API boundary
        (RateLimiter public methods), not in this internal factory method.
        """
        limit = Limit.per_minute("rpm", 100)
        # This should NOT raise - from_limit is internal and trusts its caller
        bucket = BucketState.from_limit(
            entity_id="user#123",  # Would be invalid at API boundary
            resource="api#v2",  # Would be invalid at API boundary
            limit=limit,
            now_ms=1000000,
        )
        assert bucket.entity_id == "user#123"
        assert bucket.resource == "api#v2"

    def test_bucket_state_from_limit_valid(self):
        """Test BucketState.from_limit creates bucket correctly."""
        limit = Limit.per_minute("rpm", 100)
        bucket = BucketState.from_limit(
            entity_id="user-123",
            resource="gpt-3.5-turbo",
            limit=limit,
            now_ms=1000000,
        )
        assert bucket.entity_id == "user-123"
        assert bucket.resource == "gpt-3.5-turbo"
        assert bucket.limit_name == "rpm"

    # -------------------------------------------------------------------------
    # Catching ValidationError as Category
    # -------------------------------------------------------------------------

    def test_can_catch_validation_error_category(self):
        """Test that ValidationError can catch all validation errors."""
        # InvalidNameError (via Limit)
        with pytest.raises(ValidationError):
            Limit.per_minute("rpm#test", 100)


class TestBucketStateProperties:
    """Tests for BucketState property accessors."""

    def test_tokens_property(self):
        """Test tokens property converts millitokens to tokens."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=150500,  # 150.5 tokens
            last_refill_ms=1000000,
            capacity_milli=100000,
            burst_milli=200000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.tokens == 150  # truncates to 150

    def test_tokens_property_exact(self):
        """Test tokens property with exact token value."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,  # exactly 100 tokens
            last_refill_ms=1000000,
            capacity_milli=100000,
            burst_milli=100000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.tokens == 100

    def test_capacity_property(self):
        """Test capacity property converts millitokens to tokens."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=250000,  # 250 tokens
            burst_milli=300000,
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.capacity == 250

    def test_burst_property(self):
        """Test burst property converts millitokens to tokens."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=100000,
            burst_milli=500000,  # 500 tokens
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.burst == 500

    def test_burst_property_with_fractional_millitokens(self):
        """Test burst property truncates fractional tokens."""
        bucket = BucketState(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            tokens_milli=100000,
            last_refill_ms=1000000,
            capacity_milli=100000,
            burst_milli=199999,  # 199.999 tokens
            refill_amount_milli=100000,
            refill_period_ms=60000,
        )
        assert bucket.burst == 199  # truncates to 199


class TestLimitStatusDeficit:
    """Tests for LimitStatus deficit property."""

    def test_deficit_when_exceeded(self):
        """Test deficit calculation when limit is exceeded."""
        limit = Limit.per_minute("rpm", 100)
        status = LimitStatus(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            limit=limit,
            available=30,
            requested=50,
            exceeded=True,
            retry_after_seconds=12.0,
        )
        assert status.deficit == 20  # 50 - 30

    def test_deficit_when_not_exceeded(self):
        """Test deficit is 0 when limit is not exceeded."""
        limit = Limit.per_minute("rpm", 100)
        status = LimitStatus(
            entity_id="user-123",
            resource="api",
            limit_name="rpm",
            limit=limit,
            available=100,
            requested=50,
            exceeded=False,
            retry_after_seconds=0,
        )
        assert status.deficit == 0


class TestAuditEvent:
    """Tests for AuditEvent model."""

    def test_to_dict_minimal(self):
        """Test to_dict with minimal required fields."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.ENTITY_CREATED,
            entity_id="user-123",
        )
        result = event.to_dict()
        assert result == {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "entity_created",
            "entity_id": "user-123",
        }

    def test_to_dict_with_principal(self):
        """Test to_dict includes principal when set."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_SET,
            entity_id="user-123",
            principal="admin@example.com",
        )
        result = event.to_dict()
        assert result["principal"] == "admin@example.com"

    def test_to_dict_with_resource(self):
        """Test to_dict includes resource when set."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_SET,
            entity_id="user-123",
            resource="gpt-4",
        )
        result = event.to_dict()
        assert result["resource"] == "gpt-4"

    def test_to_dict_with_details(self):
        """Test to_dict includes details when non-empty."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_SET,
            entity_id="user-123",
            details={"limits": ["rpm", "tpm"]},
        )
        result = event.to_dict()
        assert result["details"] == {"limits": ["rpm", "tpm"]}

    def test_to_dict_excludes_empty_details(self):
        """Test to_dict excludes details when empty dict."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.ENTITY_CREATED,
            entity_id="user-123",
            details={},
        )
        result = event.to_dict()
        assert "details" not in result

    def test_to_dict_full(self):
        """Test to_dict with all fields populated."""
        event = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.LIMITS_DELETED,
            entity_id="user-123",
            principal="system",
            resource="gpt-4",
            details={"reason": "quota exceeded"},
        )
        result = event.to_dict()
        assert result == {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "limits_deleted",
            "entity_id": "user-123",
            "principal": "system",
            "resource": "gpt-4",
            "details": {"reason": "quota exceeded"},
        }

    def test_from_dict_minimal(self):
        """Test from_dict with minimal required fields."""
        data = {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "entity_created",
            "entity_id": "user-123",
        }
        event = AuditEvent.from_dict(data)
        assert event.event_id == "01ARZ3NDEKTSV4RRFFQ69G5FAV"
        assert event.timestamp == "2024-01-15T10:30:00Z"
        assert event.action == "entity_created"
        assert event.entity_id == "user-123"
        assert event.principal is None
        assert event.resource is None
        assert event.details == {}

    def test_from_dict_full(self):
        """Test from_dict with all fields populated."""
        data = {
            "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
            "timestamp": "2024-01-15T10:30:00Z",
            "action": "limits_set",
            "entity_id": "user-123",
            "principal": "admin@example.com",
            "resource": "gpt-4",
            "details": {"limits": ["rpm", "tpm"]},
        }
        event = AuditEvent.from_dict(data)
        assert event.principal == "admin@example.com"
        assert event.resource == "gpt-4"
        assert event.details == {"limits": ["rpm", "tpm"]}

    def test_to_dict_from_dict_roundtrip(self):
        """Test serialization round-trip."""
        original = AuditEvent(
            event_id="01ARZ3NDEKTSV4RRFFQ69G5FAV",
            timestamp="2024-01-15T10:30:00Z",
            action=AuditAction.ENTITY_DELETED,
            entity_id="user-123",
            principal="admin",
            resource="api",
            details={"reason": "test"},
        )
        data = original.to_dict()
        restored = AuditEvent.from_dict(data)
        assert restored.event_id == original.event_id
        assert restored.timestamp == original.timestamp
        assert restored.action == original.action
        assert restored.entity_id == original.entity_id
        assert restored.principal == original.principal
        assert restored.resource == original.resource
        assert restored.details == original.details


class TestAuditAction:
    """Tests for AuditAction constants."""

    def test_action_constants(self):
        """Test audit action constant values."""
        assert AuditAction.ENTITY_CREATED == "entity_created"
        assert AuditAction.ENTITY_DELETED == "entity_deleted"
        assert AuditAction.LIMITS_SET == "limits_set"
        assert AuditAction.LIMITS_DELETED == "limits_deleted"


class TestLimiterInfo:
    """Tests for LimiterInfo model."""

    def test_basic_construction(self):
        """Test basic LimiterInfo construction."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.stack_name == "my-app"
        assert info.user_name == "my-app"
        assert info.region == "us-east-1"
        assert info.stack_status == "CREATE_COMPLETE"
        assert info.creation_time == "2024-01-15T10:30:00Z"
        assert info.last_updated_time is None
        assert info.version is None
        assert info.lambda_version is None
        assert info.schema_version is None

    def test_construction_with_all_fields(self):
        """Test LimiterInfo with all optional fields."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
            last_updated_time="2024-01-16T14:00:00Z",
            version="0.5.0",
            lambda_version="0.5.0",
            schema_version="1.0.0",
        )
        assert info.last_updated_time == "2024-01-16T14:00:00Z"
        assert info.version == "0.5.0"
        assert info.lambda_version == "0.5.0"
        assert info.schema_version == "1.0.0"

    def test_is_healthy_create_complete(self):
        """Test is_healthy returns True for CREATE_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is True

    def test_is_healthy_update_complete(self):
        """Test is_healthy returns True for UPDATE_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is True

    def test_is_healthy_false_for_in_progress(self):
        """Test is_healthy returns False for in-progress states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is False

    def test_is_healthy_false_for_failed(self):
        """Test is_healthy returns False for failed states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is False

    def test_is_in_progress_create(self):
        """Test is_in_progress for CREATE_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_update(self):
        """Test is_in_progress for UPDATE_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_delete(self):
        """Test is_in_progress for DELETE_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="DELETE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_rollback(self):
        """Test is_in_progress for ROLLBACK_IN_PROGRESS."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_ROLLBACK_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is True

    def test_is_in_progress_false_for_complete(self):
        """Test is_in_progress returns False for complete states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_in_progress is False

    def test_is_failed_create_failed(self):
        """Test is_failed for CREATE_FAILED."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_update_failed(self):
        """Test is_failed for UPDATE_FAILED."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_rollback_complete(self):
        """Test is_failed for ROLLBACK_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="ROLLBACK_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_update_rollback_complete(self):
        """Test is_failed for UPDATE_ROLLBACK_COMPLETE."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="UPDATE_ROLLBACK_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_rollback_failed(self):
        """Test is_failed for ROLLBACK_FAILED."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="ROLLBACK_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is True

    def test_is_failed_false_for_complete(self):
        """Test is_failed returns False for healthy complete states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is False

    def test_is_failed_false_for_in_progress(self):
        """Test is_failed returns False for in-progress states."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_failed is False

    def test_frozen(self):
        """Test that LimiterInfo is immutable."""
        info = LimiterInfo(
            stack_name="my-app",
            user_name="my-app",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        with pytest.raises(AttributeError):
            info.stack_name = "other"

    def test_states_are_mutually_exclusive(self):
        """Test that at most one of is_healthy/is_in_progress/is_failed is True."""
        # Healthy state
        healthy = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="CREATE_COMPLETE",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert healthy.is_healthy is True
        assert healthy.is_in_progress is False
        assert healthy.is_failed is False

        # In-progress state
        in_progress = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="UPDATE_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert in_progress.is_healthy is False
        assert in_progress.is_in_progress is True
        assert in_progress.is_failed is False

        # Failed state
        failed = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="CREATE_FAILED",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert failed.is_healthy is False
        assert failed.is_in_progress is False
        assert failed.is_failed is True

    def test_rollback_in_progress_is_both_in_progress_and_failed(self):
        """Test ROLLBACK_IN_PROGRESS matches both is_in_progress and is_failed."""
        # This is a special case: rollback is both in-progress AND indicates failure
        info = LimiterInfo(
            stack_name="test",
            user_name="test",
            region="us-east-1",
            stack_status="ROLLBACK_IN_PROGRESS",
            creation_time="2024-01-15T10:30:00Z",
        )
        assert info.is_healthy is False
        assert info.is_in_progress is True
        assert info.is_failed is True  # ROLLBACK contains "ROLLBACK"
