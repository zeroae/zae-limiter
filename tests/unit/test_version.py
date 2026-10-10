"""Tests for version tracking and compatibility checking."""

import pytest

from zae_limiter.version import (
    CURRENT_SCHEMA_VERSION,
    MIN_READER_VERSION_FOR_RESET_AFTER,
    InfrastructureVersion,
    ParsedVersion,
    check_compatibility,
    get_schema_version,
    parse_version,
    ratcheted_client_min_version,
    reads_reset_after,
    reset_after_refusal,
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
            parse_version("1.2.3.4")

    def test_parse_two_part_release(self):
        """A bare two-part release ("1.2") reads as "1.2.0" (#655)."""
        v = parse_version("1.2")
        assert v == ParsedVersion(1, 2, 0)
        assert v == parse_version("1.2.0")

    def test_parse_tagless_dev_build(self):
        """hatch-vcs's tagless fallback ("0.1.devN+g<sha>", #655) parses,
        and equals the three-part form a tagged dev build would carry."""
        v = parse_version("0.1.dev1+ge76be3284")
        assert v == ParsedVersion(0, 1, 0, "dev")
        assert v == parse_version("0.1.0.dev1+ge76be3284")

    def test_two_part_dev_orders_below_release(self):
        """A tagless dev build is still a pre-release: below the release it
        precedes, exactly like the three-part form (#655)."""
        assert parse_version("0.1.dev1+gabc") < parse_version("0.1.0")

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
        assert result.requires_client_upgrade
        assert "upgrade" in result.message.lower()

    def test_only_a_client_below_minimum_requires_a_client_upgrade(self):
        """An unparseable client version is incompatible but not "too old" (#638)."""
        infra = InfrastructureVersion("1.0.0", "1.0.0", None, "0.0.0")
        assert not check_compatibility("invalid", infra).requires_client_upgrade
        assert not check_compatibility("1.0.0", infra).requires_client_upgrade

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

    def test_tagless_ci_client_is_evaluated_not_rejected(self):
        """A CI checkout with no tags reports ``__version__`` as hatch-vcs's
        two-part fallback, ``0.1.devN+g<sha>`` (#655). It must be parsed and
        compared like any other client, not bounced as "Invalid client
        version" — that failure mode is the bug this pins.
        """
        infra = InfrastructureVersion(
            schema_version="0.10.0",
            lambda_version="0.14.0",
            template_version=None,
            client_min_version="0.0.0",
        )
        result = check_compatibility("0.1.dev1+ge76be3284", infra)

        assert result.is_compatible
        assert "invalid" not in result.message.lower()
        # Ordering: 0.1.devN reads as 0.1.0, older than the deployed 0.14.0
        # Lambda, so no update is offered — not just "didn't crash".
        assert not result.requires_lambda_update


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


class TestReadsResetAfter:
    """Whether a stack's Lambdas read ``reset_after`` limits (#638 A)."""

    def test_the_constant_is_the_introducing_release(self):
        assert MIN_READER_VERSION_FOR_RESET_AFTER == "0.15.0"

    @pytest.mark.parametrize(
        ("lambda_version", "expected"),
        [
            ("0.14.0", False),
            ("0.14.9", False),
            ("0.15.0", True),
            ("0.15.0-rc1", True),  # release part only, as check_compatibility does
            ("0.15.0rc1", True),  # the PEP 440 spelling hatch-vcs writes
            ("0.16.3", True),
            ("1.0.0", True),
            ("v0.15.0", True),
            (None, False),
            ("garbage", False),
        ],
    )
    def test_against_a_release_client(self, lambda_version, expected):
        assert reads_reset_after(lambda_version, "0.15.0") is expected

    def test_a_development_build_trusts_only_its_own_lambdas(self):
        dev = "0.14.1.dev99+gabcdef"
        assert reads_reset_after(dev, dev)
        assert not reads_reset_after("0.14.1.dev98+g000000", dev)

    def test_a_tagless_ci_build_trusts_only_its_own_lambdas(self):
        """The two-part hatch-vcs fallback a tagless checkout carries (#655)
        behaves like the three-part dev build above: trusted against itself,
        and still ordered correctly against a real release below the gate.
        """
        dev = "0.1.dev1+ge76be3284"
        assert reads_reset_after(dev, dev)
        assert not reads_reset_after("0.14.0", dev)  # ordering: 0.14.0 < 0.15.0


class TestRatchetedClientMinVersion:
    """The minimum a ``reset_after`` write leaves behind — never lowered (#638 C)."""

    @pytest.mark.parametrize("stored", [None, "0.0.0", "0.14.0", "garbage"])
    def test_raised_to_the_introducing_release(self, stored):
        assert ratcheted_client_min_version(stored, "0.15.2") == "0.15.0"

    @pytest.mark.parametrize("stored", ["0.15.0", "0.16.0"])
    def test_left_alone_when_already_high_enough(self, stored):
        assert ratcheted_client_min_version(stored, "0.16.0") is None

    def test_capped_at_a_development_writer(self):
        dev = "0.14.1.dev99+gabcdef"
        assert ratcheted_client_min_version("0.0.0", dev) == dev
        assert ratcheted_client_min_version(dev, dev) is None

    def test_capped_at_a_tagless_ci_writer(self):
        """The two-part hatch-vcs fallback (#655) is still below
        ``MIN_READER_VERSION_FOR_RESET_AFTER``, so the cap still applies —
        the same ordering ``test_capped_at_a_development_writer`` pins for
        the three-part form.
        """
        dev = "0.1.dev1+ge76be3284"
        assert ratcheted_client_min_version("0.0.0", dev) == dev
        assert ratcheted_client_min_version(dev, dev) is None

    @pytest.mark.parametrize("own", ["0.0.0+unknown", "garbage"])
    def test_an_unknown_writer_raises_nothing(self, own):
        assert ratcheted_client_min_version("0.0.0", own) is None

    def test_a_release_candidate_writer_ratchets_to_itself(self):
        assert ratcheted_client_min_version("0.0.0", "0.15.0rc1") == "0.15.0rc1"


class TestPep440PreReleases:
    """hatch-vcs writes PEP 440 versions; the gate's RC claim depends on them (#638)."""

    @pytest.mark.parametrize(
        ("text", "prerelease"),
        [
            ("0.15.0rc1", "rc1"),
            ("0.15.0a2", "a2"),
            ("0.15.0b1", "b1"),
            ("0.15.0rc1.dev3+gabcdef", "rc1-dev"),
            ("0.15.0+d20260926", None),
            ("0.0.0+unknown", None),
        ],
    )
    def test_parsed(self, text, prerelease):
        v = parse_version(text)
        assert (v.major, v.minor, v.patch, v.prerelease) == (
            0,
            int(text.split(".")[1]),
            0,
            prerelease,
        )

    def test_ordered_below_the_release(self):
        assert parse_version("0.15.0a1") < parse_version("0.15.0b1")
        assert parse_version("0.15.0b1") < parse_version("0.15.0rc1")
        assert parse_version("0.15.0rc1") < parse_version("0.15.0")

    @pytest.mark.parametrize("text", ["0.15.0rc", "0.15.0-", "0.15.0.0"])
    def test_malformed_is_still_rejected(self, text):
        with pytest.raises(ValueError):
            parse_version(text)

    def test_two_part_release_is_no_longer_malformed(self):
        """``0.15`` used to be rejected here; #655 makes it a valid ``X.Y``
        release, read as ``0.15.0`` — the same relaxed grammar the tagless
        fallback version needs.
        """
        assert parse_version("0.15") == parse_version("0.15.0")

    def test_a_release_candidate_lambda_reads_reset_after(self):
        assert reads_reset_after("0.15.0rc1", "0.15.0")
        assert not reads_reset_after("0.14.1rc1", "0.15.0")


class TestResetAfterRefusal:
    """One wording for the client and the provisioner (#638)."""

    def test_missing_record(self):
        message, auto = reset_after_refusal(False, None)
        assert "no version record" in message
        assert "zae-limiter deploy" in message
        assert auto is False

    def test_unknown_lambda_version(self):
        message, auto = reset_after_refusal(True, None)
        assert "Run 'zae-limiter upgrade' to deploy it" in message
        # upgrade skips a Lambda the stack lacks (#644): no deploy detour
        assert "zae-limiter deploy" not in message
        assert auto is False

    def test_old_lambda_version(self):
        message, auto = reset_after_refusal(True, "0.14.0")
        assert "zae-limiter upgrade" in message
        assert "zae-limiter deploy" not in message
        assert auto is True


class TestTopUpRefusal:
    """The ADR-149 gate's wording, in the ``reset_after_refusal`` contract."""

    def test_missing_record(self):
        from zae_limiter.version import top_up_refusal

        message, auto = top_up_refusal(False, None)
        assert "no version record" in message and "0.17.0" in message
        assert auto is False

    def test_unknown_lambda_version(self):
        from zae_limiter.version import top_up_refusal

        message, auto = top_up_refusal(True, None)
        assert "Run 'zae-limiter upgrade' to deploy it" in message
        assert auto is False

    def test_old_lambda_version(self):
        from zae_limiter.version import top_up_refusal

        message, auto = top_up_refusal(True, "0.16.0")
        assert "predate 0.17.0" in message
        assert auto is True
