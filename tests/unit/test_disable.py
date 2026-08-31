"""Tests for resource/entity disable (ADR-125)."""

import pytest
from botocore.exceptions import ClientError

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


@pytest.mark.asyncio
class TestFanout:
    async def test_disable_resource_stamps_existing_buckets(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        for entity in ("user-1", "user-2", "user-3"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass

        assert await disable_repo.disable_resource("gpt-4") == 3

    async def test_disable_resource_skips_entities_with_false_override(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 100)], resource="gpt-4", disabled=False
        )
        for entity in ("user-1", "vip-1"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass

        # vip-1 is carved out, so only user-1's bucket is stamped.
        assert await disable_repo.disable_resource("gpt-4") == 1

    async def test_disable_is_idempotent(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        assert await disable_repo.disable_resource("gpt-4") == 1
        assert await disable_repo.disable_resource("gpt-4") == 1

    async def test_enable_resource_clears_the_stamp(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")
        assert await disable_repo.enable_resource("gpt-4") == 1
        assert await disable_repo.get_resource_disabled("gpt-4") is False

    async def test_disable_entity_covers_all_resources(self, disable_limiter, disable_repo):
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        for res in ("gpt-4", "claude-3"):
            async with disable_limiter.acquire("user-1", res, {"rpm": 1}):
                pass

        assert await disable_repo.disable_entity("user-1") == 2

    async def test_disable_entity_scoped_to_one_resource(self, disable_limiter, disable_repo):
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        for res in ("gpt-4", "claude-3"):
            async with disable_limiter.acquire("user-1", res, {"rpm": 1}):
                pass

        assert await disable_repo.disable_entity("user-1", resource="gpt-4") == 1

    async def test_unscoped_disable_entity_skips_resource_with_override(
        self, disable_limiter, disable_repo
    ):
        # user-1 has an explicit carve-out on gpt-4 (disabled=False) that
        # outranks the entity's `_default_` in resolve_disabled's walk.
        # claude-3 has no resource-specific override, so it inherits
        # whatever the entity `_default_` directive says.
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 100)], resource="gpt-4", disabled=False
        )
        for res in ("gpt-4", "claude-3"):
            async with disable_limiter.acquire("user-1", res, {"rpm": 1}):
                pass

        # Unscoped disable_entity applies the entity `_default_` directive;
        # only claude-3's bucket (no override) should be stamped.
        assert await disable_repo.disable_entity("user-1") == 1

        gpt4_pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "gpt-4", 0)
        claude_pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "claude-3", 0)
        client = await disable_repo._get_client()

        gpt4_item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": gpt4_pk}, "SK": {"S": schema.sk_state()}},
        )
        assert schema.BUCKET_FIELD_DISABLED not in gpt4_item.get("Item", {})

        claude_item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": claude_pk}, "SK": {"S": schema.sk_state()}},
        )
        assert claude_item["Item"][schema.BUCKET_FIELD_DISABLED] == {"BOOL": True}

        # resolve_disabled must still agree with what got stamped.
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, "entity")
        assert await disable_repo.resolve_disabled("user-1", "claude-3") == (
            True,
            "entity_default",
        )


@pytest.mark.asyncio
class TestResourceDisableWithoutPriorConfig:
    """Controller Ruling R2: disable_resource must not require a prior config item.

    A resource running purely on system defaults (no set_resource_defaults call)
    is the common case during an incident. Guarding the config write with
    ConditionExpression="attribute_exists(PK)" would make disable_resource()
    fail exactly then, so the write must be an unconditional upsert instead.
    """

    async def test_disable_resource_creates_stub_config_item(self, disable_repo):
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        assert await disable_repo.get_resource_disabled("gpt-4") is None

        # No buckets exist yet, so nothing is stamped, but the config write
        # must succeed even though "gpt-4" has no prior resource config item.
        assert await disable_repo.disable_resource("gpt-4") == 0
        assert await disable_repo.get_resource_disabled("gpt-4") is True

        # The stub item carries no l_* attributes, so limits resolution still
        # falls through past the (now-existing) resource config to system
        # defaults, proving the stub is safe for limits resolution.
        limits, _on_unavailable, source = await disable_repo.resolve_limits("user-1", "gpt-4")
        assert source == "system"
        assert limits == [Limit.per_minute("rpm", 100)]

    async def test_enable_resource_creates_stub_config_item(self, disable_repo):
        assert await disable_repo.get_resource_disabled("claude-3") is None
        assert await disable_repo.enable_resource("claude-3") == 0
        assert await disable_repo.get_resource_disabled("claude-3") is False


