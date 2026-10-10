"""Reset and top-up seen through acquire, check_availability and 429s (ADR-149, Refs #470).

The repository-level behaviour (what each shard's write leaves on the item) is
in ``test_repository.py::TestResetAndTopUp``, which has a generated sync twin.
This file drives the rest of the stack: the acquire paths that must keep a
purchase (the ceiling includes ``b_{name}_tu``), the ones that must end it (a
reset edge or window roll removes it), and the readers that must show it.
"""

from datetime import timedelta

import pytest

from zae_limiter import Limit, RateLimiter, RateLimitExceeded, schema
from zae_limiter.bucket_ops import split_by_headroom, split_weighted

T0 = 1_767_268_800_000  # 2026-01-01T12:00:00Z
DAY = 86_400_000
CAL = Limit.quota("cal", 10, cron="0 0 * * *")
SES = Limit.quota("ses", 10, reset_after=timedelta(hours=1))
RPM = Limit.per_minute("rpm", 10)


@pytest.fixture
def clock(limiter):
    now = [T0]
    limiter._repository._now_ms = lambda: now[0]
    return now


async def _spend(limiter, entity, amounts, resource="r"):
    async with limiter.acquire(entity, resource, amounts):
        pass


async def _rejected(limiter, entity, amounts, resource="r"):
    with pytest.raises(RateLimitExceeded) as info:
        await _spend(limiter, entity, amounts, resource)
    return info.value


async def _tu(repo, entity, name, resource="r", shard=0):
    client = await repo._get_client()
    item = (
        await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity, resource, shard)},
                "SK": {"S": schema.sk_state()},
            },
            ConsistentRead=True,
        )
    )["Item"]
    raw = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_TU))
    return None if raw is None else int(raw["N"])


class TestPurchase:
    @pytest.mark.parametrize("speculative", [True, False])
    async def test_a_purchase_admits_exactly_n_beyond_the_plan(self, limiter, clock, speculative):
        lim = RateLimiter(repository=limiter._repository, speculative_writes=speculative)
        await limiter.set_limits("e", [CAL], resource="r")
        await _spend(lim, "e", {"cal": 10})
        await _rejected(lim, "e", {"cal": 1})
        await limiter._repository.top_up("e", "r", {"cal": 5})
        await _spend(lim, "e", {"cal": 3})  # the forced slow pass keeps the purchase
        await _spend(lim, "e", {"cal": 2})
        await _rejected(lim, "e", {"cal": 1})

    async def test_a_refund_after_a_top_up_into_spent_room_keeps_the_purchase(self, limiter, clock):
        """The top-up lands where tokens were spent; the refund of that spend
        must still fit under the raised ceiling (design-validator finding 1)."""
        await limiter.set_limits("e", [CAL], resource="r")
        async with limiter.acquire("e", "r", {"cal": 8}) as lease:
            await limiter._repository.top_up("e", "r", {"cal": 3})
            await lease.release(cal=8)
        await _spend(limiter, "e", {"cal": 13})
        await _rejected(limiter, "e", {"cal": 1})

    async def test_a_429_reports_the_topped_up_ceiling(self, limiter, clock):
        await limiter.set_limits("e", [CAL], resource="r")
        await limiter._repository.top_up("e", "r", {"cal": 5})
        error = await _rejected(limiter, "e", {"cal": 16})
        assert error.statuses[0].limit.capacity == 15
        assert error.statuses[0].available == 15

    async def test_check_availability_shows_the_purchase(self, limiter, clock):
        await limiter.set_limits("e", [CAL], resource="r")
        await _spend(limiter, "e", {"cal": 1})
        await limiter._repository.top_up("e", "r", {"cal": 5})
        check = await limiter.check_availability("e", "r")
        assert check.status("cal").available == 14

    async def test_the_next_reset_edge_ends_the_purchase(self, limiter, clock):
        await limiter.set_limits("e", [CAL], resource="r")
        await limiter._repository.top_up("e", "r", {"cal": 5})
        repo = limiter._repository
        assert await _tu(repo, "e", "cal") == 5_000
        # Past midnight a pending reset hides the purchase from readers…
        clock[0] += DAY
        assert (await limiter.check_availability("e", "r")).status("cal").available == 10
        # …and the acquire that applies it removes `tu` with it.
        await _spend(limiter, "e", {"cal": 10})
        assert await _tu(repo, "e", "cal") is None
        await _rejected(limiter, "e", {"cal": 1})

    async def test_a_window_roll_ends_a_session_purchase(self, limiter, clock):
        await limiter.set_limits("e", [SES], resource="r")
        await limiter._repository.top_up("e", "r", {"ses": 5})
        await _spend(limiter, "e", {"ses": 15})
        await _rejected(limiter, "e", {"ses": 1})
        clock[0] += 3_600_001  # the window the purchase opened has ended
        await _spend(limiter, "e", {"ses": 10})
        assert await _tu(limiter._repository, "e", "ses") is None
        await _rejected(limiter, "e", {"ses": 1})

    async def test_a_parent_reset_edge_ends_its_purchase_too(self, limiter, clock):
        repo = limiter._repository
        await repo.create_entity("p")
        await repo.create_entity("c", parent_id="p", cascade=True)
        await limiter.set_limits("p", [CAL], resource="r")
        await limiter.set_limits("c", [RPM], resource="r")
        await _spend(limiter, "c", {"rpm": 1})
        await repo.top_up("p", "r", {"cal": 5})
        clock[0] += DAY
        await _spend(limiter, "c", {"rpm": 1})  # the parent's pass applies its edge
        assert await _tu(repo, "p", "cal") is None


