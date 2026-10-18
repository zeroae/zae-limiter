"""Second-round adversarial probes for PR #720 (ADR-149). Not for commit."""

import random as _r
from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from zae_limiter import Limit, RateLimiter, RateLimitExceeded, schema

T0 = 1_767_268_800_000  # 2026-01-01T12:00:00Z
DAY = 86_400_000
CAL = Limit.quota("cal", 10, cron="0 0 * * *")
SES = Limit.quota("ses", 10, reset_after=timedelta(hours=1))


@pytest.fixture
def clock(limiter):
    now = [T0]
    limiter._repository._now_ms = lambda: now[0]
    return now


async def _try(lim, e, amounts):
    try:
        async with lim.acquire(e, "r", amounts):
            pass
        return True
    except RateLimitExceeded:
        return False


async def _drain(lim, e, name, cap=200):
    n = 0
    while n < cap and await _try(lim, e, {name: 1}):
        n += 1
    return n


async def _item(repo, e, shard=0):
    client = await repo._get_client()
    return (
        await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, e, "r", shard)},
                "SK": {"S": schema.sk_state()},
            },
            ConsistentRead=True,
        )
    ).get("Item")


def _n(item, attr):
    raw = (item or {}).get(attr)
    return None if raw is None else int(raw["N"])


async def _two_shards(limiter, repo, e, name):
    slow = RateLimiter(repository=repo, speculative_writes=False)
    assert await _try(slow, e, {name: 0}) or True
    await repo.bump_shard_count(e, "r", 1)
    real_rr = _r.randrange
    _r.randrange = lambda a, b=None: 1
    try:
        await _try(slow, e, {name: 0})
    finally:
        _r.randrange = real_rr


# ---------------------------------------------------------------------------
# P1: the `op` marker is overwritten by a concurrent operation between the
# lost response and the re-read, so a landed write looks unlanded.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("second", ["top_up", "reset"])
async def test_op_marker_overwritten_by_concurrent_operation(limiter, clock, second):
    repo = limiter._repository
    await limiter.set_limits("e", [CAL], resource="r")
    assert await _try(limiter, "e", {"cal": 10})  # spent: tk 0
    clock[0] += 1
    client = await repo._get_client()
    real = client.update_item
    state = {"armed": True}

    async def lossy(**kw):
        if state["armed"] and "#op" in kw.get("ExpressionAttributeNames", {}):
            state["armed"] = False
            await real(**kw)  # A's write commits; its response is lost
            clock[0] += 1
            # Before botocore's retry reaches DynamoDB, another operator call
            # lands on the same bucket (a second purchase webhook, say).
            if second == "top_up":
                await repo.top_up("e", "r", {"cal": 5})
            else:
                await repo.reset_bucket("e", "r")
            return await real(**kw)  # the SDK retry: fails the rf lock
        return await real(**kw)

    client.update_item = lossy
    try:
        result = await repo.top_up("e", "r", {"cal": 5})
    finally:
        client.update_item = real
    item = await _item(repo, "e")
    print(f"\n[{second}] A result={result} tk={_n(item, 'b_cal_tk')} tu={_n(item, 'b_cal_tu')}")
    clock[0] += 1
    n = await _drain(limiter, "e", "cal")
    expected = 10 if second == "top_up" else 10  # top_up: 5+5 ; reset after A: 10
    print("admitted after both calls:", n, "expected:", expected)
    if second == "top_up":
        assert n == expected, f"admitted {n}, expected {expected}"


# ---------------------------------------------------------------------------
# P2: a multi-shard transaction whose response was lost; the SDK retry with
# the same ClientRequestToken arrives while the first is still in progress.
# DynamoDB answers TransactionInProgressException, which is not retried.
# ---------------------------------------------------------------------------


async def test_transaction_in_progress_after_landing_raises(limiter, clock):
    repo = limiter._repository
    await limiter.set_limits("e", [CAL], resource="r")
    await _two_shards(limiter, repo, "e", "cal")
    clock[0] += 1
    client = await repo._get_client()
    real = client.transact_write_items
    state = {"armed": True}

    async def lossy(**kw):
        if state["armed"]:
            state["armed"] = False
            await real(**kw)  # landed
            raise ClientError(
                {"Error": {"Code": "TransactionInProgressException", "Message": "in progress"}},
                "TransactWriteItems",
            )
        return await real(**kw)

    client.transact_write_items = lossy
    raised = None
    try:
        await repo.top_up("e", "r", {"cal": 5})
    except ClientError as e:
        raised = e.response["Error"]["Code"]
    finally:
        client.transact_write_items = real
    tus = [_n(await _item(repo, "e", s), "b_cal_tu") or 0 for s in range(2)]
    print("\nraised:", raised, "tu per shard:", tus)
    assert raised is None, f"top_up raised {raised} although its write landed (tu={tus})"


# ---------------------------------------------------------------------------
# P3: cost — every refund on a topped-up quota forces a vu reset write and a
# slow pass on the next acquire, for the rest of the period.
# ---------------------------------------------------------------------------


