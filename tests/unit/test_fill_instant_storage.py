"""Dripping limits stored as fill instants on bucket items (ADR-154, #818).

A dripping limit, the reserved ``wcu`` included, stores its scaled fill instant
``b_{name}_fi`` in place of ``b_{name}_tk``, beside one item-level expression
instant ``xi``; a quota keeps ``tk``, ``rf`` and ``gc``. A legacy item is
converted on its next slow pass, by a write pinned on the ``tk`` and ``rf`` it
read. Writing either form is behind ``schema.WRITE_FILL_INSTANTS``, off outside
tests until #819-#821 teach every writer the form; reading is always on.

Not a sync-generation source: these drive the async limiter on moto, and the
sync twin of every path here is generated from the same modules.
"""

from fractions import Fraction
from typing import Any

import boto3
import pytest

from tests.fixtures.sharding import pinned_shard
from zae_limiter import (
    Limit,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    Repository,
    ScheduleEntry,
    __version__,
    schema,
)
from zae_limiter import fill_instant as fi
from zae_limiter.bucket import (
    adopt_schedule,
    balance_milli_at,
    credit_back,
    express_at,
    fill_instant_at,
    settle,
)
from zae_limiter.models import BucketState
from zae_limiter.repository_protocol import FillInstantWrite
from zae_limiter.schema import (
    BUCKET_FIELD_FI,
    BUCKET_FIELD_GC,
    BUCKET_FIELD_RF,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_XI,
    WCU_LIMIT_NAME,
    bucket_attr,
    pk_bucket,
    sk_state,
)
from zae_limiter.version import CURRENT_SCHEMA_VERSION
from zae_limiter_aggregator.processor import (
    ParsedBucketLimit,
    ParsedBucketRecord,
    _balance_milli,
    aggregate_bucket_states,
    extract_deltas,
    process_stream_records,
    propagate_shard_count,
)
from zae_limiter_provisioner.bucket_sync import build_bucket_param_update

RESOURCE = "gpt-4"
T0 = 1_800_000_000_000
RPM = Limit.per_minute("rpm", 100)
TPM = Limit.per_minute("tpm", 10_000)
RPD = Limit.quota("rpd", 50, cron="0 0 * * *")
WCU = fi.ShardParams(1_000_000, 1_000_000, 1_000, 1)


def rpm_params(shard_count: int = 1) -> fi.ShardParams:
    return fi.ShardParams(100_000, 100_000, 60_000, shard_count)


class Clock:
    def __init__(self, now: int) -> None:
        self.now = now


@pytest.fixture
def clock(limiter) -> Clock:
    clock = Clock(T0)
    limiter._repository._now_ms = lambda: clock.now
    return clock


@pytest.fixture
def switch(monkeypatch):
    """Turn the internal switch on or off for the rest of the test."""

    def _set(on: bool) -> None:
        monkeypatch.setattr(schema, "WRITE_FILL_INSTANTS", on)

    return _set


def _key(repo, entity_id: str, shard: int = 0) -> dict[str, Any]:
    return {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
        "SK": {"S": sk_state()},
    }


async def _raw(repo, entity_id: str, shard: int = 0) -> dict[str, Any]:
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key=_key(repo, entity_id, shard))
    return response.get("Item") or {}


def _n(item: dict[str, Any], limit_name: str, field: str) -> int:
    return int(item[bucket_attr(limit_name, field)]["N"])


async def _legacy_item(
    limiter, clock: Clock, switch, entity_id: str, tk: int, shard_count: int = 1
) -> None:
    """A bucket written before fill instants: ``rpm`` holds ``tk`` at ``rf = T0``."""
    repo = limiter._repository
    switch(False)
    await repo.set_limits(entity_id, [RPM, TPM], resource=RESOURCE)
    clock.now = T0
    async with limiter.acquire(entity_id, RESOURCE, consume={"tpm": 1}):
        pass
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key=_key(repo, entity_id),
        UpdateExpression="SET #tk = :tk, #rf = :rf, #sc = :sc",
        ExpressionAttributeNames={
            "#tk": bucket_attr("rpm", BUCKET_FIELD_TK),
            "#rf": BUCKET_FIELD_RF,
            "#sc": "shard_count",
        },
        ExpressionAttributeValues={
            ":tk": {"N": str(tk)},
            ":rf": {"N": str(T0)},
            ":sc": {"N": str(shard_count)},
        },
    )
    assert bucket_attr("rpm", BUCKET_FIELD_FI) not in await _raw(repo, entity_id)


