"""Bypass: admit everything, debit nothing, keep counting (#311, ADR-151)."""

import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded, schema
from zae_limiter.exceptions import ResourceDisabled, VersionMismatchError
from zae_limiter.lease import BypassLostError, LeaseEntry, _bypass_write_failed
from zae_limiter.models import BucketState, Limit
from zae_limiter.repository import Repository
from zae_limiter.repository_protocol import SpeculativeFailureReason

RPM = Limit.per_minute("rpm", 2)


@pytest.fixture
async def repo(mock_dynamodb):
    """Moto-backed repository on a stack whose Lambdas read bypass."""
    from zae_limiter import __version__
    from zae_limiter.version import get_schema_version

    repo = Repository(name="test-bypass", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    await repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    yield repo
    await repo.close()


@pytest.fixture
async def limiter(repo):
    await repo.set_resource_defaults("llm", [RPM])
    async with RateLimiter(repository=repo) as limiter:
        yield limiter


def _pk(repo: Repository, entity: str = "u", resource: str = "llm", shard: int = 0) -> str:
    return schema.pk_bucket(repo._namespace_id, entity, resource, shard)


async def _item(repo: Repository, entity: str = "u", resource: str = "llm", shard: int = 0):
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={"PK": {"S": _pk(repo, entity, resource, shard)}, "SK": {"S": schema.sk_state()}},
        ConsistentRead=True,
    )
    return response.get("Item")


def _n(item, attr: str) -> int:
    return int(item[attr]["N"])


def _live(item, now_ms: int | None = None) -> bool:
    """The item's `bypass` stamp is a deadline still ahead (ADR-151 decision 8)."""
    raw = (item or {}).get("bypass", {}).get("N")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return raw is not None and int(raw) > now_ms


async def _use(limiter: RateLimiter, n: int = 1, entity: str = "u") -> None:
    async with limiter.acquire(entity, "llm", {"rpm": n}):
        pass


class TestCodec:
    def test_encode(self):
        assert schema.encode_disabled("bypass") == {"S": "bypass"}
        assert schema.encode_disabled(True) == {"BOOL": True}
        with pytest.raises(ValueError, match="bypass"):
            schema.encode_disabled("off")

    def test_decode(self):
        assert schema.decode_disabled({"disabled": {"S": "bypass"}}) == "bypass"
        # An unknown string reads as explicit enforce, as a pre-0.17 reader does.
        assert schema.decode_disabled({"disabled": {"S": "later"}}) is False


class TestWalk:
    async def _resolve(self, repo, entity="u"):
        access = await repo.resolve_access(entity, "llm")
        return access.disabled, access.bypass

    async def test_resource_bypass_reaches_entities(self, repo):
        await repo.bypass_resource("llm")
        assert await repo.get_resource_disabled("llm") == "bypass"
        assert await self._resolve(repo) == (False, True)
        # The fetched-config twin agrees, and declines on a partial read.
        ns = repo._namespace_id
        fetched = {
            (schema.pk_entity(ns, "u"), schema.sk_config("llm")): None,
            (schema.pk_entity(ns, "u"), schema.sk_config("_default_")): None,
            (schema.pk_resource(ns, "llm"), schema.sk_config()): "bypass",
        }
        assert repo.resolve_bypass_from_fetched("u", "llm", fetched) is True
        assert repo.resolve_disabled_from_fetched("u", "llm", fetched) == (False, "resource")
        assert repo.resolve_bypass_from_fetched("u", "llm", {}) is None

    async def test_an_entity_bypass_is_a_carve_out_from_a_disabled_resource(self, repo):
        await repo.disable_resource("llm")
        await repo.bypass_entity("vip", "llm")
        assert await repo.get_entity_disabled("vip", "llm") == "bypass"
        assert await self._resolve(repo, "vip") == (False, True)
        assert await self._resolve(repo, "u") == (True, False)

    async def test_an_entity_disable_blocks_under_a_bypassed_resource(self, repo, limiter):
        await repo.bypass_resource("llm")
        await repo.disable_entity("bad", "llm")
        with pytest.raises(ResourceDisabled):
            await _use(limiter, entity="bad")

    async def test_an_explicit_enforce_carves_out_of_a_bypass(self, repo, limiter):
        await repo.bypass_resource("llm")
        await repo.enable_entity("u", "llm")
        await _use(limiter, 2)
        with pytest.raises(RateLimitExceeded):
            await _use(limiter)

    async def test_an_entity_wide_bypass_covers_every_resource(self, repo, limiter):
        await repo.bypass_entity("u")
        for _ in range(5):
            await _use(limiter)
        lease_item = await _item(repo)
        assert _live(lease_item)


