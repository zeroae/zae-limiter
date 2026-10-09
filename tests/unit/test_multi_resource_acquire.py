"""One acquire debiting several resources, all or none (ADR-148, #675).

The #674 shape throughout: ``search`` is metered per user and cascades to the
organisation, ``budget`` is a per-user allowance that does not cascade. Every
assertion on DynamoDB reads the consumption counter (``tc``), which every
debit and every refund moves, so "nothing debited" means the counter is back
where it started.
"""

import sys
import warnings

import pytest

from zae_limiter import (
    LeaseExpiredError,
    OnUnavailable,
    RateLimiter,
    RateLimiterUnavailable,
    RateLimitExceeded,
    Repository,
    ResourceDisabled,
    ValidationError,
    __version__,
)
from zae_limiter.lease import Lease, LeaseEntry, QuotaMoveLostError
from zae_limiter.models import Limit, QuotaDonorDebit
from zae_limiter.version import get_schema_version

# Refill of 1 token/hour keeps refill negligible over a test.
SLOW = dict(refill_amount=1, refill_period_seconds=3600)


@pytest.fixture
async def repo(mock_dynamodb):
    r = Repository(
        name="test-multi-resource",
        region="us-east-1",
        rejection_cache_ttl=0,
        _skip_deprecation_warning=True,
    )
    await r.create_table()
    await r.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    await r.set_resource_defaults("search", [Limit.custom("rpm", 100, **SLOW)])
    await r.set_resource_defaults("budget", [Limit.custom("weekly", 1000, **SLOW)])
    await r.set_resource_cascade("budget", False)
    await r.create_entity("org", parent_id=None, name="org")
    await r.create_entity("user", parent_id="org", name="user", cascade=True)
    yield r
    await r.close()


async def _consumed(repo, entity_id, resource):
    """Net consumption per limit, summed over every shard."""
    totals: dict[str, int] = {}
    for bucket in await repo.get_buckets(entity_id, resource=resource):
        totals[bucket.limit_name] = (
            totals.get(bucket.limit_name, 0) + (bucket.total_consumed_milli or 0) // 1000
        )
    return totals


async def _ledger(repo):
    return (
        await _consumed(repo, "user", "search"),
        await _consumed(repo, "org", "search"),
        await _consumed(repo, "user", "budget"),
        await _consumed(repo, "org", "budget"),
    )


async def _admit(limiter, rpm=1, weekly=10):
    async with limiter.acquire(
        "user", "search", {"rpm": rpm}, also={"budget": {"weekly": weekly}}
    ) as lease:
        return lease


