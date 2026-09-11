"""BatchGetItem must never silently return a partial read (issue #444).

DynamoDB may answer a ``BatchGetItem`` partially and report the remainder in
``UnprocessedKeys`` -- documented behaviour under throttling or when the
response would exceed 16 MB, not an error, and boto3 does not retry it. A
withheld item is indistinguishable from "this item does not exist", which is
wrong at every call site and *silently* wrong in the config precedence walk:
an entity's restrictive custom limit falls through to the resource or system
default.

These tests wrap the real moto-backed client and move an item from
``Responses`` into ``UnprocessedKeys``, leaving the stored data untouched --
only the response framing is simulated, the code under test is real. The
pattern is the one established for ``resolve_disabled`` in d0d0996d
(``tests/unit/test_disable.py::TestResolveDisabledPartialBatchResponse``).
"""

import time

import pytest

from zae_limiter import schema
from zae_limiter.exceptions import RateLimiterUnavailable
from zae_limiter.models import BucketState, Limit
from zae_limiter.repository import Repository


@pytest.fixture
async def repo(mock_dynamodb):
    """Moto-backed repository."""
    repo = Repository(name="test-unprocessed", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    yield repo
    await repo.close()


async def _create_buckets(repo: Repository, entity_id: str, resources: list[str]) -> None:
    """Create one composite bucket item per resource, plus the entity META."""
    await repo.create_entity(entity_id, parent_id=None, name=entity_id)
    limits = [Limit.per_minute("rpm", 100), Limit.per_minute("tpm", 10000)]
    now_ms = int(time.time() * 1000)
    for resource in resources:
        states = [BucketState.from_limit(entity_id, resource, limit, now_ms) for limit in limits]
        await repo.transact_write(
            [repo.build_composite_create(entity_id, resource, states, now_ms)]
        )


async def _withhold(repo: Repository, monkeypatch, withheld_pk: str, *, forever: bool = False):
    """Move items whose PK is ``withheld_pk`` into ``UnprocessedKeys``.

    By default only the first call is degraded, so a retry sees the full
    response. With ``forever=True`` every call is degraded, standing in for
    sustained throttling. Returns a dict whose ``calls`` key counts
    ``batch_get_item`` invocations.
    """
    client = await repo._get_client()
    original = client.batch_get_item
    table = repo.table_name
    state = {"calls": 0}

    async def flaky_batch_get_item(**kwargs):
        response = await original(**kwargs)
        state["calls"] += 1
        if not forever and state["calls"] > 1:
            return response
        items = response.get("Responses", {}).get(table, [])
        withheld = [i for i in items if i.get("PK", {}).get("S") == withheld_pk]
        if not withheld:
            return response
        response["Responses"][table] = [i for i in items if i.get("PK", {}).get("S") != withheld_pk]
        response["UnprocessedKeys"] = {
            table: {
                "Keys": [{"PK": i["PK"], "SK": i["SK"]} for i in withheld],
                "ConsistentRead": False,
            }
        }
        return response

    monkeypatch.setattr(client, "batch_get_item", flaky_batch_get_item)
    return state


@pytest.mark.asyncio
class TestGetBucketsPartialResponse:
    """`get_buckets` feeds admin listings and the ADR-125 disable fan-out.

    A bucket withheld here is a bucket the fan-out never stamps, so a
    disabled resource keeps admitting traffic on that shard.
    """

    async def test_withheld_bucket_is_retried_not_dropped(self, repo, monkeypatch):
        await _create_buckets(repo, "entity-1", ["gpt-4", "gpt-3.5"])
        withheld_pk = schema.pk_bucket(repo._namespace_id, "entity-1", "gpt-4", 0)
        state = await _withhold(repo, monkeypatch, withheld_pk)

        buckets = await repo.get_buckets("entity-1")

        assert {b.resource for b in buckets} == {"gpt-4", "gpt-3.5"}, (
            "the gpt-4 bucket landed in UnprocessedKeys and was reported as absent"
        )
        assert state["calls"] == 2, "the unprocessed remainder was never re-requested"

    async def test_persistently_withheld_bucket_raises(self, repo, monkeypatch):
        await _create_buckets(repo, "entity-1", ["gpt-4", "gpt-3.5"])
        withheld_pk = schema.pk_bucket(repo._namespace_id, "entity-1", "gpt-4", 0)
        await _withhold(repo, monkeypatch, withheld_pk, forever=True)

        with pytest.raises(RateLimiterUnavailable):
            await repo.get_buckets("entity-1")


@pytest.mark.asyncio
class TestBatchGetBucketsPartialResponse:
    """`batch_get_buckets` backs the `acquire()` slow path."""

    async def test_withheld_bucket_is_retried_not_dropped(self, repo, monkeypatch):
        await _create_buckets(repo, "entity-1", ["gpt-4"])
        withheld_pk = schema.pk_bucket(repo._namespace_id, "entity-1", "gpt-4", 0)
        await _withhold(repo, monkeypatch, withheld_pk)

        result = await repo.batch_get_buckets([("entity-1", "gpt-4", 0)])

        assert ("entity-1", "gpt-4", "rpm") in result, (
            "a live bucket reported as missing sends acquire() down the create path"
        )

    async def test_persistently_withheld_bucket_raises(self, repo, monkeypatch):
        await _create_buckets(repo, "entity-1", ["gpt-4"])
        withheld_pk = schema.pk_bucket(repo._namespace_id, "entity-1", "gpt-4", 0)
        await _withhold(repo, monkeypatch, withheld_pk, forever=True)

        with pytest.raises(RateLimiterUnavailable):
            await repo.batch_get_buckets([("entity-1", "gpt-4", 0)])


@pytest.mark.asyncio
class TestBatchGetEntityAndBucketsPartialResponse:
    """`batch_get_entity_and_buckets` also carries the entity META record."""

    async def test_withheld_meta_is_retried_not_dropped(self, repo, monkeypatch):
        await _create_buckets(repo, "entity-1", ["gpt-4"])
        entity_pk = schema.pk_entity(repo._namespace_id, "entity-1")
        await _withhold(repo, monkeypatch, entity_pk)

        entity, _buckets = await repo.batch_get_entity_and_buckets(
            "entity-1", [("entity-1", "gpt-4", 0)]
        )

        assert entity is not None, (
            "a withheld META record reads as a nonexistent entity, and is then "
            "cached as cascade=False/parent_id=None"
        )


@pytest.mark.asyncio
class TestBatchGetConfigsPartialResponse:
    """The precedence walk is where a partial read is silently wrong."""

    async def test_withheld_config_is_retried_not_dropped(self, repo, monkeypatch):
        await repo.set_limits("entity-1", [Limit.per_minute("rpm", 1)], resource="gpt-4")
        entity_pk = schema.pk_entity(repo._namespace_id, "entity-1")
        entity_key = (entity_pk, schema.sk_config("gpt-4"))
        system_key = (schema.pk_system(repo._namespace_id), schema.sk_config())
        await _withhold(repo, monkeypatch, entity_pk)

        result = await repo.batch_get_configs([entity_key, system_key])

        assert entity_key in result

    async def test_persistently_withheld_config_raises(self, repo, monkeypatch):
        await repo.set_limits("entity-1", [Limit.per_minute("rpm", 1)], resource="gpt-4")
        entity_pk = schema.pk_entity(repo._namespace_id, "entity-1")
        await _withhold(repo, monkeypatch, entity_pk, forever=True)

        with pytest.raises(RateLimiterUnavailable):
            await repo.batch_get_configs([(entity_pk, schema.sk_config("gpt-4"))])

    async def test_withheld_config_does_not_leak_a_stale_disabled_answer(self, repo, monkeypatch):
        """`disabled_out` claims every key in the chunk was genuinely read.

        That claim is what lets `resolve_disabled_from_fetched` answer the
        ADR-125 admission gate without a second read, so a withheld key
        recorded as None ("no explicit value") fails the gate open.
        """
        await repo.set_limits(
            "entity-1", [Limit.per_minute("rpm", 1)], resource="gpt-4", disabled=True
        )
        entity_pk = schema.pk_entity(repo._namespace_id, "entity-1")
        entity_key = (entity_pk, schema.sk_config("gpt-4"))
        await _withhold(repo, monkeypatch, entity_pk)

        disabled_out: dict[tuple[str, str], bool | None] = {}
        await repo.batch_get_configs([entity_key], disabled_out=disabled_out)

        assert disabled_out[entity_key] is True


@pytest.mark.asyncio
class TestResolveLimitsPartialResponse:
    """The user-visible consequence: the wrong limits are applied."""

    async def test_entity_limit_wins_when_its_config_is_withheld_once(self, repo, monkeypatch):
        await repo.set_system_defaults([Limit.per_minute("rpm", 10)])
        await repo.set_limits("entity-1", [Limit.per_minute("rpm", 1)], resource="gpt-4")
        await repo.invalidate_config_cache()
        entity_pk = schema.pk_entity(repo._namespace_id, "entity-1")
        await _withhold(repo, monkeypatch, entity_pk)

        limits, _on_unavailable, source = await repo.resolve_limits("entity-1", "gpt-4")

        assert source == "entity", (
            "the entity config landed in UnprocessedKeys, the walk fell through, "
            "and the more permissive system default was applied"
        )
        assert limits is not None
        assert [(limit.name, limit.capacity) for limit in limits] == [("rpm", 1)]