async def test_refunds_on_topped_up_quota_force_slow_passes(limiter, clock):
    repo = limiter._repository
    big = Limit.quota("cal", 1000, cron="0 0 * * *")
    await limiter.set_limits("e", [big], resource="r")
    assert await _try(limiter, "e", {"cal": 1})
    await repo.top_up("e", "r", {"cal": 500})
    clock[0] += 1
    assert await _try(limiter, "e", {"cal": 1})  # forced pass clears vu
    calls = {"vu_reset": 0, "slow": 0}
    real_build = repo.build_vu_reset

    def counting(*a, **k):
        calls["vu_reset"] += 1
        return real_build(*a, **k)

    repo.build_vu_reset = counting
    real_batch = repo.batch_get_entity_and_buckets

    async def counting_batch(*a, **k):
        calls["slow"] += 1
        return await real_batch(*a, **k)

    repo.batch_get_entity_and_buckets = counting_batch
    for _ in range(10):
        clock[0] += 1
        async with limiter.acquire("e", "r", {"cal": 10}) as lease:
            await lease.adjust(cal=-5)  # estimate 10, used 5
    print("\n10 leases with a refund on a topped-up quota:", calls)
    assert calls["vu_reset"] == 0, f"{calls}"


async def test_refunds_baseline_without_top_up(limiter, clock):
    repo = limiter._repository
    big = Limit.quota("cal", 1000, cron="0 0 * * *")
    await limiter.set_limits("e", [big], resource="r")
    assert await _try(limiter, "e", {"cal": 1})
    calls = {"vu_reset": 0, "slow": 0}
    real_build = repo.build_vu_reset

    def counting(*a, **k):
        calls["vu_reset"] += 1
        return real_build(*a, **k)

    repo.build_vu_reset = counting
    real_batch = repo.batch_get_entity_and_buckets

    async def counting_batch(*a, **k):
        calls["slow"] += 1
        return await real_batch(*a, **k)

    repo.batch_get_entity_and_buckets = counting_batch
    for _ in range(10):
        clock[0] += 1
        async with limiter.acquire("e", "r", {"cal": 10}) as lease:
            await lease.adjust(cal=-5)
    print("\nbaseline:", calls)


# ---------------------------------------------------------------------------
# P4: session reset on a sharded bucket, then spend everything.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spent_first", [True, False])
async def test_session_reset_sharded_total(limiter, clock, spent_first):
    repo = limiter._repository
    await limiter.set_limits("e", [SES], resource="r")
    await _two_shards(limiter, repo, "e", "ses")
    clock[0] += 1
    before = 0
    if spent_first:
        before = await _drain(limiter, "e", "ses")
    clock[0] += 1
    res = await repo.reset_bucket("e", "r")
    print("\nreset result:", res)
    avail = await limiter.check_availability("e", "r")
    print("check_availability after reset:", avail.available, avail.statuses[0].resets_at_ms)
    clock[0] += 1
    after = await _drain(limiter, "e", "ses")
    print("before:", before, "after reset:", after)
    assert after <= 10


async def test_session_check_availability_after_reset(limiter, clock):
    repo = limiter._repository
    await limiter.set_limits("e", [SES], resource="r")
    assert await _try(limiter, "e", {"ses": 10})
    clock[0] += 1
    await repo.reset_bucket("e", "r")
    avail = await limiter.check_availability("e", "r")
    print("\navailable after a session reset:", avail.available)
    assert avail.available == {"ses": 10}


# ---------------------------------------------------------------------------
# P5: session reset while a sibling carries a pending roll (fan-out landed,
# not yet applied), plus lost-lock retries on that sibling.
# ---------------------------------------------------------------------------


async def test_session_reset_with_pending_roll_on_sibling(limiter, clock):
    repo = limiter._repository
    slow = RateLimiter(repository=repo, speculative_writes=False)
    await limiter.set_limits("e", [SES], resource="r")
    await _two_shards(limiter, repo, "e", "ses")
    # Spend both shards (window W0)
    spent = 0
    for shard in (0, 1):
        _r.randrange, real = (lambda a, b=None, s=shard: s), _r.randrange
        try:
            while await _try(slow, "e", {"ses": 1}):
                spent += 1
        finally:
            _r.randrange = real
    print("\nW0 admitted:", spent)
    # W0 ends; shard 0 opens W1 and fans out to shard 1 (pending roll there)
    clock[0] += 3_600_000 + 1
    _r.randrange, real = (lambda a, b=None: 0), _r.randrange
    try:
        assert await _try(slow, "e", {"ses": 1})
    finally:
        _r.randrange = real
    i1 = await _item(repo, "e", 1)
    print("shard1 ws/wa:", _n(i1, "b_ses_ws"), _n(i1, "b_ses_wa"), "tk", _n(i1, "b_ses_tk"))
    clock[0] += 1
    res = await repo.reset_bucket("e", "r")
    i1 = await _item(repo, "e", 1)
    print("reset", res, "shard1 after reset tk", _n(i1, "b_ses_tk"), "ws", _n(i1, "b_ses_ws"))
    clock[0] += 1
    after = await _drain(limiter, "e", "ses")
    print("after reset:", after)
    assert after <= 10
