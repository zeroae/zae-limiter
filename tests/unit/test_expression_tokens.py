"""Every bucket write expression is safe for every legal limit name (#634).

`models.NAME_PATTERN` accepts `.` and `-` in a limit name. Neither is legal in
an `ExpressionAttributeNames` key or an `ExpressionAttributeValues` placeholder,
and `.` in expression text parses as a document-path separator. So a write
whose tokens are built from the name is rejected by DynamoDB (and by moto)
with a `ValidationException`, on every path except the create `Put`.

This file builds every bucket write the library issues — the fast path, the
three composite builders, the aggregator refill and clone funding, and (as
regressions) the param-sync and disable fan-out writes that were already positional —
for limits named `rpm.v2` and `req-min`, and checks each against the token
rules and against its own declarations.
"""

import re
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter import Limit, RateLimiter, Repository
from zae_limiter.models import BucketState, CreditCeiling, QuotaDonorDebit
from zae_limiter.repository_protocol import FillInstantWrite
from zae_limiter.schedule import ScheduleEntry
from zae_limiter.schema import (
    BUCKET_FIELD_GC,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_WA,
    STAMP_EXPIRED,
    bucket_attr,
    pk_bucket,
)
from zae_limiter_aggregator.processor import (
    BucketRefillState,
    LimitRefillInfo,
    _donor_update_items,
    _quota_count_freeze,
    try_refill_bucket,
)
from zae_limiter_provisioner.applier import (
    entity_registry_cleanup,
    entity_registry_update,
    resource_registry_update,
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

    def test_normal_pinning_the_balance_it_raised(self) -> None:
        """#730 R5: a limit whose refill delta is positive is pinned on `tk` as
        read; a trim (delta <= 0) is not, so it still clamps."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000, HYPHENATED: 2000, "rpm": 0},
            refill_amounts={DOTTED: -500, HYPHENATED: 500, "rpm": 300},
            now_ms=2_000,
            expected_rf=1_000,
            read_tokens={DOTTED: 9_000, HYPHENATED: 4_000, "rpm": 7_000},
        )["Update"]
        assert_expression_safe(update)
        condition = update["ConditionExpression"]
        assert "#bt0 <= :bk0" not in condition  # the trim
        assert "#bt1 <= :bk1" in condition and "#bt2 <= :bk2" in condition
        assert update["ExpressionAttributeValues"][":bk1"] == {"N": "4000"}

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
            applied_windows={DOTTED: 1_500, HYPHENATED: 900},
            owner=(True, "org-1"),
        )["Update"]
        assert_expression_safe(update)
        # #640: the window-applied marker is aliased positionally too.
        names = update["ExpressionAttributeNames"]
        assert {names["#wa0"], names["#wa1"]} == {
            bucket_attr(DOTTED, BUCKET_FIELD_WA),
            bucket_attr(HYPHENATED, BUCKET_FIELD_WA),
        }

    def test_normal_repairing_an_owner_without_a_parent(self) -> None:
        """#684: the owner stamp REMOVEs `parent_id` when the owner has none."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            ttl_seconds=0,
            owner=(False, None),
        )["Update"]
        assert_expression_safe(update)
        assert "#opid" in update["UpdateExpression"].split("REMOVE", 1)[1]

    def test_normal_owner_stamp_with_its_parent_generation(self) -> None:
        """ADR-150: the owner stamp carries `pgen` and is pinned on it."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            owner=(True, "org-1"),
            owner_pgen=3,
            owner_pinc="abc123",
        )["Update"]
        assert_expression_safe(update)
        assert "#opg <= :opg" in update["ConditionExpression"]
        assert "#opi = :opi" in update["ConditionExpression"]  # same incarnation only

    def test_normal_owner_stamp_with_a_generation_but_no_incarnation(self) -> None:
        """A backend that tracks no incarnation keeps the generation-only pin."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            owner=(True, "org-1"),
            owner_pgen=3,
        )["Update"]
        assert_expression_safe(update)
        assert "(attribute_not_exists(#opg) OR #opg <= :opg)" in update["ConditionExpression"]
        assert "#opi" not in update["ExpressionAttributeNames"]
        assert update["ExpressionAttributeValues"][":opg"] == {"N": "3"}

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

    @pytest.mark.parametrize("expected_vu", [None, 0])
    def test_normal_pinning_the_vu_it_read(self, expected_vu) -> None:
        """#701: the pin's `:evu` is declared exactly when it is used."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1, HYPHENATED: 1},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            clear_vu=True,
            pin_vu=True,
            expected_vu=expected_vu,
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

    def test_normal_with_fill_instants(self) -> None:
        """#818: a read fill instant and a conversion share one expression with
        the ADD branch, the expression instant and the shard-count pin."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={"tpm": 1000},
            refill_amounts={"tpm": 0},
            now_ms=2_000,
            expected_rf=1_000,
            fill_instants={
                DOTTED: FillInstantWrite(
                    fill_instant=9_000,
                    consumed_milli=1000,
                    read_fill_instant=8_000,
                    read_tokens_milli=None,
                    capacity_milli=100_000,
                    refill_amount_milli=100_000,
                    refill_period_ms=60_000,
                ),
                HYPHENATED: FillInstantWrite(
                    fill_instant=7_000,
                    consumed_milli=0,
                    read_fill_instant=None,
                    read_tokens_milli=50_000,
                    capacity_milli=10_000,
                    refill_amount_milli=10_000,
                    refill_period_ms=1_000,
                ),
            },
            expressed=(2_000, 1_500),
            pin_shard_count_equal=4,
        )["Update"]
        assert_expression_safe(update)
        assert "REMOVE" in update["UpdateExpression"]

    @pytest.mark.parametrize("read_xi", [None, 1_500])
    def test_normal_with_only_a_conversion(self, read_xi: int | None) -> None:
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            fill_instants={
                HYPHENATED: FillInstantWrite(
                    fill_instant=7_000,
                    consumed_milli=0,
                    read_fill_instant=None,
                    read_tokens_milli=-3_000,
                    capacity_milli=10_000,
                    refill_amount_milli=10_000,
                    refill_period_ms=1_000,
                )
            },
            expressed=(2_000, read_xi),
        )["Update"]
        assert_expression_safe(update)

    def test_normal_with_a_fill_instant_seed(self) -> None:
        """#818: a dripping seed stored as a fill instant guards on neither form."""
        state = BucketState.from_limit("user-1", "api", Limit.per_minute(DOTTED, 100), 2_000)
        state.fill_instant, state.expressed_ms = 5_000, 2_000
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            seeds={DOTTED: state},
        )["Update"]
        assert_expression_safe(update)
        assert "attribute_not_exists(#sz0)" in update["ConditionExpression"]

    def _fill_state(self, name: str, *, sched: str | None = None) -> BucketState:
        state = BucketState.from_limit("user-1", "api", Limit.per_minute(name, 100), 2_000)
        state.fill_instant, state.expressed_ms = 5_000, 2_000
        state.stored_sched_raw = (sched, None)
        return state

    @pytest.mark.parametrize("bypassed", [False, True])
    @pytest.mark.parametrize("guards", [True, False])
    def test_fill_write(self, bypassed: bool, guards: bool) -> None:
        """#819: ADR-154's two writes, every side, beside a quota's `tk` terms."""
        from zae_limiter import fill_instant

        params = fill_instant.ShardParams(100_000, 100_000, 60_000, 1)
        request = _repo().build_fill_write(
            "user-1",
            "api",
            0,
            {
                DOTTED: (
                    fill_instant.not_full_write(2_000, 1_000, params),
                    self._fill_state(DOTTED),
                ),
                HYPHENATED: (
                    fill_instant.full_write(2_000, 1_000, params),
                    self._fill_state(HYPHENATED, sched="1h9-17s500"),
                ),
                "tok.sec": (
                    fill_instant.not_full_write(2_000, 1_000, params, bounded=False),
                    self._fill_state("tok.sec"),
                ),
            },
            {DOTTED: 1_000, HYPHENATED: 1_000, "tok.sec": 1_000, "q.v1": 1_000},
            {"q.v1": 1_000},
            now_ms=2_000,
            ttl_seconds=60,
            expressed_ms=2_000,
            shard_count=4,
            item_sched=("1h9-17s500", "UTC"),
            bypassed=bypassed,
            guards=guards,
            soft_pins={"soft.a": None, "soft-b": 9_000},
        )
        assert_expression_safe(request)
        assert " BETWEEN " in request["ConditionExpression"]

    def test_param_sync_re_expression(self) -> None:
        """#820: the parameter sync's re-expression of a fill instant (`fill_sync`)."""
        from zae_limiter import fill_sync

        item = {
            "PK": {"S": "x"},
            "xi": {"N": "2000"},
            "shard_count": {"N": "1"},
            **{
                f"b_{name}_{field}": {"N": value}
                for name in (DOTTED, HYPHENATED)
                for field, value in (
                    ("fi", "200000000"),
                    ("cp", "100000"),
                    ("ra", "100000"),
                    ("rp", "60000"),
                )
            },
        }
        plan = (
            "SET #c0 = :c0, #c1 = :c1 REMOVE #s0",
            {"#c0": f"b_{DOTTED}_cp", "#c1": f"b_{HYPHENATED}_cp", "#s0": "b_old.x_tk"},
            {":c0": {"N": "50000"}, ":c1": {"N": "50000"}},
        )
        extended = fill_sync.reexpression(item, *plan, 3_000)
        assert extended is not None
        expression, names, values, condition = extended
        assert_expression_safe(
            {
                "UpdateExpression": expression,
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": values,
            }
        )

    def test_reexpress_item(self) -> None:
        """#820: re-expressing every fill instant on an item, pinned (form 3)."""
        captured: dict[str, Any] = {}

        class Client:
            pass

        repo = _repo()

        async def fake_update(client, **kwargs):
            captured.update(kwargs)
            return {"Attributes": {}}

        async def fake_client():
            return Client()

        repo._update_item_retrying_conflict = fake_update
        repo._get_client = fake_client
        state_item = {
            "PK": {"S": "x"},
            "entity_id": {"S": "user-1"},
            "resource": {"S": "api"},
            "rf": {"N": "1000"},
            "xi": {"N": "1000"},
            f"b_{DOTTED}_fi": {"N": "200000000"},
            f"b_{DOTTED}_cp": {"N": "100000"},
            f"b_{DOTTED}_ra": {"N": "100000"},
            f"b_{DOTTED}_rp": {"N": "60000"},
            f"b_{DOTTED}_tc": {"N": "0"},
        }
        import asyncio

        asyncio.run(repo._reexpress_item("user-1", "api", 0, state_item, 3_000))
        assert_expression_safe(captured)

    def test_fill_return(self) -> None:
        """#819: the exact return of a fill instant (ADR-155)."""
        for counter in (True, False):
            request = _repo().build_fill_return(
                "user-1",
                "api",
                0,
                {
                    DOTTED: (1_000, self._fill_state(DOTTED)),
                    HYPHENATED: (5, self._fill_state(HYPHENATED)),
                },
                expressed_ms=2_000,
                shard_count=1,
                counter=counter,
            )
            assert_expression_safe(request)

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
            pin_shard_count=2,
        )["Update"]
        assert_expression_safe(update)
        assert "#pinsc <= :pinsc" in update["ConditionExpression"]

    def test_normal_with_grant_counts(self) -> None:
        update = _repo().build_composite_normal(
            entity_id="e",
            resource="r",
            consumed={DOTTED: 1000, HYPHENATED: 0},
            refill_amounts={},
            now_ms=2,
            expected_rf=1,
            grant_counts={DOTTED: 4, HYPHENATED: 4},
            pin_shard_count=4,
        )["Update"]
        assert_expression_safe(update)
        assert "#pinsc <= :pinsc" in update["ConditionExpression"]

    def test_normal_clearing_top_ups(self) -> None:
        """ADR-149: a reset or roll removes `b_{name}_tu` on positional `#tu*`."""
        update = _repo().build_composite_normal(
            entity_id="e",
            resource="r",
            consumed={DOTTED: 0, HYPHENATED: 0},
            refill_amounts={},
            now_ms=2,
            expected_rf=1,
            grant_counts={DOTTED: 1, HYPHENATED: 1},
            pin_shard_count=1,
            cleared_top_ups=[DOTTED, HYPHENATED],
        )["Update"]
        assert_expression_safe(update)
        assert update["UpdateExpression"].endswith("REMOVE #tu0, #tu1")

    @pytest.mark.parametrize("stored", [True, False])
    def test_bucket_operation(self, stored: bool) -> None:
        """ADR-149: a reset or top-up's write, every clause at once."""
        from zae_limiter.bucket_ops import ShardWrite

        write = ShardWrite(
            shard_id=1,
            shard_count=2,
            shard_count_stored=stored,
            expected_rf=1,
            written_rf=2,
            deltas={DOTTED: 5, HYPHENATED: -5},
            grant_counts={DOTTED: 2, HYPHENATED: 2},
            windows={DOTTED: (1, 60)},
            applied_windows={DOTTED: 1},
            top_ups={HYPHENATED: 7},
            cleared_top_ups={DOTTED},
            cleared_window_marks={DOTTED},
            reset_numbers={DOTTED, HYPHENATED},
        )
        update = _repo().build_bucket_operation("e", "r", write)["Update"]
        assert_expression_safe(update)
        assert "#on0 :on0" in update["UpdateExpression"]
        update = _repo().build_bucket_operation("e", "r", write, ttl=123)["Update"]
        assert_expression_safe(update)
        assert "#ottl = :ottl" in update["UpdateExpression"]
        update = _repo().build_bucket_operation("e", "r", write, remove_ttl=True)["Update"]
        assert_expression_safe(update)
        assert update["UpdateExpression"].endswith("#ottl")

    @pytest.mark.parametrize("read_xi", [1_500, None])
    def test_bucket_operation_with_fill_instants(self, read_xi: int | None) -> None:
        """#820: a reset or top-up that SETs fill instants and stamps `xi` (ADR-155)."""
        from zae_limiter.bucket_ops import ShardWrite

        write = ShardWrite(
            shard_id=0,
            shard_count=1,
            shard_count_stored=True,
            expected_rf=1,
            written_rf=2,
            deltas={HYPHENATED: 5},
            fill_sets={
                DOTTED: (7_000, self._fill_state(DOTTED)),
                HYPHENATED: (9_000, self._fill_state(HYPHENATED)),
            },
            expressed=(2_000, read_xi),
        )
        update = _repo().build_bucket_operation("e", "r", write)["Update"]
        assert_expression_safe(update)
        assert "#oxi = :oxi" in update["UpdateExpression"]

    def test_bucket_operation_with_nothing_but_the_lock(self) -> None:
        from zae_limiter.bucket_ops import ShardWrite

        write = ShardWrite(
            shard_id=0, shard_count=1, shard_count_stored=True, expected_rf=1, written_rf=2
        )
        update = _repo().build_bucket_operation("e", "r", write)["Update"]
        assert_expression_safe(update)
        assert update["UpdateExpression"] == "SET #rf = :rf, #vu = :vu"

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

    def test_retry_with_period_guards(self) -> None:
        """#743: the retry requires the period the lost write applied."""
        update = _repo().build_composite_retry(
            "user-1",
            "api",
            consumed={DOTTED: 1000, HYPHENATED: 2000, "rpm": 500},
            period_guards={DOTTED: (9_000, None), HYPHENATED: (None, 7_000)},
        )["Update"]
        assert_expression_safe(update)
        condition = update["ConditionExpression"]
        assert "#prf >= :pe0" in condition and "#pw1 = :pw1" in condition

    def test_retry_with_only_unconsumed_seeds(self) -> None:
        """A seed nothing consumes needs no balance condition: only the raise
        marker's (ADR-156)."""
        update = _repo().build_composite_retry(
            "user-1", "api", consumed={}, seeds={HYPHENATED: self._seed_states()[HYPHENATED]}
        )["Update"]
        assert_expression_safe(update)
        assert update["ConditionExpression"] == "(attribute_not_exists(#rx) OR #rx <= :rxt)"

    def test_normal_with_soft_stamps_and_floors(self) -> None:
        """#467: soft floors OR the item's live stamp, a trusted soft limit has
        no floor, and the re-stamp reuses a floor's alias rather than declaring
        a second one for the same attribute. Each stamp is a deadline, pinned
        on the one read (ADR-151 decisions 4 and 8)."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 5000, HYPHENATED: 5000, "rpm": 3000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            soft_stamps={DOTTED: None, HYPHENATED: 9_000, "rpm": 9_000},
            soft_reads={DOTTED: 1_500, HYPHENATED: None, "rpm": STAMP_EXPIRED},
            soft_trusted=frozenset({HYPHENATED}),
        )["Update"]
        assert_expression_safe(update)
        names = update["ExpressionAttributeNames"]
        values = update["ExpressionAttributeValues"]
        condition = update["ConditionExpression"]
        soft_attrs = [v for v in names.values() if v.endswith("_soft")]
        assert len(soft_attrs) == len(set(soft_attrs)) == 3
        assert "(#bo0 > :bnow OR #bt0 >= :bf0)" in condition
        assert values[":bnow"] == {"N": "2000"}
        assert "#bt1 >=" not in condition
        assert {"N": "9000"} in values.values()
        # Restamps go in name order: req-min (#bs0), rpm (#bo2), then the dotted one (#bo0).
        assert "attribute_not_exists(#bs0)" in condition
        assert "#bo2 = :bsr1" in condition and values[":bsr1"] == {"BOOL": True}
        assert "#bo0 = :bsr2" in condition and values[":bsr2"] == {"N": "1500"}

    @pytest.mark.parametrize(
        "deadline, read",
        [(9_000, None), (None, 3_000), (9_000, 3_000), (None, STAMP_EXPIRED)],
    )
    def test_normal_setting_or_removing_the_bypass_stamp(self, deadline, read) -> None:
        """#311, ADR-151 decisions 4 and 8: the stamp follows config, as a
        deadline, pinned on the deadline read (a pre-release BOOL as itself)."""
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={DOTTED: 1000},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            bypass_stamp=(deadline, read),
        )["Update"]
        assert_expression_safe(update)
        expression = update["UpdateExpression"]
        values = update["ExpressionAttributeValues"]
        assert ("#byp = :byp" in expression) is (deadline is not None)
        assert ("REMOVE #byp" in expression) is (deadline is None)
        if deadline is not None:
            assert values[":byp"] == {"N": str(deadline)}
        if read is None:
            assert "attribute_not_exists(#byp)" in update["ConditionExpression"]
        else:
            assert "#byp = :bypr" in update["ConditionExpression"]
            expected = {"BOOL": True} if read == STAMP_EXPIRED else {"N": str(read)}
            assert values[":bypr"] == expected

    def test_normal_with_a_soft_seed(self) -> None:
        from zae_limiter.models import BucketState

        seed = BucketState.from_limit(
            "user-1", "api", Limit.per_minute(HYPHENATED, 10, soft=True), 2_000
        )
        seed.soft_until_ms = 9_000
        update = _repo().build_composite_normal(
            "user-1",
            "api",
            consumed={},
            refill_amounts={},
            now_ms=2_000,
            expected_rf=1_000,
            seeds={HYPHENATED: seed},
        )["Update"]
        assert_expression_safe(update)
        assert update["ExpressionAttributeValues"][":so0"] == {"N": "9000"}

    def test_retry_with_soft_limits(self) -> None:
        from zae_limiter.models import BucketState

        seed = BucketState.from_limit(
            "user-1", "api", Limit.per_minute("sess.v1", 10, soft=True), 2_000
        )
        update = _repo().build_composite_retry(
            "user-1",
            "api",
            consumed={DOTTED: 1000, HYPHENATED: 2000, "sess.v1": 3000},
            seeds={"sess.v1": seed},
            soft_trusted=frozenset({HYPHENATED}),
        )["Update"]
        assert_expression_safe(update)
        condition = update["ConditionExpression"]
        # The stamp waives the balance only beside `cp` and `tk`, and a
        # trusted soft limit is debited only where the item carries it (#771).
        assert condition == (
            "(attribute_not_exists(#rx) OR #rx <= :rxt)"
            " AND (#bt0 >= :bf0 OR (#bo0 > :bnow"
            " AND attribute_exists(#be0) AND attribute_exists(#bt0)))"
            " AND (attribute_exists(#be1) AND attribute_exists(#bt1))"
        )

    def test_adjust(self) -> None:
        update = _repo().build_composite_adjust(
            "user-1", "api", deltas={DOTTED: 1000, "rpm": 0, HYPHENATED: -2000}
        )["Update"]
        assert_expression_safe(update)

    def test_adjust_counters_or_tokens_only(self) -> None:
        """#311: a bypassed lease writes `tc` only; a bypass refund writes `tk` only."""
        deltas = {DOTTED: 1000, HYPHENATED: -2000}
        tc_only = _repo().build_composite_adjust("user-1", "api", deltas, tokens=False)["Update"]
        tk_only = _repo().build_composite_adjust("user-1", "api", deltas, counter=False)["Update"]
        for update in (tc_only, tk_only):
            assert_expression_safe(update)
        assert all(v.endswith("_tc") for v in tc_only["ExpressionAttributeNames"].values())
        assert all(v.endswith("_tk") for v in tk_only["ExpressionAttributeNames"].values())

    def test_bypass_consume(self) -> None:
        update = _repo().build_bypass_consume(
            "user-1", "api", {DOTTED: 1000, HYPHENATED: 2000}, shard_id=2, bypass_until_ms=9_000
        )["Update"]
        assert_expression_safe(update)
        # Any deadline: this is the renewal (ADR-151 decision 8).
        assert "attribute_exists(#byp)" in update["ConditionExpression"]
        assert update["ExpressionAttributeValues"][":byp"] == {"N": "9000"}

    def test_bypass_consume_with_nothing_declared(self) -> None:
        update = _repo().build_bypass_consume("user-1", "api", {}, bypass_until_ms=9_000)["Update"]
        assert_expression_safe(update)
        assert update["UpdateExpression"] == "SET #byp = :byp"

    @pytest.mark.parametrize(
        "bypass, soft",
        [
            # Renew a pre-release stamp, set a missing one, remove a stale one.
            ((9_000, STAMP_EXPIRED), {DOTTED: (9_000, None), HYPHENATED: (None, 5)}),
            ((None, 5), None),  # remove only: no values but the pin
            (None, {DOTTED: (None, None)}),  # nothing read, nothing set
            (None, {HYPHENATED: (9_000, 5)}),
        ],
    )
    def test_stamp_fix(self, bypass, soft) -> None:
        """Review E1: a rejected pass's stamp-only correction, pinned as read."""
        update = _repo().build_stamp_fix("user-1", "api", 2, bypass=bypass, soft=soft)["Update"]
        assert_expression_safe(update)
        assert update["ConditionExpression"].startswith("attribute_exists(PK)")
        written = set(update["ExpressionAttributeNames"].values())
        assert all(name == "bypass" or name.endswith("_soft") for name in written)

    @pytest.mark.parametrize(
        "exists, pgen, pinc",
        [(False, None, None), (True, 0, "-"), (True, 0, "2026-01-01T00:00:00Z"), (True, 7, "a1")],
    )
    def test_owner_check(self, exists, pgen, pinc) -> None:
        """ADR-150: the ConditionCheck pinning a bucket create to its owner's META."""
        check = _repo().build_owner_check("user-1", exists=exists, pgen=pgen, pinc=pinc)
        assert_expression_safe(check["ConditionCheck"])

    def test_self_trimming_credit(self) -> None:
        """#721: each credited limit with a ceiling is conditioned on staying under it."""
        update = _repo().build_composite_adjust(
            "user-1",
            "api",
            deltas={DOTTED: -1000, "rpm": 0, HYPHENATED: -2000},
            ceilings={
                DOTTED: CreditCeiling(5000, 10_000, None, ("1h9-17s500", None)),
                HYPHENATED: CreditCeiling(9000, 9000, 1, (None, "1m0h0")),
            },
            pin=(4, 1_800_000_000_000, ("1h9-17s500", None, "America/New_York")),
        )["Update"]
        assert_expression_safe(update)
        assert "ConditionExpression" in update

    def test_quota_period_guards(self) -> None:
        """#721: a quota credit's period terms, in the conditional and trimmed writes."""
        from zae_limiter.models import QuotaPeriodPin

        ceilings = {
            DOTTED: CreditCeiling(
                5000, 10_000, 2, (None, None), True, QuotaPeriodPin(9, None, ("1m0h0", None, None))
            ),
            HYPHENATED: CreditCeiling(
                9000, 9000, 1, (None, "1m0h0"), True, QuotaPeriodPin(window=(3, 3))
            ),
        }
        for trim in (False, True):
            update = _repo().build_composite_adjust(
                "user-1",
                "api",
                deltas={DOTTED: -1000, HYPHENATED: -2000},
                ceilings=ceilings,
                trim=trim,
                pin=(2, None, (None, None, None)),
            )["Update"]
            assert_expression_safe(update)

    def test_quota_top_up_pin(self) -> None:
        """ADR-149: a quota credit pins its top-up, present (`=`) or absent."""
        from zae_limiter.models import QuotaPeriodPin

        period = QuotaPeriodPin(9, None, ("1m0h0", None, None))
        update = _repo().build_composite_adjust(
            "user-1",
            "api",
            deltas={DOTTED: -1000, HYPHENATED: -2000},
            ceilings={
                DOTTED: CreditCeiling(
                    5000, 10_000, 2, (None, None), True, period, topped_up_milli=3000
                ),
                HYPHENATED: CreditCeiling(9000, 9000, 1, (None, "1m0h0"), True, period),
            },
            pin=(2, None, (None, None, None)),
        )["Update"]
        assert_expression_safe(update)
        assert "#bu0 = :bu0" in update["ConditionExpression"]
        assert "attribute_not_exists(#bu1)" in update["ConditionExpression"]

    def test_quota_reset_number_pin(self) -> None:
        """ADR-149 (F1): a calendar quota credit pins its operator reset count."""
        from zae_limiter.models import QuotaPeriodPin

        sched = ("1m0h0", None, None)
        update = _repo().build_composite_adjust(
            "user-1",
            "api",
            deltas={DOTTED: -1000, HYPHENATED: -2000},
            ceilings={
                DOTTED: CreditCeiling(
                    5000, 10_000, 2, (None, None), True, QuotaPeriodPin(9, None, sched, 3)
                ),
                HYPHENATED: CreditCeiling(
                    9000, 9000, 1, (None, "1m0h0"), True, QuotaPeriodPin(9, None, sched)
                ),
            },
            pin=(2, None, (None, None, None)),
        )["Update"]
        assert_expression_safe(update)
        assert "#bqn0 = :bqn0" in update["ConditionExpression"]
        assert "attribute_not_exists(#bqn1)" in update["ConditionExpression"]

    def test_trimmed_credit(self) -> None:
        """#721: the fallback re-issues the credit with ``vu = 0`` in the same write."""
        update = _repo().build_composite_adjust(
            "user-1", "api", deltas={DOTTED: -1000, HYPHENATED: 2000}, shard_id=3, trim=True
        )["Update"]
        assert_expression_safe(update)
        assert update["ConditionExpression"] == "attribute_exists(PK)"

    def test_quota_donor_debits(self) -> None:
        for item in _repo().build_quota_donor_debits(
            "e",
            "r",
            [
                QuotaDonorDebit(0, DOTTED, 1, 1, 5, None),
                QuotaDonorDebit(0, HYPHENATED, 1, 2, None, 7),
            ],
        ):
            assert_expression_safe(item["Update"])

    def test_quota_donor_debit_off_a_legacy_donor(self) -> None:
        """R7: an inferred grant count and the stored count are two tokens."""
        (item,) = _repo().build_quota_donor_debits(
            "e", "r", [QuotaDonorDebit(0, DOTTED, 1, 1, 5, None, legacy_shard_count=2)]
        )
        assert_expression_safe(item["Update"])
        values = item["Update"]["ExpressionAttributeValues"]
        assert (values[":qg0"], values[":ql0"]) == ({"N": "1"}, {"N": "2"})

    def test_quota_count_freeze(self) -> None:
        assert_expression_safe(
            _repo()._build_quota_count_freeze("e", "r", 0, 4, [(DOTTED, 2), (HYPHENATED, 1)])
        )

    def test_quota_count_freeze_without_quotas(self) -> None:
        assert_expression_safe(_repo()._build_quota_count_freeze("e", "r", 1, 4, []))

    def test_quota_donor_debit_without_a_period_guard(self) -> None:
        for item in _repo().build_quota_donor_debits(
            "e", "r", [QuotaDonorDebit(1, DOTTED, 1, 2, None, None)]
        ):
            assert_expression_safe(item["Update"])


