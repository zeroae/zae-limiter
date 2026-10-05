"""Tests for the per-resource cascade policy (ADR-146, #676)."""

from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, schema
from zae_limiter.exceptions import VersionMismatchError
from zae_limiter.models import Entity, Limit, effective_cascade
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


class TestEffectiveCascade:
    """The policy wins; unset falls back to META; no parent never cascades."""

    @pytest.mark.parametrize(
        ("policy", "meta_cascade", "parent", "expected"),
        [
            (None, True, "org", True),
            (None, False, "org", False),
            (True, False, "org", True),
            (False, True, "org", False),
            (True, True, None, False),
        ],
    )
    def test_effective_cascade(self, policy, meta_cascade, parent, expected):
        entity = Entity(id="user-1", parent_id=parent, cascade=meta_cascade)
        assert effective_cascade(policy, entity) is expected

    def test_no_meta_never_cascades(self):
        assert effective_cascade(True, None) is False


class TestResolveCascade:
    """The ADR-125 walk, for cascade: entity(resource) -> entity(_default_) -> resource."""

    LEVELS = ("entity", "entity_default", "resource")

    async def _setup(self, repo, entity, entity_default, resource):
        ns = repo._namespace_id
        values = {
            "entity": (entity, schema.pk_entity(ns, "user-1"), schema.sk_config("gpt-4")),
            "entity_default": (
                entity_default,
                schema.pk_entity(ns, "user-1"),
                schema.sk_config(schema.DEFAULT_RESOURCE),
            ),
            "resource": (resource, schema.pk_resource(ns, "gpt-4"), schema.sk_config()),
        }
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.set_limits("user-1", [RPM], resource="gpt-4")
        await repo.set_limits("user-1", [RPM])
        for value, pk, sk in values.values():
            if value is not None:
                await _stamp_config_cascade(repo, pk, sk, value)

    @pytest.mark.parametrize(
        ("entity", "entity_default", "resource", "expected", "level"),
        [
            (None, None, None, None, None),
            (None, None, False, False, "resource"),
            (None, True, False, True, "entity_default"),
            (False, True, True, False, "entity"),
            (True, None, False, True, "entity"),
        ],
    )
    async def test_first_explicit_level_wins(
        self, cascade_repo, entity, entity_default, resource, expected, level
    ):
        await self._setup(cascade_repo, entity, entity_default, resource)

        access = await cascade_repo.resolve_access("user-1", "gpt-4")

        assert (access.cascade, access.cascade_level) == (expected, level)

    async def test_the_walk_is_independent_of_disabled(self, cascade_repo):
        await self._setup(cascade_repo, None, None, False)
        await cascade_repo.set_resource_defaults("gpt-4", [RPM], disabled=True)

        access = await cascade_repo.resolve_access("user-1", "gpt-4")

        assert (access.disabled, access.disabled_level) == (True, "resource")
        assert (access.cascade, access.cascade_level) == (False, "resource")

    async def test_resolve_disabled_still_answers_alone(self, cascade_repo):
        await cascade_repo.set_resource_defaults("gpt-4", [RPM], disabled=True)
        assert await cascade_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

    async def test_the_config_fetch_answers_the_same(self, cascade_repo):
        await self._setup(cascade_repo, None, True, False)
        await cascade_repo.invalidate_config_cache()
        fetched: dict = {}

        await cascade_repo.resolve_limits("user-1", "gpt-4", cascade_out=fetched)

        assert cascade_repo.resolve_cascade_from_fetched("user-1", "gpt-4", fetched) == (
            True,
            "entity_default",
        )

    async def test_a_cached_level_declines(self, cascade_repo):
        await self._setup(cascade_repo, None, None, False)
        await cascade_repo.resolve_limits("user-1", "gpt-4")  # warm the cache
        fetched: dict = {}

        await cascade_repo.resolve_limits("user-1", "gpt-4", cascade_out=fetched)

        assert cascade_repo.resolve_cascade_from_fetched("user-1", "gpt-4", fetched) is None

    async def test_the_default_resource_has_no_entity_default_level(self, cascade_repo):
        ns = cascade_repo._namespace_id
        await cascade_repo.set_limits("user-1", [RPM])
        await _stamp_config_cascade(
            cascade_repo,
            schema.pk_entity(ns, "user-1"),
            schema.sk_config(schema.DEFAULT_RESOURCE),
            False,
        )

        access = await cascade_repo.resolve_access("user-1", schema.DEFAULT_RESOURCE)

        assert (access.cascade, access.cascade_level) == (False, "entity")


