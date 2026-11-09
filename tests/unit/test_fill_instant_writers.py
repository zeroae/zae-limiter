"""Every other writer of a fill instant is exact (ADR-155, #820).

The fast path is #819's. This covers the rest: a positive adjustment is the
debit pair; a return moves the fill instant back with no read; a reset sets
``Z = t·ra``; a top-up is a read and a write pinned on it; the entity-level
parameter sync re-expresses every fill instant against the parameters it
writes; a schedule boundary is integrated window by window.
"""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import boto3
import pytest

from tests.fixtures.balances import balance_milli
from tests.fixtures.capacity import CapacityCounter, _counting_client
from tests.fixtures.sharding import pinned_shard
from tests.unit.test_fill_instant_fast_path import (
    P_RPM,
    RESOURCE,
    RPM,
    T0,
    TPM,
    Clock,
    _fi,
    _key,
    _raw,
    _set_fi,
    _warm,
)
from zae_limiter import (
    Limit,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    Repository,
    ScheduleEntry,
    schema,
)
from zae_limiter import fill_instant as fi
from zae_limiter import limiter as limiter_module
from zae_limiter.schema import BUCKET_FIELD_FI, bucket_attr

MIDNIGHT = 1_800_057_600_000  # 2027-01-15 00:00 UTC


@pytest.fixture
def clock(limiter) -> Clock:
    clock = Clock(T0)
    limiter._repository._now_ms = lambda: clock.now
    return clock


class TestAdjustments:
    async def test_a_positive_adjustment_on_a_full_bucket_records_max_z_t_plus_d(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}) as lease:
            clock.now = T0 + 3_600_000  # the call ran an hour: the bucket refilled
            await lease.adjust(rpm=40)
        # #817's reference: a debit at t records max(z, t) + d.
        assert await _fi(repo, "e1", "rpm") == fi.stamp(clock.now, P_RPM) + fi.span(40_000, P_RPM)

    async def test_a_failed_debit_is_retried_with_a_fresh_stamp(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        real = repo._update_item_retrying_conflict
        stamps: list[str] = []

        async def overtake_first(client, **kwargs):
            names = kwargs.get("ExpressionAttributeNames", {})
            if "#zf0" in names and "#disabled" not in names:  # the debit pair, not an admission
                stamps.append(kwargs["ExpressionAttributeValues"][":zs0"]["N"])
                if len(stamps) == 1:
                    await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + 50)
                    clock.now += 7
            return await real(client, **kwargs)

        repo._update_item_retrying_conflict = overtake_first
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}) as lease:
            clock.now = T0 + 3_600_000
            await lease.adjust(rpm=2)
        assert len(stamps) == 2 and stamps[0] != stamps[1]

    async def test_a_debit_that_cannot_land_is_raised_not_dropped(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        real = repo._update_item_retrying_conflict

        async def always_overtaken(client, **kwargs):
            names = kwargs.get("ExpressionAttributeNames", {})
            if "#zf0" in names and "#disabled" not in names:
                side_full = "#zf0 < :zs0" in kwargs["ConditionExpression"]
                await _set_fi(
                    repo,
                    "e1",
                    "rpm",
                    fi.stamp(clock.now, P_RPM) + (50 if side_full else -50),
                )
            return await real(client, **kwargs)

        repo._update_item_retrying_conflict = always_overtaken
        with pytest.raises(RateLimiterUnavailable, match="could not land"):
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}) as lease:
                await lease.adjust(rpm=2)

    async def test_a_return_sends_no_read_and_never_lifts_above_capacity(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        clock.now = T0 + 3_600_000  # full again
        counter = CapacityCounter()
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 30}) as lease:
            with _counting_client(counter, limiter):
                await lease.release(rpm=30)
                await lease.adjust(rpm=-10)  # more than was debited
        assert counter.get_item == counter.query == 0
        assert counter.batch_get_item == []
        z = await _fi(repo, "e1", "rpm")
        assert fi.balance_milli(z, clock.now, P_RPM) == 100_000  # capped for free


