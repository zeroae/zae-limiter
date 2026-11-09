"""A raise of a bucket holding a dripping limit conserves its balance (ADR-156, #821).

ADR-133 doubled ``shard_count`` without touching the existing shards and created
each new shard at a full share on first use: 1252, 1402 and 1902 admitted
against 1000 across doublings (2026-11-06 review). A raise stamps a marker on
every shard, reads them through it, and in one transaction re-expresses each
shard at the new count and creates the shard it splits off holding its balance
above the new share.
"""

from typing import Any

import pytest

from tests.fixtures.balances import balance_milli
from tests.fixtures.sharding import pinned_shard
from tests.unit.test_fill_instant_fast_path import RESOURCE, T0, Clock
from zae_limiter import Limit, RateLimiter, RateLimitExceeded, Repository, schema, shard_raise
from zae_limiter import fill_instant as fi
from zae_limiter.bucket import balance_milli_at
from zae_limiter.schema import BUCKET_FIELD_FI, BUCKET_FIELD_TC, bucket_attr, pk_bucket, sk_state

RPM = Limit.per_minute("rpm", 1000)
TPM = Limit.per_minute("tpm", 100_000)
QUOTA = Limit.quota("rpd", 500, cron="0 0 * * *")


@pytest.fixture
def clock(limiter) -> Clock:
    clock = Clock(T0)
    limiter._repository._now_ms = lambda: clock.now
    return clock


def _key(repo, entity_id: str, shard: int) -> dict[str, Any]:
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
        "SK": {"S": sk_state()},
    }


async def _item(repo, entity_id: str, shard: int) -> dict[str, Any] | None:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name, Key=_key(repo, entity_id, shard), ConsistentRead=True
    )
    return response.get("Item")


async def _balances(repo, entity_id: str, name: str, count: int, now_ms: int) -> list[int | None]:
    """Each shard's balance of ``name`` in millitokens at ``now_ms``, read exactly."""
    out: list[int | None] = []
    for shard in range(count):
        item = await _item(repo, entity_id, shard)
        if item is None:
            out.append(None)
            continue
        state = next(s for s in repo._deserialize_composite_bucket(item) if s.limit_name == name)
        out.append(balance_milli_at(state, now_ms))
    return out


async def _spend_on(limiter, entity_id: str, shard: int, **consume: int) -> None:
    with pinned_shard(shard):
        async with limiter.acquire(entity_id, RESOURCE, consume=consume):
            pass