class TestReset:
    async def test_an_exhausted_quota_admits_its_full_share_after_a_reset(self, limiter, clock):
        await limiter.set_limits("e", [CAL], resource="r")
        await _spend(limiter, "e", {"cal": 10})
        await limiter._repository.reset_bucket("e", "r")
        await _spend(limiter, "e", {"cal": 10})
        await _rejected(limiter, "e", {"cal": 1})

    async def test_a_session_reset_lets_the_next_request_open_a_fresh_window(self, limiter, clock):
        repo = limiter._repository
        await limiter.set_limits("e", [SES], resource="r")
        await _spend(limiter, "e", {"ses": 10})
        clock[0] += 60_000
        await repo.reset_bucket("e", "r")
        clock[0] += 1
        await _spend(limiter, "e", {"ses": 10})
        check = await limiter.check_availability("e", "r")
        assert check.status("ses").resets_at_ms == clock[0] + 3_600_000

    async def test_a_cascading_childs_reset_leaves_the_parent_alone(self, limiter, clock):
        repo = limiter._repository
        await repo.create_entity("p")
        await repo.create_entity("c", parent_id="p", cascade=True)
        await limiter.set_limits("p", [CAL], resource="r")
        await limiter.set_limits("c", [CAL], resource="r")
        await _spend(limiter, "c", {"cal": 10})
        await repo.reset_bucket("c", "r")
        error = await _rejected(limiter, "c", {"cal": 1})
        assert error.statuses[-1].entity_id == "p"


class TestSplits:
    def test_a_weighted_split_is_exact(self):
        assert split_weighted(10, {0: 2, 1: 1, 3: 1}) == {0: 6, 1: 2, 3: 2}
        assert split_weighted(5, {}) == {}

    def test_a_headroom_split_never_exceeds_the_room(self):
        assert split_by_headroom(100, {0: 3, 1: 4}) == {0: 3, 1: 4}
        assert split_by_headroom(0, {0: 3}) == {0: 0}
        assert split_by_headroom(5, {0: 0, 1: 0}) == {0: 0, 1: 0}
        assert split_by_headroom(3, {0: 1, 1: 1, 2: 1, 3: 1}) == {0: 1, 1: 1, 2: 1, 3: 0}


async def _try(lim, entity, amounts):
    try:
        await _spend(lim, entity, amounts)
        return True
    except RateLimitExceeded:
        return False


async def _drain(lim, entity, name, cap=100):
    n = 0
    while n < cap and await _try(lim, entity, {name: 1}):
        n += 1
    return n


class TestIdempotency:
    """PR #720 review, finding 1: a write that landed but whose response was
    lost is not applied twice."""

    @pytest.mark.parametrize("shards", [1, 2])
    async def test_a_top_up_whose_response_was_lost_is_applied_once(self, limiter, clock, shards):
        """The write commits but its response is lost; botocore retries the
        identical request, whose rf condition then fails. One top_up(5) must
        record 5 topped-up tokens, not 10."""
        import random as _r

        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await limiter.set_limits("e", [CAL], resource="r")
        assert await _try(slow, "e", {"cal": 1})
        if shards == 2:
            await repo.bump_shard_count("e", "r", 1)
            real_rr = _r.randrange
            _r.randrange = lambda a, b=None: 1
            try:
                await _try(slow, "e", {"cal": 1})
            finally:
                _r.randrange = real_rr
        clock[0] += 1
        client = await repo._get_client()
        method = "update_item" if shards == 1 else "transact_write_items"
        real = getattr(client, method)
        state = {"armed": True}

        async def lossy(**kw):
            if shards > 1:
                # Idempotent for 10 minutes on DynamoDB (moto ignores it).
                assert kw["ClientRequestToken"]
            if state["armed"] and (shards > 1 or "#ot0" in str(kw.get("ExpressionAttributeNames"))):
                state["armed"] = False
                await real(**kw)  # commits server-side; the response is "lost"
                return await real(**kw)  # the SDK's automatic retry of the same request
            return await real(**kw)

        setattr(client, method, lossy)
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            setattr(client, method, real)
        assert result.amounts == {"cal": 5}
        top_ups = [await _tu(repo, "e", "cal", shard=s) or 0 for s in range(shards)]
        assert sum(top_ups) == 5_000

    async def test_a_lost_create_response_is_applied_once(self, limiter, clock):
        """D5: the create landed, its response was lost, and the retry's
        `attribute_not_exists(PK)` failed. The re-read finds this call's own
        operation id and returns, instead of topping up the new bucket again."""
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        client = await repo._get_client()
        real = client.put_item

        async def lossy(**kw):
            await real(**kw)
            return await real(**kw)

        client.put_item = lossy
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.put_item = real
        assert (result.shards, result.amounts) == (1, {"cal": 5})
        assert await _tu(repo, "e", "cal") == 5_000
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 15


class TestNothingFailsAfterTheCommit:
    """PR #720 review, finding 2: an error raised after the write landed makes
    the caller retry an operation that already happened."""

    async def test_an_invalid_principal_is_refused_before_anything_is_written(self, limiter, clock):
        from zae_limiter.exceptions import ValidationError

        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        assert await _try(limiter, "e", {"cal": 10})
        clock[0] += 1
        with pytest.raises(ValidationError):
            await repo.top_up("e", "r", {"cal": 5}, principal="ops team!")
        assert await _tu(repo, "e", "cal") is None  # nothing was granted
        await repo.top_up("e", "r", {"cal": 5}, principal="ops-team")  # the caller's retry
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 5  # one purchase of 5, once
