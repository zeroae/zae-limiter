"""A new shard of a quota must not mint allowance (#587).

``Limit.quota`` has no drip (ADR-137), so a shard created mid-period at
``capacity // shard_count`` is pure net-new allowance that nothing reclaims
before the next reset edge. These tests pin the conserving rule that replaces
it, and — just as importantly — pin that the **dripping** path is untouched.
"""

from datetime import timedelta
from unittest.mock import ANY, patch

import pytest
from botocore.exceptions import ClientError

from tests.fixtures.sharding import (
    RESOURCE,
    drain_shard,
    materialise,
    shard_balances,
    spendable,
    walk_doublings,
)
from zae_limiter import Limit, RateLimiter, schema
from zae_limiter.models import (
    BucketState,
    QuotaDonorDebit,
    QuotaGrant,
    new_shard_starting_tokens_milli,
)

QUOTA_CRON = "0 0 * * *"


def freeze(repo) -> int:
    """Pin the token-bucket clock: no wall-clock drip, no reset edge crossed."""
    frozen = repo._now_ms()
    repo._now_ms = lambda: frozen
    return frozen


async def seed_shard0(limiter, entity_id, limit, now_ms, resource=RESOURCE):
    """Create the entity and its shard-0 composite item at ``shard_count=1``."""
    repo = limiter._repository
    await limiter.create_entity(entity_id)
    await limiter.set_system_defaults([limit])
    state = BucketState.from_limit(entity_id, resource, limit, now_ms)
    # `vu` the way the slow path stamps it (#222 §2.1): without it a quota's
    # fast path never demotes, so a crossed reset edge is never materialised.
    vu, _reset = RateLimiter._materialisation_stamps(limit, state, now_ms)
    await repo.transact_write(
        [
            repo.build_composite_create(
                entity_id, resource, [state], now_ms, shard_id=0, shard_count=1, vu=vu
            )
        ]
    )
    repo._entity_cache[(repo._namespace_id, entity_id)] = (False, None, {resource: 1})


async def _write_shard(
    repo,
    entity_id,
    limit,
    shard_id,
    tokens_milli,
    resource=RESOURCE,
    *,
    grant_count: int | None = None,
    shard_count: int = 2,
    window_start_ms: int | None = None,
):
    """Put a sibling shard item directly, holding a chosen balance.

    ``grant_count`` writes ``b_{name}_gc`` (ADR-145); ``None`` leaves it off,
    the shape of a v0.14 item. ``window_start_ms`` opens a session window.
    """
    now_ms = repo._now_ms()
    state = BucketState.from_limit(entity_id, resource, limit, now_ms)
    state.tokens_milli = tokens_milli
    state.grant_count = grant_count
    if window_start_ms is not None:
        state.window_start_ms = window_start_ms
    vu, _reset = RateLimiter._materialisation_stamps(limit, state, now_ms)
    await repo.transact_write(
        [
            repo.build_composite_create(
                entity_id,
                resource,
                [state],
                now_ms,
                shard_id=shard_id,
                shard_count=shard_count,
                vu=vu,
            )
        ]
    )


async def _item(repo, entity_id, shard_id, resource=RESOURCE):
    """One shard's raw item, or ``{}`` if it does not exist."""
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response.get("Item") or {}


class TestQuotaShardCreation:
    """#587: creating a shard must not change what the entity can still spend."""

    async def test_quota_doubling_walk_does_not_multiply_the_allowance(self, limiter):
        """1000/day admitted 3428 inside one frozen period before the fix."""
        repo = limiter._repository
        freeze(repo)
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        await seed_shard0(limiter, "quota-walk", limit, repo._now_ms())

        admitted, shard_count, spends = await walk_doublings(limiter, "quota-walk", "rpd")
        left = await spendable(repo, "quota-walk", "rpd", shard_count)

        assert shard_count == schema.MAX_SHARD_COUNT
        assert admitted + left <= 1000, (
            f"admitted {admitted} + {left} still drawable against a quota of 1000; "
            f"per-doubling spendable before/after: {spends}"
        )

    async def test_doubling_conserves_the_entity_wide_quota(self, limiter):
        """The invariant: spendable tokens are equal either side of a doubling."""
        repo = limiter._repository
        freeze(repo)
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        await seed_shard0(limiter, "quota-conserve", limit, repo._now_ms())

        _admitted, _count, spends = await walk_doublings(limiter, "quota-conserve", "rpd")
        for index, (before, after) in enumerate(spends):
            assert after == before, f"doubling {index}: {before} -> {after}"