def _strict(start_milli: Fraction, start_ms: int, events: list[tuple[int, int]], t: int) -> int:
    """The strict bucket's balance at ``t`` in millitokens, rounded down (one shard).

    Holds ``start_milli`` at ``start_ms`` (capped at capacity), refills at the
    limit's rate, and pays every admitted ``(instant, millitokens)`` in order.
    """
    capacity = Fraction(100_000)
    rate = Fraction(100_000, 60_000)  # millitokens per ms
    balance, last = min(capacity, start_milli), start_ms
    for at, amount in [*events, (t, 0)]:
        balance = min(capacity, balance + rate * (at - last)) - amount
        last = at
    return balance.__floor__()


async def _available(limiter, entity_id: str) -> int:
    availability = await limiter.check_availability(entity_id, RESOURCE)
    status = availability.status("rpm")
    assert status is not None
    return status.available


class TestTheSwitch:
    def test_it_is_off_outside_tests(self) -> None:
        # #821 removes it once every writer understands fill instants.
        assert schema.WRITE_FILL_INSTANTS is False

    async def test_off_a_bucket_is_created_as_tk(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        item = await _raw(repo, "e1")
        assert bucket_attr("rpm", BUCKET_FIELD_TK) in item
        assert bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK) in item
        assert not any(name.endswith(f"_{BUCKET_FIELD_FI}") for name in item)
        assert BUCKET_FIELD_XI not in item

    def test_the_schema_version_moved(self) -> None:
        assert CURRENT_SCHEMA_VERSION == "0.11.0"

    async def test_open_accepts_a_table_at_the_previous_schema(self, mock_dynamodb) -> None:
        # ADR-009's tracking refuses no 0.x client: conversion is lazy.
        setup = Repository(name="schema-0-10", region="us-east-1", _skip_deprecation_warning=True)
        await setup.create_table()
        await setup._register_namespace("default")
        await setup.set_version_record(schema_version="0.10.0", lambda_version=__version__)
        await setup.close()

        repo = await Repository.open(stack="schema-0-10", region="us-east-1")
        try:
            await repo.set_limits("e1", [RPM], resource=RESOURCE)
            async with RateLimiter(repository=repo).acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        finally:
            await repo.close()


class TestCreate:
    async def test_dripping_limits_and_wcu_are_stored_as_fill_instants(
        self, limiter, clock, switch
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM, RPD], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 10, "rpd": 1}):
            pass

        item = await _raw(repo, "e1")
        p = rpm_params()
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in item
        assert _n(item, "rpm", BUCKET_FIELD_FI) == fi.stamp(T0, p) + fi.span(10_000, p)
        assert bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK) not in item
        assert _n(item, WCU_LIMIT_NAME, BUCKET_FIELD_FI) == fi.created_full(T0, WCU)
        assert int(item[BUCKET_FIELD_XI]["N"]) == T0
        # The quota on the same item keeps its balance, `rf` and grant count.
        assert _n(item, "rpd", BUCKET_FIELD_TK) == 49_000
        assert int(item[BUCKET_FIELD_RF]["N"]) == T0
        assert _n(item, "rpd", BUCKET_FIELD_GC) == 1
        assert bucket_attr("rpd", BUCKET_FIELD_FI) not in item

    async def test_a_later_pass_moves_the_fill_instant_exactly(
        self, limiter, clock, switch
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 10}):
            pass
        clock.now = T0 + 3_000
        # The fast path's write cannot hold on an item with no `tk` (nothing
        # is written); the slow path writes the fill instant from its read.
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}):
            pass

        item = await _raw(repo, "e1")
        p = rpm_params()
        first = fi.stamp(T0, p) + fi.span(10_000, p)
        assert _n(item, "rpm", BUCKET_FIELD_FI) == max(first, fi.stamp(T0 + 3_000, p)) + fi.span(
            5_000, p
        )
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in item
        assert int(item[BUCKET_FIELD_XI]["N"]) == T0 + 3_000


