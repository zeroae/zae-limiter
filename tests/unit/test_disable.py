"""Tests for resource/entity disable (ADR-125)."""

import pytest
from botocore.exceptions import ClientError

from zae_limiter import RateLimiter, schema
from zae_limiter.exceptions import RateLimitError, ResourceDisabled
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

    async def test_unscoped_disable_entity_restamps_each_resource_to_its_resolved_value(
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

        # Unscoped disable_entity re-resolves and (re)writes EVERY discovered
        # bucket to its own resolved value — the count is bucket items
        # written, not buckets matching the `disabled` argument. gpt-4's
        # bucket is written but ends up unstamped (its carve-out resolves to
        # False, i.e. REMOVE); claude-3's bucket is written and stamped True.
        # Both buckets count, so the total is 2.
        assert await disable_repo.disable_entity("user-1") == 2

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

        # resolve_disabled must agree with each bucket's stamp (or lack of one).
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
        # The bucket must be created while the resource is still enabled
        # (Task 7 enforcement blocks acquire() on an already-disabled
        # resource); disable_resource() below both flips the config to
        # disabled=True and stamps the now-existing bucket.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
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

    async def test_unscoped_clear_restamps_resource_whose_own_config_now_decides(
        self, disable_limiter, disable_repo
    ):
        """Regression (Critical 1): the unscoped clear must actively re-stamp a
        bucket whose own resource-level config now decides, not merely skip it
        because its resolution disagrees with the (stale) entity default.

        Before the clear, the entity's `_default_: false` carve-out outranks
        gpt-4's own `disabled: true`, so the bucket is created enabled. After
        `_default_` is cleared, gpt-4's own config becomes the deciding level
        and the bucket must flip to disabled. The pre-fix `_fanout_entity`
        compared each bucket's freshly-resolved value against the single
        `effective` value computed for the (now-nonexistent) `_default_`
        level itself, saw a disagreement, and left the bucket's stale
        `disabled=false`/absent stamp untouched -- a silent kill-switch
        bypass with the config correctly saying `disabled: true`.
        """
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        await disable_repo.set_limits("user-1", [Limit.per_minute("rpm", 100)], disabled=False)
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

        # The entity-wide carve-out currently wins over the resource's own True.
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, "entity_default")

        assert await disable_repo.clear_entity_disabled("user-1") == 1

        # gpt-4's own config is now the deciding level, and says disabled.
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

        pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "gpt-4", 0)
        client = await disable_repo._get_client()
        item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
        )
        assert item["Item"][schema.BUCKET_FIELD_DISABLED] == {"BOOL": True}

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


@pytest.mark.asyncio
class TestSetterExplicitDisabledFansOut:
    """set_resource_defaults()/set_limits() must fan out immediately when
    `disabled` is passed explicitly -- not only when disable_resource()/
    disable_entity() are called separately (I1, ADR-125)."""

    async def test_set_resource_defaults_explicit_disabled_fans_out(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, None)

        # Passing `disabled=True` alongside new limits must fan out on its
        # own -- the pre-fix setter only preserved/wrote the value and never
        # called _fanout_resource(), so existing buckets kept accepting
        # traffic despite the config now saying disabled.
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 200)], disabled=True
        )

        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")
        pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "gpt-4", 0)
        client = await disable_repo._get_client()
        item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
        )
        assert item["Item"][schema.BUCKET_FIELD_DISABLED] == {"BOOL": True}

    async def test_set_limits_explicit_disabled_fans_out(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, None)

        # Same for the entity-level setter: an explicit `disabled=True` must
        # fan out immediately, not wait for a separate disable_entity() call.
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 50)], resource="gpt-4", disabled=True
        )

        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "entity")
        pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "gpt-4", 0)
        client = await disable_repo._get_client()
        item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
        )
        assert item["Item"][schema.BUCKET_FIELD_DISABLED] == {"BOOL": True}