class TestResetAndTopUp:
    async def test_a_clock_behind_xi_never_moves_it_back(self, limiter, clock) -> None:
        """A reset sent from a clock behind the item's `xi` keeps `xi`: every fill
        instant it writes is expressed at `max(now, xi)`, and an `xi` moved back
        would have the next reader re-apply the boundaries in between (#821)."""
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        clock.now = T0 + 5_000
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 30}, limits=[RPM]):
            pass
        xi = int((await _raw(repo, "e1"))["xi"]["N"])
        clock.now = xi - 2_000  # this process's clock lags the writer's
        await repo.reset_bucket("e1", RESOURCE)
        assert int((await _raw(repo, "e1"))["xi"]["N"]) == xi

    async def test_a_reset_of_both_forms_is_one_transaction(self, limiter, clock) -> None:
        repo = limiter._repository
        quota = Limit.quota("rpd", 50, cron="0 0 * * *")
        await _warm(limiter, "e1", [RPM, quota])
        async with limiter.acquire("e1", RESOURCE, consume={"rpd": 5}):
            pass
        sent: list[list[dict[str, Any]]] = []
        real = repo.transact_write

        async def spy(items, *args, **kwargs):
            sent.append(items)
            return await real(items, *args, **kwargs)

        repo.transact_write = spy
        clock.now = T0 + 1_000
        await repo.reset_bucket("e1", RESOURCE)
        assert len(sent) == 1
        update = sent[0][0]["Update"]
        assert BUCKET_FIELD_FI in update["ExpressionAttributeNames"]["#of0"]
        assert update["ExpressionAttributeNames"]["#ot0"].endswith("_tk")
        assert {next(iter(i)) for i in sent[0]} == {"Update", "Put"}  # shard, record, audit
        item = await _raw(repo, "e1")
        assert int(item[bucket_attr("rpm", BUCKET_FIELD_FI)]["N"]) == fi.reset(clock.now, P_RPM)
        assert int(item["b_rpd_tk"]["N"]) == 50_000

    async def test_a_top_up_is_pinned_and_retried_when_overtaken(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        clock.now = T0 + 1_000
        # 60 of 100 spent: room for 40.
        await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + fi.span(60_000, P_RPM))
        real = repo.transact_write
        calls = 0

        async def overtake_once(items, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:  # another consumer's admission lands first
                await _set_fi(
                    repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + fi.span(70_000, P_RPM)
                )
            return await real(items, *args, **kwargs)

        repo.transact_write = overtake_once
        result = await repo.top_up("e1", RESOURCE, {"rpm": 25})
        assert calls == 2
        assert result.amounts == {"rpm": 25}
        z = await _fi(repo, "e1", "rpm")
        assert z == fi.stamp(clock.now, P_RPM) + fi.span(45_000, P_RPM)  # not a lost update

    async def test_a_top_up_records_each_shards_split(self, limiter, clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        clock.now = T0 + 1_000
        await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + fi.span(60_000, P_RPM))
        await repo.top_up("e1", RESOURCE, {"rpm": 500}, idempotency_key="k1")
        client = await repo._get_client()
        record = await client.get_item(
            TableName=repo.table_name,
            Key={"PK": _key(repo, "e1")["PK"], "SK": {"S": schema.sk_operation("k1")}},
        )
        assert record["Item"]["splits"] == {"M": {"rpm": {"M": {"0": {"N": "60"}}}}}


class TestParamSync:
    async def _lower(self, limiter, clock) -> tuple[Any, int]:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        clock.now = T0 + 1_000
        # 30 of 100 left.
        z = fi.stamp(clock.now, P_RPM) + fi.span(70_000, P_RPM)
        await _set_fi(repo, "e1", "rpm", z)
        return repo, z

    async def test_lowering_capacity_re_expresses_the_fill_instant(self, limiter, clock) -> None:
        repo, z = await self._lower(limiter, clock)
        await repo.set_limits("e1", [Limit.per_minute("rpm", 50)], resource=RESOURCE)
        half = fi.ShardParams(50_000, 50_000, 60_000, 1)
        assert await _fi(repo, "e1", "rpm") == fi.re_express(z, clock.now, P_RPM, half)
        assert fi.balance_milli(await _fi(repo, "e1", "rpm"), clock.now, half) == 30_000

    async def test_a_write_in_between_is_retried_not_lost(self, limiter, clock) -> None:
        repo, z = await self._lower(limiter, clock)
        client = await repo._get_client()
        real = client.update_item
        landed: list[int] = []

        async def overtake(**kwargs):
            if "#yxi" in kwargs.get("ExpressionAttributeNames", {}) and not landed:
                # A fast-path admission of 10 lands between the read and the write.
                landed.append(z + fi.span(10_000, P_RPM))
                await real(
                    TableName=repo.table_name,
                    Key=_key(repo, "e1"),
                    UpdateExpression="SET #f = :f",
                    ExpressionAttributeNames={"#f": bucket_attr("rpm", BUCKET_FIELD_FI)},
                    ExpressionAttributeValues={":f": {"N": str(landed[0])}},
                )
            return await real(**kwargs)

        client.update_item = overtake
        try:
            await repo.set_limits("e1", [Limit.per_minute("rpm", 50)], resource=RESOURCE)
        finally:
            client.update_item = real
        half = fi.ShardParams(50_000, 50_000, 60_000, 1)
        assert await _fi(repo, "e1", "rpm") == fi.re_express(landed[0], clock.now, P_RPM, half)

    async def test_the_provisioner_sync_re_expresses_and_retries(
        self, limiter, clock, monkeypatch
    ) -> None:
        from zae_limiter_provisioner import bucket_sync

        repo, z = await self._lower(limiter, clock)
        monkeypatch.setattr(bucket_sync, "read_clock_ms", lambda: clock.now)
        client = boto3.client("dynamodb", region_name="us-east-1")
        real = client.update_item
        landed: list[int] = []

        def overtake(**kwargs):
            if "#yxi" in kwargs.get("ExpressionAttributeNames", {}) and not landed:
                landed.append(z + fi.span(10_000, P_RPM))
                real(
                    TableName=repo.table_name,
                    Key=_key(repo, "e1"),
                    UpdateExpression="SET #f = :f",
                    ExpressionAttributeNames={"#f": bucket_attr("rpm", BUCKET_FIELD_FI)},
                    ExpressionAttributeValues={":f": {"N": str(landed[0])}},
                )
            return real(**kwargs)

        monkeypatch.setattr(client, "update_item", overtake)
        written = bucket_sync.sync_bucket_params(
            client,
            repo.table_name,
            repo._namespace_id,
            "e1",
            RESOURCE,
            {"rpm": {"capacity": 50, "refill_amount": 50, "refill_period": 60}},
            0,
            None,
            clock.now,
        )
        assert written >= 1
        half = fi.ShardParams(50_000, 50_000, 60_000, 1)
        assert await _fi(repo, "e1", "rpm") == fi.re_express(landed[0], clock.now, P_RPM, half)


class TestSchedules:
    async def test_a_raise_at_a_boundary_accrues_from_there(self, limiter, clock) -> None:
        """0.1x until 01:00, then 1x: the strict bucket admits 116, never 1000."""
        repo = limiter._repository
        scheduled = Limit.per_minute("rpm", 1000).with_schedule(
            (ScheduleEntry(cron="* 0 * * *", scale=0.1),)
        )
        await repo.set_limits("e1", [scheduled], resource=RESOURCE)
        clock.now = MIDNIGHT + 59 * 60_000  # 00:59, cap 100 at 100/min
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 100}):
            pass  # empty at 00:59
        clock.now = MIDNIGHT + 60 * 60_000 + 1_000  # 01:00:01
        admitted = 0
        while admitted < 2_000:
            try:
                async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
                    admitted += 1
            except Exception:
                break
        # 100 refilled at 0.1x by 01:00, then 1 s at 1000/min.
        assert admitted == 116