class TestTheRaiseConserves:
    @pytest.mark.parametrize("start", [1, 2, 4, 8, 16])
    async def test_every_doubling_conserves_and_keeps_each_shard_under_its_share(
        self, limiter, clock, start
    ) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM, TPM], resource=RESOURCE)
        count = 1
        # Build up to `start` shards by raising, spending unevenly on the way.
        await _spend_on(limiter, "e1", 0, rpm=1, tpm=1)
        while count < start:
            count = await repo.raise_shards("e1", RESOURCE, count)
            for shard in range(count):
                await _spend_on(limiter, "e1", shard, rpm=1 + shard, tpm=37 * (shard + 1))
        clock.now += 1_234
        before = {
            name: await _balances(repo, "e1", name, count, clock.now) for name in ("rpm", "tpm")
        }

        assert await repo.raise_shards("e1", RESOURCE, count) == 2 * count

        for name, limit in (("rpm", RPM), ("tpm", TPM)):
            after = await _balances(repo, "e1", name, 2 * count, clock.now)
            assert all(b is not None for b in after)
            assert sum(after) == sum(b for b in before[name] if b is not None)
            share = limit.capacity * 1000 // (2 * count)
            assert all(b <= share for b in after)
        for shard in range(2 * count):
            item = await _item(repo, "e1", shard)
            assert item is not None
            assert int(item["shard_count"]["N"]) == 2 * count
            assert schema.BUCKET_FIELD_RAISE not in item

    async def test_every_new_shards_wcu_starts_full(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        for _ in range(5):
            await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        wcu = await _balances(repo, "e1", schema.WCU_LIMIT_NAME, 2, clock.now)
        assert wcu[1] == schema.WCU_LIMIT_CAPACITY * 1000
        assert wcu[0] is not None and wcu[0] < schema.WCU_LIMIT_CAPACITY * 1000

    async def test_a_new_shard_starts_its_usage_counter_at_zero(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=600)
        await repo.raise_shards("e1", RESOURCE, 1)
        child = await _item(repo, "e1", 1)
        assert child is not None
        assert child[bucket_attr("rpm", BUCKET_FIELD_TC)] == {"N": "0"}
        parent = await _item(repo, "e1", 0)
        assert parent is not None
        assert int(parent[bucket_attr("rpm", BUCKET_FIELD_TC)]["N"]) == 600_000

    async def test_a_new_shard_copies_its_parents_stamps_and_schedules(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await repo.create_entity("org")
        await repo.create_entity("e1", parent_id="org", cascade=True)
        await repo.set_limits("org", [RPM], resource=RESOURCE)
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        await repo.raise_shards("e1", RESOURCE, 1)
        parent, child = await _item(repo, "e1", 0), await _item(repo, "e1", 1)
        assert parent is not None and child is not None
        for attr in ("cascade", "parent_id", "pgen", "pinc", "entity_id", "resource", "GSI3PK"):
            assert child.get(attr) == parent.get(attr), attr
        assert child["GSI3SK"] == {"S": schema.gsi3_sk_bucket(RESOURCE, 1)}
        assert child["GSI2SK"] == {"S": schema.gsi2_sk_bucket("e1", 1)}

    async def test_a_quota_is_left_for_the_new_shard_to_seed(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM, QUOTA], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1, rpd=100)
        await repo.raise_shards("e1", RESOURCE, 1)
        child = await _item(repo, "e1", 1)
        assert child is not None
        assert not any(attr.startswith("b_rpd_") for attr in child)
        # The first acquire on it seeds the quota, funded by a move off shard 0
        # (ADR-145): the entity still holds exactly what it held.
        await _spend_on(limiter, "e1", 1, rpd=1)
        quota = await _balances(repo, "e1", "rpd", 2, clock.now)
        assert sum(b for b in quota if b is not None) == 500_000 - 101_000


class TestTheRaiseGivesUp:
    async def test_a_live_marker_held_by_another_raise_leaves_the_count(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key=_key(repo, "e1", 0),
            **shard_raise.marker_write(clock.now + 500, clock.now),
        )
        assert await repo.raise_shards("e1", RESOURCE, 1) == 1
        assert await _item(repo, "e1", 1) is None
        # Once it lapses, the raise lands.
        clock.now += 501
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2

    async def test_a_cancelled_transaction_removes_its_own_markers(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        client = await repo._get_client()
        # A disable fan-out landing between the marker and the transaction is
        # not held off by the marker: the raise is pinned on it instead.
        real = client.transact_write_items

        async def disable_first(**kwargs):
            await client.update_item(
                TableName=repo.table_name,
                Key=_key(repo, "e1", 0),
                UpdateExpression="SET disabled = :t",
                ExpressionAttributeValues={":t": {"BOOL": True}},
            )
            return await real(**kwargs)

        client.transact_write_items = disable_first
        try:
            assert await repo.raise_shards("e1", RESOURCE, 1) == 1
        finally:
            client.transact_write_items = real
        item = await _item(repo, "e1", 0)
        assert item is not None and schema.BUCKET_FIELD_RAISE not in item
        assert await _item(repo, "e1", 1) is None

    async def test_a_larger_stored_count_is_adopted_without_writing(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        # A client still holding count 1 asks again: the stored 2 wins.
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        assert await _item(repo, "e1", 2) is None


class TestMissingShards:
    async def test_a_missing_shard_is_created_empty_with_its_split(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        client = await repo._get_client()
        await client.delete_item(TableName=repo.table_name, Key=_key(repo, "e1", 1))  # swept
        before = await _balances(repo, "e1", "rpm", 2, clock.now)
        assert await repo.raise_shards("e1", RESOURCE, 2) == 4
        after = await _balances(repo, "e1", "rpm", 4, clock.now)
        assert after[1] == 0 and after[3] == 0
        assert sum(after) == sum(b for b in before if b is not None)

    async def test_the_client_creates_a_swept_shard_empty(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        client = await repo._get_client()
        await client.delete_item(TableName=repo.table_name, Key=_key(repo, "e1", 1))
        with pytest.raises(RateLimitExceeded):
            await _spend_on(limiter, "e1", 1, rpm=1)
        item = await _item(repo, "e1", 1)
        assert item is not None
        z = int(item[bucket_attr("rpm", BUCKET_FIELD_FI)]["N"])
        assert fi.balance_milli(z, clock.now, fi.ShardParams(1_000_000, 1_000_000, 60_000, 2)) == 0

    async def test_a_swept_shard_0_is_created_empty_by_a_client_that_knows_no_sibling(
        self, limiter, clock
    ) -> None:
        """ADR-156: created full only when no shard of the bucket exists.

        Shard 0 is swept by its TTL while shard 1 lives. A client with a cold
        cache knows of no other shard and used to create shard 0 full, at a
        count of 1: 1496 admitted against 1000 (#821's verification).
        """
        slow = Limit.custom("rpm", 1000, refill_amount=1, refill_period_seconds=86_400)
        repo = limiter._repository
        await repo.set_limits("e1", [slow], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2  # 500 and 499
        client = await repo._get_client()
        await client.delete_item(TableName=repo.table_name, Key=_key(repo, "e1", 0))  # swept
        cold = await Repository.open(stack="test-rate-limits")
        cold._now_ms = lambda: clock.now
        try:
            with pytest.raises(RateLimitExceeded):  # created empty
                async with RateLimiter(repository=cold).acquire("e1", RESOURCE, consume={"rpm": 5}):
                    pass
        finally:
            await cold.close()
        item = await _item(repo, "e1", 0)
        assert item is not None and item["shard_count"]["N"] == "2"
        assert await _balances(repo, "e1", "rpm", 2, clock.now) == [0, 499_000]

    async def test_a_new_bucket_asks_once_and_is_created_full(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        asked: list[tuple[str, str]] = []
        real = repo.sibling_shard_count

        async def spy(entity_id: str, resource: str) -> int | None:
            asked.append((entity_id, resource))
            return await real(entity_id, resource)

        repo.sibling_shard_count = spy
        await _spend_on(limiter, "e1", 0, rpm=1)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert asked == [("e1", RESOURCE)]  # the create only
        assert await _balances(repo, "e1", "rpm", 1, clock.now) == [998_000]

    async def test_a_sibling_swept_since_the_index_listed_it_floors_the_count(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        assert await repo.raise_shards("e1", RESOURCE, 2) == 4
        client = await repo._get_client()
        real_get = client.get_item

        async def gone(**kwargs: Any) -> Any:
            if kwargs.get("ProjectionExpression") == "shard_count":
                return {}  # deleted between the Query and this read
            return await real_get(**kwargs)

        client.get_item = gone
        try:
            assert await repo.sibling_shard_count("e1", RESOURCE) == 4  # shard 3 exists
        finally:
            client.get_item = real_get


P_WCU = fi.ShardParams(1_000_000, 1_000_000, 1_000, 1)


async def _drain_wcu(repo, entity_id: str, now_ms: int, *, full: bool = False) -> None:
    """Empty every shard's ``wcu`` at ``now_ms`` (the next write raises), or fill it."""
    client = await repo._get_client()
    value = fi.created_full(now_ms, P_WCU) if full else fi.created_empty(now_ms, P_WCU)
    for shard in range(schema.MAX_SHARD_COUNT):
        if await _item(repo, entity_id, shard) is None:
            continue
        await client.update_item(
            TableName=repo.table_name,
            Key=_key(repo, entity_id, shard),
            UpdateExpression="SET #w = :w",
            ExpressionAttributeNames={"#w": bucket_attr(schema.WCU_LIMIT_NAME, BUCKET_FIELD_FI)},
            ExpressionAttributeValues={":w": {"N": str(value)}},
        )


class TestDoublingsNeverOverAdmit:
    """The 2026-11-06 review's reproductions: 1252, 1402 and 1902 against 1000.

    The clock is frozen, so no refill accrues and ADR-153's bound is the
    capacity itself: every token admitted across the walk came from the
    bucket's 1000, however the doublings fall.
    """

    @pytest.mark.parametrize(
        ("raises", "spend_before"),
        [(1, 500), (2, 250), (5, 0)],
        ids=["one-doubling-half-spent", "two-doublings", "walk-to-32"],
    )
    async def test_admitted_never_exceeds_capacity(
        self, limiter, clock, raises, spend_before
    ) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        admitted = 0
        if spend_before:
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": spend_before}):
                admitted += spend_before
        else:
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                admitted += 1
        count = 1
        while count < 2**raises:  # each write raises
            await _drain_wcu(repo, "e1", clock.now)
            try:
                async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}):
                    admitted += 5
            except RateLimitExceeded:
                pass
            shard0 = await _item(repo, "e1", 0)
            assert shard0 is not None
            count = int(shard0["shard_count"]["N"])
        assert count == 2**raises
        await _drain_wcu(repo, "e1", clock.now, full=True)
        misses = 0
        while misses < 4 * count:  # then spend what every shard holds
            try:
                async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}):
                    admitted += 5
                misses = 0
            except RateLimitExceeded:
                misses += 1
        assert admitted <= RPM.capacity
        # Every shard was drawn down to (nearly) nothing: the walk spent the bucket.
        balances = await _balances(repo, "e1", "rpm", count, clock.now)
        assert admitted + sum(b for b in balances if b is not None) // 1000 == RPM.capacity


async def _hold(repo, entity_id: str, shard: int, deadline_ms: int, now_ms: int) -> None:
    """Stamp a raise marker on one shard, as another raise's first step does."""
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id, shard),
        **shard_raise.marker_write(deadline_ms, now_ms),
    )


@pytest.fixture
def sleeps(monkeypatch, clock):
    """``asyncio.sleep`` advances the clock instead of waiting; records each wait in ms."""
    import asyncio

    real = asyncio.sleep
    waited: list[int] = []

    async def fake(seconds, *args, **kwargs):
        ms = int(seconds * 1000)
        if ms:
            waited.append(ms)
            clock.now += ms + 1
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", fake)
    return waited


class TestWritersAndTheMarker:
    """Every write that moves a fill instant refuses a live marker; an expired one is ignored."""

    async def test_the_fast_path_answers_contended_then_admits(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        for _ in range(2):  # create, then learn the image
            await _spend_on(limiter, "e1", 0, rpm=1)
        await _hold(repo, "e1", 0, clock.now + 1_000, clock.now)
        with pytest.raises(RateLimitExceeded) as caught:
            await _spend_on(limiter, "e1", 0, rpm=1)
        assert caught.value.contended
        clock.now += 1_001
        await _spend_on(limiter, "e1", 0, rpm=1)

    async def test_the_slow_path_answers_contended(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        await _hold(repo, "e1", 0, clock.now + 1_000, clock.now)
        repo._fill_images.clear()
        limiter._speculative_writes = False
        try:
            with pytest.raises(RateLimitExceeded) as caught:
                await _spend_on(limiter, "e1", 0, rpm=1)
        finally:
            limiter._speculative_writes = True
        assert caught.value.contended
        before = await _balances(repo, "e1", "rpm", 1, clock.now)
        assert before == [RPM.capacity * 1000 - 1000]  # nothing written

    async def test_a_debit_waits_the_raise_out(self, limiter, clock, sleeps) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}) as lease:
            await _hold(repo, "e1", 0, clock.now + 700, clock.now)
            await lease.adjust(rpm=10)
        assert sleeps and sleeps[0] <= 700
        # Full again by the time the raise lapsed (1 token per 60 ms): the
        # debit lands after the wait, at that instant.
        assert (await _balances(repo, "e1", "rpm", 1, clock.now))[0] == (
            RPM.capacity * 1000 - 10_000
        )

    async def test_the_param_sync_waits_the_raise_out(self, limiter, clock, sleeps) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        await _hold(repo, "e1", 0, clock.now + 700, clock.now)
        await repo.set_limits("e1", [Limit.per_minute("rpm", 500)], resource=RESOURCE)
        assert sleeps
        item = await _item(repo, "e1", 0)
        assert item is not None and item[bucket_attr("rpm", schema.BUCKET_FIELD_CP)] == {
            "N": "500000"
        }

    async def test_a_reset_waits_the_raise_out(self, limiter, clock, sleeps) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=300)
        await _hold(repo, "e1", 0, clock.now + 700, clock.now)
        await repo.reset_bucket("e1", RESOURCE)
        assert sleeps
        assert (await _balances(repo, "e1", "rpm", 1, clock.now))[0] == RPM.capacity * 1000

    async def test_a_return_during_a_raise_is_dropped(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 50}) as lease:
            await _hold(repo, "e1", 0, clock.now + 700, clock.now)
            await lease.release(rpm=50)
        # Under-admission only: the 50 stay debited.
        assert (await _balances(repo, "e1", "rpm", 1, clock.now))[0] == RPM.capacity * 1000 - 51_000

    async def test_the_aggregator_refill_refuses_a_live_marker(self, limiter, clock) -> None:
        import boto3

        from zae_limiter_aggregator.processor import aggregate_bucket_states, try_refill_bucket

        repo = limiter._repository
        await repo.set_limits("e1", [RPM, QUOTA], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1, rpd=10)
        midnight = T0 - T0 % 86_400_000 + 86_400_000
        clock.now = midnight + 5_000
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(repo.table_name)

        async def record() -> dict[str, Any]:
            item = await _item(repo, "e1", 0)
            return {"eventName": "MODIFY", "dynamodb": {"NewImage": item, "OldImage": item}}

        await _hold(repo, "e1", 0, clock.now + 1_000, clock.now)
        state = next(iter(aggregate_bucket_states([await record()]).values()))
        assert not try_refill_bucket(table, state, clock.now)
        clock.now += 1_001
        assert try_refill_bucket(table, state, clock.now)  # the quota's midnight reset


