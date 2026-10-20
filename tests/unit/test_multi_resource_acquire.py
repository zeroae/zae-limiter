"""One acquire debiting several resources, all or none (ADR-148, #675).

The #674 shape throughout: ``search`` is metered per user and cascades to the
organisation, ``budget`` is a per-user allowance that does not cascade. Every
assertion on DynamoDB reads the consumption counter (``tc``), which every
debit and every refund moves, so "nothing debited" means the counter is back
where it started.
"""

import asyncio
import sys
import warnings
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

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
from zae_limiter.lease import Lease, LeaseEntry, PeriodNotAppliedError, QuotaMoveLostError
from zae_limiter.models import BucketState, Limit, LimitStatus, QuotaDonorDebit
from zae_limiter.schema import BUCKET_FIELD_TK, bucket_attr, pk_bucket, sk_state
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

    async def test_disabled_outranks_a_fast_rejection_that_comes_first(self, repo, speculative):
        """A disabled resource with no bucket yet is only found by the slow
        path, after the fast path already rejected another resource: the 403
        still wins (ADR-148)."""
        limiter = RateLimiter(repository=repo, speculative_writes=speculative)
        await repo.create_entity("fresh", parent_id=None, name="fresh")
        await repo.set_limits("fresh", [Limit.custom("rpm", 1, **SLOW)], "search")
        async with limiter.acquire("fresh", "search", {"rpm": 1}):
            pass  # search is now exhausted, budget has no bucket
        await repo.disable_resource("budget")

        with pytest.raises(ResourceDisabled) as exc_info:
            async with limiter.acquire(
                "fresh", "search", {"rpm": 1}, also={"budget": {"weekly": 1}}
            ):
                pytest.fail("a disabled resource must not admit")

        assert exc_info.value.resource == "budget"

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

    async def test_a_cancelled_acquire_refunds_the_fast_wins(self, repo):
        limiter = RateLimiter(repository=repo)
        await _admit(limiter)
        before = await _ledger(repo)
        search_landed = asyncio.Event()
        budget_blocked = asyncio.Event()
        real = repo.speculative_consume

        async def speculative_consume(entity_id, resource, *args, **kwargs):
            if resource == "budget":
                await search_landed.wait()
                budget_blocked.set()
                await asyncio.Event().wait()  # never returns: the caller is cancelled
            result = await real(entity_id, resource, *args, **kwargs)
            search_landed.set()
            return result

        repo.speculative_consume = speculative_consume

        async def acquire():
            async with limiter.acquire(
                "user", "search", {"rpm": 1}, also={"budget": {"weekly": 1}}
            ):
                pytest.fail("a cancelled acquire must not admit")

        task = asyncio.ensure_future(acquire())
        await budget_blocked.wait()
        await asyncio.sleep(0)  # let search's outcome be recorded
        assert await _ledger(repo) != before  # search's debit is written
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

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

    @pytest.mark.parametrize("contended", [True, False])
    async def test_the_second_loss_decides_whether_the_zeroed_plan_is_contended(
        self, repo, monkeypatch, contended
    ):
        """A move lost to write conflicts is a busy donor, not a spent one
        (#724): the zeroed re-plan must hear which, as in `_slow_acquire`."""
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        real = Lease._commit_initial
        # `pop()` takes from the end: the first loss is never contended, the
        # second (the one that zeroes the slot) is `contended`.
        losses = [QuotaMoveLostError(contended=contended), QuotaMoveLostError(contended=False)]

        async def commit_initial(self):
            if losses and len({entry.resource for entry in self.entries}) > 1:
                raise losses.pop()
            return await real(self)

        monkeypatch.setattr(Lease, "_commit_initial", commit_initial)
        passes: list[tuple[bool, bool]] = []
        real_do = limiter._do_acquire

        async def do_acquire(**kwargs):
            passes.append((kwargs["disable_moves"], kwargs.get("moves_contended", False)))
            return await real_do(**kwargs)

        limiter._do_acquire = do_acquire

        await _admit(limiter)

        assert passes[-1] == (True, contended)

    @pytest.mark.parametrize("losses", [1, 2])
    async def test_an_unapplied_period_is_redone_once(self, repo, monkeypatch, losses):
        """The retry found a quota period nobody applied (#743): the combined
        commit re-plans once from a fresh read, and a second loss stands."""
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        status = LimitStatus(
            entity_id="user",
            resource="budget",
            limit_name="weekly",
            limit=Limit.custom("weekly", 1000, **SLOW),
            available=0,
            requested=10,
            exceeded=True,
            retry_after_seconds=1.0,
        )
        real = Lease._commit_initial
        left = [losses]

        async def commit_initial(self):
            if left[0] and len({entry.resource for entry in self.entries}) > 1:
                left[0] -= 1
                raise PeriodNotAppliedError([status])
            return await real(self)

        monkeypatch.setattr(Lease, "_commit_initial", commit_initial)
        planned: list[str] = []
        real_do = limiter._do_acquire

        async def do_acquire(**kwargs):
            planned.append(kwargs["resource"])
            return await real_do(**kwargs)

        limiter._do_acquire = do_acquire

        if losses == 1:
            await _admit(limiter)
            assert await _ledger(repo) == ({"rpm": 1}, {"rpm": 1}, {"weekly": 10}, {})
        else:
            with pytest.raises(RateLimitExceeded):
                await _admit(limiter)
            assert await _ledger(repo) == ({}, {}, {}, {})
        assert len(planned) == 4  # two parts, planned twice: never a third time

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
                state=BucketState(
                    entity_id="e",
                    resource=name,
                    limit_name="rpm",
                    tokens_milli=10_000,
                    last_refill_ms=0,
                    capacity_milli=10_000,
                    refill_amount_milli=10_000,
                    refill_period_ms=60_000,
                ),
            )
            for name in ("a", "b", "a")
        ]
        lease = Lease(repository=None, entries=entries)
        assert lease.resources == ("a", "b")
        assert lease.consumed == {"rpm": 0}
        assert lease.resource("b").consumed == {"rpm": 0}