class TestNoLegacyWriters:
    async def test_a_limit_configured_later_starts_full_on_every_shard(
        self, limiter, clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        # Shard 1 exists before the new limit does: a raise created it (ADR-156).
        assert await repo.raise_shards("e1", RESOURCE, 1) == 2
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await repo.set_limits("e1", [RPM, TPM], resource=RESOURCE)
        clock.now = T0 + 1_000
        p = fi.ShardParams(10_000_000, 10_000_000, 60_000, 2)
        client = await repo._get_client()
        for shard in (0, 1):
            with pinned_shard(shard):
                async with slow.acquire("e1", RESOURCE, consume={"tpm": 0}):
                    pass
            item = (
                await client.get_item(TableName=repo.table_name, Key=_key(repo, "e1", shard=shard))
            )["Item"]
            assert int(item[bucket_attr("tpm", BUCKET_FIELD_FI)]["N"]) == fi.created_full(
                clock.now, p
            )
            assert bucket_attr("tpm", "tk") not in item

    async def test_no_tk_builder_ever_sees_a_dripping_limit(self, limiter, clock) -> None:
        repo = limiter._repository
        dripping: set[str] = {"rpm", "tpm", schema.WCU_LIMIT_NAME}
        real_normal = repo.build_composite_normal

        def normal(*args, **kwargs):
            consumed = kwargs.get("consumed") or (args[2] if len(args) > 2 else {})
            refill = kwargs.get("refill_amounts") or (args[3] if len(args) > 3 else {})
            assert not dripping & set(consumed), consumed
            assert not dripping & set(refill), refill
            for state in (kwargs.get("seeds") or {}).values():
                assert state.fill_instant is not None, state.limit_name
            return real_normal(*args, **kwargs)

        def retry(*args, **kwargs):
            raise AssertionError("the consumption-only retry wrote a dripping limit")

        repo.build_composite_normal = normal
        repo.build_composite_retry = retry
        slow = RateLimiter(repository=repo, speculative_writes=False)
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        for at in (T0, T0 + 1_000):
            clock.now = at
            async with slow.acquire("e1", RESOURCE, consume={"rpm": 1}):
                pass
        await repo.set_limits("e1", [RPM, TPM], resource=RESOURCE)  # a seed
        clock.now = T0 + 2_000
        async with slow.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 1}):
            pass


