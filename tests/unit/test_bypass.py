"""Bypass: admit everything, debit nothing, keep counting (#311, ADR-151)."""

from datetime import timedelta
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded, schema
from zae_limiter.exceptions import ResourceDisabled, VersionMismatchError
from zae_limiter.lease import BypassLostError, LeaseEntry, _bypass_write_failed
from zae_limiter.models import BucketState, Limit
from zae_limiter.repository import Repository

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
        assert lease_item["bypass"] == {"BOOL": True}


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
        assert item["bypass"] == {"BOOL": True}
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
        assert item["bypass"] == {"BOOL": True} and "disabled" not in item
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
        assert (await _item(repo))["bypass"] == {"BOOL": True}
        assert (await _item(repo, resource="api"))["disabled"] == {"BOOL": True}
        await repo.clear_entity_disabled("u")
        assert "bypass" not in await _item(repo)

    async def test_setters_take_the_bypass_value(self, repo, limiter):
        await _use(limiter)
        await repo.set_resource_defaults("llm", [RPM], disabled="bypass")
        assert (await _item(repo))["bypass"] == {"BOOL": True}
        await repo.set_limits("u", [RPM], resource="llm", disabled="bypass")
        assert await repo.get_entity_disabled("u", "llm") == "bypass"
        await repo.set_resource_defaults("llm", [RPM], disabled=None)
        assert (await _item(repo))["bypass"] == {"BOOL": True}  # the entity still bypasses
        await repo.delete_limits("u", resource="llm")
        assert "bypass" not in await _item(repo)

    async def test_an_unknown_value_is_rejected_before_any_write(self, repo):
        with pytest.raises(ValueError):
            await repo.set_resource_defaults("llm", [RPM], disabled="maybe")
        with pytest.raises(ValueError):
            await repo._set_entity_disabled("u", "llm", "maybe", None)
        assert await repo.get_resource_defaults("llm") == []


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