class TestAdmission:
    async def test_admits_without_debiting_and_counts(self, repo, limiter):
        await _use(limiter)  # bucket exists, enforced
        await repo.bypass_resource("llm")
        for _ in range(5):
            async with limiter.acquire("u", "llm", {"rpm": 1}) as lease:
                assert lease.bypassed is True
        item = await _item(repo)
        assert _n(item, "b_rpm_tk") == 1_000  # untouched since the enforced use
        assert _n(item, "b_rpm_tc") == 6_000

    async def test_a_bypassed_create_is_funded_and_counts(self, repo, limiter):
        await repo.bypass_resource("llm")
        async with limiter.acquire("u", "llm", {"rpm": 5}) as lease:
            assert lease.bypassed is True
        item = await _item(repo)
        assert _live(item)
        assert _n(item, "b_rpm_tk") == 2_000  # the full share, nothing debited
        assert _n(item, "b_rpm_tc") == 5_000

    async def test_lifting_the_bypass_leaves_no_debt(self, repo, limiter):
        await _use(limiter)
        await repo.bypass_resource("llm")
        for _ in range(10):
            await _use(limiter)
        await repo.clear_resource_disabled("llm")
        assert "bypass" not in await _item(repo)
        await _use(limiter)  # the one token left is still there
        with pytest.raises(RateLimitExceeded):
            await _use(limiter)

    async def test_availability_reports_it(self, repo, limiter):
        await repo.bypass_resource("llm")
        await _use(limiter)
        check = await limiter.check_availability("u", "llm")
        assert check.bypassed is True

    async def test_no_bucket_reads_as_not_bypassed(self, limiter):
        check = await limiter.check_availability("u", "llm")
        assert check.bypassed is False


class TestWarmPathLearning:
    async def test_the_first_request_after_bypass_is_refunded_once(self, repo, limiter):
        await _use(limiter)  # warm, enforce-shaped cache
        await repo.bypass_resource("llm")
        with patch.object(repo, "write_each", wraps=repo.write_each) as refunds:
            await _use(limiter)
            await _use(limiter)
        assert refunds.await_count == 1  # the refund, once; then bypass-shaped
        assert (repo._namespace_id, "u", "llm") in repo._bypass_cache
        item = await _item(repo)
        assert _n(item, "b_rpm_tk") == 1_000
        assert _n(item, "b_rpm_tc") == 3_000

    async def test_a_cleared_bypass_is_enforced_on_the_next_request(self, repo, limiter):
        await _use(limiter)
        await repo.bypass_resource("llm")
        await _use(limiter)
        await _use(limiter)  # cache says bypass
        # Cleared by another process: the cache still believes bypass.
        other = Repository(name="test-bypass", region="us-east-1", _skip_deprecation_warning=True)
        await other.clear_resource_disabled("llm")
        await other.close()
        await _use(limiter)  # one failed bypass write, then enforced: 1 left -> 0
        assert (repo._namespace_id, "u", "llm") not in repo._bypass_cache
        with pytest.raises(RateLimitExceeded):
            await _use(limiter)

    async def test_a_contended_bypass_shaped_write_goes_to_the_slow_path(self, repo, limiter):
        """Review D8: a #724 CONTENTION on the bypass-shaped write wrote
        nothing and returned no image, yet was read as "bypass cleared": the
        request was written again enforce-shaped on the same busy item. It
        now goes to the slow path, and the pair stays learned."""
        from botocore.exceptions import ClientError

        await repo.bypass_resource("llm")
        await _use(limiter)
        await _use(limiter)  # learns the stamp
        key = (repo._namespace_id, "u", "llm")
        assert key in repo._bypass_cache
        conflict = ClientError({"Error": {"Code": "TransactionConflictException"}}, "UpdateItem")
        with patch.object(repo, "_update_item_retrying_conflict", side_effect=conflict) as writes:
            result = await repo._speculative_consume_single("u", "llm", {"rpm": 1})
        assert result.failure_reason is SpeculativeFailureReason.CONTENTION
        assert writes.await_count == 1  # no enforce-shaped second write
        assert key in repo._bypass_cache

    async def test_a_refund_after_a_refill_to_capacity_trims_itself(self, repo, limiter):
        """Review D6: the bypass refund was a bare `ADD tk`, outside the #721
        self-trimming credit. A refill to capacity landing between the debit
        and the refund lifted the balance above the ceiling with nothing to
        stop the fast path spending it once the bypass is lifted."""
        await _use(limiter)  # warm, enforce-shaped cache; 1 of 2 left
        await repo.bypass_resource("llm")
        real = repo._refund_bypassed_debit
        client = await repo._get_client()

        async def refilled_first(*args, **kwargs):
            await client.update_item(
                TableName=repo.table_name,
                Key={"PK": {"S": _pk(repo)}, "SK": {"S": schema.sk_state()}},
                UpdateExpression="SET b_rpm_tk = :cap",
                ExpressionAttributeValues={":cap": {"N": "2000"}},
            )
            return await real(*args, **kwargs)

        with patch.object(repo, "_refund_bypassed_debit", side_effect=refilled_first):
            await _use(limiter)
        item = await _item(repo)
        # Above the ceiling only with `vu = 0`: the fast path cannot spend it,
        # and the next slow pass clamps it.
        assert _n(item, "b_rpm_tk") <= 2_000 or _n(item, "vu") == 0
        assert _n(item, "b_rpm_tc") == 2_000  # the refund leaves the count alone

    async def test_a_failed_refund_is_logged_and_keeps_the_debit(self, repo, limiter):
        await _use(limiter)
        await repo.bypass_resource("llm")
        with patch.object(repo, "write_each", side_effect=RuntimeError("throttled")):
            await _use(limiter)
        item = await _item(repo)
        assert _n(item, "b_rpm_tk") == 0  # debited, not refunded: under-admission only

    async def test_wcu_still_gates_and_doubles_under_bypass(self, repo, limiter):
        await _use(limiter)
        await repo.bypass_resource("llm")
        await _use(limiter)  # learn
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={"PK": {"S": _pk(repo)}, "SK": {"S": schema.sk_state()}},
            UpdateExpression="SET b_wcu_tk = :zero, b_wcu_rp = :long",
            ExpressionAttributeValues={":zero": {"N": "0"}, ":long": {"N": "86400000"}},
        )
        async with limiter.acquire("u", "llm", {"rpm": 1}) as lease:
            pass
        assert lease.bypassed is True
        assert _n(await _item(repo), "shard_count") == 2

    async def test_a_missing_bucket_in_bypass_shape_is_not_retried(self, repo):
        repo._bypass_cache.add((repo._namespace_id, "ghost", "llm"))
        result = await repo._speculative_consume_single("ghost", "llm", {"rpm": 1})
        assert result.failure_reason.name == "BUCKET_MISSING"
        assert (repo._namespace_id, "ghost", "llm") not in repo._bypass_cache