class TestConversion:
    @pytest.mark.parametrize("shard_count", [1, 4])
    @pytest.mark.parametrize(
        "fraction",
        [Fraction(1), Fraction(2, 5), Fraction(5, 2), Fraction(-3, 10)],
        ids=["full", "partial", "surplus", "debt"],
    )
    async def test_the_next_slow_pass_converts_exactly(
        self, limiter, clock, switch, fraction: Fraction, shard_count: int
    ) -> None:
        repo = limiter._repository
        share = 100_000 // shard_count
        tk = int(share * fraction)
        await _legacy_item(limiter, clock, switch, "e1", tk, shard_count)
        before = await _raw(repo, "e1")
        wcu_tk = _n(before, WCU_LIMIT_NAME, BUCKET_FIELD_TK)

        switch(True)
        clock.now = T0 + 15_000
        slow = RateLimiter(repository=repo, speculative_writes=False)
        with pinned_shard(0):
            # `rpm` is not consumed, so its fill instant is the conversion alone.
            async with slow.acquire("e1", RESOURCE, consume={"tpm": 1}):
                pass

        item = await _raw(repo, "e1")
        assert _n(item, "rpm", BUCKET_FIELD_FI) == fi.from_tokens(tk, T0, rpm_params(shard_count))
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in item
        assert _n(item, WCU_LIMIT_NAME, BUCKET_FIELD_FI) == fi.from_tokens(wcu_tk, T0, WCU)
        assert bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK) not in item
        assert bucket_attr("tpm", BUCKET_FIELD_TK) not in item
        assert int(item[BUCKET_FIELD_XI]["N"]) == T0 + 15_000

    async def test_a_write_landing_in_between_fails_the_conversion(
        self, limiter, clock, switch
    ) -> None:
        repo = limiter._repository
        await _legacy_item(limiter, clock, switch, "e1", 40_000)
        client = await repo._get_client()
        real = repo.transact_write

        async def interfering(items, *args, **kwargs):
            # A fast-path debit lands between the pass's read and its write.
            await client.update_item(
                TableName=repo.table_name,
                Key=_key(repo, "e1"),
                UpdateExpression="ADD #tk :d",
                ExpressionAttributeNames={"#tk": bucket_attr("rpm", BUCKET_FIELD_TK)},
                ExpressionAttributeValues={":d": {"N": "-1000"}},
            )
            return await real(items, *args, **kwargs)

        repo.transact_write = interfering
        switch(True)
        clock.now = T0 + 15_000
        slow = RateLimiter(repository=repo, speculative_writes=False)
        with pytest.raises(RateLimitExceeded) as raised:
            async with slow.acquire("e1", RESOURCE, consume={"tpm": 1}):
                pass

        # Each attempt lost its pin, and the re-plan from a fresh read lost too:
        # nothing was written but the two interfering debits.
        assert raised.value.contended
        item = await _raw(repo, "e1")
        assert _n(item, "rpm", BUCKET_FIELD_TK) == 38_000
        assert not any(name.endswith(f"_{BUCKET_FIELD_FI}") for name in item)
        assert BUCKET_FIELD_XI not in item

    async def test_a_re_plan_converts_what_it_reads(self, limiter, clock, switch) -> None:
        repo = limiter._repository
        await _legacy_item(limiter, clock, switch, "e1", 40_000)
        client = await repo._get_client()
        real = repo.transact_write
        calls = 0

        async def interfering_once(items, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                await client.update_item(
                    TableName=repo.table_name,
                    Key=_key(repo, "e1"),
                    UpdateExpression="ADD #tk :d",
                    ExpressionAttributeNames={"#tk": bucket_attr("rpm", BUCKET_FIELD_TK)},
                    ExpressionAttributeValues={":d": {"N": "-1000"}},
                )
            return await real(items, *args, **kwargs)

        repo.transact_write = interfering_once
        switch(True)
        clock.now = T0 + 15_000
        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("e1", RESOURCE, consume={"tpm": 1}):
            pass

        item = await _raw(repo, "e1")
        assert _n(item, "rpm", BUCKET_FIELD_FI) == fi.from_tokens(39_000, T0, rpm_params())
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in item


class TestReaders:
    async def test_check_availability_after_conversion_matches_the_strict_bucket(
        self, limiter, clock, switch
    ) -> None:
        await _legacy_item(limiter, clock, switch, "e1", 40_000)
        switch(True)
        events: list[tuple[int, int]] = []
        for at, amount in [(15_000, 30), (21_500, 40), (22_250, 7)]:
            clock.now = T0 + at
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": amount}):
                pass
            events.append((T0 + at, amount * 1000))
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in await _raw(limiter._repository, "e1")

        for at in (22_250, 22_251, 23_000, 30_000, 47_777, 60_000, 400_000):
            clock.now = T0 + at
            expected = _strict(Fraction(40_000), T0, events, T0 + at) // 1000
            assert await _available(limiter, "e1") == expected, at

    async def test_a_rejection_is_decided_on_the_exact_balance(
        self, limiter, clock, switch
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 100}):
            pass
        # 600 ms refill 1 token; 599 ms do not.
        clock.now = T0 + 599
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        clock.now = T0 + 600
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        assert await _available(limiter, "e1") == 0

    async def test_a_tk_beside_a_fill_instant_is_ignored(self, limiter, clock, switch) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 100}):
            pass
        client = await repo._get_client()
        # An older writer's `tk`, for the limit and for `wcu`: the fast path's
        # balance terms would hold on it, so its `xi` guard must refuse.
        await client.update_item(
            TableName=repo.table_name,
            Key=_key(repo, "e1"),
            UpdateExpression="SET #tk = :tk, #wtk = :wtk",
            ExpressionAttributeNames={
                "#tk": bucket_attr("rpm", BUCKET_FIELD_TK),
                "#wtk": bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK),
            },
            ExpressionAttributeValues={":tk": {"N": "100000"}, ":wtk": {"N": "1000000"}},
        )
        assert await _available(limiter, "e1") == 0
        clock.now = T0 + 1
        with pytest.raises(RateLimitExceeded):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        assert _n(await _raw(repo, "e1"), "rpm", BUCKET_FIELD_TK) == 100_000