@pytest.mark.asyncio
class TestClearDisabledWithoutPriorConfig:
    """Clearing back to "inherit" must be a no-op when no config item exists.

    Without the ConditionExpression on the REMOVE, an UpdateItem against a
    missing item would create a stub item containing only keys. The handler
    must instead catch ConditionalCheckFailedException and treat it as a
    no-op (Controller Ruling R2).
    """

    async def test_clear_resource_disabled_with_no_config_item_is_noop(self, disable_repo):
        assert await disable_repo.clear_resource_disabled("never-touched") == 0
        assert await disable_repo.get_resource_disabled("never-touched") is None

    async def test_clear_entity_disabled_with_no_config_item_is_noop(self, disable_repo):
        assert await disable_repo.clear_entity_disabled("ghost-entity") == 0
        assert await disable_repo.get_entity_disabled("ghost-entity", "_default_") is None


@pytest.mark.asyncio
class TestClearDisabledRevertsToInherited:
    """Clearing an explicit value with an existing config item recomputes
    the effective state and restamps buckets accordingly."""

    async def test_clear_resource_disabled_reverts_buckets_to_inherited_value(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        # Clearing reverts to the (now unset) inherited value: not disabled.
        assert await disable_repo.clear_resource_disabled("gpt-4") == 1
        assert await disable_repo.get_resource_disabled("gpt-4") is None

    async def test_clear_entity_disabled_recomputes_effective_state(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 100)], resource="gpt-4", disabled=False
        )
        async with disable_limiter.acquire("vip-1", "gpt-4", {"rpm": 1}):
            pass

        # Clearing the entity override reverts to the resource-level disabled=True.
        assert await disable_repo.clear_entity_disabled("vip-1", resource="gpt-4") == 1
        assert await disable_repo.get_entity_disabled("vip-1", "gpt-4") is None
        assert await disable_repo.resolve_disabled("vip-1", "gpt-4") == (True, "resource")

    async def test_enable_entity_creates_stub_config_item(self, disable_repo):
        assert await disable_repo.get_entity_disabled("user-9", "_default_") is None
        assert await disable_repo.enable_entity("user-9") == 0
        assert await disable_repo.get_entity_disabled("user-9", "_default_") is False


@pytest.mark.asyncio
class TestStampBucketDisabledErrorHandling:
    """Coverage for _stamp_bucket_disabled's ConditionalCheckFailedException handler."""

    async def test_swallows_missing_bucket(self, disable_repo):
        # Bucket vanished between discovery and stamp (TTL or delete race);
        # no such bucket item exists, so this must not raise.
        pk = schema.pk_bucket(disable_repo._namespace_id, "ghost-entity", "gpt-4", 0)
        await disable_repo._stamp_bucket_disabled(pk, True)

    async def test_reraises_non_conditional_errors(self, disable_repo, monkeypatch):
        client = await disable_repo._get_client()

        async def failing_update_item(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                "UpdateItem",
            )

        monkeypatch.setattr(client, "update_item", failing_update_item)

        pk = schema.pk_bucket(disable_repo._namespace_id, "ghost-entity", "gpt-4", 0)
        with pytest.raises(ClientError):
            await disable_repo._stamp_bucket_disabled(pk, True)


@pytest.mark.asyncio
class TestSetResourceDisabledClearErrorHandling:
    """Coverage for _set_resource_disabled's clear-branch error handling."""

    async def test_clear_resource_disabled_reraises_non_conditional_errors(
        self, disable_repo, monkeypatch
    ):
        # A prior config item exists, so the REMOVE would normally succeed;
        # inject a non-conditional error to prove it propagates rather than
        # being swallowed like a ConditionalCheckFailedException.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])

        client = await disable_repo._get_client()
        original_update_item = client.update_item

        async def failing_update_item(**kwargs):
            if kwargs.get("UpdateExpression") == "REMOVE #disabled" and "/RESOURCE#" in kwargs.get(
                "Key", {}
            ).get("PK", {}).get("S", ""):
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                    "UpdateItem",
                )
            return await original_update_item(**kwargs)

        monkeypatch.setattr(client, "update_item", failing_update_item)

        with pytest.raises(ClientError):
            await disable_repo.clear_resource_disabled("gpt-4")