class TestProvisionerRegistryWrites:
    """The registries the CLI's list commands read, kept by the provisioner.

    The resource name is the wide-column attribute name in
    #ENTITY_CONFIG_RESOURCES, so it may only ever be an alias value.
    """

    @pytest.mark.parametrize(
        "kwargs",
        [
            resource_registry_update("ns", "openai/gpt-4o.mini:2024", add=True),
            resource_registry_update("ns", "openai/gpt-4o.mini:2024", add=False),
            entity_registry_update("ns", "openai/gpt-4o.mini:2024", 1),
            entity_registry_update("ns", "openai/gpt-4o.mini:2024", -1),
            entity_registry_cleanup("ns", "openai/gpt-4o.mini:2024"),
        ],
    )
    def test_registry_update(self, kwargs: dict[str, Any]) -> None:
        assert_expression_safe(kwargs)


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
                # Two quotas whose midnight edge falls inside the gap: both
                # reset. A dripping limit is never refilled here (ADR-156).
                DOTTED: LimitRefillInfo(
                    tc_delta=5_000_000,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=0,
                    rp_ms=1_000,
                    reset_sched=daily,
                ),
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

    def test_restamp_pins_each_unclamped_quota(self) -> None:
        """#681: re-opening the gate is pinned on a skipped quota staying within its ceiling."""
        now = self.NOW + 3_600_000  # an hour past midnight: no reset edge in the gap
        state = self._state()
        state.rf_ms = now - 60_000
        state.vu_ms = 0  # expired: this pass may re-stamp it
        # An hourly quota whose 01:00 edge is in the gap makes the write.
        state.limits["q.h"] = LimitRefillInfo(
            tc_delta=0,
            tk_milli=0,
            cp_milli=5_000,
            ra_milli=0,
            rp_ms=1_000,
            reset_sched=(ScheduleEntry.reset(cron="0 * * * *"),),
        )
        table = MagicMock()
        assert try_refill_bucket(table, state, now) is True
        kwargs = table.update_item.call_args.kwargs
        assert_expression_safe(kwargs)
        names = kwargs["ExpressionAttributeNames"]
        assert {names["#vq0"], names["#vq1"]} == {
            bucket_attr(DOTTED, BUCKET_FIELD_TK),
            bucket_attr(HYPHENATED, BUCKET_FIELD_TK),
        }
        assert "#vq0 <= :vq0" in kwargs["ConditionExpression"]

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
        # #730 R5: each raise is pinned on the balance the image showed.
        values = kwargs["ExpressionAttributeValues"]
        for i in range(2):
            if values[f":rd{i}"] > 0:
                assert f"#rt{i} <= :rk{i}" in kwargs["ConditionExpression"]

    def test_window_roll_and_marker(self) -> None:
        """#640: a rolled window and a carried one each stamp `wa` through a
        positional alias beside the roll's own `#wtk`/`#wws` tokens."""
        window = {
            "cp_milli": 5_000_000,
            "ra_milli": 0,
            "rp_ms": 1_000,
            "reset_after_seconds": 18_000,
        }
        state = BucketRefillState(
            namespace_id="ns",
            entity_id="user-1",
            resource="api",
            rf_ms=self.NOW - 60_000,
            limits={
                # Rolled: a window start after the one its balance reflects.
                DOTTED: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    window_start_ms=self.NOW - 1_000,
                    window_applied_ms=self.NOW - 18_000_000,
                    **window,
                ),
                # Carried: applied under the pre-#640 `rf` rule, unmarked.
                HYPHENATED: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    window_start_ms=self.NOW - 120_000,
                    **window,
                ),
            },
        )
        table = MagicMock()
        assert try_refill_bucket(table, state, self.NOW) is True
        kwargs = table.update_item.call_args.kwargs
        assert_expression_safe(kwargs)
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        marked = {names[f"#wa{i}"]: values[f":wa{i}"] for i in range(2)}
        assert marked == {
            bucket_attr(DOTTED, BUCKET_FIELD_WA): self.NOW - 1_000,
            bucket_attr(HYPHENATED, BUCKET_FIELD_WA): self.NOW - 120_000,
        }

    def test_reset_stamps_the_grant_count_and_pins_the_shard_count(self) -> None:
        """ADR-145 I3/I4: the reset's `gc` SET and the `shard_count` pin ride
        on positional `#gq*` tokens beside the `#rt*` reset delta."""
        state = self._state()
        state.shard_count = 4
        table = MagicMock()
        assert try_refill_bucket(table, state, self.NOW + 1) is True
        kwargs = table.update_item.call_args.kwargs
        assert_expression_safe(kwargs)
        names, values = kwargs["ExpressionAttributeNames"], kwargs["ExpressionAttributeValues"]
        assert {names["#gq0"], names["#gq1"]} == {
            bucket_attr(DOTTED, BUCKET_FIELD_GC),
            bucket_attr(HYPHENATED, BUCKET_FIELD_GC),
        }
        assert values[":gq0"] == values[":gq1"] == 4
        assert names["#gqsc"] == "shard_count"
        assert values[":gqpin"] == 4

    def test_window_roll_stamps_the_grant_count(self) -> None:
        window = {"cp_milli": 5_000_000, "ra_milli": 0, "rp_ms": 1_000}
        state = BucketRefillState(
            namespace_id="ns",
            entity_id="user-1",
            resource="api",
            rf_ms=self.NOW - 60_000,
            shard_count=2,
            limits={
                DOTTED: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    window_start_ms=self.NOW - 1_000,
                    window_applied_ms=self.NOW - 18_000_000,
                    reset_after_seconds=18_000,
                    **window,
                ),
            },
        )
        table = MagicMock()
        assert try_refill_bucket(table, state, self.NOW) is True
        kwargs = table.update_item.call_args.kwargs
        assert_expression_safe(kwargs)
        assert kwargs["ExpressionAttributeNames"]["#gq0"] == bucket_attr(DOTTED, BUCKET_FIELD_GC)
        # #730 R5: the roll raises the balance, so it is pinned on the image's.
        assert "#wtk0 <= :wk0" in kwargs["ConditionExpression"]
        assert kwargs["ExpressionAttributeValues"][":wk0"] == 0

    def test_clone_donor_update(self) -> None:
        """Path 2's donor debit (ADR-145): both guards, legacy branch, two
        quotas sharing one donor, all on positional tokens."""
        debits = [
            QuotaDonorDebit(1, DOTTED, 1_000, 2, 1_700_000_000_000, None),
            QuotaDonorDebit(1, HYPHENATED, 2_000, 2, None, 1_700_000_000_000, 4),
            QuotaDonorDebit(3, DOTTED, 500, 4, None, None),
        ]
        items = _donor_update_items("t", "ns", "user-1", "api", debits)
        assert len(items) == 2, "one Update per donor shard"
        for item in items:
            assert_expression_safe(item["Update"])

    def test_path_1_freeze(self) -> None:
        kwargs = _quota_count_freeze({"PK": "p", "SK": "s"}, 4, [(DOTTED, 2), (HYPHENATED, 1)])
        assert_expression_safe(kwargs)