class TestLeaseWritesCountersOnly:
    async def _bypassed(self, repo, limiter):
        await repo.bypass_resource("llm")
        await _use(limiter)
        await _use(limiter)

    async def test_adjust_and_release(self, repo, limiter):
        await self._bypassed(repo, limiter)
        async with limiter.acquire("u", "llm", {"rpm": 1}) as lease:
            await lease.consume(rpm=10)  # never rejected under bypass
            await lease.release(rpm=3)
        item = await _item(repo)
        assert _n(item, "b_rpm_tk") == 2_000
        assert _n(item, "b_rpm_tc") == 10_000  # 2 + 1 + 10 - 3

    async def test_rollback(self, repo, limiter):
        await self._bypassed(repo, limiter)
        with pytest.raises(RuntimeError):
            async with limiter.acquire("u", "llm", {"rpm": 1}):
                raise RuntimeError("boom")
        item = await _item(repo)
        assert _n(item, "b_rpm_tk") == 2_000
        assert _n(item, "b_rpm_tc") == 2_000


class TestSlowPath:
    async def test_existing_bucket_slow_path_writes_tc_only(self, repo, limiter):
        await _use(limiter)
        await repo.bypass_resource("llm")
        cold = RateLimiter(repository=repo)
        lease = await cold._do_acquire("u", "llm", None, {"rpm": 5})
        assert lease.bypassed is True
        await lease._commit_initial()
        item = await _item(repo)
        assert _n(item, "b_rpm_tk") == 1_000
        assert _n(item, "b_rpm_tc") == 6_000

    async def test_a_stamp_config_no_longer_backs_is_enforced(self, repo, limiter):
        await _use(limiter, 2)
        await repo._stamp_bucket_disabled(_pk(repo), "bypass")  # stale stamp
        lease_states = await repo.get_buckets("u", "llm")
        assert all(state.bypass for state in lease_states)
        with pytest.raises(RateLimitExceeded):
            await RateLimiter(repository=repo)._do_acquire("u", "llm", None, {"rpm": 1})

    async def test_config_bypass_without_the_stamp_is_enforced(self, repo, limiter):
        await _use(limiter, 2)
        await repo._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, "bypass")
        with pytest.raises(RateLimitExceeded):
            await RateLimiter(repository=repo)._do_acquire("u", "llm", None, {"rpm": 1})

    async def test_a_slow_pass_removes_a_stamp_config_no_longer_backs(self, repo, limiter):
        """Review D16: the clear's fan-out missed this bucket, and the slow
        pass that read config uncached removes the stale stamp (ADR-151
        decision 4) instead of leaving it to admit on the fast path."""
        await repo.bypass_resource("llm")
        await _use(limiter)  # created stamped, nothing debited
        assert "bypass" in await _item(repo)
        await repo._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, None)
        async with RateLimiter(repository=repo, speculative_writes=False) as slow:
            async with slow.acquire("u", "llm", {"rpm": 1}) as lease:
                assert lease.bypassed is False
        item = await _item(repo)
        assert "bypass" not in item
        assert _n(item, "b_rpm_tk") == 1_000  # enforced: debited
        # The fast path enforces from here on.
        await _use(limiter)
        with pytest.raises(RateLimitExceeded):
            await _use(limiter)

    async def test_a_slow_pass_sets_a_stamp_config_asks_for(self, repo, limiter):
        """Review D16, the other direction: bypass config whose fan-out never
        reached the bucket. This request is enforced; its write stamps the
        bucket, and the fast path bypasses from then on."""
        await _use(limiter)
        await repo._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, "bypass")
        async with RateLimiter(repository=repo, speculative_writes=False) as slow:
            async with slow.acquire("u", "llm", {"rpm": 1}) as lease:
                assert lease.bypassed is False
        item = await _item(repo)
        assert "bypass" in item
        assert _n(item, "b_rpm_tk") < 1_000  # this request was enforced
        for _ in range(5):
            await _use(limiter)
        assert _n(await _item(repo), "b_rpm_tc") == 7_000

    async def test_a_bypass_lost_before_the_write_is_replanned_enforced(self, repo, limiter):
        await _use(limiter)
        await repo.bypass_resource("llm")
        cold = RateLimiter(repository=repo)
        lease = await cold._do_acquire("u", "llm", None, {"rpm": 1})
        await repo.clear_resource_disabled("llm")  # lands between read and write
        with pytest.raises(BypassLostError):
            await lease._commit_initial()

        real = cold._do_acquire
        calls = []

        async def clearing(*args, **kwargs):
            calls.append(kwargs.get("disable_bypass"))
            planned = await real(*args, **kwargs)
            if len(calls) == 1:
                await repo._stamp_bucket_disabled(_pk(repo), False)
            return planned

        await repo.bypass_resource("llm")
        with patch.object(cold, "_do_acquire", side_effect=clearing):
            lease = await cold._slow_acquire("u", "llm", None, {"rpm": 1})
        assert calls == [False, True]
        assert lease.bypassed is False
        assert _n(await _item(repo), "b_rpm_tk") < 1_000  # debited, enforced

    async def test_a_multi_resource_bypass_lost_before_the_write_is_replanned_enforced(
        self, repo, limiter
    ):
        """Review D5: the multi-resource slow path had no `BypassLostError`
        handling, so a bypass cleared between a part's config read and the
        joint commit reached `acquire()`'s generic handler (admitted without
        limit under ALLOW). It re-plans every part enforced, as `_slow_acquire`
        does."""
        await repo.set_resource_defaults("api", [RPM])
        await _use(limiter)
        async with limiter.acquire("u", "api", {"rpm": 1}):
            pass
        await repo.bypass_resource("api")
        cold = RateLimiter(repository=repo)
        real = cold._do_acquire
        calls: list[tuple[str, bool]] = []

        async def clearing(*args, **kwargs):
            calls.append((kwargs["resource"], kwargs.get("disable_bypass", False)))
            planned = await real(*args, **kwargs)
            if len(calls) == 2:  # both parts planned: the clear lands now
                await repo._stamp_bucket_disabled(_pk(repo, resource="api"), False)
            return planned

        parts = [("llm", {"rpm": 1}, 0, 1, None), ("api", {"rpm": 1}, 0, 1, None)]
        with patch.object(cold, "_do_acquire", side_effect=clearing):
            lease, _unknown = await cold._slow_acquire_resources("u", parts)
        assert sorted(calls) == [("api", False), ("api", True), ("llm", False), ("llm", True)]
        assert lease.bypassed is False
        assert _n(await _item(repo, resource="api"), "b_rpm_tk") < 1_000  # enforced

    async def test_an_undeclared_limit_is_not_written_under_bypass(self, repo, limiter):
        await repo.set_resource_defaults("llm", [RPM, Limit.per_minute("tpm", 10)])
        await _use(limiter)
        await repo.bypass_resource("llm")
        lease = await RateLimiter(repository=repo)._do_acquire("u", "llm", None, {"rpm": 1})
        assert [e.limit.name for e in lease.entries] == ["rpm"]
        await lease._commit_initial()
        item = await _item(repo)
        assert _n(item, "b_tpm_tc") == 0

    async def _lose_the_create_race(self, repo, stamp: bool):
        await repo.bypass_resource("llm")
        cold = RateLimiter(repository=repo)
        lease = await cold._do_acquire("u", "llm", None, {"rpm": 1})
        # Another creator lands first.
        other = await RateLimiter(repository=repo)._do_acquire("u", "llm", None, {"rpm": 1})
        await other._commit_initial()
        if not stamp:
            await repo._stamp_bucket_disabled(_pk(repo), False)
        return lease

    async def test_a_bypassed_create_that_loses_its_race_counts_on_the_winner(self, repo, limiter):
        lease = await self._lose_the_create_race(repo, stamp=True)
        await lease._commit_initial()
        item = await _item(repo)
        assert _n(item, "b_rpm_tc") == 2_000
        assert _n(item, "b_rpm_tk") == 2_000

    async def test_a_lost_race_onto_an_unstamped_item_is_replanned(self, repo, limiter):
        lease = await self._lose_the_create_race(repo, stamp=False)
        with pytest.raises(BypassLostError):
            await lease._commit_initial()

    async def test_a_refund_of_nothing_writes_nothing(self, repo):
        from zae_limiter.repository_protocol import SpeculativeResult

        with patch.object(repo, "write_each") as write:
            await repo._refund_bypassed_debit(
                "u", "llm", {"rpm": 0}, 0, SpeculativeResult(success=True)
            )
        write.assert_not_called()

    def test_failure_attribution(self):
        def entry(bypass: bool, new: bool = False) -> LeaseEntry:
            return LeaseEntry(
                entity_id="u",
                resource="llm",
                limit=RPM,
                state=BucketState.from_limit("u", "llm", RPM, 0),
                _bypass=bypass,
                _is_new=new,
            )

        assert _bypass_write_failed([[entry(True)]], None) is True
        assert _bypass_write_failed([[entry(False)], [entry(True)]], ["None", "None"]) is False
        assert _bypass_write_failed([[entry(True)]], ["ConditionalCheckFailed"]) is True
        assert (
            _bypass_write_failed(
                [[entry(True, new=True)]], ["ConditionalCheckFailed"], creates_lose_races=True
            )
            is False
        )

    async def test_a_session_quota_opens_no_window_under_bypass(self, repo, limiter):
        session = Limit.quota("session", 10, reset_after=timedelta(hours=1))
        await repo.set_resource_defaults("llm", [session])
        await repo.bypass_resource("llm")
        async with limiter.acquire("u", "llm", {"session": 3}):
            pass
        item = await _item(repo)
        assert "b_session_ws" not in item
        assert _n(item, "b_session_tk") == 10_000
        assert _n(item, "b_session_tc") == 3_000
        # Once lifted, the first enforced pass opens the window.
        await repo.clear_resource_disabled("llm")
        async with limiter.acquire("u", "llm", {"session": 3}):
            pass
        item = await _item(repo)
        assert "b_session_ws" in item
        assert _n(item, "b_session_tk") == 7_000

    async def test_a_parent_only_pass_defers_a_bypassed_parent(self, repo):
        await repo.set_resource_defaults("llm", [RPM])
        limiter = RateLimiter(repository=repo)
        await repo.bypass_entity("org", "llm")
        await repo.create_entity("org")
        await repo._stamp_bucket_disabled(_pk(repo, "org"), "bypass")
        state = BucketState.from_limit("org", "llm", RPM, repo._now_ms())
        state.bypass = True
        with patch.object(
            limiter,
            "_fetch_entity_and_buckets",
            return_value=(None, {("org", "llm", "rpm"): state}),
        ):
            assert (
                await limiter._try_parent_only_acquire("org", "llm", {"rpm": 1}, [], 0, 1)
            ) is None