class TestARaiseUnderLoad:
    async def test_a_raise_lands_while_another_task_keeps_writing(self, limiter, clock) -> None:
        """Stepped race: a writer keeps acquiring while the raise runs. The raise
        holds its writes off with the marker, lands, and nothing is lost."""
        import asyncio

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        admitted = 1
        stop = asyncio.Event()

        async def writer() -> None:
            nonlocal admitted
            while not stop.is_set():
                try:
                    async with limiter.acquire("e1", RESOURCE, consume={"rpm": 3}):
                        admitted += 3
                except RateLimitExceeded:
                    pass
                await asyncio.sleep(0)

        task = asyncio.create_task(writer())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        raised = await repo.raise_shards("e1", RESOURCE, 1)
        for _ in range(5):
            await asyncio.sleep(0)
        stop.set()
        await task
        assert raised == 2
        balances = await _balances(repo, "e1", "rpm", 2, clock.now)
        assert admitted * 1000 + sum(b for b in balances if b is not None) == RPM.capacity * 1000


def _image(shard_count: int = 1, **extra: Any) -> dict[str, Any]:
    """A wire-format shard holding ``rpm`` as a fill instant, as a marker write returns it."""
    params = fi.ShardParams(1_000_000, 1_000_000, 60_000, shard_count)
    item: dict[str, Any] = {
        "PK": {"S": "ns/BUCKET#e1#gpt-4#0"},
        "SK": {"S": "#STATE"},
        "shard_count": {"N": str(shard_count)},
        "rf": {"N": str(T0)},
        "xi": {"N": str(T0)},
        "rx": {"N": str(T0 + 2_000)},
        "b_rpm_cp": {"N": "1000000"},
        "b_rpm_ra": {"N": "1000000"},
        "b_rpm_rp": {"N": "60000"},
        "b_rpm_tc": {"N": "0"},
        "b_rpm_fi": {"N": str(fi.created_full(T0, params))},
        "b_wcu_cp": {"N": "1000000"},
        "b_wcu_ra": {"N": "1000000"},
        "b_wcu_rp": {"N": "1000"},
        "b_wcu_fi": {"N": str(T0 * 1_000_000)},
    }
    item.update(extra)
    return item