class TestBuildersPinXiAndNeverVu:
    def test_every_dripping_builder(self, limiter) -> None:
        from zae_limiter import fill_sync
        from zae_limiter.bucket_ops import ShardWrite
        from zae_limiter.models import BucketState
        from zae_limiter.repository_protocol import FillInstantWrite

        repo = limiter._repository
        state = BucketState.from_limit("e1", RESOURCE, RPM, T0)
        state.fill_instant, state.expressed_ms = fi.stamp(T0, P_RPM), T0
        state.stored_fill_instant, state.stored_expressed_ms = state.fill_instant, T0
        conditions = {
            "fill write": repo.build_fill_write(
                "e1",
                RESOURCE,
                0,
                {"rpm": (fi.full_write(T0, 1_000, P_RPM), state)},
                {"rpm": 1_000},
                {},
                now_ms=T0,
                ttl_seconds=None,
                expressed_ms=T0,
                shard_count=1,
                item_sched=(None, None),
            )["ConditionExpression"],
            "return": repo.build_fill_return(
                "e1", RESOURCE, 0, {"rpm": (1_000, state)}, expressed_ms=T0, shard_count=1
            )["ConditionExpression"],
            "slow path": repo.build_composite_normal(
                "e1",
                RESOURCE,
                {},
                {},
                T0,
                T0,
                fill_instants={
                    "rpm": FillInstantWrite(
                        fill_instant=state.fill_instant + 1,
                        consumed_milli=0,
                        read_fill_instant=state.fill_instant,
                        read_tokens_milli=None,
                        capacity_milli=100_000,
                        refill_amount_milli=100_000,
                        refill_period_ms=60_000,
                    )
                },
                expressed=(T0, T0),
            )["Update"]["ConditionExpression"],
            "operation": repo.build_bucket_operation(
                "e1",
                RESOURCE,
                ShardWrite(
                    shard_id=0,
                    shard_count=1,
                    shard_count_stored=True,
                    expected_rf=T0,
                    written_rf=T0 + 1,
                    fill_sets={"rpm": (state.fill_instant + 1, state)},
                    expressed=(T0, T0),
                ),
            )["Update"]["ConditionExpression"],
        }
        item = {
            "PK": {"S": "x"},
            "xi": {"N": str(T0)},
            "shard_count": {"N": "1"},
            "b_rpm_fi": {"N": str(state.fill_instant)},
            "b_rpm_cp": {"N": "100000"},
            "b_rpm_ra": {"N": "100000"},
            "b_rpm_rp": {"N": "60000"},
        }
        extended = fill_sync.reexpression(
            item,
            "SET #c = :c",
            {"#c": "b_rpm_cp"},
            {":c": {"N": "50000"}},
            T0,
        )
        assert extended is not None
        conditions["param sync"] = extended[3]
        for builder, condition in conditions.items():
            assert "xi" in condition or "#zxi" in condition or "#oxi" in condition, builder
            assert "#vu" not in condition, builder


