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
    """PR #720 reviews: a write that landed but whose response was lost is not
    applied twice, whatever else happens to the bucket in between."""

    @staticmethod
    def _lose_first_response(client, fired, during=None):
        """Every reset / top-up is one `TransactWriteItems` carrying its operation
        record. The first one commits, its response is "lost", optionally
        something else happens, then the SDK resends the identical request."""
        real = client.transact_write_items

        async def lossy(**kw):
            carries_record = any(
                "Put" in item and item["Put"]["Item"]["SK"]["S"].startswith("#OP#")
                for item in kw["TransactItems"]
            )
            if carries_record and not fired:
                fired.append(kw)
                await real(**kw)  # commits server-side; the response is "lost"
                if during is not None:
                    await during()
                return await real(**kw)  # the SDK's automatic retry
            return await real(**kw)

        client.transact_write_items = lossy
        return real

    @pytest.mark.parametrize("shards", [1, 2])
    async def test_a_top_up_whose_response_was_lost_is_applied_once(self, limiter, clock, shards):
        """The retry fails the rf lock and the record's condition; one
        top_up(5) must record 5 topped-up tokens, not 10."""
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
        fired: list = []
        real = self._lose_first_response(client, fired)
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.transact_write_items = real
        assert fired and fired[0]["ClientRequestToken"]
        assert result.amounts == {"cal": 5}
        top_ups = [await _tu(repo, "e", "cal", shard=s) or 0 for s in range(shards)]
        assert sum(top_ups) == 5_000

    async def test_a_lost_create_response_is_applied_once(self, limiter, clock):
        """D5: the create landed, its response was lost, and the retry's
        `attribute_not_exists(PK)` failed. The operation record says it landed,
        so the new bucket is not topped up again."""
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        client = await repo._get_client()
        fired: list = []
        real = self._lose_first_response(client, fired)
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.transact_write_items = real
        assert fired
        assert (result.shards, result.amounts) == (1, {"cal": 5})
        assert await _tu(repo, "e", "cal") == 5_000
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 15

    @pytest.mark.parametrize("second", ["top_up", "reset"])
    async def test_op_marker_overwritten_by_concurrent_operation(self, limiter, clock, second):
        """A's write lands and its response is lost; before the SDK retry,
        another operator call lands on the same bucket. A must still find that
        it landed: two top_up(5) on a spent quota admit 10, not 15; a reset
        after A leaves exactly the full allowance."""
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        assert await _try(limiter, "e", {"cal": 10})  # spent
        clock[0] += 1
        client = await repo._get_client()

        async def concurrent():
            clock[0] += 1
            if second == "top_up":
                await repo.top_up("e", "r", {"cal": 5})
            else:
                await repo.reset_bucket("e", "r")

        fired: list = []
        real = self._lose_first_response(client, fired, during=concurrent)
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.transact_write_items = real
        assert fired
        assert result.amounts == {"cal": 5}
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 10


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


class TestResetRacingALostLock:
    """PR #720 review, finding 3: slow-path acquires that read before a reset
    and write after it lose their rf lock; the consumption-only retry then
    debits whatever the reset left on the item."""

    @pytest.mark.parametrize("racers", [2, 5])
    @pytest.mark.parametrize(("limit", "name"), [(SES, "ses"), (CAL, "cal")])
    async def test_admits_at_most_the_old_period_plus_one_allowance(
        self, limiter, clock, limit, name, racers
    ):
        import asyncio

        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await limiter.set_limits("e", [limit], resource="r")
        assert await _try(slow, "e", {name: 8})  # old period: 8 spent, 2 left
        clock[0] += 1

        real = repo.transact_write
        arrived = []
        gate = asyncio.Event()
        state = {"armed": True}

        async def hooked(items, *a, **k):
            if state["armed"]:
                arrived.append(1)
                if len(arrived) == racers:
                    state["armed"] = False
                    await repo.reset_bucket("e", "r")  # its own write runs unhooked
                    gate.set()
                await gate.wait()
            return await real(items, *a, **k)

        repo.transact_write = hooked
        try:
            results = await asyncio.gather(*[_try(slow, "e", {name: 2}) for _ in range(racers)])
        finally:
            repo.transact_write = real
        clock[0] += 1
        after = sum(2 for ok in results if ok) + await _drain(slow, "e", name)
        # The old period's 10 plus exactly one allowance from the reset.
        assert 8 + after <= 20