class TestNewShardStartingTokens:
    """The rule itself, with no DynamoDB in the way."""

    SHARE = 500_000

    def test_a_dripping_limit_always_gets_the_full_share(self):
        """The regression pin. ``ra`` is stored undivided, so the shards'
        ceilings still sum to the configured capacity and a new one starting
        full is a burst token-bucket semantics already permit."""
        for taken in (None, 0, 1, self.SHARE, self.SHARE * 4):
            got = new_shard_starting_tokens_milli(self.SHARE, taken, is_quota=False)
            assert got == self.SHARE

    def test_a_quota_with_nothing_to_reclaim_from_gets_the_full_share(self):
        """``None`` means no shard exists at all, so nothing has been spent."""
        assert new_shard_starting_tokens_milli(self.SHARE, None, is_quota=True) == self.SHARE

    def test_a_spent_quota_gets_nothing(self):
        """#587: the siblings held no surplus, so there is nothing to transfer."""
        assert new_shard_starting_tokens_milli(self.SHARE, 0, is_quota=True) == 0

    def test_a_full_quota_gets_a_full_share(self):
        """Unchanged from before #587 — the case the bug hid behind."""
        assert new_shard_starting_tokens_milli(self.SHARE, self.SHARE, is_quota=True) == self.SHARE

    def test_a_partly_spent_quota_gets_exactly_what_was_reclaimed(self):
        assert new_shard_starting_tokens_milli(self.SHARE, 120_000, is_quota=True) == 120_000

    def test_the_grant_is_capped_at_one_share(self):
        """Several siblings can between them yield more than one shard's worth;
        the rest stays with whoever is created next."""
        got = new_shard_starting_tokens_milli(self.SHARE, self.SHARE * 3, is_quota=True)
        assert got == self.SHARE

    def test_a_negative_reclaim_cannot_produce_a_negative_balance(self):
        assert new_shard_starting_tokens_milli(self.SHARE, -1, is_quota=True) == 0


class TestFromLimitStartingBalance:
    """``BucketState.from_limit`` routes the rule, and nothing else changed."""

    def test_quota_shard_created_from_a_transfer(self):
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        state = BucketState.from_limit("e1", "gpt-4", limit, 0, 4, reclaimed_milli=0)
        assert state.tokens_milli == 0
        assert state.capacity_milli == 1_000_000  # stored base stays undivided
        assert state.reset_target_milli(0) == 250_000

    def test_quota_shard_with_no_siblings_starts_full(self):
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        state = BucketState.from_limit("e1", "gpt-4", limit, 0, 4)
        assert state.tokens_milli == 250_000

    def test_dripping_shard_ignores_the_transfer_entirely(self):
        """Pins that the dripping path is untouched by #587."""
        limit = Limit.per_minute("rpm", 1000)
        state = BucketState.from_limit("e1", "gpt-4", limit, 0, 4, reclaimed_milli=0)
        assert state.tokens_milli == 250_000