@pytest.mark.asyncio
class TestDeleteFansOut:
    """delete_limits()/delete_resource_defaults() delete the config item that
    holds `disabled`, so they must re-run the fan-out against the newly
    resolved value (C2, ADR-125)."""

    async def test_delete_resource_defaults_unstamps_buckets(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

        # Deleting the resource config removes the only level that was
        # setting `disabled` -- the pre-fix delete_resource_defaults() never
        # called _fanout_resource() at all, so the bucket kept its stale
        # `disabled: true` stamp (and ResourceDisabled kept firing) even
        # though the config item, and get_resource_disabled(), now say
        # "not disabled".
        await disable_repo.delete_resource_defaults("gpt-4")

        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, None)
        pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "gpt-4", 0)
        client = await disable_repo._get_client()
        item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
        )
        assert schema.BUCKET_FIELD_DISABLED not in item.get("Item", {})

    async def test_delete_limits_unstamps_buckets(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        # disable_entity() writes the same entity+resource config item
        # delete_limits() below will delete, and stamps the bucket -- using
        # it here (rather than set_limits(disabled=True), I1's own fix)
        # isolates this test to delete_limits()'s fan-out specifically.
        await disable_repo.disable_entity("user-1", resource="gpt-4")
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "entity")

        # Deleting the entity's resource-specific config removes the level
        # that was overriding the resource's own (not-disabled) value -- the
        # pre-fix delete_limits() never fanned out, leaving the bucket
        # stamped `disabled: true` with nothing in DynamoDB saying so
        # anymore.
        await disable_repo.delete_limits("user-1", resource="gpt-4")

        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, None)
        pk = schema.pk_bucket(disable_repo._namespace_id, "user-1", "gpt-4", 0)
        client = await disable_repo._get_client()
        item = await client.get_item(
            TableName=disable_repo.table_name,
            Key={"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
        )
        assert schema.BUCKET_FIELD_DISABLED not in item.get("Item", {})


@pytest.mark.asyncio
class TestEnforcement:
    async def test_fast_path_raises_resource_disabled(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        # First acquire creates the bucket while still enabled.
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        with pytest.raises(ResourceDisabled) as exc_info:
            async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass
        assert exc_info.value.resource == "gpt-4"
        assert exc_info.value.entity_id == "user-1"

    async def test_slow_path_raises_before_creating_a_bucket(self, disable_limiter, disable_repo):
        # No bucket exists, so the fast-path guard cannot fire.
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        with pytest.raises(ResourceDisabled):
            async with disable_limiter.acquire("brand-new", "gpt-4", {"rpm": 1}):
                pass
        assert await disable_repo.get_buckets("brand-new") == []

    async def test_disabled_is_not_a_rate_limit_error(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        # Callers catching RateLimitError must NOT swallow a disabled resource.
        with pytest.raises(ResourceDisabled):
            try:
                async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                    pass
            except RateLimitError:
                pytest.fail("ResourceDisabled must not be caught as RateLimitError")

    async def test_entity_override_still_admitted_after_resource_disable(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 100)], resource="gpt-4", disabled=False
        )
        for entity in ("user-1", "vip-1"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass
        await disable_repo.disable_resource("gpt-4")

        # The carve-out keeps working...
        async with disable_limiter.acquire("vip-1", "gpt-4", {"rpm": 1}):
            pass
        # ...while everyone else is blocked.
        with pytest.raises(ResourceDisabled):
            async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

    async def test_enable_restores_access(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")
        with pytest.raises(ResourceDisabled):
            async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

        await disable_repo.enable_resource("gpt-4")
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

    async def test_on_unavailable_allow_does_not_swallow_disabled(self, disable_repo):
        await disable_repo.set_system_defaults(
            [Limit.per_minute("rpm", 100)], on_unavailable="allow"
        )
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        limiter = RateLimiter(repository=disable_repo)
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        # `allow` covers infrastructure unavailability, not policy decisions.
        with pytest.raises(ResourceDisabled):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

    async def test_cascade_parent_disabled_compensates_child_and_raises(
        self, disable_limiter, disable_repo
    ):
        # Covers the DISABLED branch of _handle_nested_parent_failure: the
        # child's speculatively consumed tokens must be returned before the
        # exception propagates.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.create_entity("parent-1")
        await disable_repo.create_entity("child-1", parent_id="parent-1", cascade=True)

        # First acquire populates the entity cache (cascade=True, parent_id),
        # so the second acquire below takes the parallel child+parent
        # speculative path and can hit _handle_nested_parent_failure.
        async with disable_limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
            pass

        # Disable only the parent's bucket; the child stays enabled.
        assert await disable_repo.disable_entity("parent-1", resource="gpt-4") == 1

        tk_before = {
            b.limit_name: b.tokens_milli for b in await disable_repo.get_buckets("child-1")
        }

        with pytest.raises(ResourceDisabled) as exc_info:
            async with disable_limiter.acquire("child-1", "gpt-4", {"rpm": 1}):
                pass
        assert exc_info.value.entity_id == "parent-1"
        assert exc_info.value.resource == "gpt-4"
        assert exc_info.value.level == "bucket"

        # The child's tokens from this failed attempt must have been
        # compensated (returned) rather than left consumed.
        tk_after = {b.limit_name: b.tokens_milli for b in await disable_repo.get_buckets("child-1")}
        assert tk_after == tk_before

    async def test_slow_path_parent_disabled_raises_before_creating_buckets(
        self, disable_limiter, disable_repo
    ):
        # Covers the parent slow-path gate in _do_acquire: a brand-new cascade
        # entity whose parent is disabled must be rejected before either the
        # child or the parent bucket is created.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.create_entity("parent-2")
        await disable_repo.create_entity("child-2", parent_id="parent-2", cascade=True)
        await disable_repo.disable_entity("parent-2")

        with pytest.raises(ResourceDisabled) as exc_info:
            async with disable_limiter.acquire("child-2", "gpt-4", {"rpm": 1}):
                pass
        assert exc_info.value.entity_id == "parent-2"
        assert exc_info.value.level == "entity_default"

        assert await disable_repo.get_buckets("child-2") == []
        assert await disable_repo.get_buckets("parent-2") == []

    async def test_cache_miss_cascade_parent_disabled_compensates_child_and_raises(
        self, disable_limiter, disable_repo
    ):
        # Regression test: the "cache miss cascade" sequential path in
        # _try_speculative_acquire (the `elif result.cascade and
        # result.parent_id:` branch) must guard against a disabled parent
        # the same way the cache-hit parallel path
        # (_handle_nested_parent_failure) already does. Without the guard,
        # `would_refill_satisfy` sees the parent's tokens still intact (a
        # disabled bucket isn't drained) and falls through to
        # `_try_parent_only_acquire`, which has no disabled check of its own
        # and would silently admit the request.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.create_entity("parent-3")
        await disable_repo.create_entity("child-3", parent_id="parent-3", cascade=True)

        # Seed both buckets via a *separate* Repository instance sharing the
        # same moto-backed table, so disable_repo's own entity cache stays
        # cold for "child-3". That cold cache is what forces the sequential
        # cache-miss path below instead of the parallel cache-hit path.
        seed_repo = Repository(
            name="test-disable", region="us-east-1", _skip_deprecation_warning=True
        )
        seed_limiter = RateLimiter(repository=seed_repo)
        async with seed_limiter:
            async with seed_limiter.acquire("child-3", "gpt-4", {"rpm": 1}):
                pass
        await seed_repo.close()

        # Confirm disable_repo's cache never saw "child-3" — the precondition
        # for taking the cache-miss branch.
        assert (disable_repo._namespace_id, "child-3") not in disable_repo._entity_cache

        # Disable only the parent's bucket; the child stays enabled.
        assert await disable_repo.disable_entity("parent-3", resource="gpt-4") == 1

        tk_before = {
            b.limit_name: b.tokens_milli for b in await disable_repo.get_buckets("child-3")
        }

        with pytest.raises(ResourceDisabled) as exc_info:
            async with disable_limiter.acquire("child-3", "gpt-4", {"rpm": 1}):
                pass
        assert exc_info.value.entity_id == "parent-3"
        assert exc_info.value.resource == "gpt-4"
        assert exc_info.value.level == "bucket"

        # The child's tokens from this failed attempt must have been
        # compensated (returned) rather than left consumed.
        tk_after = {b.limit_name: b.tokens_milli for b in await disable_repo.get_buckets("child-3")}
        assert tk_after == tk_before