def _plan(images, old_count=1):
    return shard_raise.plan_raise("t", "ns", "e1", "gpt-4", images, old_count, T0, T0 + 2_000)


class TestThePlanner:
    """`shard_raise.plan_raise`, pure: what it refuses to raise, and how it reads a shard."""

    def test_no_shard_is_nothing_to_raise(self) -> None:
        assert _plan({0: None}) is None

    def test_a_larger_stored_count_is_left_alone(self) -> None:
        assert _plan({0: _image(shard_count=2)}) is None

    def test_shards_with_two_owners_are_left_to_the_move(self) -> None:
        """ADR-150: a move's fan-out in progress; the owner check could match one only."""
        images = {
            0: _image(2, pgen={"N": "1"}, pinc={"S": "a"}),
            1: _image(2, pgen={"N": "2"}, pinc={"S": "a"}),
        }
        assert _plan(images, old_count=2) is None

    def test_a_quota_only_bucket_is_not_raised_here(self) -> None:
        image = _image()
        for field in ("cp", "ra", "rp", "tc", "fi"):
            image.pop(f"b_rpm_{field}")
        image.update(
            {
                "b_q_tk": {"N": "5000"},
                "b_q_cp": {"N": "10000"},
                "b_q_ra": {"N": "0"},
                "b_q_rp": {"N": "1000"},
                "b_q_rsched": {"S": "1m0h0"},
            }
        )
        assert not shard_raise.holds_dripping(image)
        assert _plan({0: image}) is None

    def test_a_limit_with_a_reset_beside_a_rate_is_not_dripping(self) -> None:
        """A corrupt shape (ADR-137 rejects it) reads as the aggregator reads it."""
        assert not shard_raise.holds_dripping(_image(b_rpm_rsched={"S": "1m0h0"}))
        assert not shard_raise.holds_dripping(_image(rsched={"S": "1m0h0"}))
        assert shard_raise.holds_dripping(_image(rsched={"S": "1m0h0"}, b_rpm_rsched={"S": "-"}))
        assert not shard_raise.holds_dripping(None)

    def test_a_lagging_count_is_trimmed_to_the_raised_share(self) -> None:
        """A shard left at a lower count holds a larger share: trimmed first, never moved whole."""
        lagging = _image(shard_count=1)  # full at the old count: 1000
        plan = _plan({0: _image(shard_count=2), 1: lagging}, old_count=2)
        assert plan is not None
        put = next(
            item["Put"]["Item"]
            for item in plan.transact_items
            if "Put" in item and item["Put"]["Item"]["PK"]["S"].endswith("#3")
        )
        quarter = fi.ShardParams(1_000_000, 1_000_000, 60_000, 4)
        z = int(put["b_rpm_fi"]["N"])
        assert fi.balance_milli(z, T0, quarter) == 250_000  # one new share, the rest trimmed

    def test_a_limit_with_nothing_to_express_is_left_off(self) -> None:
        """A dripping limit missing its `rp` (corrupt) is neither split nor copied."""
        image = _image()
        image.pop("b_rpm_rp")
        image["b_tpm_cp"] = {"N": "1000"}
        image["b_tpm_ra"] = {"N": "1000"}
        image["b_tpm_rp"] = {"N": "60000"}
        image["b_tpm_tk"] = {"N": "500"}
        plan = _plan({0: image})
        assert plan is not None
        put = next(item["Put"]["Item"] for item in plan.transact_items if "Put" in item)
        assert "b_rpm_fi" not in put and "b_tpm_fi" in put

    def test_a_legacy_quota_beside_a_dripping_limit_is_frozen(self) -> None:
        image = _image(
            b_q_tk={"N": "5000"},
            b_q_cp={"N": "10000"},
            b_q_ra={"N": "0"},
            b_q_rp={"N": "1000"},
            b_q_rsched={"S": "1m0h0"},
        )
        plan = _plan({0: image})
        assert plan is not None
        update = plan.transact_items[0]["Update"]
        assert "b_q_gc" in update["ExpressionAttributeNames"].values()
        assert "if_not_exists" in update["UpdateExpression"]
        put = next(item["Put"]["Item"] for item in plan.transact_items if "Put" in item)
        assert not any(attr.startswith("b_q_") for attr in put)

    def test_a_corrupt_limit_is_neither_split_nor_copied(self) -> None:
        """A dripping limit with no capacity, or a fill instant that is no number."""
        image = _image(b_tpm_cp={"N": "0"}, b_tpm_ra={"N": "1000"}, b_tpm_rp={"N": "60000"})
        image["b_tpm_fi"] = {"N": "5"}
        image["b_spm_cp"] = {"N": "1000"}
        image["b_spm_ra"] = {"N": "1000"}
        image["b_spm_rp"] = {"N": "60000"}
        image["b_spm_fi"] = {"S": "corrupt"}
        plan = _plan({0: image})
        assert plan is not None
        put = next(item["Put"]["Item"] for item in plan.transact_items if "Put" in item)
        assert "b_rpm_fi" in put
        assert "b_tpm_fi" not in put and "b_spm_fi" not in put

    def test_a_shard_with_no_stored_count_reads_as_one(self) -> None:
        image = _image()
        image.pop("shard_count")
        plan = _plan({0: image})
        assert plan is not None
        assert (
            "attribute_not_exists(#sc)" in plan.transact_items[0]["Update"]["ConditionExpression"]
        )

    def test_a_boundary_since_xi_is_applied_before_the_split(self) -> None:
        """Re-expressed window by window first (ADR-155), then split at the instant."""
        from zae_limiter import ScheduleEntry
        from zae_limiter.schedule import encode

        night = (ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5),)
        midnight = T0 - T0 % 86_400_000
        xi = midnight + 6 * 3_600_000 + 59 * 60_000  # 06:59, inside the half window
        at = midnight + 7 * 3_600_000 + 30 * 60_000  # 07:30, past it
        half = fi.ShardParams(500_000, 500_000, 60_000, 1)
        image = _image(xi={"N": str(xi)}, rf={"N": str(xi)}, sched={"S": encode(night)[0]})
        image["b_rpm_fi"] = {"N": str(fi.created_empty(xi, half))}  # empty at 06:59
        plan = shard_raise.plan_raise("t", "ns", "e1", "gpt-4", {0: image}, 1, at, at + 2_000)
        assert plan is not None
        kept = int(plan.transact_items[0]["Update"]["ExpressionAttributeValues"][":lf0"]["N"])
        two = fi.ShardParams(1_000_000, 1_000_000, 60_000, 2)
        # One minute at the half rate (250) and thirty at the full one, capped:
        # 500 kept by each side of the split.
        assert fi.balance_milli(kept, at, two) == 500_000


