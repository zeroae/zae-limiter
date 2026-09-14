"""An entity-wide `_default_` limit change must reach existing buckets (#487).

`set_limits(entity_id, limits)` defaults to the `_default_` resource — the
entity-WIDE config scope, not a resource anyone acquires against. The bucket
sync forwarded that sentinel to its GSI3 discovery query, which built the
prefix `BUCKET#_default_#`. No bucket item can carry that GSI3SK (a bucket's is
`BUCKET#{real_resource}#{shard}`), so the query matched zero items and the sync
wrote nothing and raised nothing. Entity configs carry no TTL, so the affected
buckets enforced the params they were born with forever.

These tests go through the real `acquire()` that creates the bucket, so they
exercise the sentinel exactly as an application produces it.

Third instance of the same class as #468 (shards N>0) and #481 (the provisioner
never syncing at all); named residual of GHSA-w6c2-33wf-qfwf.
"""

import pytest

from zae_limiter.models import Limit
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    bucket_attr,
    pk_bucket,
    sk_state,
)

RESOURCE = "gpt-4"
OTHER_RESOURCE = "claude-3"


async def _raw(repo, entity_id: str, resource: str, shard_id: int = 0) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
            "SK": {"S": sk_state()},
        },
    )
    return response.get("Item") or {}


async def _capacity_milli(repo, entity_id: str, resource: str, limit_name: str = "rpm") -> int:
    item = await _raw(repo, entity_id, resource)
    attr = bucket_attr(limit_name, BUCKET_FIELD_CP)
    assert attr in item, f"{limit_name} missing from the {resource} bucket: {sorted(item)}"
    return int(item[attr]["N"])


@pytest.mark.integration
@pytest.mark.asyncio
class TestEntityWideLimitChangeReachesBuckets:
    """`set_limits` with no resource must propagate to the entity's buckets."""

    async def test_bucket_created_by_acquire_gets_the_new_params(
        self, localstack_limiter, unique_name
    ):
        """The reproduction from the issue, verbatim."""
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"default-scope-{unique_name}"

        await limiter.create_entity(entity_id)
        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 100)])
        async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
            pass
        assert await _capacity_milli(repo, entity_id, RESOURCE) == 100_000

        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 500)])

        assert await _capacity_milli(repo, entity_id, RESOURCE) == 500_000

    async def test_a_resource_specific_config_is_not_clobbered(
        self, localstack_limiter, unique_name
    ):
        """Precedence survives the widened fan-out.

        Entity(resource) outranks Entity(`_default_`), so widening discovery
        without re-resolving per bucket would overwrite the more specific
        config with the less specific one — a worse bug than the silent no-op
        it replaces.
        """
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"default-scope-carveout-{unique_name}"

        await limiter.create_entity(entity_id)
        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 100)])
        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 900)], resource=RESOURCE)
        for resource in (RESOURCE, OTHER_RESOURCE):
            async with limiter.acquire(entity_id, resource, {"rpm": 1}):
                pass

        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 500)])

        assert await _capacity_milli(repo, entity_id, RESOURCE) == 900_000, (
            "gpt-4 has its own entity config, which outranks `_default_`"
        )
        assert await _capacity_milli(repo, entity_id, OTHER_RESOURCE) == 500_000

    async def test_delete_reconciles_each_bucket_at_its_own_level(
        self, localstack_limiter, unique_name
    ):
        """`delete_limits('_default_')` follows the same unscoped path.

        The limiter hands `reconcile_bucket_to_defaults` the limits that
        `_default_` itself falls back to. Those are right for a bucket with
        nothing more specific and wrong for every other bucket, so each is
        re-resolved for its own resource. TTL follows whichever level answered:
        entity means persist, resource or system means expire (#271, #296).
        """
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"default-scope-delete-{unique_name}"

        await limiter.set_system_defaults([Limit.per_minute("rpm", 50)])
        await limiter.create_entity(entity_id)
        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 100)])
        await limiter.set_limits(entity_id, [Limit.per_minute("rpm", 900)], resource=RESOURCE)
        for resource in (RESOURCE, OTHER_RESOURCE):
            async with limiter.acquire(entity_id, resource, {"rpm": 1}):
                pass

        await limiter.delete_limits(entity_id)

        assert await _capacity_milli(repo, entity_id, RESOURCE) == 900_000, (
            "gpt-4's own entity config still applies"
        )
        assert await _capacity_milli(repo, entity_id, OTHER_RESOURCE) == 50_000, (
            "claude-3 fell back to the system defaults"
        )
        assert "ttl" not in await _raw(repo, entity_id, RESOURCE), (
            "still on entity limits, so the bucket must persist"
        )
        assert "ttl" in await _raw(repo, entity_id, OTHER_RESOURCE), (
            "now on defaults, so the bucket must expire and be recreated"
        )
