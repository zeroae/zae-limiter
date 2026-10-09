"""Soft limits: metered, never enforced (#467, ADR-151)."""

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


def _soft_stamp(item, name: str) -> bool:
    return bool(item.get(schema.bucket_attr(name, schema.BUCKET_FIELD_SOFT), {}).get("BOOL"))


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

    async def test_a_failed_write_reports_progress(self, soft_repo, soft_limiter):
        await soft_repo.set_resource_defaults("llm", [HARD_TPM])
        await self._bucket(soft_repo, soft_limiter)
        with patch.object(soft_repo, "_stamp_bucket_soft", side_effect=RuntimeError("boom")):
            with pytest.raises(FanoutIncomplete) as exc_info:
                await soft_repo.set_resource_defaults("llm", [SOFT_TPM])
        assert exc_info.value.stamped == 0

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