class TestSlowPathShardCreation:
    """What the slow path actually writes when it creates a shard (ADR-133)."""

    async def _seed(self, limiter, entity_id, limit):
        """Shard 0 at full capacity, then a real ``wcu``-style doubling to 2.

        ``bump_shard_count`` writes the new count onto the item as well as the
        entity cache, which is what the reset edge divides the capacity by.
        """
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, entity_id, limit, repo._now_ms())
        assert await repo.bump_shard_count(entity_id, RESOURCE, 1) == 2
        return repo

    async def test_a_full_quota_transfers_half_and_conserves(self, limiter):
        """The case #587 hid behind: the grant is paid for by the clamp, so the
        entity-wide spendable total is the same either side of the doubling."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-full", limit)

        assert await spendable(repo, "q-full", "rpd", 2) == 1000
        assert await materialise(limiter, "q-full", "rpd", 1) == 1
        # 1000 conserved, less the single token the creating acquire consumed.
        assert await spendable(repo, "q-full", "rpd", 2) == 999
        assert await shard_balances(repo, "q-full", "rpd", 2) == [500_000, 499_000]

    async def test_a_spent_quota_transfers_nothing(self, limiter):
        """#587 itself: 998 of 1000 already spent, and the doubling must not
        hand back 499 of them."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-spent", limit)
        await drain_shard(repo, "q-spent", "rpd", 0)

        assert await spendable(repo, "q-spent", "rpd", 2) == 2
        assert await materialise(limiter, "q-spent", "rpd", 1) == 0  # rejected
        assert await spendable(repo, "q-spent", "rpd", 2) == 2

    async def test_a_partly_spent_quota_transfers_only_the_surplus(self, limiter):
        """Shard 0 holds 700 against a new ceiling of 500: 200 moves, 500 stays."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-part", limit)
        await drain_shard(repo, "q-part", "rpd", 0, floor=700)

        assert await spendable(repo, "q-part", "rpd", 2) == 700
        assert await materialise(limiter, "q-part", "rpd", 1) == 1
        assert await shard_balances(repo, "q-part", "rpd", 2) == [500_000, 199_000]
        assert await spendable(repo, "q-part", "rpd", 2) == 699

    async def test_a_dripping_limit_is_untouched(self, limiter):
        """The regression pin. A spent dripping shard still mints its new
        sibling a full share, and its own balance is not clamped early."""
        limit = Limit.per_minute("rpm", 1000)
        repo = await self._seed(limiter, "d-spent", limit)
        await drain_shard(repo, "d-spent", "rpm", 0)

        assert await materialise(limiter, "d-spent", "rpm", 1) == 1
        assert await shard_balances(repo, "d-spent", "rpm", 2) == [2_000, 499_000]

    async def test_a_reset_edge_restores_exactly_the_configured_quota(self, limiter):
        """#222 §3.6 sets each shard to its share, so a doubled entity comes
        back to exactly C — the transient is the only thing #587 changes."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-reset", limit)
        assert await materialise(limiter, "q-reset", "rpd", 1) == 1
        await drain_shard(repo, "q-reset", "rpd", 0, floor=0)
        await drain_shard(repo, "q-reset", "rpd", 1, floor=0)
        assert await spendable(repo, "q-reset", "rpd", 2) == 0

        frozen = repo._now_ms()
        repo._now_ms = lambda: frozen + 2 * 86_400_000
        assert await materialise(limiter, "q-reset", "rpd", 0) == 1
        assert await materialise(limiter, "q-reset", "rpd", 1) == 1
        # Both shards back to their 500 share, less the two tokens just spent.
        assert await spendable(repo, "q-reset", "rpd", 2) == 998


class TestReclaimQuotaSurplus:
    """``Repository.reclaim_quota_surplus`` — the eager half of the transfer."""

    LIMIT = Limit.quota("rpd", 1000, cron=QUOTA_CRON)

    async def test_no_limits_reads_nothing(self, limiter):
        repo = limiter._repository
        with patch.object(repo, "_discover_entity_bucket_pks") as discover:
            assert await repo.reclaim_quota_surplus("nobody", RESOURCE, {}) == (0, {})
        discover.assert_not_called()

    async def test_no_shards_is_distinguishable_from_no_surplus(self, limiter):
        """Zero shards means nothing was ever spent, so the caller grants a full
        share; zero *reclaimed* from shards that do exist means the opposite."""
        repo = limiter._repository
        assert await repo.reclaim_quota_surplus("nobody", RESOURCE, {"rpd": 500_000}) == (
            0,
            {"rpd": 0},
        )

    async def test_clamps_every_shard_and_sums_what_it_took(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, "rq-multi", self.LIMIT, repo._now_ms())
        await _write_shard(repo, "rq-multi", self.LIMIT, shard_id=1, tokens_milli=700_000)

        shares = {"rpd": 250_000}
        found, reclaimed = await repo.reclaim_quota_surplus("rq-multi", RESOURCE, shares)
        assert found == 2
        # shard 0 held 1_000_000 and shard 1 held 700_000 against a 250_000 share.
        assert reclaimed == {"rpd": 750_000 + 450_000}
        assert await shard_balances(repo, "rq-multi", "rpd", 2) == [250_000, 250_000]

    async def test_a_shard_already_at_its_share_is_not_written(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, "rq-flat", self.LIMIT, repo._now_ms())
        await repo.reclaim_quota_surplus("rq-flat", RESOURCE, {"rpd": 500_000})

        client = await repo._get_client()
        with patch.object(client, "update_item", side_effect=AssertionError("wrote")) as write:
            assert await repo.reclaim_quota_surplus("rq-flat", RESOURCE, {"rpd": 500_000}) == (
                1,
                {"rpd": 0},
            )
        write.assert_not_called()

    async def test_a_shard_without_that_limit_contributes_nothing(self, limiter):
        """A bucket written before the quota was added to the config carries no
        ``b_rpd_tk`` at all."""
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, "rq-other", Limit.per_minute("rpm", 1000), repo._now_ms())
        assert await repo.reclaim_quota_surplus("rq-other", RESOURCE, {"rpd": 500_000}) == (
            1,
            {"rpd": 0},
        )

    async def test_a_shard_spent_below_the_share_since_the_read_is_skipped(self, limiter):
        """The conditional write is what makes the reclaim safe against a
        concurrent speculative spend: there is no surplus left to move."""
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, "rq-race", self.LIMIT, repo._now_ms())
        client = await repo._get_client()
        error = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "raced"}},
            "UpdateItem",
        )
        with patch.object(client, "update_item", side_effect=error):
            assert await repo.reclaim_quota_surplus("rq-race", RESOURCE, {"rpd": 500_000}) == (
                1,
                {"rpd": 0},
            )

    async def test_any_other_client_error_propagates(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, "rq-boom", self.LIMIT, repo._now_ms())
        client = await repo._get_client()
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}},
            "UpdateItem",
        )
        with patch.object(client, "update_item", side_effect=error):
            with pytest.raises(ClientError):
                await repo.reclaim_quota_surplus("rq-boom", RESOURCE, {"rpd": 500_000})