class TestEdges:
    async def test_the_slow_paths_pair_is_unbounded_for_a_soft_limit(self, limiter, clock) -> None:
        repo = limiter._repository
        soft_tpm = Limit.per_minute("tpm", 10, soft=True)
        await repo.set_limits("e1", [RPM, soft_tpm], resource=RESOURCE)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 1}):
            pass
        sent: list[dict[str, Any]] = []
        real = repo.transact_write

        async def spy(items, *args, **kwargs):
            sent.extend(items)
            return await real(items, *args, **kwargs)

        repo.transact_write = spy
        clock.now = T0 + 1_000
        async with slow.acquire("e1", RESOURCE, consume={"rpm": 1, "tpm": 30}):
            pass
        condition = sent[0]["Update"]["ConditionExpression"]
        assert " >= :zs" in condition  # the soft limit's unbounded not-full write
        # The hard limit, full again a second later, takes the full write.
        assert " < :zs" in condition and "#zf0 = :zf0" not in condition

    async def test_a_debit_across_a_boundary_re_expresses_first(self, limiter, clock) -> None:
        repo = limiter._repository
        scheduled = Limit.per_minute("rpm", 100).with_schedule(
            (ScheduleEntry(cron="* 0 * * *", scale=0.5),)
        )
        await repo.set_limits("e1", [scheduled], resource=RESOURCE)
        clock.now = MIDNIGHT - 120_000
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}) as lease:
            clock.now = MIDNIGHT + 60_000  # into the 0.5x window
            repo._fill_images.clear()  # and read the item
            await lease.adjust(rpm=5)
        item = await _raw(repo, "e1")
        assert int(item["xi"]["N"]) == MIDNIGHT + 60_000
        half = fi.params_at(clock.now, 100_000, 100_000, 60_000, scheduled.schedule, 1)
        assert (
            fi.balance_milli(int(item[bucket_attr("rpm", BUCKET_FIELD_FI)]["N"]), clock.now, half)
            == 50_000 - 5_000
        )

    async def test_a_re_expression_overtaken_reads_the_truth(self, limiter, clock) -> None:
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        item = await _raw(repo, "e1")
        stale = {**item, "xi": {"N": str(T0 - 5)}}
        current = await repo._reexpress_item("e1", RESOURCE, 0, stale, T0 + 10)
        assert current is not None and current["xi"] == item["xi"]  # the failure's ALL_OLD

        async def broken(client, **kwargs):
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "x"}}, "Update")

        repo._update_item_retrying_conflict = broken
        with pytest.raises(ClientError):
            await repo._reexpress_item("e1", RESOURCE, 0, item, T0 + 10)

    async def test_a_sync_that_keeps_losing_is_raised(self, limiter, clock, monkeypatch) -> None:
        from zae_limiter_provisioner import bucket_sync

        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        client = await repo._get_client()
        real = client.update_item

        async def always_overtaken(**kwargs):
            if "#yxi" in kwargs.get("ExpressionAttributeNames", {}):
                await _set_fi(repo, "e1", "rpm", fi.stamp(clock.now, P_RPM) + clock.now % 97 + 1)
                clock.now += 1
            return await real(**kwargs)

        client.update_item = always_overtaken
        try:
            with pytest.raises(RateLimiterUnavailable, match="kept changing"):
                await repo._sync_one_bucket_shard(
                    _key(repo, "e1")["PK"]["S"],
                    "SET #c = :c",
                    {"#c": "b_rpm_cp"},
                    {":c": {"N": "50000"}},
                )
        finally:
            client.update_item = real

        boto = boto3.client("dynamodb", region_name="us-east-1")
        boto_real = boto.update_item

        def boto_overtaken(**kwargs):
            if "#yxi" in kwargs.get("ExpressionAttributeNames", {}):
                boto_real(
                    TableName=repo.table_name,
                    Key=_key(repo, "e1"),
                    UpdateExpression="ADD #f :one",
                    ExpressionAttributeNames={"#f": bucket_attr("rpm", BUCKET_FIELD_FI)},
                    ExpressionAttributeValues={":one": {"N": "1"}},
                )
            return boto_real(**kwargs)

        monkeypatch.setattr(boto, "update_item", boto_overtaken)
        with pytest.raises(RuntimeError, match="kept changing"):
            bucket_sync._update_one_shard(
                boto,
                repo.table_name,
                _key(repo, "e1")["PK"]["S"],
                "SET #c = :c",
                {"#c": "b_rpm_cp"},
                {":c": {"N": "50000"}},
            )

    def test_an_operation_on_a_shard_with_no_xi_pins_its_absence(self, limiter) -> None:
        from zae_limiter.bucket_ops import ShardWrite
        from zae_limiter.models import BucketState

        state = BucketState.from_limit("e1", RESOURCE, RPM, T0)
        state.stored_fill_instant = 5
        update = limiter._repository.build_bucket_operation(
            "e1",
            RESOURCE,
            ShardWrite(
                shard_id=0,
                shard_count=1,
                shard_count_stored=True,
                expected_rf=T0,
                written_rf=T0 + 1,
                fill_sets={"rpm": (7, state)},
                expressed=(T0, None),
            ),
        )["Update"]
        assert "attribute_not_exists(#oxi)" in update["ConditionExpression"]