class TestTheClientRaiseGivesUp:
    async def test_at_the_cap_it_warns_once_and_writes_nothing(self, limiter, clock, caplog):
        repo = limiter._repository
        cap = schema.MAX_SHARD_COUNT
        assert await repo.raise_shards("e1", RESOURCE, cap) == cap
        assert await repo.raise_shards("e1", RESOURCE, cap) == cap
        assert caplog.text.count("refusing to raise further") == 1

    async def test_a_contended_marker_leaves_the_count(self, limiter, clock) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        conflict = ClientError({"Error": {"Code": "TransactionConflictException"}}, "UpdateItem")
        real = repo._update_item_retrying_conflict

        async def busy(client, **kwargs):
            if kwargs.get("ExpressionAttributeNames", {}).get("#rx") and "SET #rx" in kwargs.get(
                "UpdateExpression", ""
            ):
                raise conflict
            return await real(client, **kwargs)

        repo._update_item_retrying_conflict = busy
        assert await repo.raise_shards("e1", RESOURCE, 1) == 1
        assert await _item(repo, "e1", 1) is None

    async def test_any_other_marker_error_propagates(self, limiter, clock) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        error = ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")

        async def broken(client, **kwargs):
            raise error

        repo._update_item_retrying_conflict = broken
        with pytest.raises(ClientError):
            await repo.raise_shards("e1", RESOURCE, 1)

    async def test_an_empty_plan_removes_the_markers(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [QUOTA], resource=RESOURCE)  # quota only: not raised here
        await _spend_on(limiter, "e1", 0, rpd=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 1
        item = await _item(repo, "e1", 0)
        assert item is not None and schema.BUCKET_FIELD_RAISE not in item

    async def test_a_transaction_error_removes_the_markers_and_propagates(
        self, limiter, clock
    ) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        client = await repo._get_client()
        real = client.transact_write_items

        async def throttled(**kwargs):
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "TransactWriteItems")

        client.transact_write_items = throttled
        try:
            with pytest.raises(ClientError):
                await repo.raise_shards("e1", RESOURCE, 1)
        finally:
            client.transact_write_items = real
        item = await _item(repo, "e1", 0)
        assert item is not None and schema.BUCKET_FIELD_RAISE not in item

    async def test_a_marker_that_cannot_be_removed_lapses(self, limiter, clock) -> None:
        """Giving up removes this raise's markers best effort: one that fails lapses."""
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        await _hold(repo, "e1", 1, clock.now + 1_000, clock.now)  # another raise holds slot 1
        client = await repo._get_client()
        real = client.update_item

        async def no_release(**kwargs):
            if kwargs.get("UpdateExpression") == "REMOVE #rx":
                raise ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
            return await real(**kwargs)

        client.update_item = no_release
        try:
            assert await repo.raise_shards("e1", RESOURCE, 2) == 2
        finally:
            client.update_item = real
        item = await _item(repo, "e1", 0)
        assert item is not None and schema.BUCKET_FIELD_RAISE in item  # lapses at its deadline