class TestPlanQuotaShard:
    """``Repository.plan_quota_shard`` and ``build_quota_donor_debits`` (ADR-145)."""

    QUOTA = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
    GC = schema.bucket_attr("rpd", schema.BUCKET_FIELD_GC)

    async def test_no_shards_is_a_fresh_grant(self, limiter):
        count, grants, debits = await limiter._repository.plan_quota_shard(
            "e1",
            RESOURCE,
            [self.QUOTA],
            shard_id=1,
            shard_count=2,
            now_ms=freeze(limiter._repository),
        )
        assert count == 2
        assert grants == {"rpd": QuotaGrant(donor_shard=None, tokens_milli=500_000)}
        assert debits == []

    async def test_no_quota_or_one_shard_reads_nothing(self, limiter):
        repo = limiter._repository
        now = freeze(repo)
        with patch.object(repo, "_discover_entity_bucket_pks") as discover:
            assert await repo.plan_quota_shard(
                "e1", RESOURCE, [Limit.per_minute("rpm", 10)], 1, 2, now
            ) == (2, {}, [])
            assert await repo.plan_quota_shard("e1", RESOURCE, [self.QUOTA], 0, 1, now) == (
                1,
                {},
                [],
            )
        discover.assert_not_called()

    async def test_split_plans_a_move_from_the_parent(self, limiter):
        now = freeze(limiter._repository)
        await seed_shard0(limiter, "e1", self.QUOTA, now, RESOURCE)  # 1000 held, count 1
        count, grants, debits = await limiter._repository.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], shard_id=1, shard_count=2, now_ms=now
        )
        assert count == 2
        assert grants["rpd"] == QuotaGrant(donor_shard=0, tokens_milli=500_000, donor_grant_count=1)
        assert debits == [QuotaDonorDebit(0, "rpd", 500_000, 1, guard_rf_ms=ANY, guard_wa_ms=None)]
        assert debits[0].guard_rf_ms is not None and debits[0].guard_rf_ms <= now

    async def test_the_count_is_the_largest_stored(self, limiter):
        """A stale caller at 2 plans at the siblings' 4: slot 1's share is 250."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 1_000_000, grant_count=1, shard_count=4)
        count, grants, _debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        assert count == 4
        assert grants["rpd"] == QuotaGrant(donor_shard=0, tokens_milli=250_000, donor_grant_count=1)

    async def test_a_spent_donor_plans_no_debit(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 0, grant_count=1, shard_count=2)
        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        assert grants["rpd"] == QuotaGrant(donor_shard=0, tokens_milli=0, donor_grant_count=1)
        assert debits == []

    async def test_a_sibling_without_the_quota_is_not_a_donor(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", Limit.per_minute("rpm", 10), 0, 10_000, shard_count=2)
        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        assert grants["rpd"].donor_shard is None and debits == []

    async def test_lagging_sibling_count_is_propagated_before_a_fresh_grant(self, limiter):
        """R5: shard 1 still says count 2 while the caller is at 4."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 250_000, grant_count=4, shard_count=4)
        await _write_shard(repo, "e1", self.QUOTA, 1, 500_000, grant_count=2, shard_count=2)

        count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 2, 4, repo._now_ms()
        )
        # Slot 2: shard 0 at gc 4 covers {0}; shard 1 at gc 2 covers {1, 3}.
        assert count == 4
        assert grants["rpd"] == QuotaGrant(donor_shard=None, tokens_milli=250_000)
        assert debits == []
        assert (await _item(repo, "e1", 1))["shard_count"] == {"N": "4"}
        assert (await _item(repo, "e1", 0))["shard_count"] == {"N": "4"}

    async def test_a_lagging_shard_zero_is_propagated_too(self, limiter):
        """``_propagate_shard_count`` alone starts at shard 1; the planner names 0."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 500_000, grant_count=2, shard_count=2)
        await _write_shard(repo, "e1", self.QUOTA, 1, 250_000, grant_count=4, shard_count=4)

        _count, grants, _debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 3, 4, repo._now_ms()
        )
        # Slot 3: shard 0 at gc 2 covers {0, 2}; shard 1 at gc 4 covers {1}.
        assert grants["rpd"].donor_shard is None
        assert (await _item(repo, "e1", 0))["shard_count"] == {"N": "4"}

    async def test_a_move_does_not_propagate(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 1_000_000, grant_count=1, shard_count=1)
        with patch.object(repo, "_freeze_and_raise_shard_counts") as propagate:
            await repo.plan_quota_shard("e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms())
        propagate.assert_not_called()

    async def test_raising_a_legacy_sibling_freezes_its_grant_size(self, limiter):
        """Review fix: raising a no-gc sibling's count must not shrink its coverage.

        Legacy shard 0 at count 2 holding 500 covers {0, 2}. Planning shard 3
        raises it to 4; it must keep covering slot 2, so shard 2 is a move off
        it rather than a second 250 minted beside the 500 it still holds.
        """
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 500_000, shard_count=2)
        await _write_shard(repo, "e1", self.QUOTA, 1, 250_000, grant_count=4, shard_count=4)

        _count, grants, _debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 3, 4, repo._now_ms()
        )
        assert grants["rpd"].donor_shard is None
        shard0 = await _item(repo, "e1", 0)
        assert shard0["shard_count"] == {"N": "4"}
        assert shard0[self.GC] == {"N": "2"}

        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 2, 4, repo._now_ms()
        )
        assert grants["rpd"] == QuotaGrant(donor_shard=0, tokens_milli=250_000, donor_grant_count=2)
        await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, debits))
        assert await shard_balances(repo, "e1", "rpd", 1) == [250_000]

    async def test_a_move_off_a_legacy_donor_survives_a_fresh_grant_beside_it(self, limiter):
        """Review fix: quota A moves off legacy shard 0 while quota B is fresh.

        B's fresh grant raises shard 0's count before the debit is written; the
        debit must still pass, because the freeze stamps A's gc at the count
        the debit expects.
        """
        repo = limiter._repository
        freeze(repo)
        quota_b = Limit.quota("rpw", 700, cron="0 0 * * 1")
        await _write_shard(repo, "e1", self.QUOTA, 0, 1_000_000, shard_count=1)

        count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA, quota_b], 1, 2, repo._now_ms()
        )
        assert count == 2
        assert grants["rpd"].donor_shard == 0
        assert grants["rpw"] == QuotaGrant(donor_shard=None, tokens_milli=350_000)
        shard0 = await _item(repo, "e1", 0)
        assert shard0["shard_count"] == {"N": "2"}
        assert shard0[self.GC] == {"N": "1"}
        assert schema.bucket_attr("rpw", schema.BUCKET_FIELD_GC) not in shard0

        await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, debits))
        assert await shard_balances(repo, "e1", "rpd", 1) == [500_000]

    async def test_a_sibling_already_raised_is_left_alone(self, limiter):
        repo = limiter._repository
        client = await repo._get_client()
        error = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "raced"}},
            "UpdateItem",
        )
        with patch.object(client, "update_item", side_effect=error):
            assert (
                await repo._freeze_and_raise_shard_counts("e1", RESOURCE, [(0, 2, ["rpd"])], 4) == 0
            )

    async def test_any_other_error_raising_a_sibling_propagates(self, limiter):
        repo = limiter._repository
        client = await repo._get_client()
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}},
            "UpdateItem",
        )
        with patch.object(client, "update_item", side_effect=error):
            with pytest.raises(ClientError):
                await repo._freeze_and_raise_shard_counts("e1", RESOURCE, [(0, 2, ["rpd"])], 4)

    async def test_a_corrupt_grant_count_reads_as_the_shard_count(self, limiter):
        """A stored ``gc < 1`` must not become a modulus of zero."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 1_000_000, grant_count=0, shard_count=1)
        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        assert grants["rpd"] == QuotaGrant(donor_shard=0, tokens_milli=500_000, donor_grant_count=1)
        assert [d.grant_count for d in debits] == [1]

    async def test_a_session_donor_is_guarded_on_its_window_marker(self, limiter):
        repo = limiter._repository
        now = freeze(repo)
        session = Limit.quota("session", 1000, reset_after=timedelta(hours=5))
        await _write_shard(
            repo, "e1", session, 0, 1_000_000, shard_count=1, window_start_ms=now - 1000
        )
        _count, grants, debits = await repo.plan_quota_shard("e1", RESOURCE, [session], 1, 2, now)
        assert grants["session"].donor_shard == 0
        assert debits == [QuotaDonorDebit(0, "session", 500_000, 1, None, now - 1000)]

    async def test_legacy_donor_without_gc_can_donate(self, limiter):
        """Review focus 4: a v0.14 item (no gc) reads as gc = shard_count."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 1_000_000, shard_count=1)
        assert self.GC not in await _item(repo, "e1", 0)

        _count, _grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, debits))
        assert await shard_balances(repo, "e1", "rpd", 1) == [500_000]

    async def test_a_donor_regranted_since_the_read_is_not_debited(self, limiter):
        """R3: the donor's grant count moved, so the planned debit must fail."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 1_000_000, grant_count=1, shard_count=1)
        _count, _grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        client = await repo._get_client()
        await client.update_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "e1", RESOURCE, 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET #gc = :two",
            ExpressionAttributeNames={"#gc": self.GC},
            ExpressionAttributeValues={":two": {"N": "2"}},
        )
        with pytest.raises(ClientError) as raised:
            await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, debits))
        assert raised.value.response["Error"]["Code"] == "ConditionalCheckFailedException"
        assert await shard_balances(repo, "e1", "rpd", 1) == [1_000_000]

    async def test_a_debit_larger_than_the_balance_is_refused(self, limiter):
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 100_000, grant_count=1, shard_count=1)
        debit = QuotaDonorDebit(0, "rpd", 200_000, 1, None, None)
        with pytest.raises(ClientError):
            await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, [debit]))
        assert await shard_balances(repo, "e1", "rpd", 1) == [100_000]

    async def test_two_quotas_same_donor_merge_into_one_update(self, limiter):
        """Review focus 3: a transaction may touch an item once."""
        items = limiter._repository.build_quota_donor_debits(
            "e1",
            RESOURCE,
            [
                QuotaDonorDebit(0, "rpd", 500_000, 1, None, None),
                QuotaDonorDebit(0, "rpm2", 50_000, 1, None, None),
            ],
        )
        assert len(items) == 1
        expr = items[0]["Update"]["UpdateExpression"]
        assert expr.count("#qt") == 2

    async def test_debits_on_two_donors_are_two_updates_in_shard_order(self, limiter):
        repo = limiter._repository
        items = repo.build_quota_donor_debits(
            "e1",
            RESOURCE,
            [
                QuotaDonorDebit(2, "rpd", 1, 4, None, None),
                QuotaDonorDebit(0, "rpd", 1, 4, None, None),
            ],
        )
        assert [item["Update"]["Key"]["PK"]["S"] for item in items] == [
            schema.pk_bucket(repo._namespace_id, "e1", RESOURCE, shard) for shard in (0, 2)
        ]
