"""A resource- or system-level change reaches active buckets (ADR-153, #823).

Such a change used to reach a bucket only when its TTL expired, and every
acquire re-stamps the TTL, so lowering a resource-level limit from 1000 to 100
per minute left every busy entity at 1000 for good. The setters now fan the
change out to every bucket it decides: GSI2 for a resource, the namespace's
GSI4 partition for the system, each bucket read and written once, its fill
instants re-expressed in a pinned write (ADR-155).
"""

from unittest.mock import AsyncMock, patch

import pytest

from tests.fixtures.balances import balance_milli
from zae_limiter import Limit, RateLimiter, RateLimitExceeded, schema
from zae_limiter.exceptions import FanoutIncomplete

RESOURCE = "gpt-4"
T0 = 1_800_000_000_000
FAST = Limit.per_minute("rpm", 1000)
SLOW = Limit.per_minute("rpm", 100)


async def _item(repo, entity_id: str, resource: str = RESOURCE) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, 0)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


def _cp(item: dict, name: str = "rpm") -> int:
    return int(item[schema.bucket_attr(name, schema.BUCKET_FIELD_CP)]["N"])


async def _burst(limiter, entity_id: str, resource: str = RESOURCE) -> int:
    """How many single requests are admitted before the first rejection."""
    admitted = 0
    while admitted < 2_000:
        try:
            async with limiter.acquire(entity_id, resource, {"rpm": 1}):
                admitted += 1
        except RateLimitExceeded:
            break
    return admitted


@pytest.fixture
def clock(limiter):
    class Clock:
        now = T0

    repo = limiter._repository
    repo._now_ms = lambda: Clock.now
    return Clock


class TestTheReproduction:
    """The issue's steps: an active bucket, a lowered default, a burst."""

    @pytest.mark.parametrize("level", ["resource", "system"])
    async def test_a_lowered_default_reaches_an_active_bucket(self, limiter, clock, level):
        repo = limiter._repository

        async def configure(limit):
            if level == "resource":
                await repo.set_resource_defaults(RESOURCE, [limit])
            else:
                await repo.set_system_defaults([limit])

        await configure(FAST)
        for _ in range(3):  # active: well inside the bucket's TTL
            async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
                pass
            clock.now += 100_000

        await configure(SLOW)

        item = await _item(repo, "user-1")
        assert _cp(item) == SLOW.capacity * 1000
        assert balance_milli(repo, item, "rpm", clock.now) <= SLOW.capacity * 1000
        clock.now += 60_000  # a minute idle: full again, at the new capacity
        assert await _burst(limiter, "user-1") == SLOW.capacity

    async def test_a_raised_default_reaches_an_active_bucket(self, limiter, clock):
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [SLOW])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
            pass
        await repo.set_resource_defaults(RESOURCE, [FAST])
        assert _cp(await _item(repo, "user-1")) == FAST.capacity * 1000
        clock.now += 60_000
        assert await _burst(limiter, "user-1") == FAST.capacity

    async def test_deleting_resource_defaults_hands_the_bucket_to_the_system(self, limiter, clock):
        repo = limiter._repository
        await repo.set_system_defaults([SLOW])
        await repo.set_resource_defaults(RESOURCE, [FAST])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
            pass
        await repo.delete_resource_defaults(RESOURCE)
        assert _cp(await _item(repo, "user-1")) == SLOW.capacity * 1000


class TestWhatTheFanOutLeavesAlone:
    async def test_an_entity_with_its_own_config_keeps_it(self, limiter, clock):
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [FAST])
        await repo.set_limits("vip", [FAST], resource=RESOURCE)
        await repo.set_limits("vip-wide", [FAST])  # the entity's _default_
        for entity_id in ("vip", "vip-wide", "user-1"):
            async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
                pass

        await repo.set_resource_defaults(RESOURCE, [SLOW])

        assert _cp(await _item(repo, "vip")) == FAST.capacity * 1000
        assert _cp(await _item(repo, "vip-wide")) == FAST.capacity * 1000
        assert _cp(await _item(repo, "user-1")) == SLOW.capacity * 1000

    async def test_a_system_change_leaves_a_resource_level_bucket(self, limiter, clock):
        repo = limiter._repository
        await repo.set_system_defaults([FAST])
        await repo.set_resource_defaults("other", [FAST])
        for resource in (RESOURCE, "other"):
            async with limiter.acquire("user-1", resource, {"rpm": 1}):
                pass

        await repo.set_system_defaults([SLOW])

        assert _cp(await _item(repo, "user-1", "other")) == FAST.capacity * 1000
        assert _cp(await _item(repo, "user-1")) == SLOW.capacity * 1000

    async def test_an_unchanged_default_writes_no_bucket(self, limiter, clock):
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [FAST])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
            pass
        with patch.object(repo, "_fanout_params", AsyncMock(return_value=0)) as fanout:
            await repo.set_resource_defaults(RESOURCE, [FAST])
            await repo.set_resource_defaults(RESOURCE, [Limit.per_minute("rpm", 1000, soft=True)])
        fanout.assert_not_called()  # a soft-only change has its own fan-out (#467)

    async def test_a_dropped_limit_goes_and_a_new_one_waits_for_its_seed(self, limiter, clock):
        """Only limits the item carries are written: a new one is seeded by the
        next slow pass (#633), never given parameters with no balance."""
        repo = limiter._repository
        tpm = Limit.per_minute("tpm", 10_000)
        await repo.set_resource_defaults(RESOURCE, [FAST, tpm])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1, "tpm": 1}):
            pass

        await repo.set_resource_defaults(RESOURCE, [FAST, Limit.per_minute("tpd", 5)])

        item = await _item(repo, "user-1")
        assert schema.bucket_attr("tpm", schema.BUCKET_FIELD_CP) not in item
        assert schema.bucket_attr("tpm", schema.BUCKET_FIELD_FI) not in item
        assert schema.bucket_attr("tpd", schema.BUCKET_FIELD_CP) not in item
        async with RateLimiter(repository=repo, speculative_writes=False).acquire(
            "user-1", RESOURCE, {"rpm": 1, "tpd": 1}
        ):
            pass
        assert _cp(await _item(repo, "user-1"), "tpd") == 5_000  # seeded by the pass