class TestRaiseWrites:
    """ADR-156 (#821): the raise marker, its release and the raise transaction."""

    NOW = 1_800_000_000_000

    def _image(self, *, legacy: bool, shard_count: int = 1) -> dict[str, Any]:
        """A shard as the marker write returns it: a dripping limit and a quota."""
        from zae_limiter import fill_instant as fi

        rpm = fi.ShardParams(1_000_000, 1_000_000, 60_000, shard_count)
        image: dict[str, Any] = {
            "PK": {"S": "ns/BUCKET#user-1#api#0"},
            "SK": {"S": "#STATE"},
            "shard_count": {"N": str(shard_count)},
            "rf": {"N": str(self.NOW - 1_000)},
            "rx": {"N": str(self.NOW + 2_000)},
            "pinc": {"S": "inc"},
            "pgen": {"N": "3"},
            bucket_attr(DOTTED, "cp"): {"N": "1000000"},
            bucket_attr(DOTTED, "ra"): {"N": "1000000"},
            bucket_attr(DOTTED, "rp"): {"N": "60000"},
            bucket_attr(DOTTED, "tc"): {"N": "5000"},
            bucket_attr(HYPHENATED, "tk"): {"N": "20000"},
            bucket_attr(HYPHENATED, "cp"): {"N": "50000"},
            bucket_attr(HYPHENATED, "ra"): {"N": "0"},
            bucket_attr(HYPHENATED, "rp"): {"N": "1000"},
            bucket_attr(HYPHENATED, "rsched"): {"S": "1m0h0"},
            "b_wcu_cp": {"N": "1000000"},
            "b_wcu_ra": {"N": "1000000"},
            "b_wcu_rp": {"N": "1000"},
        }
        if legacy:  # `tk` for both dripping limits, no `xi`, no `gc` on the quota
            image[bucket_attr(DOTTED, "tk")] = {"N": "400000"}
            image["b_wcu_tk"] = {"N": "999000"}
        else:
            image["xi"] = {"N": str(self.NOW - 1_000)}
            image[bucket_attr(DOTTED, "fi")] = {
                "N": str(fi.stamp(self.NOW - 1_000, rpm) + fi.span(600_000, rpm))
            }
            image["b_wcu_fi"] = {"N": str(self.NOW * 1_000_000)}
            image[bucket_attr(HYPHENATED, "gc")] = {"N": str(shard_count)}
        return image

    def test_the_marker_and_its_release(self) -> None:
        from zae_limiter import shard_raise

        assert_expression_safe(shard_raise.marker_write(self.NOW + 2_000, self.NOW))
        assert_expression_safe(shard_raise.release_write(self.NOW + 2_000))

    @pytest.mark.parametrize("legacy", [False, True], ids=["fill-instants", "legacy-tk"])
    def test_every_item_of_the_raise(self, legacy: bool) -> None:
        from zae_limiter import shard_raise

        plan = shard_raise.plan_raise(
            "t",
            "ns",
            "user-1",
            "api",
            {0: self._image(legacy=legacy)},
            1,
            self.NOW,
            self.NOW + 2_000,
        )
        assert plan is not None and plan.new_count == 2
        kinds = [next(iter(item)) for item in plan.transact_items]
        assert kinds == ["Update", "Put", "ConditionCheck"]
        for item in plan.transact_items:
            body = next(iter(item.values()))
            assert_expression_safe(body)
        update = plan.transact_items[0]["Update"]
        names = update["ExpressionAttributeNames"]
        if legacy:
            # The conversion is pinned on `tk` and REMOVEs it; the legacy
            # quota's grant size is frozen (ADR-145 R5).
            assert any(v == bucket_attr(DOTTED, "tk") for v in names.values())
            assert any(v == bucket_attr(HYPHENATED, "gc") for v in names.values())
        assert "#rx = :rxd" in update["ConditionExpression"]

    def test_a_missing_slot_and_its_split_are_puts(self) -> None:
        from zae_limiter import shard_raise

        plan = shard_raise.plan_raise(
            "t",
            "ns",
            "user-1",
            "api",
            {0: self._image(legacy=False, shard_count=2), 1: None},
            2,
            self.NOW,
            self.NOW + 2_000,
        )
        assert plan is not None
        kinds = [next(iter(item)) for item in plan.transact_items]
        assert kinds == ["Update", "Put", "Put", "Put", "ConditionCheck"]
        for item in plan.transact_items:
            assert_expression_safe(next(iter(item.values())))

    def test_the_aggregator_re_expression(self) -> None:
        """A `vu` re-stamp past a dripping boundary re-expresses with `#xf*` / `#xxi`."""
        half = (ScheduleEntry(cron="* 0-6 * * *", tz="UTC", scale=0.5),)
        daily = (ScheduleEntry.reset(cron="0 0 * * *"),)
        now = 1_704_067_200_000 + 7 * 3_600_000 + 5_000  # 07:00:05, the window closed at 07:00
        state = BucketRefillState(
            namespace_id="ns",
            entity_id="user-1",
            resource="api",
            rf_ms=now - 86_400_000,
            vu_ms=0,
            xi_ms=now - 60_000,
            limits={
                DOTTED: LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=60_000,
                    sched=half,
                    fill_instant=(now - 60_000) * 500_000,
                ),
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
        table = MagicMock()
        assert try_refill_bucket(table, state, now) is True
        kwargs = table.update_item.call_args.kwargs
        assert_expression_safe(kwargs)
        assert kwargs["ExpressionAttributeNames"]["#xf0"] == bucket_attr(DOTTED, "fi")
        assert "#xxi = :xxir" in kwargs["ConditionExpression"]


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
        # An expired bypass stamp fails the enforce shape (ADR-151 decision 8).
        condition = spy.call_args.kwargs["ConditionExpression"]
        assert "(attribute_not_exists(#byp) OR #byp > :vu_now)" in condition

    async def test_fast_path_bypass_shape(self, limiter: RateLimiter) -> None:
        """#311: `tc` and `wcu` only, conditioned on the stamp, no `vu` guard."""
        repo = limiter._repository
        client = await repo._get_client()
        spy = await self._spy(repo)
        repo._bypass_cache.add((repo._namespace_id, "user-1", "api"))
        with patch.object(client, "update_item", spy.forward):
            await repo._speculative_consume_single(
                "user-1", "api", {DOTTED: 1, HYPHENATED: 2}, ttl_seconds=60
            )
        bypass_write = spy.call_args_list[0].kwargs
        assert_expression_safe(bypass_write)
        # Only a live stamp admits (ADR-151 decision 8).
        assert "#byp > :vu_now" in bypass_write["ConditionExpression"]
        assert "#vu" not in bypass_write["ConditionExpression"]

    async def test_fast_path_with_ttl(self, limiter: RateLimiter) -> None:
        repo = limiter._repository
        client = await repo._get_client()
        spy = await self._spy(repo)
        with patch.object(client, "update_item", spy.forward):
            await repo._speculative_consume_single(
                "user-1", "api", {DOTTED: 1, HYPHENATED: 2}, ttl_seconds=60
            )
        assert_expression_safe(spy.call_args.kwargs)

    async def test_quota_move_transaction(self, limiter: RateLimiter) -> None:
        """An ADR-145 move — the seed's rf-locked write plus the donor debit —
        reaches moto as one transaction whose every item is token-safe, and
        lands: dotted and hyphenated quota names both moved."""
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
        assert await repo.bump_shard_count("user-1", "api", 1) == 2
        client = await repo._get_client()
        original = client.transact_write_items
        sent: list[dict[str, Any]] = []

        async def forward(**kwargs: Any) -> Any:
            sent.extend(kwargs["TransactItems"])
            return await original(**kwargs)

        with patch.object(client, "transact_write_items", forward):
            with patch("zae_limiter.repository.random.randrange", return_value=1):
                async with limiter.acquire("user-1", "api", consume={DOTTED: 1}):
                    pass
        # The create, its donor debit, and the owner's META check (ADR-150).
        assert [next(iter(item)) for item in sent] == ["Put", "Update", "ConditionCheck"]
        for item in sent:
            assert_expression_safe(next(iter(item.values())))

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
            await repo._stamp_bucket_disabled(pk, "bypass", 9_000)
            await repo._stamp_bucket_disabled(pk, False)
        for call in spy.call_args_list:
            assert_expression_safe(call.kwargs)

    async def test_soft_fan_out(self, limiter: RateLimiter) -> None:
        """#467: the resource/system soft restamp, SET and REMOVE together."""
        repo = limiter._repository
        client = await repo._get_client()
        spy = await self._spy(repo)
        pk = pk_bucket(repo._namespace_id, "user-1", "api", 0)
        with patch.object(client, "update_item", spy.forward):
            await repo._stamp_bucket_soft(pk, {DOTTED: True, HYPHENATED: False})
            await repo._stamp_bucket_soft(pk, {DOTTED: False})
            await repo._stamp_bucket_soft(pk, {DOTTED: True})
        assert spy.call_count == 3
        for call in spy.call_args_list:
            assert_expression_safe(call.kwargs)

    async def test_cascade_fan_out(self, limiter: RateLimiter) -> None:
        """ADR-146: the cascade-policy stamp, with and without a parent."""
        repo = limiter._repository
        await repo.set_limits("user-1", [Limit.per_minute(DOTTED, 10)], resource="api")
        async with limiter.acquire("user-1", "api", consume={DOTTED: 1}):
            pass
        client = await repo._get_client()
        spy = await self._spy(repo)
        pk = pk_bucket(repo._namespace_id, "user-1", "api", 0)
        with patch.object(client, "update_item", spy.forward):
            await repo._stamp_bucket_cascade(pk, True, "org-1")
            await repo._stamp_bucket_cascade(pk, False, None)
            await repo._stamp_bucket_cascade(pk, True, "org-1", 1)  # ADR-150
            await repo._stamp_bucket_cascade(pk, False, None, 2)
            await repo._stamp_bucket_cascade(pk, True, "org-1", 3, "abc123")  # incarnation
        assert spy.call_count == 5
        for call in spy.call_args_list:
            assert_expression_safe(call.kwargs)

    async def test_parent_move_meta_write(self, limiter: RateLimiter) -> None:
        """ADR-150: the META write, to a parent and to no parent."""
        repo = limiter._repository
        await repo.create_entity("org-1")
        await repo.create_entity("user-1", parent_id="org-1")
        client = await repo._get_client()
        spy = await self._spy(repo)
        with patch.object(client, "update_item", spy.forward):
            await repo._write_parent("user-1", None)
            await repo._write_parent("user-1", "org-1")
        assert spy.call_count == 2
        for call in spy.call_args_list:
            assert_expression_safe(call.kwargs)