@pytest.mark.parametrize("speculative", [True, False])
class TestAllAdmitted:
    async def test_every_resource_is_debited_and_only_search_cascades(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        for _ in range(2):  # cold (slow path), then warm (fast path when enabled)
            lease = await _admit(limiter)

        assert lease.resources == ("search", "budget")
        # `consumed` sums a cascade's child and parent entries, as it always has.
        assert lease.consumed == {"rpm": 2}
        assert lease.resource("search").consumed == {"rpm": 2}
        assert lease.resource("budget").consumed == {"weekly": 10}
        assert lease.resource("budget").name == "budget"
        assert repr(lease.resource("budget")).endswith("('budget')")
        assert await _ledger(repo) == ({"rpm": 2}, {"rpm": 2}, {"weekly": 20}, {})

    async def test_each_resource_reconciles_on_exit(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        for _ in range(2):
            async with limiter.acquire(
                "user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}
            ) as lease:
                await lease.adjust(rpm=2)
                budget = lease.resource("budget")
                await budget.adjust(weekly=5)
                await budget.consume(weekly=1)
                await budget.release(weekly=2)

        assert await _ledger(repo) == ({"rpm": 6}, {"rpm": 6}, {"weekly": 28}, {})

    async def test_an_exception_in_the_body_refunds_every_resource(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await _admit(limiter)
        before = await _ledger(repo)

        with pytest.raises(RuntimeError, match="boom"):
            async with limiter.acquire(
                "user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}
            ) as lease:
                await lease.resource("budget").adjust(weekly=5)
                raise RuntimeError("boom")

        assert await _ledger(repo) == before
        with pytest.raises(LeaseExpiredError):
            await lease.resource("budget").adjust(weekly=1)
        with pytest.raises(LeaseExpiredError):
            await lease.adjust(rpm=1)

    async def test_two_resources_may_declare_the_same_limit_name(self, repo, speculative):
        await repo.set_resource_defaults("images", [Limit.custom("rpm", 100, **SLOW)])
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        for _ in range(2):
            async with limiter.acquire(
                "user", "search", {"rpm": 1}, also={"images": {"rpm": 3}}
            ) as lease:
                await lease.adjust(rpm=1)
                await lease.resource("images").adjust(rpm=1)
                # Both cascade (child + parent); each handle sees its own.
                assert lease.consumed == {"rpm": 4}
                assert lease.resource("images").consumed == {"rpm": 8}

        assert await _consumed(repo, "user", "images") == {"rpm": 8}
        assert await _consumed(repo, "user", "search") == {"rpm": 4}


@pytest.mark.parametrize("speculative", [True, False])
class TestAllOrNone:
    async def test_a_rejected_resource_refunds_the_admitted_one(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await _admit(limiter)
        before = await _ledger(repo)

        with pytest.raises(RateLimitExceeded) as exc_info:
            await _admit(limiter, weekly=5000)

        assert await _ledger(repo) == before
        violations = {(s.resource, s.limit_name) for s in exc_info.value.violations}
        assert violations == {("budget", "weekly")}
        assert {s["resource"] for s in exc_info.value.as_dict()["limits"]} >= {"budget"}

    async def test_a_disabled_resource_refunds_the_admitted_one(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await _admit(limiter)
        before = await _ledger(repo)
        await repo.disable_resource("budget")

        with pytest.raises(ResourceDisabled) as exc_info:
            await _admit(limiter)

        assert exc_info.value.resource == "budget"
        assert await _ledger(repo) == before

    async def test_disabled_outranks_rejected(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await _admit(limiter)
        await repo.disable_resource("budget")

        with pytest.raises(ResourceDisabled):
            await _admit(limiter, rpm=500)

    async def test_a_backend_error_refunds_and_blocks(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await _admit(limiter)
        before = await _ledger(repo)
        _fail_budget(repo, speculative)

        with pytest.raises(RateLimiterUnavailable) as exc_info:
            await _admit(limiter)

        assert exc_info.value.resource == "search"
        assert await _ledger(repo) == before

    async def test_a_backend_error_degrades_under_allow(self, repo, speculative):
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await _admit(limiter)
        before = await _ledger(repo)
        _fail_budget(repo, speculative)

        async with limiter.acquire(
            "user",
            "search",
            {"rpm": 1},
            on_unavailable=OnUnavailable.ALLOW,
            also={"budget": {"weekly": 10}},
        ) as lease:
            assert lease.degraded
            await lease.resource("budget").adjust(weekly=5)  # a no-op, never an error
            await lease.resource("anything").adjust(weekly=5)
            assert lease.resource("budget").consumed == {}

        assert await _ledger(repo) == before


def _fail_budget(repo, speculative):
    """Make every DynamoDB call touching the budget resource fail."""
    if speculative:
        real = repo.speculative_consume

        async def speculative_consume(entity_id, resource, consume, *args, **kwargs):
            if resource == "budget":
                raise RuntimeError("throttled")
            return await real(entity_id, resource, consume, *args, **kwargs)

        repo.speculative_consume = speculative_consume
    else:
        real_fetch = repo.batch_get_entity_and_buckets

        async def batch_get_entity_and_buckets(entity_id, bucket_keys):
            if any(key[1] == "budget" for key in bucket_keys):
                raise RuntimeError("throttled")
            return await real_fetch(entity_id, bucket_keys)

        repo.batch_get_entity_and_buckets = batch_get_entity_and_buckets


class TestFastAndSlowTogether:
    """``search`` admitted on the fast path, ``budget`` sent to the slow path."""

    @staticmethod
    async def _budget_needs_the_slow_path(repo, capacity):
        # An entity-level change fans out with `vu = 0`: the fast path cannot
        # spend that bucket until a slow pass re-materialises it (#222).
        await repo.set_limits("user", [Limit.custom("weekly", capacity, **SLOW)], "budget")

    async def test_the_fast_win_is_kept_and_the_rest_committed(self, repo):
        limiter = RateLimiter(repository=repo)
        await _admit(limiter)
        await self._budget_needs_the_slow_path(repo, 1000)
        writes = _count_transactions(repo)

        await _admit(limiter)

        assert await _ledger(repo) == ({"rpm": 2}, {"rpm": 2}, {"weekly": 20}, {})
        assert writes == [1]  # only budget went to the transaction

    async def test_a_slow_rejection_refunds_the_fast_win(self, repo):
        limiter = RateLimiter(repository=repo)
        # search's bucket exists, budget's does not: the fast path admits
        # search and finds budget missing, so the slow path plans it.
        async with limiter.acquire("user", "search", {"rpm": 1}):
            pass
        async with limiter.acquire("user", "search", {"rpm": 1}):
            pass
        before = await _ledger(repo)
        spent: list[str] = []
        real = repo.speculative_consume

        async def speculative_consume(entity_id, resource, *args, **kwargs):
            result = await real(entity_id, resource, *args, **kwargs)
            if result.success:
                spent.append(resource)
            return result

        repo.speculative_consume = speculative_consume

        with pytest.raises(RateLimitExceeded) as exc_info:
            await _admit(limiter, weekly=5000)

        assert spent == ["search"]
        assert await _ledger(repo) == before
        statuses = {(s.resource, s.limit_name, s.exceeded) for s in exc_info.value.statuses}
        # The fast path evaluated search (child and parent); the slow one budget.
        assert ("budget", "weekly", True) in statuses
        assert ("search", "rpm", False) in statuses

    async def test_a_slow_backend_error_refunds_the_fast_win(self, repo):
        limiter = RateLimiter(repository=repo)
        await _admit(limiter)
        await self._budget_needs_the_slow_path(repo, 1000)
        before = await _ledger(repo)

        async def transact_write(items):
            raise RuntimeError("throttled")

        repo.transact_write = transact_write

        with pytest.raises(RateLimiterUnavailable):
            await _admit(limiter)

        assert await _ledger(repo) == before


def _count_transactions(repo):
    """Record the item count of every transact_write."""
    counts: list[int] = []
    real = repo.transact_write

    async def transact_write(items):
        counts.append(len(items))
        return await real(items)

    repo.transact_write = transact_write
    return counts


class TestSlowPathPlanning:
    async def test_every_slow_part_commits_in_one_transaction(self, repo):
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        writes = _count_transactions(repo)

        await _admit(limiter)

        # user/search + org/search + user/budget, one transaction.
        assert writes == [3]

    async def test_a_plan_over_the_item_limit_is_re_planned_without_moves(self, repo, monkeypatch):
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        monkeypatch.setattr(sys.modules[type(limiter).__module__], "_MAX_TRANSACT_ITEMS", 1)
        calls: list[bool] = []
        real = limiter._do_acquire

        async def do_acquire(**kwargs):
            calls.append(kwargs["disable_moves"])
            return await real(**kwargs)

        limiter._do_acquire = do_acquire

        await _admit(limiter)

        assert calls == [False, False, True, True]
        assert await _ledger(repo) == ({"rpm": 1}, {"rpm": 1}, {"weekly": 10}, {})

    async def test_a_lost_move_re_plans_every_part(self, repo, monkeypatch):
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        real = Lease._commit_initial
        losses = [QuotaMoveLostError(), QuotaMoveLostError()]

        async def commit_initial(self):
            if losses and len({entry.resource for entry in self.entries}) > 1:
                raise losses.pop()
            return await real(self)

        monkeypatch.setattr(Lease, "_commit_initial", commit_initial)
        calls: list[bool] = []
        real_do = limiter._do_acquire

        async def do_acquire(**kwargs):
            calls.append(kwargs["disable_moves"])
            return await real_do(**kwargs)

        limiter._do_acquire = do_acquire

        await _admit(limiter)

        assert calls == [False, False, False, False, True, True]
        assert await _ledger(repo) == ({"rpm": 1}, {"rpm": 1}, {"weekly": 10}, {})

    async def test_a_rejected_part_does_not_lose_another_parts_move(self, repo):
        """ADR-145 I5: a planned move commits with nothing consumed on rejection."""
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        await _admit(limiter)
        real = limiter._do_acquire

        async def do_acquire(**kwargs):
            lease = await real(**kwargs)
            if kwargs["resource"] == "search":
                lease.entries[0]._donor_debit = QuotaDonorDebit(
                    shard_id=1,
                    limit_name="rpm",
                    tokens_milli=1,
                    grant_count=2,
                    guard_rf_ms=None,
                    guard_wa_ms=None,
                )
            return lease

        committed: list[set[str]] = []

        async def commit_rejected_moves(entries, carriers):
            committed.append({entry.resource for entry in entries})
            for entry in entries:  # as the real commit does: nothing consumed
                entry.consumed = 0

        limiter._do_acquire = do_acquire
        limiter._commit_rejected_moves = commit_rejected_moves

        with pytest.raises(RateLimitExceeded) as exc_info:
            await _admit(limiter, weekly=5000)

        assert committed == [{"search"}]
        # The statuses report what search was admitted with, before the move
        # commit undid it.
        search = [s for s in exc_info.value.statuses if s.resource == "search"]
        assert search and all(s.requested == 1 for s in search)


class TestRejectionCache:
    async def test_a_resource_known_short_rejects_with_no_write(self, mock_dynamodb):
        repo = Repository(
            name="test-multi-cache", region="us-east-1", _skip_deprecation_warning=True
        )
        await repo.create_table()
        await repo.set_resource_defaults("search", [Limit.custom("rpm", 100, **SLOW)])
        await repo.set_resource_defaults("budget", [Limit.custom("weekly", 20, **SLOW)])
        await repo.create_entity("user", parent_id=None, name="user")
        limiter = RateLimiter(repository=repo)
        await _admit(limiter, weekly=10)
        await _admit(limiter, weekly=10)  # warm: the cache now holds budget's state
        before = (await _consumed(repo, "user", "search"), await _consumed(repo, "user", "budget"))
        calls: list[str] = []
        real = repo.speculative_consume

        async def speculative_consume(entity_id, resource, *args, **kwargs):
            calls.append(resource)
            return await real(entity_id, resource, *args, **kwargs)

        repo.speculative_consume = speculative_consume

        with pytest.raises(RateLimitExceeded) as exc_info:
            await _admit(limiter, weekly=10)

        assert calls == []
        assert exc_info.value.primary_violation.resource == "budget"
        after = (await _consumed(repo, "user", "search"), await _consumed(repo, "user", "budget"))
        assert after == before
        await repo.close()


class TestValidation:
    @pytest.fixture
    def multi_limiter(self, repo):
        return RateLimiter(repository=repo)

    async def test_the_primary_resource_may_not_appear_in_also(self, multi_limiter):
        with pytest.raises(ValidationError, match="primary resource"):
            async with multi_limiter.acquire(
                "user", "search", {"rpm": 1}, also={"search": {"rpm": 1}}
            ):
                pass

    async def test_too_many_resources(self, multi_limiter):
        also = {f"r{i}": {"rpm": 1} for i in range(16)}
        with pytest.raises(ValidationError, match="at most 16 resources"):
            async with multi_limiter.acquire("user", "search", {"rpm": 1}, also=also):
                pass

    async def test_limits_cannot_be_combined_with_also(self, multi_limiter):
        with pytest.raises(ValidationError, match="limits="):
            async with multi_limiter.acquire(
                "user",
                "search",
                {"rpm": 1},
                limits=[Limit.per_minute("rpm", 10)],
                also={"budget": {"weekly": 1}},
            ):
                pass

    async def test_a_bad_resource_name(self, multi_limiter):
        with pytest.raises(ValidationError):
            async with multi_limiter.acquire(
                "user", "search", {"rpm": 1}, also={"bad#name": {"x": 1}}
            ):
                pass

    async def test_an_empty_also_is_a_single_resource_acquire(self, multi_limiter, repo):
        async with multi_limiter.acquire("user", "search", {"rpm": 1}, also={}) as lease:
            assert lease.resources == ("search",)
            with pytest.raises(ValidationError, match="not a resource of this lease"):
                lease.resource("budget")
        assert await _consumed(repo, "user", "search") == {"rpm": 1}

    async def test_a_handle_for_a_resource_not_in_the_lease(self, multi_limiter):
        async with multi_limiter.acquire(
            "user", "search", {"rpm": 1}, also={"budget": {"weekly": 1}}
        ) as lease:
            with pytest.raises(ValidationError, match="not a resource of this lease"):
                lease.resource("images")

    async def test_an_undeclared_key_on_a_handle_warns_with_its_resource(self, multi_limiter):
        async with multi_limiter.acquire(
            "user", "search", {"rpm": 1}, also={"budget": {"weekly": 1}}
        ) as lease:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await lease.resource("budget").adjust(rpm=1)
        messages = [str(w.message) for w in caught if issubclass(w.category, FutureWarning)]
        assert len(messages) == 1
        assert "lease.resource('budget').adjust()" in messages[0]
        assert "also={'budget': ...}" in messages[0]


class TestLeaseWithoutBoundResources:
    """A lease built without `_bind_resources` keeps its pre-ADR-148 meaning."""

    def test_resources_come_from_its_entries(self):
        entries = [
            LeaseEntry(
                entity_id="e",
                resource=name,
                limit=Limit.per_minute("rpm", 10),
                state=None,
            )
            for name in ("a", "b", "a")
        ]
        lease = Lease(repository=None, entries=entries)
        assert lease.resources == ("a", "b")
        assert lease.consumed == {"rpm": 0}
        assert lease.resource("b").consumed == {"rpm": 0}
