"""Tests for version tracking and compatibility checking."""

import pytest

from zae_limiter.version import (
    CURRENT_SCHEMA_VERSION,
    InfrastructureVersion,
    ParsedVersion,
    check_compatibility,
    get_schema_version,
    parse_version,
)


class TestParseVersion:
    """Tests for parse_version function."""

    def test_parse_simple_version(self):
        """Test parsing simple semantic version."""
        v = parse_version("1.2.3")
        assert v.major == 1
        assert v.minor == 2
        assert v.patch == 3
        assert v.prerelease is None

    def test_parse_version_with_prerelease(self):
        """Test parsing version with prerelease."""
        v = parse_version("1.0.0-beta")
        assert v.major == 1
        assert v.minor == 0
        assert v.patch == 0
        assert v.prerelease == "beta"

    def test_parse_version_with_v_prefix(self):
        """Test parsing version with 'v' prefix."""
        v = parse_version("v2.0.0")
        assert v.major == 2
        assert v.minor == 0
        assert v.patch == 0

    def test_parse_dev_version(self):
        """Test parsing PEP 440 dev version."""
        v = parse_version("0.1.0.dev123+gabcdef")
        assert v.major == 0
        assert v.minor == 1
        assert v.patch == 0
        assert v.prerelease == "dev"

    def test_parse_invalid_version(self):
        """Test parsing invalid version raises ValueError."""
        with pytest.raises(ValueError):
            parse_version("invalid")

        with pytest.raises(ValueError):
            parse_version("1.2")

    def test_version_string(self):
        """Test version string representation."""
        assert str(ParsedVersion(1, 2, 3)) == "1.2.3"
        assert str(ParsedVersion(1, 0, 0, "beta")) == "1.0.0-beta"


class TestVersionComparison:
    """Tests for version comparison operators."""

    def test_equal_versions(self):
        """Test equal version comparison."""
        v1 = parse_version("1.2.3")
        v2 = parse_version("1.2.3")
        assert v1 == v2
        assert not (v1 < v2)
        assert not (v1 > v2)

    def test_major_version_comparison(self):
        """Test major version comparison."""
        v1 = parse_version("1.0.0")
        v2 = parse_version("2.0.0")
        assert v1 < v2
        assert v2 > v1

    def test_minor_version_comparison(self):
        """Test minor version comparison."""
        v1 = parse_version("1.1.0")
        v2 = parse_version("1.2.0")
        assert v1 < v2
        assert v2 > v1

    def test_patch_version_comparison(self):
        """Test patch version comparison."""
        v1 = parse_version("1.0.1")
        v2 = parse_version("1.0.2")
        assert v1 < v2
        assert v2 > v1

    def test_prerelease_comparison(self):
        """Test prerelease versions are less than release."""
        v1 = parse_version("1.0.0-dev")
        v2 = parse_version("1.0.0")
        assert v1 < v2
        assert v2 > v1

    def test_le_and_ge(self):
        """Test less-than-or-equal and greater-than-or-equal."""
        v1 = parse_version("1.0.0")
        v2 = parse_version("1.0.0")
        v3 = parse_version("2.0.0")

        assert v1 <= v2
        assert v1 >= v2
        assert v1 <= v3
        assert v3 >= v1


class TestInfrastructureVersion:
    """Tests for InfrastructureVersion dataclass."""

    def test_from_record(self):
        """Test creating InfrastructureVersion from record dict."""
        record = {
            "schema_version": "1.0.0",
            "lambda_version": "1.2.3",
            "client_min_version": "1.0.0",
        }
        v = InfrastructureVersion.from_record(record)
        assert v.schema_version == "1.0.0"
        assert v.lambda_version == "1.2.3"
        assert v.client_min_version == "1.0.0"

    def test_from_record_with_defaults(self):
        """Test creating InfrastructureVersion with missing fields."""
        record: dict = {}
        v = InfrastructureVersion.from_record(record)
        assert v.schema_version == "1.0.0"
        assert v.lambda_version is None
        assert v.client_min_version == "0.0.0"


class TestCheckCompatibility:
    """Tests for check_compatibility function."""

    def test_compatible_versions(self):
        """Test fully compatible versions."""
        infra = InfrastructureVersion(
            schema_version="1.0.0",
            lambda_version="1.2.0",
            template_version=None,
            client_min_version="0.0.0",
        )
        result = check_compatibility("1.2.0", infra)

        assert result.is_compatible
        assert not result.requires_schema_migration
        assert not result.requires_lambda_update

    def test_lambda_update_available(self):
        """Test when Lambda update is available."""
        infra = InfrastructureVersion(
            schema_version="1.0.0",
            lambda_version="1.1.0",
            template_version=None,
            client_min_version="0.0.0",
        )
        result = check_compatibility("1.2.0", infra)

        assert result.is_compatible
        assert not result.requires_schema_migration
        assert result.requires_lambda_update
        assert "update available" in result.message.lower()

    def test_schema_migration_required(self):
        """Test when schema migration is required (major version mismatch)."""
        infra = InfrastructureVersion(
            schema_version="1.0.0",
            lambda_version="1.0.0",
            template_version=None,
            client_min_version="0.0.0",
        )
        result = check_compatibility("2.0.0", infra)

        assert not result.is_compatible
        assert result.requires_schema_migration
        assert "migration" in result.message.lower()

    def test_client_below_minimum(self):
        """Test when client is below minimum version."""
        infra = InfrastructureVersion(
            schema_version="1.0.0",
            lambda_version="1.5.0",
            template_version=None,
            client_min_version="1.3.0",
        )
        result = check_compatibility("1.2.0", infra)

        assert not result.is_compatible
        assert "upgrade" in result.message.lower()

    def test_invalid_client_version(self):
        """Test with invalid client version."""
        infra = InfrastructureVersion(
            schema_version="1.0.0",
            lambda_version="1.0.0",
            template_version=None,
            client_min_version="0.0.0",
        )
        result = check_compatibility("invalid", infra)

        assert not result.is_compatible
        assert "invalid" in result.message.lower()


class TestSchemaVersion:
    """Tests for schema version constants."""

    def test_current_schema_version(self):
        """Test CURRENT_SCHEMA_VERSION is valid."""
        v = parse_version(CURRENT_SCHEMA_VERSION)
        assert v.major >= 0

    def test_get_schema_version(self):
        """Test get_schema_version returns valid version."""
        version = get_schema_version()
        assert version == CURRENT_SCHEMA_VERSION
        v = parse_version(version)
        assert v.major >= 0

    def test_schema_version_reflects_bucket_pk_change(self):
        """Schema version >= 0.9.0 for bucket PK migration."""
        v = parse_version(CURRENT_SCHEMA_VERSION)
        assert v >= ParsedVersion(0, 9, 0)
