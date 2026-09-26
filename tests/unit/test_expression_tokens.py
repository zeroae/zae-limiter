"""Every bucket write expression is safe for every legal limit name (#634).

`models.NAME_PATTERN` accepts `.` and `-` in a limit name. Neither is legal in
an `ExpressionAttributeNames` key or an `ExpressionAttributeValues` placeholder,
and `.` in expression text parses as a document-path separator. So a write
whose tokens are built from the name is rejected by DynamoDB (and by moto)
with a `ValidationException`, on every path except the create `Put`.

This file builds every bucket write the library issues — the fast path, the
three composite builders, the aggregator refill, and (as regressions) the
reclaim, param-sync and disable fan-out writes that were already positional —
for limits named `rpm.v2` and `req-min`, and checks each against the token
rules and against its own declarations.
"""

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter import Limit, RateLimiter, Repository
from zae_limiter.schedule import ScheduleEntry
from zae_limiter.schema import BUCKET_FIELD_TK, bucket_attr, pk_bucket
from zae_limiter_aggregator.processor import (
    BucketRefillState,
    LimitRefillInfo,
    _reclaim_quota_surplus,
    try_refill_bucket,
)
from zae_limiter_provisioner.bucket_sync import build_bucket_param_update

from .conftest import _setup_moto_table

DOTTED = "rpm.v2"
HYPHENATED = "req-min"
NAMES = (DOTTED, HYPHENATED)

_NAME_KEY = re.compile(r"#[A-Za-z0-9_]+")
_VALUE_KEY = re.compile(r":[A-Za-z0-9_]+")
_EXPRESSION_FIELDS = (
    "UpdateExpression",
    "ConditionExpression",
    "ProjectionExpression",
    "KeyConditionExpression",
    "FilterExpression",
)


def assert_expression_safe(kwargs: dict[str, Any]) -> None:
    """Every token is legal, declared, and used; no raw limit name in the text."""
    names: dict[str, str] = kwargs.get("ExpressionAttributeNames", {})
    values: dict[str, Any] = kwargs.get("ExpressionAttributeValues", {})
    for key in names:
        assert _NAME_KEY.fullmatch(key), f"illegal attribute-name key {key!r}"
    for key in values:
        assert _VALUE_KEY.fullmatch(key), f"illegal attribute-value key {key!r}"

    text = " ".join(kwargs.get(field, "") for field in _EXPRESSION_FIELDS)
    for name in NAMES:
        # A raw name in the text is a path DynamoDB cannot parse, aliased or not.
        assert name not in text, f"raw limit name {name!r} in {text!r}"

    used_names = set(_NAME_KEY.findall(text))
    used_values = set(_VALUE_KEY.findall(text))
    # Undeclared tokens fail with ValidationException; so do unused ones.
    assert used_names == set(names), (used_names, set(names))
    assert used_values == set(values), (used_values, set(values))


def _repo() -> Repository:
    return Repository(name="tokens", region="us-east-1", _skip_deprecation_warning=True)