class TestFillSync:
    """`fill_sync`, the re-expression both parameter syncs share (#820)."""

    def _item(self, **extra: Any) -> dict[str, Any]:
        return {
            "PK": {"S": "x"},
            "xi": {"N": str(T0)},
            "shard_count": {"N": "1"},
            "b_rpm_fi": {"N": str(fi.stamp(T0, P_RPM))},
            "b_rpm_cp": {"N": "100000"},
            "b_rpm_ra": {"N": "100000"},
            "b_rpm_rp": {"N": "60000"},
            **extra,
        }

    def test_an_item_storing_no_fill_instant_needs_nothing(self) -> None:
        from zae_limiter import fill_sync

        item = {k: v for k, v in self._item().items() if k != "xi"}
        assert fill_sync.reexpression(item, "SET #c = :c", {"#c": "b_rpm_cp"}, {}, T0) is None

    @pytest.mark.parametrize("plan", ["ADD #c :c", "SET #c = #c + :c"])
    def test_a_plan_of_another_shape_is_refused(self, plan: str) -> None:
        from zae_limiter import fill_sync

        with pytest.raises(ValueError, match="not a parameter-sync"):
            fill_sync.apply_update(self._item(), plan, {"#c": "b_rpm_cp"}, {":c": {"N": "1"}})

    def test_a_limit_it_cannot_express_is_left_alone(self) -> None:
        from zae_limiter import fill_sync

        item = self._item(**{"b_old_fi": {"N": "5"}, "b_old_cp": {"N": "1"}})  # no ra/rp
        extended = fill_sync.reexpression(
            item, "SET #c = :c", {"#c": "b_rpm_cp"}, {":c": {"N": "50000"}}, T0
        )
        assert extended is not None
        assert "b_old_fi" not in extended[1].values()

    @pytest.mark.parametrize(
        ("override", "default"),
        [("-", None), (None, None), (None, "window")],
        ids=["none-marker", "unscheduled", "item-default"],
    )
    def test_each_limits_schedule_is_read_as_the_client_reads_it(
        self, override: str | None, default: str | None
    ) -> None:
        from zae_limiter import fill_sync
        from zae_limiter.schedule import encode

        window = (ScheduleEntry(cron="* 0 * * *", scale=0.5),)
        extra: dict[str, Any] = {
            "xi": {"N": str(MIDNIGHT - 60_000)},
            "b_rpm_fi": {"N": str(fi.stamp(MIDNIGHT - 60_000, P_RPM))},
            "b_wcu_fi": {
                "N": str(
                    fi.stamp(MIDNIGHT - 60_000, fi.ShardParams(1_000_000, 1_000_000, 1_000, 1))
                )
            },
            "b_wcu_cp": {"N": "1000000"},
            "b_wcu_ra": {"N": "1000000"},
            "b_wcu_rp": {"N": "1000"},
        }
        if override is not None:
            extra["b_rpm_sched"] = {"S": override}
        if default is not None:
            extra["sched"] = {"S": encode(window)[0]}
        item = self._item(**extra)
        extended = fill_sync.reexpression(
            item, "SET #c = :c", {"#c": "b_rpm_cp"}, {":c": {"N": "100000"}}, MIDNIGHT + 60_000
        )
        assert extended is not None
        assert {"b_rpm_fi", "b_wcu_fi"} <= set(extended[1].values())

    def test_a_fill_instant_the_plan_removes_is_not_set(self) -> None:
        from zae_limiter import fill_sync

        extended = fill_sync.reexpression(
            self._item(),
            "SET #c = :c REMOVE #f",
            {"#c": "b_rpm_cp", "#f": "b_rpm_fi"},
            {":c": {"N": "50000"}},
            T0,
        )
        assert extended is not None
        assert "#yf0" not in extended[1] and "REMOVE #f" in extended[0]


@contextmanager
def racing_commits(repo: Any) -> Iterator[None]:
    """Release every slow-path commit sent within 50 ms together.

    moto serialises requests (#656), so without this each pass reads after the
    previous one wrote and none ever loses. Here every pass of a round plans
    from the same read, and each round of re-plans races again, as concurrent
    callers do on a real table.
    """
    real = repo.transact_write
    window: dict[str, asyncio.Event] = {}

    async def gated(*args: Any, **kwargs: Any) -> Any:
        event = window.get("open")
        if event is None:
            event = window["open"] = asyncio.Event()

            async def release() -> None:
                await asyncio.sleep(0.05)
                del window["open"]
                event.set()

            asyncio.ensure_future(release())
        await event.wait()
        return await real(*args, **kwargs)

    with patch.object(repo, "transact_write", gated):
        yield


@pytest.fixture
def moving_clock(limiter) -> None:
    """A clock that moves with wall time from T0.

    A frozen clock hides the race: every pass then stamps the same instant.
    """
    base = time.monotonic()
    limiter._repository._now_ms = lambda: T0 + int((time.monotonic() - base) * 1000)


async def _consumed(repo: Any, entity_id: str, limit: str, resource: str = RESOURCE) -> int:
    item = await _raw(repo, entity_id, resource)
    return int(item[bucket_attr(limit, schema.BUCKET_FIELD_TC)]["N"])


async def _outcomes(calls: list[Any]) -> list[str]:
    async def one(call: Any) -> str:
        try:
            await call()
        except RateLimitExceeded as exc:
            return "contended" if exc.contended else "exhausted"
        return "admitted"

    return await asyncio.gather(*[one(call) for call in calls])