class TestRefundAfterATopUpEnded:
    """PR #720 review, finding 4: a lease read with `tu`; a reset or the period
    edge then removed it; the lease's refund must still force the clamping
    pass, which the stale in-memory ceiling (`C // gc + tu`) skipped."""

    async def _refund_into_new_period(self, limiter, clock, end_period) -> tuple[int | None, int]:
        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await limiter.set_limits("e", [CAL], resource="r")
        await repo.top_up("e", "r", {"cal": 5})  # tk 15, tu 5
        clock[0] += 1
        with pytest.raises(RuntimeError):
            async with slow.acquire("e", "r", {"cal": 5}):  # tk 10
                await end_period(repo)
                assert await _try(slow, "e", {"cal": 1})  # the new period's first token
                clock[0] += 1
                raise RuntimeError("body fails")  # rollback credits 5 -> tk 14
        item_vu = await _vu(repo, "e")
        clock[0] += 1
        return item_vu, 1 + await _drain(limiter, "e", "cal")

    async def test_after_a_reset(self, limiter, clock):
        async def reset(repo):
            await repo.reset_bucket("e", "r")
            clock[0] += 1

        vu, admitted = await self._refund_into_new_period(limiter, clock, reset)
        assert vu == 0  # the credit above C // gc forced a clamping pass
        # The new period admits its 10 plus, until the refund write trims
        # itself (#721), the one old-period token the clamp cannot tell apart.
        assert admitted <= 11

    async def test_after_the_period_edge(self, limiter, clock):
        async def edge(repo):
            clock[0] += DAY  # past midnight

        vu, admitted = await self._refund_into_new_period(limiter, clock, edge)
        assert vu == 0
        assert admitted <= 11  # the no-top-up baseline today; 10 after #721


async def _vu(repo, entity, resource="r", shard=0):
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
    raw = item.get(schema.BUCKET_FIELD_VU)
    return None if raw is None else int(raw["N"])


class TestTransactionInProgress:
    """PR #720 second review, finding 2b: DynamoDB answers the SDK's retry of
    a transaction still running under the same ClientRequestToken with
    TransactionInProgressException, which botocore does not retry. The write
    may have landed; the call must not raise it."""

    async def test_transaction_in_progress_after_landing_raises(self, limiter, clock):
        """Named after the probe that found it: it must no longer raise."""
        import random as _r

        from botocore.exceptions import ClientError

        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await limiter.set_limits("e", [CAL], resource="r")
        assert await _try(slow, "e", {"cal": 0})
        await repo.bump_shard_count("e", "r", 1)
        real_rr = _r.randrange
        _r.randrange = lambda a, b=None: 1
        try:
            await _try(slow, "e", {"cal": 0})
        finally:
            _r.randrange = real_rr
        clock[0] += 1
        client = await repo._get_client()
        real = client.transact_write_items
        state = {"armed": True}

        async def in_progress(**kw):
            if state["armed"]:
                state["armed"] = False
                await real(**kw)  # landed
                raise ClientError(
                    {"Error": {"Code": "TransactionInProgressException", "Message": "x"}},
                    "TransactWriteItems",
                )
            return await real(**kw)

        client.transact_write_items = in_progress
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.transact_write_items = real
        assert result.amounts == {"cal": 5}
        top_ups = [await _tu(repo, "e", "cal", shard=s) or 0 for s in range(2)]
        assert sum(top_ups) == 5_000  # landed once, reported once


class TestRefundsUnderALiveTopUp:
    """PR #720 second review, finding 3 (cost): a refund that fits under a
    live top-up must not force a clamping pass (`vu = 0`, then a slow pass)."""

    async def test_refunds_on_topped_up_quota_force_slow_passes(self, limiter, clock):
        """Named after the probe: ten leases refunding inside the topped-up
        ceiling force no clamp at all."""
        repo = limiter._repository
        big = Limit.quota("cal", 1000, cron="0 0 * * *")
        await limiter.set_limits("e", [big], resource="r")
        assert await _try(limiter, "e", {"cal": 1})
        await repo.top_up("e", "r", {"cal": 500})
        clock[0] += 1
        assert await _try(limiter, "e", {"cal": 1})  # the forced pass clears vu
        forced = []
        real = repo.build_vu_reset

        def counting(*a, **k):
            forced.append(1)
            return real(*a, **k)

        repo.build_vu_reset = counting
        for _ in range(10):
            clock[0] += 1
            async with limiter.acquire("e", "r", {"cal": 10}) as lease:
                await lease.adjust(cal=-5)  # estimated 10, used 5
        assert forced == []