class TestTheCheckItself:
    """The helper must reject exactly the shapes #634 produced on main."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            # build_composite_* on main: alias key carries the name
            {
                "UpdateExpression": "ADD #b_rpm.v2_tk :d",
                "ExpressionAttributeNames": {"#b_rpm.v2_tk": "b_rpm.v2_tk"},
                "ExpressionAttributeValues": {":d": {"N": "1"}},
            },
            # fast path on main: placeholder carries the name
            {
                "UpdateExpression": "ADD #t :neg_req-min",
                "ExpressionAttributeNames": {"#t": "b_req-min_tk"},
                "ExpressionAttributeValues": {":neg_req-min": {"N": "1"}},
            },
            # aggregator on main: raw inline path
            {
                "UpdateExpression": "ADD b_rpm.v2_tk :rd0",
                "ExpressionAttributeValues": {":rd0": 1},
            },
            # declared but unused
            {
                "UpdateExpression": "ADD #a :b",
                "ExpressionAttributeNames": {"#a": "x", "#unused": "y"},
                "ExpressionAttributeValues": {":b": 1},
            },
        ],
    )
    def test_rejects(self, kwargs: dict[str, Any]) -> None:
        with pytest.raises(AssertionError):
            assert_expression_safe(kwargs)


class TestCompositeBuilders:
    """Slow path (normal, retry) and adjust/rollback, pure builders."""

    def test_normal(self) -> None:
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000, HYPHENATED: 2000, "rpm": 0},
            refill_amounts={DOTTED: 0, HYPHENATED: 500},
            now_ms=2_000,
            expected_rf=1_000,
        )["Update"]
        assert_expression_safe(update)
        names = update["ExpressionAttributeNames"]
        assert {names["#bt0"], names["#bt1"]} == {
            bucket_attr(DOTTED, BUCKET_FIELD_TK),
            bucket_attr(HYPHENATED, BUCKET_FIELD_TK),
        }

    def test_normal_with_every_optional_clause(self) -> None:
        """Windows, window lengths, ttl and vu share the expression without
        colliding with the per-limit tokens."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000, HYPHENATED: 2000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            ttl_seconds=60,
            vu=9_000,
            windows={DOTTED: (1_500, 3600)},
            window_lengths={HYPHENATED: 60},
        )["Update"]
        assert_expression_safe(update)

    def test_normal_removing_ttl_and_vu(self) -> None:
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1, HYPHENATED: 1},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            ttl_seconds=0,
            clear_vu=True,
        )["Update"]
        assert_expression_safe(update)

    def test_retry(self) -> None:
        update = _repo().build_composite_retry(
            "user-1", "api", consumed={DOTTED: 1000, HYPHENATED: 2000}
        )["Update"]
        assert_expression_safe(update)

    @staticmethod
    def _seed_states() -> dict:
        """A scheduled rate limit, a calendar quota and a session quota, all
        missing from an existing item (#633)."""
        from datetime import timedelta

        from zae_limiter.models import BucketState

        limits = [
            Limit.per_minute(DOTTED, 100).with_schedule(
                (ScheduleEntry(cron="* 9-17 * * *", scale=0.5),)
            ),
            Limit.quota(HYPHENATED, 1000, cron="0 0 * * *"),
            Limit.quota("sess.v1", 10, reset_after=timedelta(hours=5)),
        ]
        return {
            limit.name: BucketState.from_limit("user-1", "api", limit, 2_000) for limit in limits
        }

    def test_normal_with_seeds(self) -> None:
        """#633: the seed branch shares one expression with the ADD branch,
        the windows and the lock without a token collision."""
        seeds = self._seed_states()
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={"rpm": 1000, "wcu": 0},
            refill_amounts={"rpm": 500},
            now_ms=2_000,
            expected_rf=1_000,
            ttl_seconds=60,
            vu=9_000,
            windows={"sess.v1": (2_000, 18_000)},
            seeds=seeds,
        )["Update"]
        assert_expression_safe(update)
        for name in (DOTTED, HYPHENATED, "sess.v1"):
            assert name not in update["UpdateExpression"]

    def test_normal_with_only_seeds(self) -> None:
        """No ADD clause at all when every limit written is a seed."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            seeds={HYPHENATED: self._seed_states()[HYPHENATED]},
        )["Update"]
        assert_expression_safe(update)
        assert " ADD " not in update["UpdateExpression"]

    def test_normal_with_a_pinned_quota_seed(self) -> None:
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={"rpm": 1000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            seeds={HYPHENATED: self._seed_states()[HYPHENATED]},
            seed_shard_count=2,
        )["Update"]
        assert_expression_safe(update)
        assert "#pinsc <= :pinsc" in update["ConditionExpression"]

    async def test_persist_seed(self) -> None:
        """Both attempts of the transfer-seed persist (#633), captured."""
        from unittest.mock import AsyncMock

        from botocore.exceptions import ClientError

        repo = _repo()
        client = MagicMock()
        lost = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        client.update_item = AsyncMock(side_effect=[lost, {}])
        with patch.object(repo, "_get_client", AsyncMock(return_value=client)):
            assert await repo.persist_seed(
                "user-1", "api", 0, self._seed_states()["sess.v1"], vu=9, seed_shard_count=2
            )
        for call in client.update_item.call_args_list:
            assert_expression_safe(call.kwargs)

    def test_retry_with_seeds(self) -> None:
        seeds = self._seed_states()
        update = _repo().build_composite_retry(
            "user-1",
            "api",
            consumed={"rpm": 1000, DOTTED: 1000},
            seeds=seeds,
        )["Update"]
        assert_expression_safe(update)
        assert update["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"

    def test_retry_with_only_unconsumed_seeds(self) -> None:
        """A seed nothing consumes needs no condition at all."""
        update = _repo().build_composite_retry(
            "user-1", "api", consumed={}, seeds={HYPHENATED: self._seed_states()[HYPHENATED]}
        )["Update"]
        assert_expression_safe(update)
        assert "ConditionExpression" not in update

    def test_adjust(self) -> None:
        update = _repo().build_composite_adjust(
            "user-1", "api", deltas={DOTTED: 1000, "rpm": 0, HYPHENATED: -2000}
        )["Update"]
        assert_expression_safe(update)


class TestParamSyncBuilders:
    """Already positional since #487; pinned so a refactor cannot regress them."""

    def test_client_param_sync(self) -> None:
        limits = [
            Limit.per_minute(DOTTED, 100).with_schedule(
                (ScheduleEntry(cron="* 9-17 * * *", scale=0.5),)
            ),
            Limit.quota(HYPHENATED, 1000, cron="0 0 * * *"),
        ]
        expr, names, values = _repo()._build_bucket_param_update(limits, 7, {"old.x-y"})
        assert_expression_safe(
            {
                "UpdateExpression": expr,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        )

    def test_provisioner_param_sync(self) -> None:
        limits = {
            DOTTED: {"capacity": 100, "refill_amount": 100, "refill_period": 60},
            HYPHENATED: {"capacity": 10, "refill_amount": 10, "refill_period": 1},
        }
        expr, names, values = build_bucket_param_update(limits, 7, {"old.x-y"}, 1_000)
        assert_expression_safe(
            {
                "UpdateExpression": expr,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        )


class TestAggregatorWrites:
    NOW = 1_704_067_200_000

    def _state(self) -> BucketRefillState:
        daily = (ScheduleEntry.reset(cron="0 0 * * *"),)
        state = BucketRefillState(
            namespace_id="ns",
            entity_id="user-1",
            resource="api",
            rf_ms=self.NOW - 60_000,
            limits={
                # A drip limit consumed faster than it refills: topped up.
                DOTTED: LimitRefillInfo(
                    tc_delta=5_000_000,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=60_000,
                ),
                # A quota whose midnight edge falls inside the gap: reset.
                HYPHENATED: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    cp_milli=5_000_000,
                    ra_milli=0,
                    rp_ms=1_000,
                    reset_sched=daily,
                ),
            },
        )
        return state

    def test_refill_and_reset(self) -> None:
        table = MagicMock()
        assert try_refill_bucket(table, self._state(), self.NOW + 1) is True
        kwargs = table.update_item.call_args.kwargs
        assert_expression_safe(kwargs)
        names = kwargs["ExpressionAttributeNames"]
        assert {names["#rt0"], names["#rt1"]} == {
            bucket_attr(DOTTED, BUCKET_FIELD_TK),
            bucket_attr(HYPHENATED, BUCKET_FIELD_TK),
        }

    def test_reclaim(self) -> None:
        table = MagicMock()
        table.update_item.return_value = {"Attributes": {}}
        _reclaim_quota_surplus(table, "ns", "user-1", "api", 1, {DOTTED: 1, HYPHENATED: 1})
        assert table.update_item.call_count == 2
        for call in table.update_item.call_args_list:
            assert_expression_safe(call.kwargs)


class TestClientWritesThroughMoto:
    """Writes built inside async client methods, captured on the way to moto."""

    @pytest.fixture
    async def limiter(self, mock_dynamodb):
        await _setup_moto_table()
        repo = await Repository.open(stack="test-rate-limits")
        async with RateLimiter(repository=repo) as limiter:
            yield limiter

    @staticmethod
    async def _spy(repo: Repository) -> MagicMock:
        """Record every `update_item` call and forward it to moto."""
        client = await repo._get_client()
        original = client.update_item
        spy = MagicMock()

        async def forward(**kwargs: Any) -> Any:
            spy(**kwargs)
            return await original(**kwargs)

        spy.forward = forward
        return spy

    async def test_fast_path(self, limiter: RateLimiter) -> None:
        repo = limiter._repository
        client = await repo._get_client()
        spy = await self._spy(repo)
        with patch.object(client, "update_item", spy.forward):
            await repo._speculative_consume_single("user-1", "api", {DOTTED: 1, HYPHENATED: 2})
        assert_expression_safe(spy.call_args.kwargs)

    async def test_fast_path_with_ttl(self, limiter: RateLimiter) -> None:
        repo = limiter._repository
        client = await repo._get_client()
        spy = await self._spy(repo)
        with patch.object(client, "update_item", spy.forward):
            await repo._speculative_consume_single(
                "user-1", "api", {DOTTED: 1, HYPHENATED: 2}, ttl_seconds=60
            )
        assert_expression_safe(spy.call_args.kwargs)

    async def test_reclaim(self, limiter: RateLimiter) -> None:
        repo = limiter._repository
        await repo.set_limits(
            "user-1",
            [
                Limit.quota(DOTTED, 10, cron="0 0 * * *"),
                Limit.quota(HYPHENATED, 10, cron="0 0 * * *"),
            ],
            resource="api",
        )
        async with limiter.acquire("user-1", "api", consume={DOTTED: 1}):
            pass
        client = await repo._get_client()
        spy = await self._spy(repo)
        with patch.object(client, "update_item", spy.forward):
            await repo.reclaim_quota_surplus("user-1", "api", {DOTTED: 1_000, HYPHENATED: 1_000})
        assert spy.call_count == 2
        for call in spy.call_args_list:
            assert_expression_safe(call.kwargs)

    async def test_disable_fan_out(self, limiter: RateLimiter) -> None:
        repo = limiter._repository
        await repo.set_limits("user-1", [Limit.per_minute(DOTTED, 10)], resource="api")
        async with limiter.acquire("user-1", "api", consume={DOTTED: 1}):
            pass
        client = await repo._get_client()
        spy = await self._spy(repo)
        pk = pk_bucket(repo._namespace_id, "user-1", "api", 0)
        with patch.object(client, "update_item", spy.forward):
            await repo._stamp_bucket_disabled(pk, True)
            await repo._stamp_bucket_disabled(pk, False)
        for call in spy.call_args_list:
            assert_expression_safe(call.kwargs)