@pytest.mark.asyncio
class TestSetEntityDisabledClearErrorHandling:
    """Coverage for _set_entity_disabled's clear-branch error handling."""

    async def test_clear_entity_disabled_reraises_non_conditional_errors(
        self, disable_repo, monkeypatch
    ):
        await disable_repo.set_limits("user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4")

        client = await disable_repo._get_client()
        original_update_item = client.update_item

        async def failing_update_item(**kwargs):
            if kwargs.get("UpdateExpression") == "REMOVE #disabled" and "/ENTITY#" in kwargs.get(
                "Key", {}
            ).get("PK", {}).get("S", ""):
                raise ClientError(
                    {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "x"}},
                    "UpdateItem",
                )
            return await original_update_item(**kwargs)

        monkeypatch.setattr(client, "update_item", failing_update_item)

        with pytest.raises(ClientError):
            await disable_repo.clear_entity_disabled("user-1", resource="gpt-4")


@pytest.mark.asyncio
class TestDiscoverBucketPksPagination:
    """Coverage for the LastEvaluatedKey pagination loops used by the fan-out."""

    async def test_discover_resource_bucket_pks_follows_pagination(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        for entity in ("user-1", "user-2"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass

        client = await disable_repo._get_client()
        original_query = client.query
        call_count = 0

        async def paginated_query(**kwargs):
            nonlocal call_count
            # Strip ExclusiveStartKey since we simulate pagination ourselves;
            # passing the fake key to moto would return 0 items.
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "ExclusiveStartKey"}
            result = await original_query(**clean_kwargs)
            call_count += 1
            if call_count == 1:
                result["LastEvaluatedKey"] = {"PK": {"S": "fake-pk"}, "SK": {"S": "fake-sk"}}
                result["Items"] = result["Items"][:1]
            else:
                result["Items"] = result["Items"][1:]
                result.pop("LastEvaluatedKey", None)
            return result

        client.query = paginated_query

        pks = await disable_repo._discover_resource_bucket_pks("gpt-4")
        assert call_count == 2
        assert len(pks) == 2

    async def test_discover_entity_bucket_pks_follows_pagination(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        for res in ("gpt-4", "claude-3"):
            async with disable_limiter.acquire("user-1", res, {"rpm": 1}):
                pass

        client = await disable_repo._get_client()
        original_query = client.query
        call_count = 0

        async def paginated_query(**kwargs):
            nonlocal call_count
            clean_kwargs = {k: v for k, v in kwargs.items() if k != "ExclusiveStartKey"}
            result = await original_query(**clean_kwargs)
            call_count += 1
            if call_count == 1:
                result["LastEvaluatedKey"] = {"PK": {"S": "fake-pk"}, "SK": {"S": "fake-sk"}}
                result["Items"] = result["Items"][:1]
            else:
                result["Items"] = result["Items"][1:]
                result.pop("LastEvaluatedKey", None)
            return result

        client.query = paginated_query

        pks = await disable_repo._discover_entity_bucket_pks("user-1", None)
        assert call_count == 2
        assert len(pks) == 2

    async def test_discover_resource_bucket_pks_skips_items_missing_pk(
        self, disable_limiter, disable_repo
    ):
        # Defensive guard: a GSI2 query result item with no PK attribute must
        # be skipped rather than crash `schema.parse_bucket_pk` on an empty
        # string. This should not occur in practice (GSI2 always projects the
        # table's key attributes), but the guard is there and must be covered.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

        client = await disable_repo._get_client()
        original_query = client.query

        async def query_with_malformed_item(**kwargs):
            result = await original_query(**kwargs)
            result["Items"] = [{}, *result["Items"]]
            return result

        client.query = query_with_malformed_item

        pks = await disable_repo._discover_resource_bucket_pks("gpt-4")
        assert len(pks) == 1