async def _bucket(repo: Repository, entity_id: str, resource: str) -> dict | None:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response.get("Item")


async def _consumed(repo: Repository, entity_id: str, resource: str) -> int:
    item = await _bucket(repo, entity_id, resource)
    return 0 if item is None else int(item[schema.bucket_attr("rpm", "tc")]["N"]) // 1000


class TestSlowPathFollowsThePolicy:
    """The slow path cascades per the resolved policy and stamps it (ADR-146)."""

    async def _hierarchy(self, repo, *, user_cascade=True, team_cascade=True):
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("org")
        await repo.create_entity("team", parent_id="org", cascade=team_cascade)
        await repo.create_entity("user", parent_id="team", cascade=user_cascade)

    async def _set_resource_policy(self, repo, resource, value):
        await _stamp_config_cascade(
            repo, schema.pk_resource(repo._namespace_id, resource), schema.sk_config(), value
        )
        await repo.invalidate_config_cache()

    async def test_no_policy_keeps_the_entity_flag(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._hierarchy(repo)

        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 3}):
            pass

        assert await _consumed(repo, "team", "llm") == 3
        assert (await _bucket(repo, "user", "llm"))["cascade"] == {"BOOL": True}

    async def test_a_policy_off_stops_a_cascading_entity(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._hierarchy(repo)
        await self._set_resource_policy(repo, "llm", False)

        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 3}):
            pass
        async with cascade_limiter.acquire("user", "gpt-4", consume={"rpm": 3}):
            pass

        assert await _bucket(repo, "team", "llm") is None
        assert (await _bucket(repo, "user", "llm"))["cascade"] == {"BOOL": False}
        assert await _consumed(repo, "team", "gpt-4") == 3

    async def test_a_policy_on_cascades_an_entity_created_without_it(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._hierarchy(repo, user_cascade=False)
        await self._set_resource_policy(repo, "gpt-4", True)

        async with cascade_limiter.acquire("user", "gpt-4", consume={"rpm": 3}):
            pass

        assert await _consumed(repo, "team", "gpt-4") == 3
        assert (await _bucket(repo, "user", "gpt-4"))["cascade"] == {"BOOL": True}

    async def test_the_parent_bucket_carries_the_parents_own_policy(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._hierarchy(repo)
        ns = repo._namespace_id
        await repo.set_limits("team", [RPM], resource="gpt-4")
        await _stamp_config_cascade(
            repo, schema.pk_entity(ns, "team"), schema.sk_config("gpt-4"), False
        )
        await repo.invalidate_config_cache()

        async with cascade_limiter.acquire("user", "gpt-4", consume={"rpm": 3}):
            pass

        team = await _bucket(repo, "team", "gpt-4")
        assert team["cascade"] == {"BOOL": False}
        assert team["parent_id"] == {"S": "org"}

    async def test_a_slow_pass_repairs_a_stale_stamp(self, cascade_repo):
        repo = cascade_repo
        await self._hierarchy(repo)
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        async with limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass
        assert (await _bucket(repo, "user", "llm"))["cascade"] == {"BOOL": True}

        await self._set_resource_policy(repo, "llm", False)
        async with limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass

        assert (await _bucket(repo, "user", "llm"))["cascade"] == {"BOOL": False}
        assert await _consumed(repo, "team", "llm") == 1  # the first call only


class TestWarmPathFollowsTheItem:
    """The cache holds cascade per (entity, resource); the item overrules it (ADR-146)."""

    async def _warm_cascading_user(self, limiter):
        repo = limiter._repository
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("team")
        await repo.create_entity("user", parent_id="team", cascade=True)
        for resource in ("gpt-4", "llm"):
            async with limiter.acquire("user", resource, consume={"rpm": 1}):
                pass  # both buckets exist, both cascading, cache warm

    async def _stamp_bucket(self, repo, entity_id, resource, value):
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #c = :c",
            ExpressionAttributeNames={"#c": "cascade"},
            ExpressionAttributeValues={":c": {"BOOL": value}},
        )

    async def test_a_parent_debited_on_a_stale_guess_is_refunded(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._warm_cascading_user(cascade_limiter)
        await self._stamp_bucket(repo, "user", "llm", False)  # the policy turned off
        repo._cascade_cache.clear()  # this process has not seen it yet
        team_before = await _consumed(repo, "team", "llm")

        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 3}) as lease:
            assert {e.entity_id for e in lease.entries} == {"user"}

        assert await _consumed(repo, "team", "llm") == team_before
        assert repo._cascade_cache[(repo._namespace_id, "user", "llm")] is False

    async def test_a_failed_parent_write_needs_no_refund(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._warm_cascading_user(cascade_limiter)
        await self._stamp_bucket(repo, "user", "llm", False)
        repo._cascade_cache.clear()
        client = await repo._get_client()
        await client.delete_item(  # the parent's write will find no bucket
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "team", "llm", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )

        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 3}) as lease:
            assert {e.entity_id for e in lease.entries} == {"user"}

        assert await _bucket(repo, "team", "llm") is None

    async def test_a_failed_child_is_judged_by_its_own_stamp(self, cascade_limiter):
        import time

        from zae_limiter.exceptions import RateLimitExceeded

        repo = cascade_limiter._repository
        ns = repo._namespace_id
        await self._warm_cascading_user(cascade_limiter)
        client = await repo._get_client()
        await client.update_item(  # the child: policy off, and exhausted
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user", "llm", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #c = :false, #tk = :zero, #rf = :now",
            ExpressionAttributeNames={
                "#c": "cascade",
                "#tk": schema.bucket_attr("rpm", "tk"),
                "#rf": "rf",
            },
            ExpressionAttributeValues={
                ":false": {"BOOL": False},
                ":zero": {"N": "0"},
                ":now": {"N": str(int(time.time() * 1000))},
            },
        )
        await client.update_item(  # the parent it no longer cascades to: disabled
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "team", "llm", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #d = :true",
            ExpressionAttributeNames={"#d": schema.BUCKET_FIELD_DISABLED},
            ExpressionAttributeValues={":true": {"BOOL": True}},
        )
        repo._cascade_cache.clear()  # the warm guess for llm is the entity-wide True

        with pytest.raises(RateLimitExceeded):
            async with cascade_limiter.acquire("user", "llm", consume={"rpm": 1}):
                pass

    async def test_a_stamp_without_parent_teaches_nothing(self, cascade_repo):
        cascade_repo._learn_shard_count("parent", "llm", 1, meta=(False, None))
        assert (cascade_repo._namespace_id, "parent", "llm") not in cascade_repo._cascade_cache

    async def test_after_one_correction_the_warm_path_skips_the_parent(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._warm_cascading_user(cascade_limiter)
        await self._stamp_bucket(repo, "user", "llm", False)
        repo._cascade_cache.clear()
        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass  # learns the item's policy
        writes = []
        real = repo._speculative_consume_single

        async def spy(entity_id, *args, **kwargs):
            writes.append(entity_id)
            return await real(entity_id, *args, **kwargs)

        repo._speculative_consume_single = spy
        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass

        assert writes == ["user"]

    async def test_one_resource_does_not_decide_another(self, cascade_limiter):
        repo = cascade_limiter._repository
        await self._warm_cascading_user(cascade_limiter)
        await self._stamp_bucket(repo, "user", "llm", False)
        repo._cascade_cache.clear()
        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass
        team_before = await _consumed(repo, "team", "gpt-4")

        async with cascade_limiter.acquire("user", "gpt-4", consume={"rpm": 2}):
            pass

        assert await _consumed(repo, "team", "gpt-4") == team_before + 2

    async def test_a_scoped_repository_shares_what_was_learned(self, cascade_repo):
        await cascade_repo.register_namespace("tenant-b")
        scoped = await cascade_repo.namespace("tenant-b")
        cascade_repo._learn_shard_count("user", "llm", 1, meta=(False, "team"))
        assert scoped._cascade_cache is cascade_repo._cascade_cache


class TestShardsThatDisagree:
    """While a policy change fans out, one entity's shards can carry different stamps."""

    async def test_a_child_only_retry_on_a_cascading_shard_still_debits_the_parent(
        self, cascade_limiter
    ):
        import time

        from zae_limiter.models import BucketState

        repo = cascade_limiter._repository
        ns = repo._namespace_id
        await repo.set_resource_defaults("llm", [RPM])
        await repo.create_entity("team")
        await repo.create_entity("user", parent_id="team", cascade=True)
        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass  # shard 0 of user and team
        # Shard 1: tokens, stamped as cascading (the policy being turned on).
        now_ms = int(time.time() * 1000)
        state = BucketState.from_limit("user", "llm", RPM, now_ms, 2)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "user",
                    "llm",
                    [state],
                    now_ms,
                    cascade=True,
                    parent_id="team",
                    shard_id=1,
                    shard_count=2,
                )
            ]
        )
        # Shard 0: drained and still stamped as not cascading.
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user", "llm", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #tk = :zero, #sc = :two, #rf = :now, #c = :false",
            ExpressionAttributeNames={
                "#tk": schema.bucket_attr("rpm", "tk"),
                "#sc": "shard_count",
                "#rf": "rf",
                "#c": "cascade",
            },
            ExpressionAttributeValues={
                ":zero": {"N": "0"},
                ":two": {"N": "2"},
                ":now": {"N": str(now_ms)},
                ":false": {"BOOL": False},
            },
        )
        repo._entity_cache.clear()  # a cold process draws shard 0
        repo._cascade_cache.clear()
        team_before = await _consumed(repo, "team", "llm")

        async with cascade_limiter.acquire("user", "llm", consume={"rpm": 1}) as lease:
            assert "team" in {e.entity_id for e in lease.entries}

        assert await _consumed(repo, "team", "llm") == team_before + 1