class TestCascade:
    async def _family(self, repo):
        await repo.create_entity("org")
        await repo.create_entity("u", parent_id="org", cascade=True)

    async def test_a_child_bypass_never_relaxes_its_parent(self, repo, limiter):
        await self._family(repo)
        await repo.bypass_entity("u", "llm")
        await _use(limiter, 2)
        with pytest.raises(RateLimitExceeded) as exc_info:
            await _use(limiter)
        assert {s.entity_id for s in exc_info.value.violations} == {"org"}
        child = await _item(repo)
        assert _n(child, "b_rpm_tc") == 2_000  # the rejected request is not counted

    async def test_warm_parallel_parent_rejection_compensates_the_child_counter(
        self, repo, limiter
    ):
        await self._family(repo)
        await repo.bypass_entity("u", "llm")
        await _use(limiter)  # creates both, learns the family
        await _use(limiter)  # warm: child bypass-shaped, parent enforced
        with pytest.raises(RateLimitExceeded):
            await _use(limiter)
        child = await _item(repo)
        assert _n(child, "b_rpm_tc") == 2_000
        assert _n(child, "b_rpm_tk") == 2_000

    async def test_a_bypassed_parent_admits_an_enforced_child(self, repo, limiter):
        await self._family(repo)
        await repo.bypass_entity("org", "llm")
        await _use(limiter, 2)
        with pytest.raises(RateLimitExceeded) as exc_info:
            await _use(limiter)
        assert {s.entity_id for s in exc_info.value.violations} == {"u"}
        parent = await _item(repo, "org")
        assert _n(parent, "b_rpm_tk") == 2_000


