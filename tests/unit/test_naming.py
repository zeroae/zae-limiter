"""Tests for naming module."""

import pytest

from zae_limiter.exceptions import ValidationError
from zae_limiter.naming import (
    PREFIX,
    normalize_name,
    normalize_stack_name,
    validate_name,
    validate_stack_name,
)


class TestValidateName:
    """Test validate_name function."""

    def test_valid_simple_name(self) -> None:
        """Valid simple name passes."""
        validate_name("limiter")  # No exception

    def test_valid_hyphenated_name(self) -> None:
        """Valid hyphenated name passes."""
        validate_name("my-app")  # No exception

    def test_valid_alphanumeric(self) -> None:
        """Valid alphanumeric name passes."""
        validate_name("app123")  # No exception

    def test_valid_uppercase(self) -> None:
        """Valid uppercase name passes."""
        validate_name("MyApp")  # No exception

    def test_valid_mixed_case_with_hyphens(self) -> None:
        """Valid mixed case with hyphens passes."""
        validate_name("My-App-123")  # No exception

    def test_empty_name_raises(self) -> None:
        """Empty name raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_name("")
        assert exc_info.value.field == "name"
        assert exc_info.value.value == ""
        assert "cannot be empty" in exc_info.value.reason

    def test_underscore_raises_helpful_message(self) -> None:
        """Underscore raises with helpful message about using hyphens."""
        with pytest.raises(ValidationError) as exc_info:
            validate_name("rate_limits")
        assert "underscore" in exc_info.value.reason.lower()
        assert "hyphen" in exc_info.value.reason.lower()
        assert exc_info.value.value == "rate_limits"

    def test_period_raises(self) -> None:
        """Period raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_name("my.app")
        assert "period" in exc_info.value.reason.lower()
        assert exc_info.value.value == "my.app"

    def test_space_raises_helpful_message(self) -> None:
        """Space raises with helpful message about using hyphens."""
        with pytest.raises(ValidationError) as exc_info:
            validate_name("my app")
        assert "space" in exc_info.value.reason.lower()
        assert "hyphen" in exc_info.value.reason.lower()
        assert exc_info.value.value == "my app"

    def test_starts_with_number_raises(self) -> None:
        """Name starting with number raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_name("123app")
        assert "start with a letter" in exc_info.value.reason

    def test_starts_with_hyphen_raises(self) -> None:
        """Name starting with hyphen raises ValidationError."""
        with pytest.raises(ValidationError) as exc_info:
            validate_name("-app")
        assert "start with a letter" in exc_info.value.reason

    def test_special_chars_raise(self) -> None:
        """Special characters raise ValidationError."""
        for char in ["@", "!", "$", "%", "&", "*", "(", ")", "+"]:
            with pytest.raises(ValidationError):
                validate_name(f"app{char}name")

    def test_max_stack_name_length_is_55(self) -> None:
        """Test stack names up to 55 chars are valid.

        With 8-char max component (ADR-116), 55 chars leaves room for format template.
        Formula: 64 (IAM limit) - 8 (max component) - 1 (dash) = 55
        """
        # 55 chars should be valid
        name = "a" * 55
        validate_name(name)  # Should not raise

    def test_stack_name_56_chars_rejected(self) -> None:
        """Test stack names over 55 chars are rejected."""
        name = "a" * 56
        with pytest.raises(ValidationError) as exc_info:
            validate_name(name)
        assert "55 character" in exc_info.value.reason
        assert "Too long" in exc_info.value.reason

    def test_single_char_valid(self) -> None:
        """Single character name is valid."""
        validate_name("a")  # No exception


class TestNormalizeName:
    """Test normalize_name function."""

    def test_returns_name_as_is(self) -> None:
        """Returns name unchanged (no prefix added)."""
        result = normalize_name("limiter")
        assert result == "limiter"

    def test_already_prefixed_returned_as_is(self) -> None:
        """Already prefixed name is returned as-is (valid name)."""
        result = normalize_name("ZAEL-limiter")
        assert result == "ZAEL-limiter"

    def test_validates_before_returning(self) -> None:
        """Invalid names are rejected."""
        with pytest.raises(ValidationError):
            normalize_name("rate_limits")

    def test_prefix_constant(self) -> None:
        """PREFIX constant is ZAEL-."""
        assert PREFIX == "ZAEL-"


class TestAliases:
    """Test backward compatibility aliases."""

    def test_validate_stack_name_is_alias(self) -> None:
        """validate_stack_name is alias for validate_name."""
        assert validate_stack_name is validate_name

    def test_normalize_stack_name_is_alias(self) -> None:
        """normalize_stack_name is alias for normalize_name."""
        assert normalize_stack_name is normalize_name


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_unicode_raises(self) -> None:
        """Unicode characters raise ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("café")

    def test_emoji_raises(self) -> None:
        """Emoji characters raise ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("app🚀")

    def test_tab_raises(self) -> None:
        """Tab character raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("my\tapp")

    def test_newline_raises(self) -> None:
        """Newline character raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("my\napp")

    def test_slash_raises(self) -> None:
        """Slash character raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("my/app")

    def test_backslash_raises(self) -> None:
        """Backslash character raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("my\\app")

    def test_colon_raises(self) -> None:
        """Colon character raises ValidationError."""
        with pytest.raises(ValidationError):
            validate_name("my:app")

    def test_consecutive_hyphens_valid(self) -> None:
        """Consecutive hyphens are valid."""
        validate_name("my--app")  # No exception

    def test_trailing_hyphen_valid(self) -> None:
        """Trailing hyphen is valid."""
        validate_name("myapp-")  # No exception

    def test_all_numbers_after_letter_valid(self) -> None:
        """All numbers after starting letter is valid."""
        validate_name("a123456789")  # No exception