MIDNIGHT = int(datetime(2026, 10, 10, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)


class TestCombinedLeaseKeepsCreditImages:
    """Combining committed leases must not recompute their credit images (#721).

    The images are what a refund pins (and, for a quota, the period it stays
    in). ``Lease.__post_init__`` recomputes them from the entries' states,
    which after a write are the *pre*-write ones: rebuilding the combined lease
    replaced the post-write images and a quota refund was dropped.
    """

    @staticmethod
    async def _daily_budget(repo, now):
        repo._now_ms = lambda: now[0]
        await repo.set_resource_defaults("budget", [Limit.quota("weekly", 1000, cron="0 0 * * *")])

    @staticmethod
    async def _tokens(repo, entity_id="user"):
        client = await repo._get_client()
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": pk_bucket(repo._namespace_id, entity_id, "budget", 0)},
                "SK": {"S": sk_state()},
            },
        )
        return int(response["Item"][bucket_attr("weekly", BUCKET_FIELD_TK)]["N"])

    async def test_a_quota_refund_after_a_slow_path_create_lands(self, repo):
        now = [MIDNIGHT - 3_600_000]
        await self._daily_budget(repo, now)
        limiter = RateLimiter(repository=repo, speculative_writes=False)

        with pytest.raises(RuntimeError):
            async with limiter.acquire(
                "user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}
            ):
                raise RuntimeError("body failed")

        assert await _consumed(repo, "user", "budget") == {"weekly": 0}
        assert await self._tokens(repo) == 1_000_000

    async def test_a_quota_refund_after_the_pass_reset_it_lands(self, repo):
        now = [MIDNIGHT - 1000]
        await self._daily_budget(repo, now)
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        await _admit(limiter, weekly=1)  # yesterday: 999 left
        now[0] = MIDNIGHT + 1000  # the next pass applies the reset edge

        with pytest.raises(RuntimeError):
            async with limiter.acquire(
                "user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}
            ):
                raise RuntimeError("body failed")

        # Reset to 1000, debited 10, refunded 10; `tc` is not reset by the edge.
        assert await _consumed(repo, "user", "budget") == {"weekly": 1}
        assert await self._tokens(repo) == 1_000_000

    async def test_a_released_quota_after_a_slow_path_create_lands(self, repo):
        now = [MIDNIGHT - 3_600_000]
        await self._daily_budget(repo, now)
        limiter = RateLimiter(repository=repo, speculative_writes=False)

        async with limiter.acquire(
            "user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}
        ) as lease:
            await lease.resource("budget").release(weekly=10)

        assert await self._tokens(repo) == 1_000_000