class TestGuards:
    """Writers still on `tk` never write it beside a fill instant (#818)."""

    async def _converted(self, limiter, clock, switch) -> Any:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 10}):
            pass
        return repo

    async def test_a_positive_adjustment_refuses(self, limiter, clock, switch) -> None:
        repo = await self._converted(limiter, clock, switch)
        clock.now = T0 + 1_000
        with pytest.raises(NotImplementedError, match="#820"):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}) as lease:
                await lease.adjust(rpm=5)
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in await _raw(repo, "e1")

    async def test_a_rollback_drops_its_credit(self, limiter, clock, switch) -> None:
        repo = await self._converted(limiter, clock, switch)
        clock.now = T0 + 1_000
        with pytest.raises(RuntimeError):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                raise RuntimeError("caller failed")
        item = await _raw(repo, "e1")
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in item
        p = rpm_params()
        after_first = fi.stamp(T0, p) + fi.span(10_000, p)
        expected = max(after_first, fi.stamp(T0 + 1_000, p)) + fi.span(1_000, p)
        assert _n(item, "rpm", BUCKET_FIELD_FI) == expected

    async def test_reset_refuses(self, limiter, clock, switch) -> None:
        repo = await self._converted(limiter, clock, switch)
        with pytest.raises(NotImplementedError, match="#820"):
            await repo.reset_bucket("e1", RESOURCE)

    async def test_an_operation_naming_only_a_quota_leaves_the_fill_instant(
        self, limiter, clock, switch
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM, RPD], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 10, "rpd": 5}):
            pass
        before = _n(await _raw(repo, "e1"), "rpm", BUCKET_FIELD_FI)
        await repo.reset_bucket("e1", RESOURCE, limits=["rpd"])
        await repo.top_up("e1", RESOURCE, {"rpd": 3})
        item = await _raw(repo, "e1")
        assert _n(item, "rpd", BUCKET_FIELD_TK) == 53_000
        assert _n(item, "rpm", BUCKET_FIELD_FI) == before
        assert bucket_attr("rpm", BUCKET_FIELD_TK) not in item

    async def test_the_aggregator_counts_usage_and_writes_nothing(
        self, limiter, clock, switch
    ) -> None:
        repo = await self._converted(limiter, clock, switch)
        old = await _raw(repo, "e1")
        clock.now = T0 + 1_000
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 3}):
            pass
        new = await _raw(repo, "e1")
        record = {"eventName": "MODIFY", "dynamodb": {"OldImage": old, "NewImage": new}}

        (state,) = aggregate_bucket_states([record]).values()
        assert state.stores_fill_instants
        (delta,) = extract_deltas(record)
        assert (delta.limit_name, delta.tokens_delta) == ("rpm", 3_000)

        result = process_stream_records([record], repo.table_name, windows=["hourly"])
        assert result.refills_written == 0
        assert await _raw(repo, "e1") == new

    async def test_the_aggregator_never_clones_a_fill_instant(self, limiter, clock, switch) -> None:
        repo = await self._converted(limiter, clock, switch)
        item = await _raw(repo, "e1")
        doubled = item | {"shard_count": {"N": "2"}}
        record = {"eventName": "MODIFY", "dynamodb": {"OldImage": item, "NewImage": doubled}}
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(repo.table_name)

        propagate_shard_count(table, record, T0)

        assert await _raw(repo, "e1", shard=1) == {}