class TestTheWriteIsPinned:
    async def test_a_debit_landing_between_the_read_and_the_write_is_kept(self, limiter, clock):
        """A stepped race: the fan-out read 999 tokens, then a fast write spent
        950 of them. Writing from the read would trim to 100 and lose the debit;
        the pinned write fails and is redone from the item, leaving 49."""
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [FAST])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
            pass

        real = repo._sync_one_bucket_shard
        raced = []

        async def debit_first(pk, *args, **kwargs):
            if not raced:
                raced.append(pk)
                await repo._speculative_consume_single("user-1", RESOURCE, {"rpm": 950}, 0)
            return await real(pk, *args, **kwargs)

        client = await repo._get_client()
        real_update = client.update_item
        refused = []

        async def counting(**kwargs):
            try:
                return await real_update(**kwargs)
            except Exception as e:
                refused.append(type(e).__name__)
                raise

        with patch.object(repo, "_sync_one_bucket_shard", debit_first):
            client.update_item = counting
            try:
                await repo.set_resource_defaults(RESOURCE, [SLOW])
            finally:
                client.update_item = real_update

        assert raced and refused  # the stale write was refused and redone
        item = await _item(repo, "user-1")
        assert int(item[schema.bucket_attr("rpm", schema.BUCKET_FIELD_TC)]["N"]) == 951_000
        assert balance_milli(repo, item, "rpm", clock.now) == 49_000
        assert _cp(item) == SLOW.capacity * 1000


class TestPartialFailure:
    async def test_a_failure_reports_how_many_buckets_were_written(self, limiter, clock):
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [FAST])
        for entity_id in ("a", "b", "c"):
            async with limiter.acquire(entity_id, RESOURCE, {"rpm": 1}):
                pass

        real = repo._sync_default_bucket
        calls = []

        async def fail_second(*args, **kwargs):
            calls.append(args[0])
            if len(calls) == 2:
                raise RuntimeError("throttled")
            return await real(*args, **kwargs)

        with patch.object(repo, "_sync_default_bucket", fail_second):
            with pytest.raises(FanoutIncomplete) as exc:
                await repo.set_resource_defaults(RESOURCE, [SLOW])
        assert exc.value.stamped == 1  # the buckets written before the failure

        # Re-running the same call reconciles the rest.
        await repo.set_resource_defaults(RESOURCE, [FAST])
        await repo.set_resource_defaults(RESOURCE, [SLOW])
        for entity_id in ("a", "b", "c"):
            assert _cp(await _item(repo, entity_id)) == SLOW.capacity * 1000


class TestEdges:
    async def test_a_vanished_bucket_or_one_carrying_nothing_is_not_written(self, limiter, clock):
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [FAST])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
            pass
        pk = schema.pk_bucket(repo._namespace_id, "user-1", RESOURCE, 0)
        gone = schema.pk_bucket(repo._namespace_id, "nobody", RESOURCE, 0)
        tpm = Limit.per_minute("tpm", 10)

        assert await repo._sync_default_bucket(gone, [FAST], clock.now, None) is False
        before = await _item(repo, "user-1")
        assert await repo._sync_default_bucket(pk, [tpm], clock.now, {"tpd"}) is False
        assert await _item(repo, "user-1") == before

    async def test_an_image_a_raise_held_is_read_again(self, limiter, clock):
        repo = limiter._repository
        await repo.set_resource_defaults(RESOURCE, [FAST])
        async with limiter.acquire("user-1", RESOURCE, {"rpm": 1}):
            pass
        pk = schema.pk_bucket(repo._namespace_id, "user-1", RESOURCE, 0)
        update = repo._build_bucket_param_update([SLOW], 7, None, clock.now)
        image = await _item(repo, "user-1")
        with patch.object(repo, "_wait_out_raise", AsyncMock(side_effect=[True, False])):
            assert await repo._sync_one_bucket_shard(pk, *update, image=image) is True
        assert _cp(await _item(repo, "user-1")) == SLOW.capacity * 1000

    def test_an_old_image_that_will_not_deserialize_counts_as_changed(self, limiter):
        repo = limiter._repository
        corrupt = {
            schema.limit_attr("tpm", "cp"): {"N": "1"},
            schema.limit_attr("tpm", "sched"): {"S": "not-a-schedule"},
        }
        assert repo._limits_changed(corrupt, [FAST]) == (True, {"tpm"})