async def _two_shards(limiter, entity, name):
    import random as _r

    repo = limiter._repository
    slow = RateLimiter(repository=repo, speculative_writes=False)
    await _try(slow, entity, {name: 0})
    await repo.bump_shard_count(entity, "r", 1)
    real_rr = _r.randrange
    _r.randrange = lambda a, b=None: 1
    try:
        await _try(slow, entity, {name: 0})
    finally:
        _r.randrange = real_rr


class TestSessionResetOnShards:
    """PR #720 second review probes that held: a session reset never gives a
    sharded entity more than one allowance, and readers see it at once."""

    @pytest.mark.parametrize("spent_first", [True, False])
    async def test_a_sharded_session_reset_gives_one_allowance(self, limiter, clock, spent_first):
        repo = limiter._repository
        await limiter.set_limits("e", [SES], resource="r")
        await _two_shards(limiter, "e", "ses")
        clock[0] += 1
        if spent_first:
            await _drain(limiter, "e", "ses")
        clock[0] += 1
        await repo.reset_bucket("e", "r")
        clock[0] += 1
        assert await _drain(limiter, "e", "ses") <= 10

    async def test_check_availability_reports_the_full_allowance_after_a_reset(
        self, limiter, clock
    ):
        repo = limiter._repository
        await limiter.set_limits("e", [SES], resource="r")
        assert await _try(limiter, "e", {"ses": 10})
        clock[0] += 1
        await repo.reset_bucket("e", "r")
        assert (await limiter.check_availability("e", "r")).available == {"ses": 10}

    async def test_a_reset_with_a_pending_roll_on_a_sibling_gives_one_allowance(
        self, limiter, clock
    ):
        """Shard 0 opened the next window and fanned it out; shard 1 holds the
        roll pending when the reset lands."""
        import random as _r

        repo = limiter._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await limiter.set_limits("e", [SES], resource="r")
        await _two_shards(limiter, "e", "ses")
        for shard in (0, 1):
            real = _r.randrange
            _r.randrange = lambda a, b=None, s=shard: s
            try:
                while await _try(slow, "e", {"ses": 1}):
                    pass
            finally:
                _r.randrange = real
        clock[0] += 3_600_001  # the window ends
        real = _r.randrange
        _r.randrange = lambda a, b=None: 0
        try:
            assert await _try(slow, "e", {"ses": 1})  # shard 0 opens and fans out
        finally:
            _r.randrange = real
        clock[0] += 1
        await repo.reset_bucket("e", "r")
        clock[0] += 1
        assert await _drain(limiter, "e", "ses") <= 10

    async def test_a_top_up_creating_a_session_bucket_keeps_the_purchase(self, limiter, clock):
        repo = limiter._repository
        await limiter.set_limits("e", [SES], resource="r")
        await repo.top_up("e", "r", {"ses": 5})
        clock[0] += 1
        assert await _drain(limiter, "e", "ses") == 15


def _tx_error(code):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "TransactWriteItems")


def _carries_record(kw):
    return any(
        "Put" in item and item["Put"]["Item"]["SK"]["S"].startswith("#OP#")
        for item in kw.get("TransactItems", [])
    )


@pytest.fixture
def aws_tokens(limiter):
    """Emulate DynamoDB's ClientRequestToken rules, which moto ignores.

    A token is kept once its first request completes, success or not: the same
    token with the same payload is a no-op success, with a different payload
    an IdempotentParameterMismatchException. ``fail_first`` cancels that many
    record-carrying transactions before they write.
    """
    state = {"fail_first": 0, "client": None, "real": None}

    async def install(fail_first=0):
        client = await limiter._repository._get_client()
        real = client.transact_write_items
        seen: dict = {}
        state.update(fail_first=fail_first, client=client, real=real)

        async def emulated(**kw):
            token = kw.get("ClientRequestToken")
            payload = repr(kw.get("TransactItems"))
            if token is not None and token in seen:
                if seen[token][0] != payload:
                    raise _tx_error("IdempotentParameterMismatchException")
                if seen[token][1]:
                    return {}
            ok = False
            try:
                if state["fail_first"] > 0 and _carries_record(kw):
                    state["fail_first"] -= 1
                    raise _tx_error("TransactionCanceledException")
                out = await real(**kw)
                ok = True
                return out
            finally:
                if token is not None and token not in seen:
                    seen[token] = (payload, ok)

        client.transact_write_items = emulated

    yield install
    if state["client"] is not None:
        state["client"].transact_write_items = state["real"]