class TestTheAggregatorRaise:
    """`processor.raise_bucket_shards`, through a wire-format client on moto."""

    @staticmethod
    def _client():
        import boto3

        return boto3.client("dynamodb", region_name="us-east-1")

    async def test_it_raises_and_conserves(self, limiter, clock) -> None:
        from zae_limiter_aggregator.processor import raise_bucket_shards

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=300)
        before = await _balances(repo, "e1", "rpm", 1, clock.now)
        raised = raise_bucket_shards(
            self._client(), repo.table_name, repo._namespace_id, "e1", RESOURCE, 1, clock.now
        )
        assert raised == 2
        after = await _balances(repo, "e1", "rpm", 2, clock.now)
        assert sum(b for b in after if b is not None) == before[0]

    async def test_it_gives_up_on_a_live_marker_at_the_cap_and_on_an_empty_plan(
        self, limiter, clock
    ) -> None:
        from zae_limiter_aggregator.processor import raise_bucket_shards

        repo = limiter._repository
        client = self._client()
        args = (repo.table_name, repo._namespace_id, "e1", RESOURCE)
        assert raise_bucket_shards(client, *args, schema.MAX_SHARD_COUNT, clock.now) == (
            schema.MAX_SHARD_COUNT
        )
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        await _hold(repo, "e1", 0, clock.now + 500, clock.now)
        assert raise_bucket_shards(client, *args, 1, clock.now) == 1  # another raise holds it
        clock.now += 501
        # Asked to raise from 2 while slot 1 has no item: it is created empty
        # with its split (ADR-156), shard 0 split as ever.
        assert raise_bucket_shards(client, *args, 2, clock.now) == 4
        assert (await _balances(repo, "e1", "rpm", 4, clock.now))[1] == 0

    async def test_it_gives_up_on_a_quota_only_bucket_and_on_a_cancelled_transaction(
        self, limiter, clock
    ) -> None:
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        from zae_limiter_aggregator.processor import raise_bucket_shards

        repo = limiter._repository
        await repo.set_limits("q1", [QUOTA], resource=RESOURCE)
        await _spend_on(limiter, "q1", 0, rpd=1)
        args = (repo.table_name, repo._namespace_id, "q1", RESOURCE)
        assert raise_bucket_shards(self._client(), *args, 1, clock.now) == 1
        assert schema.BUCKET_FIELD_RAISE not in (await _item(repo, "q1", 0) or {})

        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        real = self._client()
        client = MagicMock(wraps=real)
        client.transact_write_items.side_effect = ClientError(
            {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
        )
        args = (repo.table_name, repo._namespace_id, "e1", RESOURCE)
        assert raise_bucket_shards(client, *args, 1, clock.now) == 1
        assert schema.BUCKET_FIELD_RAISE not in (await _item(repo, "e1", 0) or {})

    async def test_a_marker_that_cannot_be_removed_lapses(self, limiter, clock) -> None:
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        from zae_limiter_aggregator.processor import raise_bucket_shards

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        real = self._client()

        def update_item(**kwargs):
            if kwargs.get("UpdateExpression") == "REMOVE #rx":
                raise ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
            return real.update_item(**kwargs)

        client = MagicMock()
        client.update_item.side_effect = update_item
        client.transact_write_items.side_effect = ClientError(
            {"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems"
        )
        args = (repo.table_name, repo._namespace_id, "e1", RESOURCE)
        assert raise_bucket_shards(client, *args, 1, clock.now) == 1
        item = await _item(repo, "e1", 0)
        assert item is not None and schema.BUCKET_FIELD_RAISE in item  # lapses at its deadline

    async def test_any_marker_error_gives_up(self, limiter, clock) -> None:
        from unittest.mock import MagicMock

        from botocore.exceptions import ClientError

        from zae_limiter_aggregator.processor import raise_bucket_shards

        repo = limiter._repository
        client = MagicMock()
        client.update_item.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "UpdateItem"
        )
        args = (repo.table_name, repo._namespace_id, "e1", RESOURCE)
        assert raise_bucket_shards(client, *args, 1, clock.now) == 1
        client.transact_write_items.assert_not_called()


class TestTheProvisionerSyncWaits:
    async def test_a_live_marker_is_waited_out(self, limiter, clock, monkeypatch) -> None:
        import boto3

        from zae_limiter_provisioner import bucket_sync

        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        await _hold(repo, "e1", 0, clock.now + 700, clock.now)
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock.now += int(seconds * 1000) + 1

        monkeypatch.setattr(bucket_sync.time, "sleep", sleep)
        monkeypatch.setattr(bucket_sync, "read_clock_ms", lambda: clock.now)
        expr, names, values = bucket_sync.build_bucket_param_update(
            {"rpm": {"capacity": 500, "refill_amount": 500, "refill_period": 60}},
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=clock.now,
        )
        assert bucket_sync._update_one_shard(
            boto3.client("dynamodb", region_name="us-east-1"),
            repo.table_name,
            pk_bucket(repo._namespace_id, "e1", RESOURCE, 0),
            expr,
            names,
            values,
        )
        assert slept and slept[0] <= 0.7
        item = await _item(repo, "e1", 0)
        assert item is not None and item["b_rpm_cp"] == {"N": "500000"}


async def _to_legacy(repo, entity_id: str) -> None:
    """Rewrite shard 0 into the pre-#821 shape: `rpm` and `wcu` as `tk`, no `xi`."""
    item = await _item(repo, entity_id, 0)
    assert item is not None
    now = repo._now_ms()
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id, 0),
        UpdateExpression=(
            "SET b_rpm_tk = :t, b_wcu_tk = :w, rf = :now REMOVE b_rpm_fi, b_wcu_fi, xi"
        ),
        ExpressionAttributeValues={
            ":t": {"N": str(balance_milli(repo, item, "rpm", now))},
            ":w": {"N": str(balance_milli(repo, item, schema.WCU_LIMIT_NAME, now))},
            ":now": {"N": str(now)},
        },
    )
    repo._fill_images.clear()