def _conflict() -> ClientError:
    """What a write meeting an in-flight transaction raises; moto never does (#724)."""
    return ClientError(
        {"Error": {"Code": "TransactionConflictException", "Message": "ongoing"}}, "UpdateItem"
    )


def _contended_429(entity_id, resource, limit_name) -> RateLimitExceeded:
    """The 429 a busy table produces: nothing exceeded, ``contended``."""
    status = LimitStatus(
        entity_id=entity_id,
        resource=resource,
        limit_name=limit_name,
        limit=Limit.custom(limit_name, 100, **SLOW),
        available=0,
        requested=1,
        exceeded=False,
        retry_after_seconds=0.1,
    )
    return RateLimitExceeded([status], contended=True)


class TestContentionAcrossResources:
    """A busy table is a contended 429 across resources too (#724, ADR-148)."""

    @pytest.mark.parametrize("on_unavailable", [OnUnavailable.BLOCK, OnUnavailable.ALLOW])
    async def test_a_contended_slow_commit_after_a_fast_win_is_a_contended_429(
        self, repo, on_unavailable
    ):
        limiter = RateLimiter(repository=repo)
        await _admit(limiter)
        # An entity-level change leaves budget's bucket needing a slow pass.
        await repo.set_limits("user", [Limit.custom("weekly", 1000, **SLOW)], "budget")
        before = await _ledger(repo)

        async def transact_write(items):
            raise _conflict()

        repo.transact_write = transact_write

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire(
                "user",
                "search",
                {"rpm": 1},
                on_unavailable=on_unavailable,
                also={"budget": {"weekly": 10}},
            ):
                pytest.fail("a contended acquire must not admit")

        assert exc_info.value.contended is True
        assert await _ledger(repo) == before

    @pytest.mark.parametrize("on_unavailable", [OnUnavailable.BLOCK, OnUnavailable.ALLOW])
    async def test_contention_while_planning_is_a_contended_429(self, repo, on_unavailable):
        limiter = RateLimiter(repository=repo, speculative_writes=False)
        await _admit(limiter)
        before = await _ledger(repo)

        async def do_acquire(**kwargs):
            raise _contended_429("user", kwargs["resource"], "rpm")

        limiter._do_acquire = do_acquire

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire(
                "user",
                "search",
                {"rpm": 1},
                on_unavailable=on_unavailable,
                also={"budget": {"weekly": 10}},
            ):
                pytest.fail("a contended acquire must not admit")

        assert exc_info.value.contended is True
        assert {s.resource for s in exc_info.value.statuses} == {"search", "budget"}
        assert await _ledger(repo) == before