async def _same_key_race(repo, clock, calls):
    """Run ``calls`` with both planning before either writes."""
    import asyncio

    original = repo._read_operation_shards
    arrived = {"n": 0}
    both = asyncio.Event()

    async def barrier(*a, **k):
        result = await original(*a, **k)
        arrived["n"] += 1
        clock[0] += 1  # each caller plans at its own instant
        if arrived["n"] == 2:
            both.set()
        if arrived["n"] <= 2:
            await asyncio.wait_for(both.wait(), 5)
        return result

    repo._read_operation_shards = barrier
    try:
        return await asyncio.gather(*calls, return_exceptions=True)
    finally:
        repo._read_operation_shards = original


class TestRequestTokens:
    """PR #720 third review, finding 1: the ClientRequestToken came from the
    caller's key, so a second call with the key reused a token with a new
    payload — IdempotentParameterMismatchException, raised to the caller, for
    ten minutes."""

    async def test_key_retry_after_unavailable_hits_token_mismatch(
        self, limiter, clock, aws_tokens
    ):
        """Named after the probe: the caller's retry with the same key, a
        second after a call that gave up, succeeds and grants once."""
        from zae_limiter.exceptions import RateLimiterUnavailable

        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        await _drain(limiter, "e", "cal")
        clock[0] += 1
        await aws_tokens(fail_first=4)
        with pytest.raises(RateLimiterUnavailable):
            await repo.top_up("e", "r", {"cal": 5}, idempotency_key="buy-9")
        clock[0] += 1000
        result = await repo.top_up("e", "r", {"cal": 5}, idempotency_key="buy-9")
        assert result.amounts == {"cal": 5}
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 5

    async def test_same_key_race_token_mismatch(self, limiter, clock, aws_tokens):
        """Two callers with one key plan at once under the real token rules:
        neither raises, and the purchase is granted once."""
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        await _drain(limiter, "e", "cal")
        clock[0] += 1
        await aws_tokens()
        results = await _same_key_race(
            repo,
            clock,
            [repo.top_up("e", "r", {"cal": 5}, idempotency_key="buy-2") for _ in range(2)],
        )
        assert all(not isinstance(r, Exception) for r in results), results
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 5

    async def test_create_path_token_reused_across_attempts(self, limiter, clock, aws_tokens):
        """Targeted review, finding 2: with no bucket, a cancelled create is
        re-planned at a later instant under a fresh token, not the same `-c`
        token with a new payload (a mismatch on every retry)."""
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        await aws_tokens(fail_first=1)
        client = await repo._get_client()
        emulated = client.transact_write_items
        tokens = []

        async def ticking(**kw):
            tokens.append(kw.get("ClientRequestToken"))
            clock[0] += 1  # each attempt plans at its own instant
            return await emulated(**kw)

        client.transact_write_items = ticking
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.transact_write_items = emulated
        assert result.amounts == {"cal": 5}
        assert len(tokens) == 2 and tokens[0] != tokens[1]
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 15

    async def test_a_token_mismatch_is_an_unclear_outcome(self, limiter, clock):
        """Whatever reuses a token, the mismatch says nothing about this call:
        it is resolved by the operation record, never raised."""
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        await _drain(limiter, "e", "cal")
        clock[0] += 1
        client = await repo._get_client()
        real = client.transact_write_items
        state = {"armed": True}

        async def mismatch(**kw):
            if state["armed"] and _carries_record(kw):
                state["armed"] = False
                raise _tx_error("IdempotentParameterMismatchException")
            return await real(**kw)

        client.transact_write_items = mismatch
        try:
            result = await repo.top_up("e", "r", {"cal": 5})
        finally:
            client.transact_write_items = real
        assert result.amounts == {"cal": 5}
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == 5


class TestSameKeyRaceAudit:
    """PR #720 third review, finding 2: two callers racing with one key write
    one operation, so they write one audit event."""

    @pytest.mark.parametrize("op", ["top_up", "reset"])
    async def test_same_key_race(self, limiter, clock, op):
        repo = limiter._repository
        await limiter.set_limits("e", [CAL], resource="r")
        await _drain(limiter, "e", "cal")
        clock[0] += 1
        if op == "top_up":
            calls = [repo.top_up("e", "r", {"cal": 5}, idempotency_key="buy-1") for _ in range(2)]
            action, admitted = "bucket_topped_up", 5
        else:
            calls = [repo.reset_bucket("e", "r", idempotency_key="rst-1") for _ in range(2)]
            action, admitted = "bucket_reset", 10
        results = await _same_key_race(repo, clock, calls)
        assert all(not isinstance(r, Exception) for r in results), results
        assert results[0] == results[1]
        clock[0] += 1
        assert await _drain(limiter, "e", "cal") == admitted
        events = [e for e in await repo.get_audit_events("e", limit=100) if e.action == action]
        assert len(events) == 1
