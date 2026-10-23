"""Soft limits: metered, never enforced (#467, ADR-151)."""

import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded, schema
from zae_limiter.bucket import try_consume
from zae_limiter.exceptions import FanoutIncomplete, VersionMismatchError
from zae_limiter.models import BucketState, Limit, LimitStatus, UsageSnapshot
from zae_limiter.repository import Repository

RPM = Limit.per_minute("rpm", 100)
SOFT_TPM = Limit.per_minute("tpm", 1_000, soft=True)
HARD_TPM = Limit.per_minute("tpm", 1_000)


@pytest.fixture
async def soft_repo(mock_dynamodb):
    """Moto-backed repository on a stack whose Lambdas read soft limits."""
    from zae_limiter import __version__
    from zae_limiter.version import get_schema_version

    repo = Repository(name="test-soft", region="us-east-1", _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    await repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    yield repo
    await repo.close()


@pytest.fixture
async def soft_limiter(soft_repo):
    limiter = RateLimiter(repository=soft_repo)
    async with limiter:
        yield limiter


async def _bucket_item(repo: Repository, entity_id: str, resource: str, shard: int = 0):
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
            "SK": {"S": schema.sk_state()},
        },
        ConsistentRead=True,
    )
    return response.get("Item")


def _soft_stamp(item, name: str, now_ms: int | None = None) -> bool:
    """The item carries a live soft stamp: a deadline still ahead (ADR-151 decision 8)."""
    raw = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_SOFT), {}).get("N")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return raw is not None and int(raw) > now_ms


class TestLimitModel:
    def test_hard_by_default(self):
        assert Limit.per_minute("rpm", 10).soft is False

    @pytest.mark.parametrize(
        "limit",
        [
            Limit.per_second("a", 1, soft=True),
            Limit.per_minute("a", 1, soft=True),
            Limit.per_hour("a", 1, soft=True),
            Limit.per_day("a", 1, soft=True),
            Limit.custom("a", 10, 1, 1, soft=True),
            Limit.quota("a", 10, cron="0 0 * * *", soft=True),
            Limit.quota("a", 10, reset_after=timedelta(hours=1), soft=True),
        ],
    )
    def test_every_factory_takes_soft(self, limit):
        assert limit.soft is True

    def test_dict_round_trip(self):
        assert SOFT_TPM.to_dict()["soft"] is True
        assert "soft" not in HARD_TPM.to_dict()
        assert Limit.from_dict(SOFT_TPM.to_dict()) == SOFT_TPM
        assert Limit.from_dict(HARD_TPM.to_dict()) == HARD_TPM

    def test_per_shard_and_bucket_state_keep_it(self):
        assert SOFT_TPM.per_shard(4, 0).soft is True
        state = BucketState.from_limit("e", "r", SOFT_TPM, 0)
        assert state.soft is True
        assert Limit.from_bucket_state(state).soft is True

    def test_the_wcu_carrier_is_never_soft(self):
        state = BucketState.from_limit("e", "r", SOFT_TPM, 0)
        state.limit_name = "wcu"
        assert Limit._carrier(state).soft is False


class TestTryConsume:
    def test_soft_admits_into_debt(self):
        state = BucketState.from_limit("e", "r", SOFT_TPM, 0)
        result = try_consume(state, 5_000, 0)
        assert result.success
        assert result.new_tokens_milli == -4_000_000
        assert result.retry_after_seconds == 0.0

    def test_hard_still_rejects(self):
        state = BucketState.from_limit("e", "r", HARD_TPM, 0)
        assert not try_consume(state, 5_000, 0).success


class TestStatus:
    def _status(self, limit, available):
        return LimitStatus(
            entity_id="e",
            resource="r",
            limit_name=limit.name,
            limit=limit,
            available=available,
            requested=10,
            exceeded=False,
            retry_after_seconds=0.0,
        )

    def test_soft_and_overdrawn(self):
        status = self._status(SOFT_TPM, -5)
        assert status.soft is True
        assert status.overdrawn is True
        assert status.deficit == 0

    def test_hard_is_never_overdrawn(self):
        status = self._status(HARD_TPM, -5)
        assert status.soft is False
        assert status.overdrawn is False


class TestConfigStorage:
    async def test_round_trips_at_every_level(self, soft_repo):
        await soft_repo.set_system_defaults([RPM, SOFT_TPM])
        await soft_repo.set_resource_defaults("llm", [RPM, SOFT_TPM])
        await soft_repo.set_limits("user-1", [RPM, SOFT_TPM], resource="llm")

        system, _ = await soft_repo.get_system_defaults()
        for limits in (
            system,
            await soft_repo.get_resource_defaults("llm"),
            await soft_repo.get_limits("user-1", "llm"),
        ):
            assert {limit.name: limit.soft for limit in limits} == {"rpm": False, "tpm": True}

    async def test_hard_limit_writes_no_attribute(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [RPM])
        client = await soft_repo._get_client()
        item = (
            await client.get_item(
                TableName=soft_repo.table_name,
                Key={
                    "PK": {"S": schema.pk_resource(soft_repo._namespace_id, "llm")},
                    "SK": {"S": schema.sk_config()},
                },
            )
        )["Item"]
        assert not any(key.endswith("_soft") for key in item)

    async def test_a_session_quota_is_soft_under_its_hidden_prefix(self, soft_repo):
        session = Limit.quota("session", 10, reset_after=timedelta(hours=1), soft=True)
        await soft_repo.set_resource_defaults("llm", [session])
        (limit,) = await soft_repo.get_resource_defaults("llm")
        assert limit.soft is True