class TestFanout:
    async def test_bypass_then_disable_then_clear(self, repo, limiter):
        await _use(limiter)
        assert await repo.bypass_resource("llm") == 1
        item = await _item(repo)
        assert _live(item) and "disabled" not in item
        await repo.disable_resource("llm")
        item = await _item(repo)
        assert item["disabled"] == {"BOOL": True} and "bypass" not in item
        await repo.clear_resource_disabled("llm")
        item = await _item(repo)
        assert "disabled" not in item and "bypass" not in item

    async def test_entity_wide_fanout_restamps_each_resource(self, repo, limiter):
        await repo.set_resource_defaults("api", [RPM])
        await _use(limiter)
        async with limiter.acquire("u", "api", {"rpm": 1}):
            pass
        await repo.disable_entity("u", "api")
        await repo.bypass_entity("u")
        assert _live(await _item(repo))
        assert (await _item(repo, resource="api"))["disabled"] == {"BOOL": True}
        await repo.clear_entity_disabled("u")
        assert "bypass" not in await _item(repo)

    async def test_setters_take_the_bypass_value(self, repo, limiter):
        await _use(limiter)
        await repo.set_resource_defaults("llm", [RPM], disabled="bypass")
        assert _live(await _item(repo))
        await repo.set_limits("u", [RPM], resource="llm", disabled="bypass")
        assert await repo.get_entity_disabled("u", "llm") == "bypass"
        await repo.set_resource_defaults("llm", [RPM], disabled=None)
        assert _live(await _item(repo))  # the entity still bypasses
        await repo.delete_limits("u", resource="llm")
        assert "bypass" not in await _item(repo)

    async def test_an_unknown_value_is_rejected_before_any_write(self, repo):
        with pytest.raises(ValueError):
            await repo.set_resource_defaults("llm", [RPM], disabled="maybe")
        with pytest.raises(ValueError):
            await repo._set_entity_disabled("u", "llm", "maybe", None)
        assert await repo.get_resource_defaults("llm") == []