class TestConcurrentSlowPasses:
    """A slow pass that loses its fill-instant write retries through the pair.

    The slow pass's write is pinned on what it read, so concurrent passes on
    one bucket serialise. With no consumption-only retry for a fill instant
    (#818), eight of ten concurrent requests on a new bucket got the contended
    429; the LocalStack e2e's `test_concurrent_lease_acquisition` caught it.
    """

    @pytest.mark.parametrize("override", [False, True], ids=["config", "override"])
    async def test_a_burst_on_a_new_bucket_is_admitted(
        self, limiter, moving_clock, override
    ) -> None:
        repo = limiter._repository
        await limiter.create_entity("e1")
        if not override:
            await repo.set_limits("e1", [RPM], resource=RESOURCE)
        limits = [RPM] if override else None

        def call() -> Any:
            async def run() -> None:
                async with limiter.acquire("e1", RESOURCE, limits=limits, consume={"rpm": 5}):
                    pass

            return run

        with racing_commits(repo):
            outcomes = await _outcomes([call() for _ in range(10)])
        assert outcomes == ["admitted"] * 10
        assert await _consumed(repo, "e1", "rpm") == 50_000

    async def test_a_burst_across_a_schedule_boundary_is_admitted(self, limiter) -> None:
        # Every request reaching the first instant past a boundary takes the
        # slow path, which re-expresses the bucket.
        repo = limiter._repository
        windowed = Limit.per_minute("rpm", 100).with_schedule(
            (ScheduleEntry(cron="* 9-17 * * *", tz="UTC", scale=0.5),)
        )
        boundary = 1_800_003_600_000  # 2027-01-15 09:00 UTC, an hour after T0
        repo._now_ms = lambda: boundary - 1_000
        await _warm(limiter, "e1", [windowed])
        base = time.monotonic()
        repo._now_ms = lambda: boundary + int((time.monotonic() - base) * 1000)

        async def run() -> None:
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 3}):
                pass

        with racing_commits(repo):
            outcomes = await _outcomes([run for _ in range(10)])
        # 50 in the window: ten requests of 3 fit.
        assert outcomes == ["admitted"] * 10
        assert await _consumed(repo, "e1", "rpm") == 32_000  # _warm debited 2

    async def test_the_redo_never_over_admits(self, limiter, moving_clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)

        async def run() -> None:
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 30}):
                pass

        with racing_commits(repo):
            outcomes = await _outcomes([run for _ in range(6)])
        # 100 fit three requests of 30: the rest are rejected as exhausted,
        # not as contended, and never admitted.
        assert sorted(outcomes) == ["admitted"] * 3 + ["exhausted"] * 3
        assert await _consumed(repo, "e1", "rpm") == 90_000

    async def test_a_multi_resource_burst_is_admitted(self, limiter, moving_clock) -> None:
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await repo.set_limits("e1", [TPM], resource="budget")

        async def run() -> None:
            async with limiter.acquire(
                "e1", RESOURCE, consume={"rpm": 5}, also={"budget": {"tpm": 100}}
            ):
                pass

        with racing_commits(repo):
            outcomes = await _outcomes([run for _ in range(6)])
        assert outcomes == ["admitted"] * 6
        assert await _consumed(repo, "e1", "rpm") == 30_000
        assert await _consumed(repo, "e1", "tpm", "budget") == 600_000

    @pytest.mark.parametrize("multi", [False, True], ids=["single", "multi"])
    async def test_a_redo_that_cannot_decide_plans_the_pass_again(
        self, limiter, moving_clock, multi
    ) -> None:
        # The pre-redo behaviour: one re-plan, then the contended 429.
        repo = limiter._repository
        await repo.set_limits("e1", [RPM], resource=RESOURCE)
        await repo.set_limits("e1", [TPM], resource="budget")
        also = {"budget": {"tpm": 100}} if multi else None

        async def run() -> None:
            async with limiter.acquire("e1", RESOURCE, consume={"rpm": 5}, also=also):
                pass

        async def undecided(*args: Any, **kwargs: Any) -> None:
            return None

        with (
            racing_commits(repo),
            patch.object(limiter, "_redo_lost_fill_instants", undecided),
        ):
            outcomes = await _outcomes([run for _ in range(6)])
        assert "contended" in outcomes
        assert "exhausted" not in outcomes

    async def test_an_override_soft_limit_is_left_to_the_slow_path(self, limiter) -> None:
        soft = Limit.per_minute("rpm", 100, soft=True)
        assert (
            await limiter._redo_lost_fill_instants("e1", [(RESOURCE, {"rpm": 1})], [soft]) is None
        )

    async def test_no_redo_without_speculative_writes(self, limiter) -> None:
        # Those writes trust the item's owner stamps, which this caller asked
        # the slow path to check.
        limiter._speculative_writes = False
        assert await limiter._redo_lost_fill_instants("e1", [(RESOURCE, {"rpm": 1})], None) is None

    async def test_an_admission_the_override_forbids_is_refunded(
        self, limiter, moving_clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        before = await _fi(repo, "e1", "rpm")
        with patch.object(limiter_module, "_waived_against_override", lambda lease, limits: True):
            redo = await limiter._redo_lost_fill_instants("e1", [(RESOURCE, {"rpm": 1})], [RPM])
        assert redo is None
        assert await _fi(repo, "e1", "rpm") == before

    async def test_a_part_that_needs_the_slow_path_refunds_the_others(
        self, limiter, moving_clock
    ) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        before = await _fi(repo, "e1", "rpm")
        # "budget" has no bucket: its write reports it missing.
        redo = await limiter._redo_lost_fill_instants(
            "e1", [(RESOURCE, {"rpm": 1}), ("budget", {"tpm": 1})], None
        )
        assert redo is None
        assert await _fi(repo, "e1", "rpm") == before

    async def test_a_rejected_part_refunds_the_others(self, limiter, moving_clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        await _warm(limiter, "e1", [TPM], resource="budget")
        before = await _fi(repo, "e1", "rpm")
        with pytest.raises(RateLimitExceeded):
            await limiter._redo_lost_fill_instants(
                "e1", [(RESOURCE, {"rpm": 1}), ("budget", {"tpm": 20_000})], None
            )
        assert await _fi(repo, "e1", "rpm") == before

    async def test_a_cancelled_redo_refunds_what_landed(self, limiter, moving_clock) -> None:
        repo = limiter._repository
        await _warm(limiter, "e1", [RPM])
        await _warm(limiter, "e1", [TPM], resource="budget")
        before = await _fi(repo, "e1", "rpm")
        real = limiter._try_speculative_acquire

        async def cancel_budget(*args: Any, **kwargs: Any) -> Any:
            if kwargs["resource"] == "budget":
                await asyncio.sleep(0.01)  # let the other part land first
                raise asyncio.CancelledError
            return await real(*args, **kwargs)

        with (
            patch.object(limiter, "_try_speculative_acquire", cancel_budget),
            pytest.raises(asyncio.CancelledError),
        ):
            await limiter._redo_lost_fill_instants(
                "e1", [(RESOURCE, {"rpm": 1}), ("budget", {"tpm": 1})], None
            )
        assert await _fi(repo, "e1", "rpm") == before


# A quarter of the rate and the ceiling from minute 0 to 14 of every hour: T0 is
# the first instant of such a window.
QUARTER = (ScheduleEntry(cron="0-14 * * * *", tz="UTC", scale=0.25),)
WINDOWED = Limit.custom("rpm", 1000, refill_amount=1000, refill_period_seconds=600).with_schedule(
    QUARTER
)
AHEAD = T0 + 100  # a clock inside the window: the item's `xi`
BEHIND = T0 - 200  # a lagging clock, still in the minute before it


class TestALaggingClockAcrossABoundary:
    """A fill instant is scaled by the refill amount in force at its `xi`.

    A writer whose clock is before `xi`, across a schedule boundary, must read
    it with the parameters in force at `xi`, as every reader does
    (`bucket.fill_instant_at`). Read with those of its own clock, the stored
    value divides by another window's refill amount: here four times too far
    back, a full bucket at the unscaled ceiling (the #825 fuzz admitted 351
    after a reset granted 250).
    """

    @pytest.fixture
    async def lagging(self, limiter):
        repo = limiter._repository
        repo._now_ms = lambda: AHEAD
        await repo.set_limits("e1", [WINDOWED], resource=RESOURCE)
        async with limiter.acquire("e1", RESOURCE, consume={"rpm": 1}):
            pass  # created inside the window: 250 in force, 249 left
        other = await Repository.open(stack="test-rate-limits", config_cache_ttl=0)
        other._now_ms = lambda: BEHIND
        yield other
        await other.close()

    async def test_the_fast_path_reads_the_ceiling_in_force_at_xi(self, limiter, lagging) -> None:
        with pytest.raises(RateLimitExceeded):
            async with RateLimiter(repository=lagging).acquire(
                "e1", RESOURCE, consume={"rpm": 500}
            ):
                pass
        item = await _raw(limiter._repository, "e1")
        assert balance_milli(limiter._repository, item, "rpm", AHEAD) == 249_000

    async def test_a_debit_takes_the_parameters_in_force_at_xi(self, limiter, lagging) -> None:
        async with RateLimiter(repository=lagging).acquire(
            "e1", RESOURCE, consume={"rpm": 1}
        ) as lease:
            await lease.adjust(rpm=100)
        item = await _raw(limiter._repository, "e1")
        assert balance_milli(limiter._repository, item, "rpm", AHEAD) == 148_000

    async def test_a_reset_fills_to_the_ceiling_in_force_at_xi(self, limiter, lagging) -> None:
        await lagging.reset_bucket("e1", RESOURCE)
        item = await _raw(limiter._repository, "e1")
        assert balance_milli(limiter._repository, item, "rpm", AHEAD) == 250_000
