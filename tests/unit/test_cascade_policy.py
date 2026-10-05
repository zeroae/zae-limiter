"""Tests for the per-resource cascade policy (ADR-146, #676)."""

import pytest

from zae_limiter import RateLimiter, schema
from zae_limiter.models import Limit
from zae_limiter.repository import Repository

RPM = Limit.per_minute("rpm", 100)


@pytest.fixture
async def cascade_repo(mock_dynamodb):
    """Moto-backed repository for cascade policy tests."""
    repo = Repository(name="test-cascade", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    yield repo
    await repo.close()


@pytest.fixture
async def cascade_limiter(cascade_repo):
    """RateLimiter sharing the cascade_repo table."""
    limiter = RateLimiter(repository=cascade_repo)
    async with limiter:
        yield limiter


async def _stamp_config_cascade(repo: Repository, pk: str, sk: str, value: bool) -> None:
    """Write a config item's `cascade` directly (before the API exists)."""
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={"PK": {"S": pk}, "SK": {"S": sk}},
        UpdateExpression="SET #c = :c",
        ExpressionAttributeNames={"#c": schema.CONFIG_FIELD_CASCADE},
        ExpressionAttributeValues={":c": {"BOOL": value}},
    )


class TestCascadeCodec:
    def test_encode_none_returns_none(self):
        assert schema.encode_cascade(None) is None

    def test_encode_true_and_false(self):
        assert schema.encode_cascade(True) == {"BOOL": True}
        assert schema.encode_cascade(False) == {"BOOL": False}

    def test_decode_absent_attribute_is_none(self):
        assert schema.decode_cascade({"PK": {"S": "ns/RESOURCE#gpt-4"}}) is None

    def test_decode_false_is_false_not_none(self):
        assert schema.decode_cascade({"cascade": {"BOOL": False}}) is False
        assert schema.decode_cascade({"cascade": {"BOOL": True}}) is True


class TestConfigCascadePersistence:
    async def test_unset_reads_as_none(self, cascade_repo):
        await cascade_repo.set_resource_defaults("gpt-4", [RPM])
        await cascade_repo.set_limits("user-1", [RPM], resource="gpt-4")
        assert await cascade_repo.get_resource_cascade("gpt-4") is None
        assert await cascade_repo.get_entity_cascade("user-1", "gpt-4") is None

    async def test_missing_config_reads_as_none(self, cascade_repo):
        assert await cascade_repo.get_resource_cascade("gpt-4") is None
        assert await cascade_repo.get_entity_cascade("user-1", "gpt-4") is None

    async def test_set_resource_defaults_preserves_a_stored_policy(self, cascade_repo):
        ns = cascade_repo._namespace_id
        await cascade_repo.set_resource_defaults("llm", [RPM])
        await _stamp_config_cascade(
            cascade_repo, schema.pk_resource(ns, "llm"), schema.sk_config(), False
        )

        # A caller that knows nothing about `cascade` must not erase it.
        await cascade_repo.set_resource_defaults("llm", [Limit.per_minute("rpm", 200)])

        assert await cascade_repo.get_resource_cascade("llm") is False

    async def test_set_limits_preserves_a_stored_policy(self, cascade_repo):
        ns = cascade_repo._namespace_id
        await cascade_repo.set_limits("user-1", [RPM], resource="llm")
        await _stamp_config_cascade(
            cascade_repo, schema.pk_entity(ns, "user-1"), schema.sk_config("llm"), True
        )

        await cascade_repo.set_limits("user-1", [Limit.per_minute("rpm", 200)], resource="llm")

        assert await cascade_repo.get_entity_cascade("user-1", "llm") is True

    async def test_an_explicit_disabled_still_keeps_the_stored_policy(self, cascade_repo):
        ns = cascade_repo._namespace_id
        await cascade_repo.set_resource_defaults("llm", [RPM])
        await _stamp_config_cascade(
            cascade_repo, schema.pk_resource(ns, "llm"), schema.sk_config(), False
        )

        await cascade_repo.set_resource_defaults("llm", [RPM], disabled=True)

        assert await cascade_repo.get_resource_cascade("llm") is False
        assert await cascade_repo.get_resource_disabled("llm") is True