#: Past every deadline a stamp written at the start could carry (ADR-151 decision 8).
PAST_T_MS = schema.DEFAULT_STAMP_TTL_SECONDS * 1000 + 1


async def _admitted(repo: Repository, attempts: int = 20) -> int:
    """Acquire `rpm: 1` from fresh limiters on a cold config cache; count admits."""
    admitted = 0
    for _ in range(attempts):
        async with RateLimiter(repository=repo) as fresh:
            await repo.invalidate_config_cache()
            try:
                await _use(fresh)
                admitted += 1
            except RateLimitExceeded:
                pass
    return admitted


class TestStampsExpire:
    """Review D1: a `bypass` stamp a fan-out missed admitted without limit,
    forever (20 admitted against rpm=2). A stamp is a deadline at most T after
    the config read that justified it (ADR-151 decision 8)."""

    async def test_a_stamp_a_fan_out_missed_stops_admitting_within_t(self, repo):
        await repo.set_resource_defaults("llm", [RPM])
        await repo.bypass_resource("llm")
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            async with RateLimiter(repository=repo) as limiter:
                await _use(limiter)
        assert _live(await _item(repo), start)
        # The clear's fan-out misses this bucket (the ADR-125 in-flight race):
        # config no longer says bypass, the stamp stays.
        await repo._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, None)
        assert (await repo.resolve_access("u", "llm")).bypass is False

        with patch.object(repo, "_now_ms", return_value=start + PAST_T_MS):
            admitted = await _admitted(repo)
        item = await _item(repo)
        assert admitted <= 2, f"admitted {admitted} against rpm=2; stamp {item.get('bypass')}"
        # The slow pass the expired stamp forced removed it.
        assert "bypass" not in item

    async def test_a_create_racing_a_bypass_clear_stops_admitting_within_t(self, repo):
        """A bypassed create planned before `clear_resource_disabled()` lands
        after its fan-out, which found no bucket: the new bucket carries the
        stamp, but only until its deadline."""
        await repo.set_resource_defaults("llm", [RPM])
        await repo.bypass_resource("llm")
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            cold = RateLimiter(repository=repo)
            lease = await cold._do_acquire("u", "llm", None, {"rpm": 1})  # a bypassed create
            assert await repo.clear_resource_disabled("llm") == 0  # no bucket yet
            await lease._commit_initial()  # lands stamped
        assert _live(await _item(repo), start)

        with patch.object(repo, "_now_ms", return_value=start + PAST_T_MS):
            admitted = await _admitted(repo)
        assert admitted <= 2, f"admitted {admitted} against rpm=2 after the bypass was cleared"
        assert "bypass" not in await _item(repo)

    async def test_an_expired_stamp_is_renewed_while_the_bypass_stands(self, repo, limiter):
        await repo.bypass_resource("llm")
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            await _use(limiter)  # created stamped, nothing debited
        later = start + PAST_T_MS
        with patch.object(repo, "_now_ms", return_value=later):
            for _ in range(5):
                async with limiter.acquire("u", "llm", {"rpm": 1}) as lease:
                    assert lease.bypassed is True
        item = await _item(repo)
        assert _live(item, later)  # renewed from config read uncached
        assert _n(item, "bypass") <= later + schema.DEFAULT_STAMP_TTL_SECONDS * 1000
        assert _n(item, "b_rpm_tk") == 2_000  # nothing debited
        assert _n(item, "b_rpm_tc") == 6_000

    async def test_an_expired_stamp_on_the_bypass_shape_costs_one_write(self, repo, limiter):
        """The bypass shape fails on the deadline and goes straight to the slow
        path: no second, enforce-shaped write that would fail the same way."""
        await repo.bypass_resource("llm")
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            await _use(limiter)
            await _use(limiter)  # learns the stamp
        key = (repo._namespace_id, "u", "llm")
        assert key in repo._bypass_cache
        client = await repo._get_client()
        writes = []
        original = client.update_item

        async def counting(**kwargs):
            writes.append(kwargs["ConditionExpression"])
            return await original(**kwargs)

        with patch.object(client, "update_item", counting):
            result = await repo._speculative_consume_single(
                "u", "llm", {"rpm": 1}, now_ms=start + PAST_T_MS
            )
        assert result.failure_reason is SpeculativeFailureReason.SCHEDULE_BOUNDARY
        assert result.bypassed is False
        assert len(writes) == 1 and "#byp > :vu_now" in writes[0]
        # The stamp is present, only not live: the pair stays learned.
        assert key in repo._bypass_cache

    async def test_a_pre_release_boolean_stamp_reads_as_expired(self, repo, limiter):
        await repo.bypass_resource("llm")
        await _use(limiter)
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={"PK": {"S": _pk(repo)}, "SK": {"S": schema.sk_state()}},
            UpdateExpression="SET #byp = :t",
            ExpressionAttributeNames={"#byp": "bypass"},
            ExpressionAttributeValues={":t": {"BOOL": True}},
        )
        states = await repo.get_buckets("u", "llm")
        assert {s.bypass_until_ms for s in states} == {schema.STAMP_EXPIRED}
        async with limiter.acquire("u", "llm", {"rpm": 1}) as lease:
            assert lease.bypassed is True
        assert _live(await _item(repo))  # replaced by a deadline

    async def test_a_restamp_is_pinned_on_the_stamp_as_read(self, repo, limiter):
        """A pass that read config before a fan-out cannot undo the fan-out."""
        await repo.bypass_resource("llm")
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            await _use(limiter)
        await repo._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, None)
        with patch.object(repo, "_now_ms", return_value=start + 1_000):
            # Plans an enforced write that REMOVEs the stamp it read.
            lease = await RateLimiter(repository=repo)._do_acquire("u", "llm", None, {"rpm": 1})
        with patch.object(repo, "_now_ms", return_value=start + 2_000):
            await repo.bypass_resource("llm")  # a later fan-out stamps a new deadline
        stamped = _n(await _item(repo), "bypass")
        with patch.object(repo, "_now_ms", return_value=start + 3_000):
            await lease._commit_initial()
        item = await _item(repo)
        assert _n(item, "bypass") == stamped  # the fan-out's stamp stands
        assert _n(item, "b_rpm_tk") == 1_000  # the retry debited, enforced

    async def test_a_rejected_pass_removes_a_stamp_config_no_longer_backs(self, repo):
        """Review E1: a rejection wrote nothing, so an expired stamp on an
        exhausted quota kept every request on the slow path until the next
        reset. The first rejection now removes it; the rest are fast."""
        await repo.set_resource_defaults("llm", [Limit.quota("rpy", 2, cron="0 0 1 1 *")])
        start = repo._now_ms()
        limiter = RateLimiter(repository=repo)
        with patch.object(repo, "_now_ms", return_value=start):
            async with limiter.acquire("u", "llm", {"rpy": 2}):
                pass  # spent
            await repo.bypass_resource("llm")
            async with limiter.acquire("u", "llm", {"rpy": 1}) as lease:
                assert lease.bypassed is True  # learned, nothing debited
        # The clear's fan-out misses this bucket.
        await repo._write_resource_config_flag("llm", schema.CONFIG_FIELD_DISABLED, None)

        slow = 0
        original = limiter._do_acquire

        async def counting(*args, **kwargs):
            nonlocal slow
            slow += 1
            return await original(*args, **kwargs)

        rejected = 0
        with (
            patch.object(repo, "_now_ms", return_value=start + PAST_T_MS),
            patch.object(limiter, "_do_acquire", counting),
        ):
            for _ in range(10):
                try:
                    async with limiter.acquire("u", "llm", {"rpy": 1}):
                        pass
                except RateLimitExceeded:
                    rejected += 1
        item = await _item(repo)
        assert rejected == 10
        assert "bypass" not in item
        assert slow == 1, f"{slow} slow passes for 10 rejections"
        assert _n(item, "b_rpy_tk") == 0

    async def test_a_bypassed_child_rejected_by_its_parent_renews_its_stamp(self, repo):
        """Review E1, cascade: the child's bypassed write would have renewed its
        stamp; the parent's 429 wrote nothing, so it is renewed alone."""
        rph = Limit.per_hour("rph", 2)
        await repo.set_resource_defaults("llm", [rph])
        await repo.create_entity("p")
        await repo.create_entity("c", parent_id="p", cascade=True)
        await repo.bypass_entity("c", resource="llm")
        start = repo._now_ms()
        limiter = RateLimiter(repository=repo)
        with patch.object(repo, "_now_ms", return_value=start):
            async with limiter.acquire("c", "llm", {"rph": 1}) as lease:
                assert lease.bypassed is True
            async with limiter.acquire("p", "llm", {"rph": 1}):
                pass  # the parent is spent
        later = start + PAST_T_MS
        with patch.object(repo, "_now_ms", return_value=later):
            with pytest.raises(RateLimitExceeded):
                async with limiter.acquire("c", "llm", {"rph": 1}):
                    pass
        child = await _item(repo, "c")
        assert _live(child, later)
        assert _n(child, "bypass") <= later + schema.DEFAULT_STAMP_TTL_SECONDS * 1000
        assert _n(child, "b_rph_tk") == 2_000  # nothing debited

    async def test_stamp_ttl_bounds_every_deadline_this_writer_sets(self, mock_dynamodb):
        """T is the writer's `stamp_ttl`: a create and a fan-out alike."""
        from zae_limiter import __version__
        from zae_limiter.version import get_schema_version

        repo = Repository(
            name="test-bypass-ttl",
            region="us-east-1",
            stamp_ttl=20,
            _skip_deprecation_warning=True,
        )
        await repo.create_table()
        await repo._register_namespace("default")
        await repo.set_version_record(
            schema_version=get_schema_version(), lambda_version=__version__
        )
        try:
            await repo.set_resource_defaults("llm", [RPM])
            now = repo._now_ms()
            with patch.object(repo, "_now_ms", return_value=now):
                await repo.bypass_resource("llm")
                async with RateLimiter(repository=repo) as limiter:
                    await _use(limiter)  # a bypassed create
            created = _n(await _item(repo), "bypass")
            assert now + 16_000 <= created <= now + 20_000
            with patch.object(repo, "_now_ms", return_value=now + 1):
                await repo._fanout_resource("llm", "bypass")
            fanned = _n(await _item(repo), "bypass")
            assert now + 1 + 16_000 <= fanned <= now + 1 + 20_000
        finally:
            await repo.close()

    def test_stamp_ttl_below_ten_seconds_is_refused(self):
        with pytest.raises(ValueError, match="at least 10"):
            Repository(name="x", region="us-east-1", stamp_ttl=5, _skip_deprecation_warning=True)

    def test_the_deadline_is_at_most_t_after_the_read(self):
        deadlines = {schema.stamp_deadline(1_000, 60) for _ in range(200)}
        assert all(1_000 + 48_000 <= d <= 1_000 + 60_000 for d in deadlines)
        assert len(deadlines) > 1  # jittered, so renewals spread out
        assert schema.decode_stamp(None) is None
        assert schema.decode_stamp({"N": "5"}) == 5
        assert schema.decode_stamp({"BOOL": True}) == schema.STAMP_EXPIRED
        assert schema.stamp_value(schema.STAMP_EXPIRED) == {"BOOL": True}
        assert schema.stamp_value(5) == {"N": "5"}


