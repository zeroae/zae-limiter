"""Tests for the per-resource cascade policy (ADR-146, #676)."""

import pytest

from zae_limiter import RateLimiter, schema
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
        cascade_repo.invalidate_config_cache()
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