class TestFanout:
    """A policy change restamps existing buckets, each from its own resolution (ADR-146)."""

    async def _two_users(self, limiter):
        repo = limiter._repository
        await repo.set_resource_defaults("llm", [RPM])
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.create_entity("team")
        for user in ("user-1", "user-2"):
            await repo.create_entity(user, parent_id="team", cascade=True)
            for resource in ("llm", "gpt-4"):
                async with limiter.acquire(user, resource, consume={"rpm": 1}):
                    pass

    @staticmethod
    async def _cascade_of(repo, entity_id, resource):
        return (await _bucket(repo, entity_id, resource))["cascade"]["BOOL"]

    async def test_a_resource_change_restamps_every_entity_but_an_override(self, cascade_limiter):
        repo = cascade_limiter._repository
        ns = repo._namespace_id
        await self._two_users(cascade_limiter)
        await _stamp_config_cascade(repo, schema.pk_resource(ns, "llm"), schema.sk_config(), False)
        await repo.set_limits("user-2", [RPM], resource="llm")
        await _stamp_config_cascade(
            repo, schema.pk_entity(ns, "user-2"), schema.sk_config("llm"), True
        )
        repo._learn_shard_count("user-1", "llm", 1, meta=(True, "team"))

        stamped = await repo._fanout_cascade(resource="llm")

        assert stamped >= 2
        assert await self._cascade_of(repo, "user-1", "llm") is False
        assert await self._cascade_of(repo, "user-2", "llm") is True  # its own override
        assert await self._cascade_of(repo, "user-1", "gpt-4") is True  # another resource
        assert repo._cascade_cache[(ns, "user-1", "llm")] is False  # taught, not forgotten
        assert repo._cascade_cache[(ns, "user-2", "llm")] is True
        assert (await _bucket(repo, "user-1", "llm"))["parent_id"] == {"S": "team"}

    async def test_an_entity_wide_change_resolves_each_resource(self, cascade_limiter):
        repo = cascade_limiter._repository
        ns = repo._namespace_id
        await self._two_users(cascade_limiter)
        await repo.set_limits("user-1", [RPM])  # entity-wide config
        await _stamp_config_cascade(
            repo, schema.pk_entity(ns, "user-1"), schema.sk_config(schema.DEFAULT_RESOURCE), False
        )
        await repo.set_limits("user-1", [RPM], resource="gpt-4")
        await _stamp_config_cascade(
            repo, schema.pk_entity(ns, "user-1"), schema.sk_config("gpt-4"), True
        )

        await repo._fanout_cascade(entity_id="user-1", resource=schema.DEFAULT_RESOURCE)

        assert await self._cascade_of(repo, "user-1", "llm") is False
        assert await self._cascade_of(repo, "user-1", "gpt-4") is True
        assert await self._cascade_of(repo, "user-2", "llm") is True  # untouched

    async def test_an_entity_without_a_parent_is_stamped_without_one(self, cascade_limiter):
        repo = cascade_limiter._repository
        await repo.set_resource_defaults("llm", [RPM])
        await repo.create_entity("solo")
        async with cascade_limiter.acquire("solo", "llm", consume={"rpm": 1}):
            pass

        await repo._fanout_cascade(entity_id="solo", resource="llm")

        item = await _bucket(repo, "solo", "llm")
        assert item["cascade"] == {"BOOL": False}
        assert "parent_id" not in item

    async def test_a_bucket_whose_entity_has_no_meta_is_skipped(self, cascade_limiter):
        repo = cascade_limiter._repository
        await repo.set_resource_defaults("llm", [RPM])
        async with cascade_limiter.acquire("ghost", "llm", consume={"rpm": 1}):
            pass  # never create_entity()'d

        assert await repo._fanout_cascade(resource="llm") == 0

    async def test_deleting_a_deciding_resource_config_restamps(self, cascade_limiter):
        repo = cascade_limiter._repository
        ns = repo._namespace_id
        await self._two_users(cascade_limiter)
        await _stamp_config_cascade(repo, schema.pk_resource(ns, "llm"), schema.sk_config(), False)
        await repo._fanout_cascade(resource="llm")
        assert await self._cascade_of(repo, "user-1", "llm") is False

        await repo.delete_resource_defaults("llm")

        assert await self._cascade_of(repo, "user-1", "llm") is True  # back to META

    async def test_deleting_a_deciding_entity_config_restamps(self, cascade_limiter):
        repo = cascade_limiter._repository
        ns = repo._namespace_id
        await self._two_users(cascade_limiter)
        await repo.set_limits("user-1", [RPM], resource="llm")
        await _stamp_config_cascade(
            repo, schema.pk_entity(ns, "user-1"), schema.sk_config("llm"), False
        )
        await repo._fanout_cascade(entity_id="user-1", resource="llm")
        assert await self._cascade_of(repo, "user-1", "llm") is False

        await repo.delete_limits("user-1", resource="llm")

        assert await self._cascade_of(repo, "user-1", "llm") is True

    async def test_a_delete_with_no_policy_does_not_fan_out(self, cascade_limiter, monkeypatch):
        repo = cascade_limiter._repository
        await self._two_users(cascade_limiter)
        await repo.set_limits("user-1", [RPM], resource="llm")
        calls = []

        async def spy(**kwargs):
            calls.append(kwargs)
            return 0

        monkeypatch.setattr(repo, "_fanout_cascade", spy)
        await repo.delete_limits("user-1", resource="llm")
        await repo.delete_resource_defaults("llm")

        assert calls == []

    async def test_a_vanished_bucket_is_not_an_error(self, cascade_limiter):
        repo = cascade_limiter._repository
        pk = schema.pk_bucket(repo._namespace_id, "nobody", "llm", 0)
        await repo._stamp_bucket_cascade(pk, True, "team")  # no exception
        assert await _bucket(repo, "nobody", "llm") is None

    async def test_a_failed_write_reports_partial_progress(self, cascade_limiter, monkeypatch):
        from botocore.exceptions import ClientError

        from zae_limiter.exceptions import FanoutIncomplete

        repo = cascade_limiter._repository
        await self._two_users(cascade_limiter)
        client = await repo._get_client()

        async def throttled(**kwargs):
            raise ClientError(
                {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
            )

        monkeypatch.setattr(client, "update_item", throttled)
        with pytest.raises(FanoutIncomplete) as info:
            await repo._fanout_cascade(resource="llm")
        assert info.value.stamped == 0


class TestCascadePolicyVersionGate:
    """Storing a cascade policy is gated on the readers' version (ADR-146, ADR-141)."""

    @staticmethod
    async def _stamp(repo, lambda_version, client_min_version="0.0.0"):
        from zae_limiter.version import get_schema_version

        await repo.set_version_record(
            schema_version=get_schema_version(),
            lambda_version=lambda_version,
            client_min_version=client_min_version,
        )

    @staticmethod
    async def _client_min(repo):
        record = await repo.get_version_record()
        return record["client_min_version"]

    async def test_refused_while_the_lambdas_predate_it(self, cascade_repo):
        await self._stamp(cascade_repo, "0.15.1")
        with patch("zae_limiter.__version__", "0.16.0"):
            with pytest.raises(VersionMismatchError) as exc_info:
                await cascade_repo._require_cascade_policy_readers()
        assert "cascade policy" in str(exc_info.value)
        assert "zae-limiter upgrade" in str(exc_info.value)
        assert exc_info.value.can_auto_update is True
        assert await self._client_min(cascade_repo) == "0.0.0"

    async def test_admitted_and_ratcheted_once_the_lambdas_read_it(self, cascade_repo):
        await self._stamp(cascade_repo, "0.16.0")
        with patch("zae_limiter.__version__", "0.16.1"):
            await cascade_repo._require_cascade_policy_readers()
        assert await self._client_min(cascade_repo) == "0.16.0"

    async def test_a_dev_build_proves_itself_and_caps_the_ratchet(self, cascade_repo):
        dev = "0.15.2.dev7+gabc1234"
        await self._stamp(cascade_repo, dev)
        with patch("zae_limiter.__version__", dev):
            await cascade_repo._require_cascade_policy_readers()
        assert await self._client_min(cascade_repo) == dev

    async def test_a_missing_record_fails_closed(self, cascade_repo):
        with patch("zae_limiter.__version__", "0.16.0"):
            with pytest.raises(VersionMismatchError) as exc_info:
                await cascade_repo._require_cascade_policy_readers()
        assert "no version record" in str(exc_info.value)
        assert exc_info.value.can_auto_update is False

    async def test_an_unknown_lambda_version_proves_nothing(self, cascade_repo):
        await self._stamp(cascade_repo, None)
        with patch("zae_limiter.__version__", "0.16.0"):
            with pytest.raises(VersionMismatchError) as exc_info:
                await cascade_repo._require_cascade_policy_readers()
        assert "Run 'zae-limiter upgrade' to deploy it" in str(exc_info.value)
        assert exc_info.value.can_auto_update is False

    def test_the_reset_after_minimum_is_still_the_default(self):
        from zae_limiter.version import ratcheted_client_min_version, reads_reset_after

        assert reads_reset_after("0.15.0", "0.16.0") is True
        assert reads_reset_after("0.15.0", "0.16.0", "0.16.0") is False
        assert ratcheted_client_min_version("0.0.0", "0.16.0") == "0.15.0"
        assert ratcheted_client_min_version("0.0.0", "0.16.0", "0.16.0") == "0.16.0"


@pytest.fixture
async def gated_limiter(cascade_repo):
    """A limiter on a stack whose version record admits a cascade policy."""
    from zae_limiter import __version__
    from zae_limiter.version import get_schema_version

    await cascade_repo.set_version_record(
        schema_version=get_schema_version(), lambda_version=__version__
    )
    limiter = RateLimiter(repository=cascade_repo)
    async with limiter:
        yield limiter


class TestCascadePolicyApi:
    """set_/clear_ methods and the setter keyword write, gate and fan out (ADR-146)."""

    async def _setup(self, limiter):
        repo = limiter._repository
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("team")
        await repo.create_entity("user", parent_id="team", cascade=True)
        await repo.create_entity("quiet", parent_id="team")  # does not cascade
        for entity in ("user", "quiet"):
            for resource in ("gpt-4", "llm"):
                async with limiter.acquire(entity, resource, consume={"rpm": 1}):
                    pass

    @staticmethod
    async def _stamp(repo, entity_id, resource):
        return (await _bucket(repo, entity_id, resource))["cascade"]["BOOL"]

    async def test_a_resource_policy_round_trip(self, gated_limiter):
        repo = gated_limiter._repository
        await self._setup(gated_limiter)

        assert await repo.set_resource_cascade("llm", False) >= 2
        assert await repo.get_resource_cascade("llm") is False
        assert await self._stamp(repo, "user", "llm") is False
        team_before = await _consumed(repo, "team", "llm")
        async with gated_limiter.acquire("user", "llm", consume={"rpm": 2}):
            pass
        assert await _consumed(repo, "team", "llm") == team_before

        await repo.clear_resource_cascade("llm")
        assert await repo.get_resource_cascade("llm") is None
        assert await self._stamp(repo, "user", "llm") is True  # back to META
        assert await self._stamp(repo, "quiet", "llm") is False

    async def test_a_resource_policy_with_no_prior_config_registers_it(self, gated_limiter):
        repo = gated_limiter._repository
        await repo.set_resource_cascade("fresh-model", True)
        assert await repo.get_resource_cascade("fresh-model") is True
        assert "fresh-model" in await repo.list_resources_with_defaults()

    async def test_clearing_a_resource_with_no_config_is_a_no_op(self, gated_limiter):
        repo = gated_limiter._repository
        assert await repo.clear_resource_cascade("never-configured") == 0

    async def test_an_entity_policy_for_one_resource(self, gated_limiter):
        repo = gated_limiter._repository
        await self._setup(gated_limiter)

        await repo.set_entity_cascade("quiet", True, resource="gpt-4")

        assert await repo.get_entity_cascade("quiet", "gpt-4") is True
        assert await self._stamp(repo, "quiet", "gpt-4") is True
        assert await self._stamp(repo, "quiet", "llm") is False
        team_before = await _consumed(repo, "team", "gpt-4")
        async with gated_limiter.acquire("quiet", "gpt-4", consume={"rpm": 2}):
            pass
        assert await _consumed(repo, "team", "gpt-4") == team_before + 2

    async def test_an_entity_wide_policy_and_its_clear(self, gated_limiter):
        repo = gated_limiter._repository
        await self._setup(gated_limiter)

        await repo.set_entity_cascade("user", False)
        assert await self._stamp(repo, "user", "gpt-4") is False
        assert await self._stamp(repo, "user", "llm") is False

        await repo.clear_entity_cascade("user")
        assert await repo.get_entity_cascade("user", schema.DEFAULT_RESOURCE) is None
        assert await self._stamp(repo, "user", "gpt-4") is True

    async def test_the_setter_keyword_sets_and_clears(self, gated_limiter):
        repo = gated_limiter._repository
        await self._setup(gated_limiter)

        await repo.set_resource_defaults("llm", [RPM], cascade=False)
        assert await self._stamp(repo, "user", "llm") is False
        await repo.set_limits("quiet", [RPM], resource="gpt-4", cascade=True)
        assert await self._stamp(repo, "quiet", "gpt-4") is True

        await repo.set_resource_defaults("llm", [RPM], cascade=None)
        assert await repo.get_resource_cascade("llm") is None
        assert await self._stamp(repo, "user", "llm") is True

    async def test_a_refused_policy_writes_nothing(self, cascade_repo):
        from zae_limiter.version import get_schema_version

        await cascade_repo.set_version_record(
            schema_version=get_schema_version(), lambda_version="0.15.1"
        )
        await cascade_repo.set_resource_defaults("llm", [RPM])
        with patch("zae_limiter.__version__", "0.16.0"):
            with pytest.raises(VersionMismatchError):
                await cascade_repo.set_resource_cascade("llm", False)
            with pytest.raises(VersionMismatchError):
                await cascade_repo.set_entity_cascade("user", False)
            with pytest.raises(VersionMismatchError):
                await cascade_repo.set_resource_defaults("llm", [RPM], cascade=False)
            with pytest.raises(VersionMismatchError):
                await cascade_repo.set_limits("user", [RPM], cascade=False)
        assert await cascade_repo.get_resource_cascade("llm") is None
        assert await cascade_repo.get_entity_cascade("user", schema.DEFAULT_RESOURCE) is None
        assert await cascade_repo.get_limits("user") == []