class TestVersionGate:
    @staticmethod
    async def _stamp(repo, lambda_version):
        from zae_limiter.version import get_schema_version

        await repo.set_version_record(
            schema_version=get_schema_version(),
            lambda_version=lambda_version,
            client_min_version="0.0.0",
        )

    @pytest.mark.parametrize(
        "write",
        [
            lambda repo: repo.bypass_resource("llm"),
            lambda repo: repo.bypass_entity("u"),
            lambda repo: repo.set_resource_defaults("llm", [RPM], disabled="bypass"),
            lambda repo: repo.set_limits("u", [RPM], resource="llm", disabled="bypass"),
        ],
    )
    async def test_refused_while_the_lambdas_predate_it(self, repo, write):
        await self._stamp(repo, "0.16.0")
        with patch("zae_limiter.__version__", "0.17.0"):
            with pytest.raises(VersionMismatchError):
                await write(repo)
        assert await repo.get_resource_disabled("llm") is None

    async def test_disable_is_not_gated(self, repo):
        await self._stamp(repo, "0.16.0")
        with patch("zae_limiter.__version__", "0.17.0"):
            await repo.disable_resource("llm")


class TestRejectionCache:
    async def test_a_bypassed_image_is_never_cached(self, repo, limiter):
        await repo.bypass_resource("llm")
        for _ in range(5):
            await _use(limiter)
        assert repo._rejection_cache.views(repo._namespace_id, "u", "llm", repo._now_ms()) == {}
        assert repo.get_cache_stats().local_rejections == 0