class TestEdges:
    async def test_a_limit_added_later_is_seeded_as_a_fill_instant(
        self, limiter, clock, switch
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        await repo.set_limits("e1", [RPM, TPM], resource=RESOURCE)
        clock.now = T0 + 1_000
        async with limiter.acquire("e1", RESOURCE, consume={"tpm": 7}):
            pass
        item = await _raw(repo, "e1")
        p = fi.ShardParams(10_000_000, 10_000_000, 60_000, 1)
        assert _n(item, "tpm", BUCKET_FIELD_FI) == fi.stamp(T0 + 1_000, p) + fi.span(7_000, p)
        assert bucket_attr("tpm", BUCKET_FIELD_TK) not in item

    async def test_a_reset_beside_a_fill_instant_wcu_leaves_it_alone(
        self, limiter, clock, switch
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPD], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpd": 5}):
            pass
        wcu_before = _n(await _raw(repo, "e1"), WCU_LIMIT_NAME, BUCKET_FIELD_FI)
        await repo.reset_bucket("e1", RESOURCE)
        item = await _raw(repo, "e1")
        assert _n(item, "rpd", BUCKET_FIELD_TK) == 50_000
        assert _n(item, WCU_LIMIT_NAME, BUCKET_FIELD_FI) == wcu_before
        assert bucket_attr(WCU_LIMIT_NAME, BUCKET_FIELD_TK) not in item

    async def test_a_corrupt_fill_instant_is_unavailable(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        item = await _raw(repo, "e1")
        item[bucket_attr("rpm", BUCKET_FIELD_FI)] = {"N": "1.5"}
        with pytest.raises(RateLimiterUnavailable, match="could not be decoded"):
            repo._deserialize_composite_bucket(item)

    async def test_a_fill_instant_on_a_quota_is_ignored(self, limiter, clock) -> None:
        # Never an unreadable item: a quota's balance is its `tk`.
        repo = limiter._repository
        await repo.set_limits("e1", [RPD], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpd": 1}):
            pass
        item = await _raw(repo, "e1")
        item[bucket_attr("rpd", BUCKET_FIELD_FI)] = {"N": "100"}
        (rpd,) = [s for s in repo._deserialize_composite_bucket(item) if s.limit_name == "rpd"]
        assert (rpd.fill_instant, rpd.stored_fill_instant, rpd.tokens_milli) == (None, None, 49_000)

    @pytest.mark.parametrize("dropped", [True, False], ids=["stale", "now-a-quota"])
    async def test_the_param_sync_removes_a_fill_instant_it_cannot_keep(
        self, limiter, clock, switch, dropped: bool
    ) -> None:
        switch(True)
        repo = limiter._repository
        await repo.set_limits("e1", [RPM, TPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        if dropped:
            # Reconciled to the resource's defaults, which do not name `rpm`;
            # the first acquire cached the resource level as having none.
            await repo.set_resource_defaults(RESOURCE, [TPM])
            await repo.invalidate_config_cache()
            await limiter.delete_limits("e1", resource=RESOURCE)
        else:
            await repo.set_limits(
                "e1", [Limit.quota("rpm", 100, cron="0 0 * * *"), TPM], resource=RESOURCE
            )
        assert bucket_attr("rpm", BUCKET_FIELD_FI) not in await _raw(repo, "e1")

    def test_the_shard_count_pin_accepts_a_missing_count_of_one(self, limiter) -> None:
        def condition(pin: int) -> str:
            update = limiter._repository.build_composite_normal(
                "e1", RESOURCE, {}, {}, T0, T0, pin_shard_count_equal=pin
            )
            return str(update["Update"]["ConditionExpression"])

        assert "attribute_not_exists(#zsc) OR #zsc = :zsc" in condition(1)
        assert "attribute_not_exists(#zsc)" not in condition(4)

    async def test_a_corrupt_expression_instant_is_unavailable(self, limiter, clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass
        item = await _raw(repo, "e1") | {BUCKET_FIELD_XI: {"N": "x"}}
        with pytest.raises(RateLimiterUnavailable, match="could not be decoded"):
            repo._deserialize_composite_bucket(item)

    def test_a_conversion_needs_the_tk_it_read(self, limiter) -> None:
        write = FillInstantWrite(
            fill_instant=1,
            consumed_milli=0,
            read_fill_instant=None,
            read_tokens_milli=None,
            capacity_milli=100_000,
            refill_amount_milli=100_000,
            refill_period_ms=60_000,
        )
        with pytest.raises(ValueError, match="needs the tk it read"):
            limiter._repository.build_composite_normal(
                "e1", RESOURCE, {}, {}, T0, T0, fill_instants={"rpm": write}
            )


class TestProjection:
    """`bucket.py` reads a fill instant exactly, schedule windows included."""

    def _state(self, z: int, expressed_ms: int, sched=()) -> BucketState:
        state = BucketState.from_limit("e1", RESOURCE, RPM, T0)
        state.sched = sched
        state.fill_instant, state.expressed_ms = z, expressed_ms
        return state

    def test_a_state_without_a_fill_instant_has_none_to_project(self) -> None:
        with pytest.raises(ValueError, match="not stored as a fill instant"):
            fill_instant_at(BucketState.from_limit("e1", RESOURCE, RPM, T0), T0)

    def test_it_is_re_expressed_across_every_boundary(self) -> None:
        # 0.1x until 01:00 UTC, then the base rate: the strict bucket accrues
        # each window at its own rate, not the end rate for the whole span.
        midnight = 1_800_057_600_000  # 2027-01-15 00:00 UTC
        sched = (ScheduleEntry(cron="* 0 * * *", scale=0.1),)
        slow = fi.params_at(midnight, 100_000, 100_000, 60_000, sched, 1)
        empty = fi.created_empty(midnight, slow)
        state = self._state(empty, midnight, sched)
        later = midnight + 3_600_000 + 30_000
        expected, _ = fi.re_express_across(
            empty, midnight, later, 100_000, 100_000, 60_000, sched, 1
        )
        assert balance_milli_at(state, later) == fi.balance_milli(expected, later, rpm_params())
        # Refilled at 0.1x for the hour (10 tokens, capped there), then 30 s at
        # the base rate: 60 tokens, nowhere near the 100 of the end rate.
        assert balance_milli_at(state, later) == 60_000

    def test_an_in_memory_credit_is_capped_at_capacity(self) -> None:
        # A release or negative adjustment inside a lease (`Lease._apply_adjust`).
        p = rpm_params()
        state = self._state(fi.stamp(T0, p) + fi.span(30_000, p), T0)  # 70 tokens at T0
        settle(state, -20, T0)
        assert balance_milli_at(state, T0) == 90_000
        settle(state, -20, T0)
        assert balance_milli_at(state, T0) == 100_000
        assert state.tokens_milli == 100_000

    def test_a_new_schedule_keeps_the_balance_it_finds(self) -> None:
        # The slow path takes config's schedule over the item's: a fill instant
        # scaled under a 0.1x window is re-expressed, not read 10x too far.
        always = (ScheduleEntry(cron="* * * * *", scale=0.1),)
        slow = fi.params_at(T0, 100_000, 100_000, 60_000, always, 1)
        state = self._state(fi.stamp(T0, slow) + fi.span(4_000, slow), T0, always)
        assert balance_milli_at(state, T0) == 6_000
        adopt_schedule(state, (), T0)
        assert state.sched == ()
        assert balance_milli_at(state, T0) == 6_000
        assert balance_milli_at(state, T0 + 600) == 7_000  # the base rate from here

    def test_the_same_schedule_moves_nothing(self) -> None:
        p = rpm_params()
        state = self._state(fi.stamp(T0, p) + 7, T0)
        adopt_schedule(state, (), T0 + 5)
        assert (state.fill_instant, state.expressed_ms) == (fi.stamp(T0, p) + 7, T0)

    def test_express_at_moves_a_fill_instant_across_a_boundary(self) -> None:
        midnight = 1_800_057_600_000
        sched = (ScheduleEntry(cron="* 0 * * *", scale=0.1),)
        slow = fi.params_at(midnight, 100_000, 100_000, 60_000, sched, 1)
        state = self._state(fi.created_empty(midnight, slow), midnight, sched)
        later = midnight + 3_600_000 + 30_000
        before = balance_milli_at(state, later)
        express_at(state, later)
        assert state.expressed_ms == later
        assert balance_milli_at(state, later) == before
        legacy = BucketState.from_limit("e1", RESOURCE, RPM, T0)
        express_at(legacy, later)
        assert legacy.fill_instant is None

    def test_a_credit_back_undoes_the_debit_from_its_instant(self) -> None:
        p = rpm_params()
        state = self._state(fi.stamp(T0, p) - 5, T0)  # full since before T0
        settle(state, 30, T0 + 1_000)
        credit_back(state, 30)
        assert state.fill_instant == fi.stamp(T0 + 1_000, p)
        assert balance_milli_at(state, T0 + 1_000) == 100_000


class TestAggregatorBalance:
    def test_a_soft_limit_overdraw_is_read_off_the_fill_instant(self) -> None:
        p = rpm_params(2)
        record = ParsedBucketRecord(
            namespace_id="ns", entity_id="e1", resource=RESOURCE, rf_ms=T0, limits={}, shard_count=2
        )
        debt = ParsedBucketLimit(
            tc_delta=1_000,
            tk_milli=0,
            cp_milli=100_000,
            ra_milli=100_000,
            rp_ms=60_000,
            soft=True,
            fill_instant=fi.stamp(T0, p) + p.capacity_units + p.units_per_milli * 1_000,
        )
        assert _balance_milli("rpm", debt, record) == -1_000
        legacy = ParsedBucketLimit(
            tc_delta=1_000, tk_milli=-7, cp_milli=100_000, ra_milli=100_000, rp_ms=60_000
        )
        assert _balance_milli("rpm", legacy, record) == -7

    def test_it_reads_the_schedule_in_force(self) -> None:
        # Scaled by the half-rate window's `ra`; read against the base `ra` the
        # debt would look like a full bucket and the overdraw would be missed.
        half = (ScheduleEntry(cron="* * * * *", scale=0.5),)
        p = fi.params_at(T0, 100_000, 100_000, 60_000, half, 1)
        record = ParsedBucketRecord(
            namespace_id="ns", entity_id="e1", resource=RESOURCE, rf_ms=T0, limits={}, xi_ms=T0
        )
        debt = ParsedBucketLimit(
            tc_delta=1_000,
            tk_milli=0,
            cp_milli=100_000,
            ra_milli=100_000,
            rp_ms=60_000,
            sched=half,
            soft=True,
            fill_instant=fi.stamp(T0, p) + p.capacity_units + p.units_per_milli * 1_000,
        )
        assert _balance_milli("rpm", debt, record) == -1_000


class TestProvisionerSync:
    def test_it_removes_the_fill_instant_of_a_stale_limit_and_a_quota(self) -> None:
        expression, names, _values = build_bucket_param_update(
            {
                "tpm": {"capacity": 10_000, "refill_amount": 10_000, "refill_period": 60},
                "rpd": {
                    "capacity": 50,
                    "refill_amount": 0,
                    "refill_period": 60,
                    "reset_schedule": [{"cron": "0 0 * * *", "tz": "UTC"}],
                },
            },
            None,
            {"rpm"},
            T0,
        )
        removed = expression.split(" REMOVE ", 1)[1].split(", ")
        removed_attrs = {names[alias] for alias in removed}
        assert bucket_attr("rpm", BUCKET_FIELD_FI) in removed_attrs
        assert bucket_attr("rpd", BUCKET_FIELD_FI) in removed_attrs
        assert bucket_attr("tpm", BUCKET_FIELD_FI) not in removed_attrs