class TestALegacyItemAndTheMarker:
    async def test_todays_write_answers_contended(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1)
        await _to_legacy(repo, "e1")
        await _hold(repo, "e1", 0, clock.now + 1_000, clock.now)
        with pytest.raises(RateLimitExceeded) as caught:
            await _spend_on(limiter, "e1", 0, rpm=1)
        assert caught.value.contended

    async def test_a_raise_landing_before_the_retry_is_contended(self, limiter, clock) -> None:
        """A quota-only request on a mixed item takes the consumption-only retry
        when its lock is lost; a marker stamped meanwhile refuses it too."""
        repo = limiter._repository
        await repo.set_limits("e1", [RPM, QUOTA], resource=RESOURCE)
        await _spend_on(limiter, "e1", 0, rpm=1, rpd=1)
        real = repo.transact_write
        held = []

        async def raise_first(items, *args, **kwargs):
            if not held:
                held.append(True)
                await _hold(repo, "e1", 0, clock.now + 1_000, clock.now)
            return await real(items, *args, **kwargs)

        repo.transact_write = raise_first
        slow = RateLimiter(repository=repo, speculative_writes=False)
        before = await _item(repo, "e1", 0)
        with pytest.raises(RateLimitExceeded) as caught:
            async with slow.acquire("e1", RESOURCE, consume={"rpd": 1}):
                pass
        assert caught.value.contended
        after = await _item(repo, "e1", 0)
        assert before is not None and after is not None
        assert after["b_rpd_tk"] == before["b_rpd_tk"]  # nothing debited
