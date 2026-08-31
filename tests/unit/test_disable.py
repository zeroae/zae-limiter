"""Tests for resource/entity disable (ADR-125)."""

import pytest

from zae_limiter import RateLimiter, schema
from zae_limiter.models import Limit
from zae_limiter.repository import Repository


@pytest.fixture
async def disable_repo(mock_dynamodb):
    """Moto-backed repository for disable tests."""
    repo = Repository(name="test-disable", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    yield repo
    await repo.close()


@pytest.fixture
async def disable_limiter(disable_repo):
    """RateLimiter sharing the disable_repo table."""
    limiter = RateLimiter(repository=disable_repo)
    async with limiter:
        yield limiter


class TestDisabledCodec:
    def test_encode_none_returns_none(self):
        assert schema.encode_disabled(None) is None

    def test_encode_true(self):
        assert schema.encode_disabled(True) == {"BOOL": True}

    def test_encode_false(self):
        assert schema.encode_disabled(False) == {"BOOL": False}

    def test_decode_absent_attribute_is_none(self):
        assert schema.decode_disabled({"PK": {"S": "ns/RESOURCE#gpt-4"}}) is None

    def test_decode_true(self):
        assert schema.decode_disabled({"disabled": {"BOOL": True}}) is True

    def test_decode_false_is_false_not_none(self):
        # Explicit False must be distinguishable from "inherit"
        assert schema.decode_disabled({"disabled": {"BOOL": False}}) is False


@pytest.mark.asyncio
class TestConfigDisabledPersistence:
    async def test_resource_disabled_defaults_to_none(self, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        assert await disable_repo.get_resource_disabled("gpt-4") is None

    async def test_set_resource_defaults_with_disabled_true(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        assert await disable_repo.get_resource_disabled("gpt-4") is True

    async def test_set_resource_defaults_preserves_existing_disabled(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        # A caller that knows nothing about `disabled` must not re-enable it.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 200)])
        assert await disable_repo.get_resource_disabled("gpt-4") is True

    async def test_explicit_false_is_preserved_and_distinct_from_none(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=False
        )
        assert await disable_repo.get_resource_disabled("gpt-4") is False

    async def test_entity_disabled_roundtrip(self, disable_repo):
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=False
        )
        assert await disable_repo.get_entity_disabled("user-1", "gpt-4") is False

    async def test_entity_disabled_defaults_to_none(self, disable_repo):
        await disable_repo.set_limits("user-2", [Limit.per_minute("rpm", 10)], resource="gpt-4")
        assert await disable_repo.get_entity_disabled("user-2", "gpt-4") is None

    async def test_set_limits_preserves_existing_disabled(self, disable_repo):
        await disable_repo.set_limits(
            "user-3", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=True
        )
        # A caller that knows nothing about `disabled` must not re-enable it.
        await disable_repo.set_limits("user-3", [Limit.per_minute("rpm", 20)], resource="gpt-4")
        assert await disable_repo.get_entity_disabled("user-3", "gpt-4") is True


@pytest.mark.asyncio
class TestResolveDisabled:
    async def test_nothing_set_resolves_false(self, disable_repo):
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, None)

    async def test_resource_disabled_applies_to_entity(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

    async def test_entity_false_overrides_resource_true(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=False
        )
        # The whole point of the feature: a carve-out for one entity.
        assert await disable_repo.resolve_disabled("vip-1", "gpt-4") == (False, "entity")
        # Other entities stay disabled.
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

    async def test_entity_true_overrides_resource_unset(self, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "bad-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=True
        )
        assert await disable_repo.resolve_disabled("bad-1", "gpt-4") == (True, "entity")

    async def test_entity_default_disables_all_resources_for_entity(self, disable_repo):
        await disable_repo.set_limits(
            "banned-1", [Limit.per_minute("rpm", 10)], resource="_default_", disabled=True
        )
        assert await disable_repo.resolve_disabled("banned-1", "gpt-4") == (
            True,
            "entity_default",
        )

    async def test_resource_specific_entity_beats_entity_default(self, disable_repo):
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 10)], resource="_default_", disabled=True
        )
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=False
        )
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, "entity")

    async def test_disabled_resolves_independently_of_limits(self, disable_repo):
        # The resource sets `disabled` but the entity supplies the limits.
        # The limits walk stops at "entity"; the disabled walk must still
        # reach "resource".
        await disable_repo.set_resource_defaults("gpt-4", [], disabled=True)
        await disable_repo.set_limits("user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4")
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")
