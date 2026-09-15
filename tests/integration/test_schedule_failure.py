"""An undecodable stored schedule against a real table (#222 §6).

Three claims that moto cannot make. The first two are about `vu`: the
speculative fast path is a single conditional `UpdateItem` that reads no config
and decodes nothing, so a corrupt *config* schedule is invisible while `vu` is
in the future, and surfaces the moment the bucket must re-materialise. That is
§2.1's load-bearing property seen from the failure side, and it only means
anything against a real conditional write.

The rest are about blast radius: a corrupt attribute stops the item it is on,
on every client path that reads it, and stops nothing else in the namespace
(§6.2).

The aggregator's half of the reconciliation — skip the bucket, keep extracting
usage — is covered by the aggregator's own tests against a stream, and is not
re-tested here: it is merged, unchanged by this work, and stands or falls
independently of the client boundary.
"""

import pytest

from zae_limiter import Limit, OnUnavailable, RateLimiter, RateLimiterUnavailable
from zae_limiter.schedule import ScheduleEntry
from zae_limiter.schema import (
    BUCKET_FIELD_SCHED,
    BUCKET_FIELD_VU,
    bucket_attr,
    limit_attr,
    pk_bucket,
    pk_entity,
    sk_config,
    sk_state,
)

RESOURCE = "gpt-4"
# Always active, so `vu` is always a real future boundary and never "now".
ALWAYS = (ScheduleEntry(cron="* * * * *", tz="America/New_York", scale=0.5),)


async def _write_attr(repo, key, attr, value):
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=key,
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": attr},
        ExpressionAttributeValues={":v": value},
    )


async def _bucket_key(repo, entity_id, shard_id=0):
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard_id)},
        "SK": {"S": sk_state()},
    }


async def _raw_bucket(repo, entity_id, shard_id=0):
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name, Key=await _bucket_key(repo, entity_id, shard_id)
    )
    return response.get("Item") or {}


async def _corrupt_config(repo, entity_id, limit_name="rpm"):
    await _write_attr(
        repo,
        {
            "PK": {"S": pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": sk_config(RESOURCE)},
        },
        limit_attr(limit_name, "sched"),
        {"S": "not-a-schedule"},
    )
    await repo.invalidate_config_cache()


@pytest.mark.integration
@pytest.mark.asyncio
class TestUnreadableScheduleIntegration:
    async def test_the_fast_path_is_unaffected_until_vu_expires(
        self, localstack_limiter, unique_name
    ):
        """`vu` in the future keeps the request on the speculative path, which
        reads no config and decodes nothing — so a corrupt *config* schedule is
        invisible until the bucket next materialises."""
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"sched-fail-fast-{unique_name}"

        await limiter.create_entity(entity_id)
        await limiter.set_limits(
            entity_id, [Limit.per_minute("rpm", 1000).with_schedule(ALWAYS)], resource=RESOURCE
        )
        async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
            pass

        item = await _raw_bucket(repo, entity_id)
        assert int(item[BUCKET_FIELD_VU]["N"]) > repo._now_ms(), "precondition: vu is ahead"

        await _corrupt_config(repo, entity_id)

        # Still admitted, and still on the fast path: the corrupt attribute is
        # never read.
        async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}) as lease:
            assert lease.degraded is False

    async def test_an_expired_vu_surfaces_the_error(self, localstack_limiter, unique_name):
        """Once `vu` has passed, the slow path resolves config, hits the
        corrupt attribute, and `on_unavailable` applies."""
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"sched-fail-slow-{unique_name}"

        await limiter.create_entity(entity_id)
        await limiter.set_limits(
            entity_id, [Limit.per_minute("rpm", 1000).with_schedule(ALWAYS)], resource=RESOURCE
        )
        async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
            pass

        # Expire the boundary exactly as a crossed one would, then corrupt.
        await _write_attr(repo, await _bucket_key(repo, entity_id), BUCKET_FIELD_VU, {"N": "1"})
        await _corrupt_config(repo, entity_id)

        with pytest.raises(RateLimiterUnavailable):
            async with limiter.acquire(
                entity_id, RESOURCE, {"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

        # And the operator's other choice still applies to the same failure.
        async with limiter.acquire(
            entity_id, RESOURCE, {"rpm": 1}, on_unavailable=OnUnavailable.ALLOW
        ) as lease:
            assert lease.degraded is True

    async def test_a_corrupt_bucket_schedule_stops_the_client_on_every_path(
        self, localstack_limiter, unique_name
    ):
        """The bucket attribute is read by both client paths — the speculative
        failure image and the slow path's `BatchGetItem` — so neither can
        proceed on a schedule it cannot decode, whatever `vu` says."""
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"sched-fail-bucket-{unique_name}"

        await limiter.create_entity(entity_id)
        await limiter.set_limits(
            entity_id, [Limit.per_minute("rpm", 10).with_schedule(ALWAYS)], resource=RESOURCE
        )
        async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
            pass
        await _write_attr(
            repo, await _bucket_key(repo, entity_id), BUCKET_FIELD_SCHED, {"S": "not-a-schedule"}
        )

        with pytest.raises(RateLimiterUnavailable):
            async with limiter.acquire(
                entity_id, RESOURCE, {"rpm": 5_000}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

        slow = RateLimiter(repository=repo, speculative_writes=False)
        with pytest.raises(RateLimiterUnavailable):
            async with slow.acquire(
                entity_id, RESOURCE, {"rpm": 1}, on_unavailable=OnUnavailable.BLOCK
            ):
                pass

    async def test_a_corrupt_per_limit_override_fails_the_whole_item(
        self, localstack_limiter, unique_name
    ):
        """§6.2 against a real item: the readable limit on the same item does
        not come back on its own, because a dropped limit would be enforced
        nowhere."""
        limiter = localstack_limiter
        repo = limiter._repository
        entity_id = f"sched-fail-item-{unique_name}"

        await limiter.create_entity(entity_id)
        await limiter.set_limits(
            entity_id,
            [
                Limit.per_minute("rpm", 1000).with_schedule(ALWAYS),
                Limit.per_minute("tpm", 100_000),
            ],
            resource=RESOURCE,
        )
        async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1, "tpm": 10}):
            pass
        await _write_attr(
            repo,
            await _bucket_key(repo, entity_id),
            bucket_attr("rpm", BUCKET_FIELD_SCHED),
            {"S": "not-a-schedule"},
        )

        with pytest.raises(RateLimiterUnavailable, match=bucket_attr("rpm", BUCKET_FIELD_SCHED)):
            await repo.get_buckets(entity_id, resource=RESOURCE)

    async def test_an_unscheduled_entity_on_the_same_table_is_unaffected(
        self, localstack_limiter, unique_name
    ):
        """Discriminates every test above against "the table is broken": the
        guard is per item, and nothing else in the namespace notices."""
        limiter = localstack_limiter
        repo = limiter._repository
        broken = f"sched-fail-broken-{unique_name}"
        healthy = f"sched-fail-healthy-{unique_name}"

        for entity_id in (broken, healthy):
            await limiter.create_entity(entity_id)
            await limiter.set_limits(
                entity_id,
                [Limit.per_minute("rpm", 1000).with_schedule(ALWAYS)],
                resource=RESOURCE,
            )
            async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
                pass
        await _write_attr(
            repo, await _bucket_key(repo, broken), BUCKET_FIELD_SCHED, {"S": "not-a-schedule"}
        )

        assert await repo.get_buckets(healthy, resource=RESOURCE)
        with pytest.raises(RateLimiterUnavailable):
            await repo.get_buckets(broken, resource=RESOURCE)