class TestRejectedMoves:
    async def test_a_rejected_bypassed_create_with_a_move_mints_nothing(self, repo):
        """Review D2: a bypassed create carrying an ADR-145 move, rejected by
        its cascade parent, is committed by `_commit_rejected_moves`, which
        undid a debit the bypass never made: the new shard held the moved
        tokens plus the request's consumption (I1)."""
        quota = Limit.quota("rpd", 10, cron="0 0 * * *")
        await repo.set_resource_defaults("llm", [quota])
        await repo.create_entity("p")
        await repo.create_entity("c", parent_id="p", cascade=True)
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("c", "llm", {"rpd": 1}):
                pass
            async with limiter.acquire("p", "llm", {"rpd": 9}):
                pass
        await repo.bump_shard_count("c", "llm", 1)
        await repo.bypass_entity("c", resource="llm")
        before = _n(await _item(repo, "c"), "b_rpd_tk")
        with pytest.raises(RateLimitExceeded):  # the parent is spent
            await RateLimiter(repository=repo)._do_acquire(
                "c", "llm", None, {"rpd": 1}, shard_id=1, shard_count=2
            )
        created = await _item(repo, "c", shard=1)
        assert created is not None  # the move was committed
        after = _n(await _item(repo, "c"), "b_rpd_tk") + _n(created, "b_rpd_tk")
        assert after == before, f"held {after} after the move, {before} before"
        # The consumption was counted only to be undone: nothing is counted.
        assert _n(created, "b_rpd_tc") == 0