class TestContendedQuotaMoveAcrossResources:
    """A quota slot zeroed by contended moves, beside another resource (#724, ADR-148)."""

    QUOTA = Limit.quota("rpd", 1000, cron="0 0 * * *")

    @pytest.mark.parametrize("on_unavailable", [OnUnavailable.BLOCK, OnUnavailable.ALLOW])
    async def test_a_zero_grant_after_contended_moves_is_a_contended_429(
        self, repo, on_unavailable
    ):
        now_ms = repo._now_ms()
        repo._now_ms = lambda: now_ms
        await repo.set_resource_defaults("daily", [self.QUOTA])
        await repo.set_resource_cascade("daily", False)
        await repo.create_entity("solo", parent_id=None, name="solo")
        # Shard 0 holds the whole quota at count 1, then the count doubles to 2,
        # so a draw on shard 1 plans a move off shard 0.
        state = BucketState.from_limit("solo", "daily", self.QUOTA, now_ms)
        vu, _reset = RateLimiter._materialisation_stamps(self.QUOTA, state, now_ms)
        await repo.transact_write(
            [
                repo.build_composite_create(
                    "solo", "daily", [state], now_ms, shard_id=0, shard_count=1, vu=vu
                )
            ]
        )
        assert await repo.bump_shard_count("solo", "daily", 1) == 2
        real = repo.transact_write

        async def moves_conflict(items):
            if len(items) > 1:  # a transaction carrying a donor debit
                raise ClientError(
                    {
                        "Error": {"Code": "TransactionCanceledException", "Message": "busy"},
                        "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}],
                    },
                    "TransactWriteItems",
                )
            return await real(items)

        repo.transact_write = moves_conflict
        limiter = RateLimiter(repository=repo, speculative_writes=False)

        with patch("zae_limiter.repository.random.randrange", return_value=1):
            with pytest.raises(RateLimitExceeded) as exc_info:
                async with limiter.acquire(
                    "solo",
                    "daily",
                    {"rpd": 1},
                    on_unavailable=on_unavailable,
                    also={"budget": {"weekly": 10}},
                ):
                    pytest.fail("a contended acquire must not admit")

        assert exc_info.value.contended is True
        assert exc_info.value.violations == []
        assert await _consumed(repo, "solo", "daily") == {"rpd": 0}
        assert await _consumed(repo, "solo", "budget") == {}


class TestDisabledCheckOnTheRejectionPath:
    """The look for a disabled resource must never cost the 429 (ADR-148)."""

    @pytest.mark.parametrize("on_unavailable", [OnUnavailable.BLOCK, OnUnavailable.ALLOW])
    async def test_a_failed_check_keeps_the_429_and_the_refund(self, repo, on_unavailable):
        await repo.set_resource_defaults("extra", [Limit.custom("daily", 1000, **SLOW)])
        await repo.set_resource_cascade("extra", False)
        limiter = RateLimiter(repository=repo)
        await _admit(limiter)  # search and budget now have buckets; extra has none
        before = await _ledger(repo)

        async def resolve_access(*args, **kwargs):
            raise RuntimeError("throttled")

        repo.resolve_access = resolve_access

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with limiter.acquire(
                "user",
                "search",
                {"rpm": 1},
                on_unavailable=on_unavailable,
                also={"budget": {"weekly": 5000}, "extra": {"daily": 1}},
            ):
                pytest.fail("a rejected acquire must not admit")

        assert {(s.resource, s.limit_name) for s in exc_info.value.violations} == {
            ("budget", "weekly")
        }
        assert await _ledger(repo) == before

    async def test_a_disabled_cascade_parent_outranks_the_429(self, repo):
        await repo.set_resource_defaults("images", [Limit.custom("daily", 1000, **SLOW)])
        await repo.disable_entity("org", resource="images")
        limiter = RateLimiter(repository=repo)
        await _admit(limiter)  # budget now has a bucket; images has none
        before = await _ledger(repo)

        # One resource alone already reports the parent, not the child.
        with pytest.raises(ResourceDisabled) as single:
            async with limiter.acquire("user", "images", {"daily": 1}):
                pytest.fail("a disabled parent must not admit")
        assert single.value.entity_id == "org"

        with pytest.raises(ResourceDisabled) as exc_info:
            async with limiter.acquire(
                "user",
                "budget",
                {"weekly": 5000},
                also={"images": {"daily": 1}},
            ):
                pytest.fail("a disabled parent must not admit")

        assert exc_info.value.entity_id == "org"
        assert exc_info.value.resource == "images"
        assert await _ledger(repo) == before