class TestFastPath:
    async def test_admits_an_exhausted_soft_limit_and_debits_it(self, soft_limiter, soft_repo):
        await soft_repo.set_resource_defaults("llm", [RPM, SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 900}):
            pass
        # Warm: the second acquire is a speculative write that drives tpm into debt.
        async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 900}) as lease:
            assert lease.overdrawn == ["tpm"]
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert int(item["b_tpm_tk"]["N"]) < 0
        assert int(item["b_tpm_tc"]["N"]) == 1_800_000

    async def test_a_hard_sibling_still_rejects(self, soft_limiter, soft_repo):
        await soft_repo.set_resource_defaults("llm", [Limit.per_minute("rpm", 1), SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 5_000}):
            pass
        with pytest.raises(RateLimitExceeded) as exc_info:
            async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 5_000}):
                pass
        exc = exc_info.value
        assert [s.limit_name for s in exc.violations] == ["rpm"]
        (tpm,) = [s for s in exc.passed if s.limit_name == "tpm"]
        assert tpm.soft is True
        assert tpm.overdrawn is True
        body = exc.as_dict()
        assert {entry["limit_name"]: entry["soft"] for entry in body["limits"]} == {
            "rpm": False,
            "tpm": True,
        }

    async def test_condition_reads_the_item_stamp(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        limiter = RateLimiter(repository=soft_repo)
        async with limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        result = await soft_repo.speculative_consume("user-1", "llm", {"tpm": 50_000})
        assert result.success
        (tpm,) = [b for b in result.buckets if b.limit_name == "tpm"]
        assert tpm.soft is True
        assert tpm.tokens_milli < 0

    async def test_a_hard_stamp_rejects_with_a_classified_failure(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        limiter = RateLimiter(repository=soft_repo)
        async with limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        result = await soft_repo.speculative_consume("user-1", "llm", {"tpm": 50_000})
        assert not result.success
        assert result.failure_reason.name == "APP_LIMIT_EXHAUSTED"


class TestSlowPath:
    async def test_create_stamps_soft_and_admits_beyond_capacity(self, soft_limiter, soft_repo):
        await soft_repo.set_resource_defaults("llm", [RPM, SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 5_000}) as lease:
            assert lease.overdrawn == ["tpm"]
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert _soft_stamp(item, "tpm") is True
        assert _soft_stamp(item, "rpm") is False

    async def test_a_create_from_a_stale_soft_cache_is_stamped_hard(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        # Warm this process's config cache with the soft definition.
        async with soft_limiter.acquire("user-0", "llm", {"tpm": 1}):
            pass
        # Another process makes it hard; this process's cache still says soft.
        other = Repository(name="test-soft", region="us-east-1", _skip_deprecation_warning=True)
        await other.set_resource_defaults("llm", [HARD_TPM])
        await other.close()

        with pytest.raises(RateLimitExceeded):
            async with soft_limiter.acquire("user-1", "llm", {"tpm": 5_000}):
                pass
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert _soft_stamp(item, "tpm") is False

    async def test_a_cached_pass_trusts_the_item_stamp(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        # The stamp goes hard behind the cache's back; the cached pass obeys it.
        await soft_repo._stamp_bucket_soft(
            schema.pk_bucket(soft_repo._namespace_id, "user-1", "llm", 0), {"tpm": False}
        )
        with pytest.raises(RateLimitExceeded):
            async with soft_limiter.acquire("user-1", "llm", {"tpm": 5_000}, limits=None):
                pass

    async def test_a_fresh_pass_restamps_a_missing_stamp(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        limiter = RateLimiter(repository=soft_repo)
        async with limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        pk = schema.pk_bucket(soft_repo._namespace_id, "user-1", "llm", 0)
        await soft_repo._stamp_bucket_soft(pk, {"tpm": False})
        # A fresh config read is the authority: it admits and re-stamps.
        await soft_repo.invalidate_config_cache()
        lease = await limiter._do_acquire("user-1", "llm", None, {"tpm": 5_000})
        await lease._commit_initial()
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True

    async def test_a_fresh_pass_restamps_a_stale_soft_stamp_hard(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        limiter = RateLimiter(repository=soft_repo)
        async with limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        pk = schema.pk_bucket(soft_repo._namespace_id, "user-1", "llm", 0)
        await soft_repo._stamp_bucket_soft(pk, {"tpm": True})
        await soft_repo.invalidate_config_cache()
        lease = await limiter._do_acquire("user-1", "llm", None, {"tpm": 1})
        await lease._commit_initial()
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_an_override_is_trusted_and_not_restamped(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        limiter = RateLimiter(repository=soft_repo)
        async with limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        lease = await limiter._do_acquire("user-1", "llm", [SOFT_TPM], {"tpm": 5_000})
        await lease._commit_initial()
        assert lease.overdrawn == ["tpm"]
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_a_seeded_soft_limit_is_stamped(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [RPM])
        limiter = RateLimiter(repository=soft_repo)
        async with limiter.acquire("user-1", "llm", {"rpm": 1}):
            pass
        await soft_repo.set_resource_defaults("llm", [RPM, SOFT_TPM])
        async with limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 5_000}):
            pass
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert _soft_stamp(item, "tpm") is True
        assert int(item["b_tpm_tk"]["N"]) < 0

    async def test_a_seed_from_a_cached_pass_reads_soft_uncached(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [RPM, SOFT_TPM])
        limiter = RateLimiter(repository=soft_repo)
        await soft_repo.set_limits("user-1", [RPM], resource="llm")
        async with limiter.acquire("user-1", "llm", {"rpm": 1}):
            pass
        await soft_repo.delete_limits("user-1", resource="llm")
        # Cached limits (rpm + soft tpm), tpm missing from the item: a seed.
        await limiter._resolve_limits("user-1", "llm", None)
        with patch.object(soft_repo, "resolve_access", wraps=soft_repo.resolve_access) as spy:
            lease = await limiter._do_acquire("user-1", "llm", None, {"rpm": 1, "tpm": 5_000})
            await lease._commit_initial()
        # The gate's uncached read carries the soft-ness: no second round trip.
        assert spy.await_args.kwargs["include_limits"] is True
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True

    async def test_a_system_level_create_reads_the_system_item(self, soft_repo, soft_limiter):
        await soft_repo.set_system_defaults([SOFT_TPM])
        async with soft_limiter.acquire("user-0", "llm", {"tpm": 1}):  # warms the cache
            pass
        with patch.object(soft_repo, "resolve_access", wraps=soft_repo.resolve_access) as spy:
            async with soft_limiter.acquire("user-1", "llm", {"tpm": 5_000}) as lease:
                pass
        assert spy.await_args.kwargs["include_system"] is True
        assert lease.overdrawn == ["tpm"]
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True

    async def test_without_the_system_item_a_bare_walk_reads_hard(self, soft_repo):
        await soft_repo.set_system_defaults([SOFT_TPM])
        access = await soft_repo.resolve_access("user-1", "llm", include_limits=True)
        assert access.limit_soft == {}
        access = await soft_repo.resolve_access(
            "user-1", "llm", include_limits=True, include_system=True
        )
        assert access.limit_soft == {"tpm": True}

    async def test_resolve_soft_limits_reads_uncached(self, soft_repo):
        assert await soft_repo.resolve_soft_limits("user-1", "llm") == {}
        await soft_repo.set_system_defaults([RPM, SOFT_TPM])
        assert await soft_repo.resolve_soft_limits("user-1", "llm") == {"rpm": False, "tpm": True}
        await soft_repo.set_limits("user-1", [HARD_TPM], resource="llm")
        assert await soft_repo.resolve_soft_limits("user-1", "llm") == {"tpm": False}

    async def test_a_create_falls_back_to_its_own_uncached_read(self, soft_repo):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        limiter = RateLimiter(repository=soft_repo)
        # Fresh walk levels but the deciding level served from cache: the
        # gate needs no read, so the create reads the soft-ness itself.
        with (
            patch.object(soft_repo, "limits_read_fresh", return_value=False),
            patch.object(
                soft_repo, "resolve_soft_limits", wraps=soft_repo.resolve_soft_limits
            ) as spy,
        ):
            lease = await limiter._do_acquire("user-1", "llm", None, {"tpm": 5_000})
            await lease._commit_initial()
        spy.assert_awaited_once()
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True

    def test_limits_read_fresh(self, soft_repo):
        ns = soft_repo._namespace_id
        entity = (schema.pk_entity(ns, "u"), schema.sk_config("llm"))
        default = (schema.pk_entity(ns, "u"), schema.sk_config(schema.DEFAULT_RESOURCE))
        resource = (schema.pk_resource(ns, "llm"), schema.sk_config())
        system = (schema.pk_system(ns), schema.sk_config())
        fresh = soft_repo.limits_read_fresh
        assert fresh("u", "llm", "entity", {entity: None}) is True
        assert fresh("u", "llm", "resource", {entity: None, resource: None}) is False
        everything = {entity: None, default: None, resource: None, system: None}
        assert fresh("u", "llm", "system", everything) is True
        assert fresh("u", "llm", None, everything) is False


class TestEntitySync:
    async def test_set_limits_restamps_every_shard(self, soft_repo, soft_limiter):
        await soft_repo.set_limits("user-1", [HARD_TPM], resource="llm")
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        await soft_repo.set_limits("user-1", [SOFT_TPM], resource="llm")
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True
        await soft_repo.set_limits("user-1", [HARD_TPM], resource="llm")
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_delete_limits_restamps_from_the_level_below(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        await soft_repo.set_limits("user-1", [SOFT_TPM], resource="llm")
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True
        # A stale soft cache must not reach the reconcile (#467).
        await soft_limiter._resolve_limits("user-2", "llm", None)
        with patch.object(soft_repo, "resolve_soft_limits", return_value={"tpm": False}):
            await soft_limiter.delete_limits("user-1", resource="llm")
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False


class TestResourceAndSystemFanout:
    async def _bucket(self, repo, limiter, entity="user-1"):
        async with limiter.acquire(entity, "llm", {"tpm": 1}):
            pass

    async def test_resource_change_restamps_buckets(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        await self._bucket(soft_repo, soft_limiter)
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_an_unchanged_set_writes_no_bucket(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        await self._bucket(soft_repo, soft_limiter)
        with patch.object(soft_repo, "_fanout_soft") as fanout:
            await soft_repo.set_resource_defaults(
                "llm", [Limit.per_minute("tpm", 2_000, soft=True)]
            )
        fanout.assert_not_called()

    async def test_an_entity_override_is_left_alone(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        await soft_repo.set_limits("vip", [HARD_TPM], resource="llm")
        await self._bucket(soft_repo, soft_limiter, "vip")
        await self._bucket(soft_repo, soft_limiter, "user-1")
        stamped = await soft_repo._fanout_soft(resource="llm", names={"tpm"})
        assert stamped == 1
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        assert _soft_stamp(await _bucket_item(soft_repo, "vip", "llm"), "tpm") is False
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True

    async def test_deleting_the_resource_level_clears_its_soft_stamps(
        self, soft_repo, soft_limiter
    ):
        await soft_repo.set_system_defaults([HARD_TPM])
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        await self._bucket(soft_repo, soft_limiter)
        await soft_repo.delete_resource_defaults("llm")
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_system_change_reaches_every_resource(self, soft_repo, soft_limiter):
        await soft_repo.set_system_defaults([HARD_TPM])
        await self._bucket(soft_repo, soft_limiter)
        await soft_repo.set_system_defaults([SOFT_TPM])
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is True
        await soft_repo.delete_system_defaults()
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_a_system_change_leaves_resource_decided_buckets_alone(
        self, soft_repo, soft_limiter
    ):
        await soft_repo.set_system_defaults([HARD_TPM])
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        await self._bucket(soft_repo, soft_limiter)
        with patch.object(
            soft_repo, "_stamp_bucket_soft", wraps=soft_repo._stamp_bucket_soft
        ) as stamp:
            await soft_repo.set_system_defaults([SOFT_TPM])
        stamp.assert_not_called()
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm") is False

    async def test_a_failed_write_reports_progress(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        await self._bucket(soft_repo, soft_limiter)
        with patch.object(soft_repo, "_stamp_bucket_soft", side_effect=RuntimeError("boom")):
            with pytest.raises(FanoutIncomplete) as exc_info:
                await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        assert exc_info.value.stamped == 0

    async def test_namespace_discovery_follows_pages(self, soft_repo, soft_limiter):
        await soft_repo.set_system_defaults([HARD_TPM])
        for entity in ("user-1", "user-2"):
            await self._bucket(soft_repo, soft_limiter, entity)
        client = await soft_repo._get_client()
        real = client.query

        async def one_per_page(**kwargs):
            kwargs["Limit"] = 1
            return await real(**kwargs)

        with patch.object(client, "query", side_effect=one_per_page):
            pks = await soft_repo._discover_namespace_bucket_pks()
        assert len(pks) == 2

    async def test_an_unexpected_stamp_error_propagates(self, soft_repo):
        from botocore.exceptions import ClientError

        client = await soft_repo._get_client()
        error = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
        with patch.object(client, "update_item", side_effect=error):
            with pytest.raises(ClientError):
                await soft_repo._stamp_bucket_soft("ns/BUCKET#x#y#0", {"tpm": True})

    async def test_a_vanished_bucket_is_skipped(self, soft_repo):
        await soft_repo._stamp_bucket_soft(
            schema.pk_bucket(soft_repo._namespace_id, "ghost", "llm", 0), {"tpm": True}
        )
        assert await _bucket_item(soft_repo, "ghost", "llm") is None

    def test_soft_changed(self, soft_repo):
        old = {"l_tpm_cp": {"N": "1"}, "l_tpm_ra": {"N": "1"}, "l_tpm_rp": {"N": "60"}}
        old_soft = {**old, "l_tpm_soft": {"BOOL": True}}
        assert soft_repo._soft_changed(None, [SOFT_TPM]) == {"tpm"}
        assert soft_repo._soft_changed(old, [SOFT_TPM]) == {"tpm"}
        assert soft_repo._soft_changed(old_soft, [SOFT_TPM]) == set()
        assert soft_repo._soft_changed(old_soft, []) == {"tpm"}
        assert soft_repo._soft_changed(old, []) == set()
        # An undecodable old image errs toward restamping.
        corrupt = {**old, "l_tpm_ra": {"N": "0"}}
        assert soft_repo._soft_changed(corrupt, []) == {"tpm"}


#: Past every deadline a stamp written at the start could carry (ADR-151 decision 8).
PAST_T_MS = schema.DEFAULT_STAMP_TTL_SECONDS * 1000 + 1


def _soft_deadline(item, name: str = "tpm") -> int | None:
    raw = item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_SOFT), {}).get("N")
    return None if raw is None else int(raw)


class TestStampsExpire:
    """Review D1, soft: a `b_{name}_soft` stamp a fan-out missed admitted the
    limit without limit, forever. A stamp is a deadline at most T after the
    config read that justified it (ADR-151 decision 8)."""

    async def _soft_then_missed_hard(self, repo: Repository) -> int:
        """A soft bucket whose soft -> hard change the fan-out never reached."""
        await repo.set_resource_defaults("llm", [SOFT_TPM])
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            async with RateLimiter(repository=repo) as limiter:
                async with limiter.acquire("user-1", "llm", {"tpm": 600}):
                    pass
        assert _soft_stamp(await _bucket_item(repo, "user-1", "llm"), "tpm", start)
        with patch.object(repo, "_fanout_soft", return_value=0):
            await repo.set_resource_defaults("llm", [HARD_TPM])
        return start

    async def test_a_stamp_a_missed_soft_to_hard_change_left_stops_admitting_within_t(
        self, soft_repo
    ):
        start = await self._soft_then_missed_hard(soft_repo)
        admitted = 0
        with patch.object(soft_repo, "_now_ms", return_value=start + PAST_T_MS):
            for _ in range(10):
                async with RateLimiter(repository=soft_repo) as fresh:
                    await soft_repo.invalidate_config_cache()
                    try:
                        async with fresh.acquire("user-1", "llm", {"tpm": 600}):
                            admitted += 1
                    except RateLimitExceeded:
                        pass
        item = await _bucket_item(soft_repo, "user-1", "llm")
        # Refilled to 1,000 by then: one 600 fits, a second does not.
        assert admitted == 1, f"admitted {admitted} x 600 against tpm=1,000"
        assert _soft_deadline(item) is None  # the slow pass removed the stale stamp

    async def test_a_cached_pass_never_takes_the_stamp_as_the_authority(self, soft_repo):
        """The config cache still says soft; the item's expired stamp sends the
        pass to config read uncached, which says hard."""
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        start = soft_repo._now_ms()
        async with RateLimiter(repository=soft_repo) as limiter:
            with patch.object(soft_repo, "_now_ms", return_value=start):
                async with limiter.acquire("user-1", "llm", {"tpm": 600}):
                    pass
            # Another process makes it hard and its fan-out misses this bucket.
            other = Repository(name="test-soft", region="us-east-1", _skip_deprecation_warning=True)
            with patch.object(other, "_fanout_soft", return_value=0):
                await other.set_resource_defaults("llm", [HARD_TPM])
            await other.close()
            admitted = 0
            with patch.object(soft_repo, "_now_ms", return_value=start + PAST_T_MS):
                for _ in range(5):
                    try:
                        async with limiter.acquire("user-1", "llm", {"tpm": 600}):
                            admitted += 1
                    except RateLimitExceeded:
                        pass
        assert admitted == 1
        assert _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm")) is None

    async def test_an_expired_stamp_is_renewed_while_the_limit_stays_soft(
        self, soft_repo, soft_limiter
    ):
        await soft_repo.set_resource_defaults("llm", [RPM, SOFT_TPM])
        start = soft_repo._now_ms()
        with patch.object(soft_repo, "_now_ms", return_value=start):
            async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 900}):
                pass
        first = _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm"))
        assert first is not None and start < first <= start + PAST_T_MS
        later = start + PAST_T_MS
        with patch.object(soft_repo, "_now_ms", return_value=later):
            for _ in range(3):
                async with soft_limiter.acquire("user-1", "llm", {"rpm": 1, "tpm": 900}):
                    pass
        item = await _bucket_item(soft_repo, "user-1", "llm")
        renewed = _soft_deadline(item)
        assert renewed is not None and later < renewed <= later + PAST_T_MS
        assert int(item["b_tpm_tk"]["N"]) < 0  # still metered into debt
        assert _soft_deadline(item, "rpm") is None

    async def test_the_fast_path_routes_an_expired_soft_stamp_to_the_slow_path(
        self, soft_repo, soft_limiter
    ):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        start = soft_repo._now_ms()
        with patch.object(soft_repo, "_now_ms", return_value=start):
            async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
                pass
        # Plenty of balance: without the guard this would debit silently and
        # leave the expired stamp in place.
        result = await soft_repo.speculative_consume(
            "user-1", "llm", {"tpm": 1}, now_ms=start + PAST_T_MS
        )
        assert not result.success
        assert result.failure_reason.name == "SCHEDULE_BOUNDARY"
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert int(item["b_tpm_tc"]["N"]) == 1_000  # nothing written

    async def test_a_pre_release_boolean_stamp_reads_as_expired(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        client = await soft_repo._get_client()
        await client.update_item(
            TableName=soft_repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(soft_repo._namespace_id, "user-1", "llm", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #s = :t",
            ExpressionAttributeNames={"#s": "b_tpm_soft"},
            ExpressionAttributeValues={":t": {"BOOL": True}},
        )
        (state,) = [
            b for b in await soft_repo.get_buckets("user-1", "llm") if b.limit_name == "tpm"
        ]
        assert state.soft and state.soft_until_ms == schema.STAMP_EXPIRED
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 5_000}) as lease:
            assert lease.overdrawn == ["tpm"]
        assert _soft_stamp(await _bucket_item(soft_repo, "user-1", "llm"), "tpm")

    async def test_fan_outs_stamp_a_deadline_at_most_t_out(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
            pass
        now = soft_repo._now_ms()
        with patch.object(soft_repo, "_now_ms", return_value=now):
            await soft_repo.set_resource_defaults("llm", [SOFT_TPM])  # soft fan-out
        deadline = _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm"))
        assert deadline is not None and now + 48_000 <= deadline <= now + 60_000
        with patch.object(soft_repo, "_now_ms", return_value=now):
            await soft_repo.set_limits("user-1", [SOFT_TPM], resource="llm")  # param sync
        deadline = _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm"))
        assert deadline is not None and now + 48_000 <= deadline <= now + 60_000


class TestRejectedPassFixesStamps:
    """Review E1: a slow pass that ends in a 429 wrote nothing, so a stamp config
    disagreed with stayed, and every rejection paid the slow path (measured: 10
    rejections, 10 slow passes, the stamp still there). It now corrects the
    stamps alone before raising (ADR-151 decisions 4 and 8)."""

    TINY_SOFT = Limit.per_minute("tpm", 2, soft=True)

    @staticmethod
    def _count_slow(limiter: RateLimiter):
        calls = {"n": 0}
        original = limiter._do_acquire

        async def counting(*args, **kwargs):
            calls["n"] += 1
            return await original(*args, **kwargs)

        return calls, patch.object(limiter, "_do_acquire", counting)

    async def _in_debt_then_missed_hard(self, repo: Repository, limiter: RateLimiter) -> int:
        """tpm=2 soft driven to -8; the soft -> hard change reaches config only."""
        await repo.set_resource_defaults("llm", [self.TINY_SOFT])
        start = repo._now_ms()
        with patch.object(repo, "_now_ms", return_value=start):
            async with limiter.acquire("user-1", "llm", {"tpm": 10}):
                pass
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_resource(repo._namespace_id, "llm")},
                "SK": {"S": schema.sk_config()},
            },
            UpdateExpression="REMOVE #s",
            ExpressionAttributeNames={"#s": "l_tpm_soft"},
        )
        await repo.invalidate_config_cache()
        return start

    async def _ten_rejections(self, repo, limiter, now_ms, consume=None) -> tuple[int, int]:
        calls, counting = self._count_slow(limiter)
        rejected = 0
        with patch.object(repo, "_now_ms", return_value=now_ms), counting:
            for _ in range(10):
                try:
                    async with limiter.acquire("user-1", "llm", consume or {"tpm": 1}):
                        pass
                except RateLimitExceeded:
                    rejected += 1
        return rejected, calls["n"]

    async def test_the_first_rejection_removes_a_stamp_config_no_longer_backs(
        self, soft_repo, soft_limiter
    ):
        start = await self._in_debt_then_missed_hard(soft_repo, soft_limiter)
        rejected, slow = await self._ten_rejections(soft_repo, soft_limiter, start + PAST_T_MS)
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert rejected == 10
        assert _soft_deadline(item) is None
        assert slow == 1, f"{slow} slow passes for 10 rejections"
        assert int(item["b_tpm_tk"]["N"]) == -8_000  # nothing debited, nothing refilled

    async def test_an_expired_stamp_config_still_backs_is_renewed(self, soft_repo, soft_limiter):
        """A hard limit beside it rejects: the soft stamp is renewed, not left
        expired to send the next request to the slow path again."""
        rph = Limit.per_hour("rph", 1)
        await soft_repo.set_resource_defaults("llm", [rph, SOFT_TPM])
        start = soft_repo._now_ms()
        with patch.object(soft_repo, "_now_ms", return_value=start):
            async with soft_limiter.acquire("user-1", "llm", {"rph": 1, "tpm": 1}):
                pass
        later = start + PAST_T_MS
        rejected, slow = await self._ten_rejections(
            soft_repo, soft_limiter, later, {"rph": 1, "tpm": 1}
        )
        deadline = _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm"))
        assert rejected == 10
        assert deadline is not None and later < deadline <= later + PAST_T_MS
        assert slow == 1

    async def test_a_stamp_changed_since_the_read_is_left_alone(self, soft_repo, soft_limiter):
        start = await self._in_debt_then_missed_hard(soft_repo, soft_limiter)
        client = await soft_repo._get_client()
        key = {
            "PK": {"S": schema.pk_bucket(soft_repo._namespace_id, "user-1", "llm", 0)},
            "SK": {"S": schema.sk_state()},
        }
        original = soft_repo.fix_bucket_stamps
        landed: list[bool] = []

        async def racing(*args, **kwargs):
            # A fan-out lands between the read and the correction.
            await client.update_item(
                TableName=soft_repo.table_name,
                Key=key,
                UpdateExpression="SET #s = :d",
                ExpressionAttributeNames={"#s": "b_tpm_soft"},
                ExpressionAttributeValues={":d": {"N": "123"}},
            )
            landed.append(await original(*args, **kwargs))
            return landed[-1]

        with (
            patch.object(soft_repo, "_now_ms", return_value=start + PAST_T_MS),
            patch.object(soft_repo, "fix_bucket_stamps", racing),
        ):
            with pytest.raises(RateLimitExceeded):
                async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
                    pass
        assert landed == [False]
        assert _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm")) == 123

    async def test_a_failed_correction_is_logged_and_the_rejection_stands(
        self, soft_repo, soft_limiter, caplog
    ):
        from botocore.exceptions import ClientError

        start = await self._in_debt_then_missed_hard(soft_repo, soft_limiter)

        async def failing(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "boom"}}, "UpdateItem"
            )

        with (
            patch.object(soft_repo, "_now_ms", return_value=start + PAST_T_MS),
            patch.object(soft_repo, "fix_bucket_stamps", failing),
            caplog.at_level("WARNING", logger="zae_limiter.limiter"),
        ):
            with pytest.raises(RateLimitExceeded):
                async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
                    pass
        assert "stamp correction" in caplog.text
        assert "user-1" not in caplog.text
        assert _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm")) is not None

    async def test_an_agreeing_live_stamp_costs_no_write(self, soft_repo, soft_limiter):
        """A rejection whose stamps already agree with config writes nothing."""
        rph = Limit.per_hour("rph", 1)
        await soft_repo.set_resource_defaults("llm", [rph, SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"rph": 1, "tpm": 1}):
            pass
        await soft_repo.invalidate_config_cache()
        with patch.object(soft_repo, "fix_bucket_stamps") as fix:
            with pytest.raises(RateLimitExceeded):
                await soft_limiter._do_acquire("user-1", "llm", None, {"rph": 1, "tpm": 1})
        fix.assert_not_called()

    async def test_a_multi_resource_part_planned_fine_is_corrected_too(
        self, soft_repo, soft_limiter
    ):
        """ADR-148: one part rejects; the part that planned fine had its expired
        stamp renewed by its plan, and that plan is never committed."""
        rph = Limit.per_hour("rph", 1)
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        await soft_repo.set_resource_defaults("budget", [rph, SOFT_TPM])
        start = soft_repo._now_ms()
        with patch.object(soft_repo, "_now_ms", return_value=start):
            async with soft_limiter.acquire(
                "user-1", "llm", {"tpm": 1}, also={"budget": {"rph": 1, "tpm": 1}}
            ):
                pass
        later = start + PAST_T_MS
        with patch.object(soft_repo, "_now_ms", return_value=later):
            with pytest.raises(RateLimitExceeded):
                async with soft_limiter.acquire(
                    "user-1", "llm", {"tpm": 1}, also={"budget": {"rph": 1, "tpm": 1}}
                ):
                    pass
        for resource in ("llm", "budget"):
            deadline = _soft_deadline(await _bucket_item(soft_repo, "user-1", resource))
            assert deadline is not None and later < deadline <= later + PAST_T_MS, resource
        assert int((await _bucket_item(soft_repo, "user-1", "llm"))["b_tpm_tc"]["N"]) == 1_000

    async def test_nothing_to_correct_writes_nothing(self, soft_repo):
        assert await soft_repo.fix_bucket_stamps("user-1", "llm") is False

    async def test_a_backend_without_the_method_skips_it(self, soft_repo, soft_limiter):
        """`getattr`: a third-party backend still gets its 429."""
        start = await self._in_debt_then_missed_hard(soft_repo, soft_limiter)
        with (
            patch.object(soft_repo, "_now_ms", return_value=start + PAST_T_MS),
            patch.object(soft_repo, "fix_bucket_stamps", None),
        ):
            with pytest.raises(RateLimitExceeded):
                async with soft_limiter.acquire("user-1", "llm", {"tpm": 1}):
                    pass
        assert _soft_deadline(await _bucket_item(soft_repo, "user-1", "llm")) is not None


class TestVersionGate:
    @staticmethod
    async def _stamp(repo, lambda_version):
        from zae_limiter.version import get_schema_version

        await repo.set_version_record(
            schema_version=get_schema_version(),
            lambda_version=lambda_version,
            client_min_version="0.0.0",
        )

    async def test_refused_while_the_lambdas_predate_it(self, soft_repo):
        await self._stamp(soft_repo, "0.16.0")
        with patch("zae_limiter.__version__", "0.17.0"):
            with pytest.raises(VersionMismatchError) as exc_info:
                await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        assert "soft limit" in str(exc_info.value)
        assert exc_info.value.can_auto_update is True
        assert await soft_repo.get_resource_defaults("llm") == []

    async def test_free_when_nothing_is_soft(self, soft_repo):
        await self._stamp(soft_repo, "0.16.0")
        with patch("zae_limiter.__version__", "0.17.0"):
            await soft_repo.set_resource_defaults("llm", [HARD_TPM])
            await soft_repo.set_limits("u", [HARD_TPM], resource="llm")
            await soft_repo.set_system_defaults([HARD_TPM])

    async def test_admitted_and_ratcheted(self, soft_repo):
        await self._stamp(soft_repo, "0.17.0")
        with patch("zae_limiter.__version__", "0.17.1"):
            await soft_repo.set_limits("u", [SOFT_TPM], resource="llm")
            await soft_repo.set_system_defaults([SOFT_TPM])
        record = await soft_repo.get_version_record()
        assert record["client_min_version"] == "0.17.0"

    @pytest.mark.parametrize(
        ("found", "version", "auto"),
        [(False, None, False), (True, None, False), (True, "0.16.0", True)],
    )
    def test_refusal_messages(self, found, version, auto):
        from zae_limiter.version import non_enforcing_refusal

        message, can_auto_update = non_enforcing_refusal(found, version)
        assert "soft limit" in message
        assert can_auto_update is auto


class TestRejectionCacheAndAvailability:
    async def test_never_locally_rejects_a_soft_limit(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        for _ in range(3):
            async with soft_limiter.acquire("user-1", "llm", {"tpm": 900}):
                pass
        assert soft_repo.get_cache_stats().local_rejections == 0

    async def test_check_availability_reports_without_exceeding(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        async with soft_limiter.acquire("user-1", "llm", {"tpm": 1_500}):
            pass
        check = await soft_limiter.check_availability("user-1", "llm", needed={"tpm": 100})
        status = check.status("tpm")
        assert status.soft is True
        assert status.overdrawn is True
        assert check.allowed is True
        assert check.retry_after_seconds == 0.0


class TestSoftQuota:
    async def test_the_reset_forgives_the_debt(self, soft_repo, soft_limiter):
        quota = Limit.quota("rpd", 10, cron="0 0 * * *", soft=True)
        await soft_repo.set_resource_defaults("llm", [quota])
        start = 1_700_000_000_000 - (1_700_000_000_000 % 86_400_000) + 3_600_000
        with patch.object(soft_repo, "_now_ms", return_value=start):
            async with soft_limiter.acquire("user-1", "llm", {"rpd": 25}) as lease:
                assert lease.overdrawn == ["rpd"]
        next_day = start + 86_400_000
        with patch.object(soft_repo, "_now_ms", return_value=next_day):
            async with soft_limiter.acquire("user-1", "llm", {"rpd": 1}) as lease:
                assert lease.overdrawn == []
        item = await _bucket_item(soft_repo, "user-1", "llm")
        assert int(item["b_rpd_tk"]["N"]) == 9_000


class TestUsageSnapshotOverdrawn:
    def test_deserialize_splits_the_counter(self, soft_repo):
        item = {
            "entity_id": {"S": "e"},
            "resource": {"S": "r"},
            "window": {"S": "hourly"},
            "window_start": {"S": "2026-01-01T00:00:00Z"},
            "total_events": {"N": "3"},
            "tpm": {"N": "500"},
            "tpm#od": {"N": "2"},
        }
        snapshot = soft_repo._deserialize_usage_snapshot(item)
        assert isinstance(snapshot, UsageSnapshot)
        assert snapshot.counters == {"tpm": 500}
        assert snapshot.overdrawn == {"tpm": 2}


class TestAggregatorOverdrawSignal:
    @staticmethod
    def _record(*, soft: bool, old_tc: int, new_tc: int, tk: int) -> dict:
        new_image = {
            "PK": {"S": "default/BUCKET#user-1#llm#0"},
            "SK": {"S": "#STATE"},
            "entity_id": {"S": "user-1"},
            "rf": {"N": "1704067200000"},
            "b_tpm_tc": {"N": str(new_tc)},
            "b_tpm_tk": {"N": str(tk)},
            "b_tpm_cp": {"N": "1000000"},
            "b_tpm_ra": {"N": "1000000"},
            "b_tpm_rp": {"N": "60000"},
        }
        if soft:
            new_image["b_tpm_soft"] = {"BOOL": True}
        old_image = {**new_image, "b_tpm_tc": {"N": str(old_tc)}}
        return {"eventName": "MODIFY", "dynamodb": {"NewImage": new_image, "OldImage": old_image}}

    @pytest.mark.parametrize(
        ("soft", "old_tc", "new_tc", "tk", "expected"),
        [
            (True, 0, 5_000_000, -4_000_000, True),
            (True, 0, 5_000_000, 1_000, False),  # still in credit
            (True, 5_000_000, 0, -4_000_000, False),  # a refund, not a debit
            (False, 0, 5_000_000, -4_000_000, False),  # hard debt (adjust) is not overdraw
        ],
    )
    def test_extract_deltas_flags_a_debit_into_debt(self, soft, old_tc, new_tc, tk, expected):
        from zae_limiter_aggregator.processor import extract_deltas

        (delta,) = extract_deltas(self._record(soft=soft, old_tc=old_tc, new_tc=new_tc, tk=tk))
        assert delta.overdrawn is expected

    async def test_the_counter_rides_in_the_same_update(self, soft_repo):
        import boto3

        from zae_limiter_aggregator.processor import extract_deltas, update_snapshot

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(soft_repo.table_name)
        hard = self._record(soft=False, old_tc=0, new_tc=1_000_000, tk=0)
        soft = self._record(soft=True, old_tc=0, new_tc=5_000_000, tk=-4_000_000)
        with patch.object(table, "update_item", wraps=table.update_item) as spy:
            for record in (hard, soft, soft):
                for delta in extract_deltas(record):
                    update_snapshot(table, delta, "hourly", 90)
        assert spy.call_count == 3
        snapshots, _ = await soft_repo.get_usage_snapshots(entity_id="user-1")
        (snapshot,) = snapshots
        assert snapshot.counters == {"tpm": 11_000}
        assert snapshot.overdrawn == {"tpm": 2}


class TestSoftLimitAddedLater:
    async def test_a_soft_limit_added_after_the_bucket_exists_is_seeded(self, soft_repo):
        """Review D3: the resource-level soft fan-out stamps `b_tpm_soft` on a
        bucket created before `tpm` existed. On the stamp alone the fast path
        admitted `tpm` and ADDed a `tk` with no `cp` that no reader sees, so
        the #633 seed never ran and the limit was never metered."""
        hard_rpm = Limit.per_minute("rpm", 2)
        await soft_repo.set_resource_defaults("llm", [hard_rpm])
        async with RateLimiter(repository=soft_repo) as limiter:
            async with limiter.acquire("u", "llm", {"rpm": 1}):
                pass
        await soft_repo.set_resource_defaults(
            "llm", [hard_rpm, Limit.per_minute("tpm", 100, soft=True)]
        )
        item = await _bucket_item(soft_repo, "u", "llm")
        assert schema.bucket_attr("tpm", schema.BUCKET_FIELD_CP) not in item
        assert _soft_stamp(item, "tpm")  # the fan-out stamped the missing limit

        for _ in range(3):
            async with RateLimiter(repository=soft_repo) as fresh:
                async with fresh.acquire("u", "llm", {"rpm": 0, "tpm": 5}) as lease:
                    assert lease.consumed["tpm"] == 5
        item = await _bucket_item(soft_repo, "u", "llm")
        assert schema.bucket_attr("tpm", schema.BUCKET_FIELD_CP) in item  # seeded
        states = await soft_repo.get_buckets("u", "llm")
        assert "tpm" in {s.limit_name for s in states}
        assert int(item[schema.bucket_attr("tpm", schema.BUCKET_FIELD_TC)]["N"]) == 15_000


class TestStampFallbacks:
    """The defensive branches of the stamp-deadline work (ADR-151 decision 8)."""

    def test_a_hand_built_entry_takes_the_commit_reading(self, soft_repo):
        from zae_limiter.lease import LeaseEntry, _stamp_deadline

        state = BucketState.from_limit("u", "llm", SOFT_TPM, 0)
        entry = LeaseEntry(entity_id="u", resource="llm", limit=SOFT_TPM, state=state)
        assert entry._stamp_deadline_ms is None
        assert 1_000 + 48_000 <= _stamp_deadline(entry, 1_000, soft_repo) <= 1_000 + 60_000

    async def test_a_parent_only_pass_defers_a_parent_soft_stamp_it_cannot_vouch_for(
        self, soft_repo
    ):
        """A cached pass never takes the parent's soft stamp as the authority:
        the full slow path reads soft-ness uncached and renews or removes it."""
        from zae_limiter.limiter import _SOFT_FROM_ITEM

        await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        limiter = RateLimiter(repository=soft_repo)
        state = BucketState.from_limit("org", "llm", SOFT_TPM, soft_repo._now_ms())
        state.soft = True
        state.soft_until_ms = soft_repo._now_ms() + 30_000
        with (
            patch.object(limiter, "_soft_authority", return_value=_SOFT_FROM_ITEM),
            patch.object(
                limiter,
                "_fetch_entity_and_buckets",
                return_value=(None, {("org", "llm", "tpm"): state}),
            ),
        ):
            assert (
                await limiter._try_parent_only_acquire("org", "llm", {"tpm": 1}, [], 0, 1)
            ) is None

    async def test_a_stamp_fix_propagates_an_error_that_is_not_a_lost_pin(self, soft_repo):
        from botocore.exceptions import ClientError

        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with (
            patch.object(soft_repo, "_update_item_retrying_conflict", side_effect=error),
            pytest.raises(ClientError),
        ):
            await soft_repo.fix_bucket_stamps("u", "llm", 0, bypass=(None, 1))

    def test_the_param_sync_draws_its_own_deadline_when_none_is_given(self, soft_repo):
        with patch.object(soft_repo, "_now_ms", return_value=1_000):
            _expr, _names, values = soft_repo._build_bucket_param_update([SOFT_TPM], None, None)
        (deadline,) = [int(v["N"]) for k, v in values.items() if k.startswith(":sft")]
        assert 1_000 + 48_000 <= deadline <= 1_000 + 60_000
