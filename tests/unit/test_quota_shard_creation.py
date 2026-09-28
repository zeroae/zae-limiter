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
    walk_doublings_no_spend,
)
from tests.fixtures.windows import T0
from zae_limiter import Limit, OnUnavailable, RateLimiter, schema
from zae_limiter.exceptions import RateLimitExceeded
from zae_limiter.models import (
    BucketState,
    QuotaDonorDebit,
    QuotaGrant,
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
        """The invariant: a doubling changes what is spendable only by what it admitted."""
        repo = limiter._repository
        freeze(repo)
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        await seed_shard0(limiter, "quota-conserve", limit, repo._now_ms())

        _admitted, _count, spends = await walk_doublings(limiter, "quota-conserve", "rpd")
        for index, (before, after, generation) in enumerate(spends):
            assert after + generation == before, f"doubling {index}: {spends}"


class TestFromLimitStartingBalance:
    """``BucketState.from_limit`` takes a move's amount, and nothing else changed."""

    def test_quota_shard_created_from_a_move(self):
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        state = BucketState.from_limit("e1", "gpt-4", limit, 0, 4, starting_tokens_milli=120_000)
        assert state.tokens_milli == 120_000
        assert state.capacity_milli == 1_000_000  # stored base stays undivided
        assert state.reset_target_milli(0) == 250_000
        assert state.grant_count == 4, "a created quota records the count it was sized at"

    def test_quota_shard_with_no_grant_starts_full(self):
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        state = BucketState.from_limit("e1", "gpt-4", limit, 0, 4)
        assert state.tokens_milli == 250_000
        assert state.grant_count == 4

    def test_a_dripping_limit_records_no_grant(self):
        """Pins that the dripping path is untouched: no ``gc``, full share."""
        limit = Limit.per_minute("rpm", 1000)
        state = BucketState.from_limit("e1", "gpt-4", limit, 0, 4)
        assert state.tokens_milli == 250_000
        assert state.grant_count is None


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

    async def test_a_full_quota_moves_half_and_conserves(self, limiter):
        """The case #587 hid behind: the new share is moved off shard 0, whose
        grant covers slot 1, so the entity-wide spendable total is the same
        either side of the doubling (ADR-145)."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-full", limit)

        assert await spendable(repo, "q-full", "rpd", 2) == 1000
        assert await materialise(limiter, "q-full", "rpd", 1) == 1
        # 1000 conserved, less the single token the creating acquire consumed.
        assert await spendable(repo, "q-full", "rpd", 2) == 999
        assert await shard_balances(repo, "q-full", "rpd", 2) == [500_000, 499_000]

    async def test_a_spent_quota_moves_only_what_is_left(self, limiter):
        """#587 itself: 998 of 1000 already spent, and the doubling must not
        hand back 499 of them. The 2 left move to the new shard, which admits
        one of them."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-spent", limit)
        await drain_shard(repo, "q-spent", "rpd", 0)

        assert await spendable(repo, "q-spent", "rpd", 2) == 2
        assert await materialise(limiter, "q-spent", "rpd", 1) == 1
        assert await shard_balances(repo, "q-spent", "rpd", 2) == [0, 1_000]
        assert await spendable(repo, "q-spent", "rpd", 2) == 1

    async def test_a_partly_spent_quota_moves_one_share(self, limiter):
        """Shard 0 holds 700: one new share of 500 moves, 200 stays."""
        limit = Limit.quota("rpd", 1000, cron=QUOTA_CRON)
        repo = await self._seed(limiter, "q-part", limit)
        await drain_shard(repo, "q-part", "rpd", 0, floor=700)

        assert await spendable(repo, "q-part", "rpd", 2) == 700
        assert await materialise(limiter, "q-part", "rpd", 1) == 1
        assert await shard_balances(repo, "q-part", "rpd", 2) == [200_000, 499_000]
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
            raised = await repo._freeze_and_raise_shard_counts(
                "e1", RESOURCE, [(0, [("rpd", 2)])], 4
            )
            assert raised == 0

    async def test_any_other_error_raising_a_sibling_propagates(self, limiter):
        repo = limiter._repository
        client = await repo._get_client()
        error = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}},
            "UpdateItem",
        )
        with patch.object(client, "update_item", side_effect=error):
            with pytest.raises(ClientError):
                await repo._freeze_and_raise_shard_counts("e1", RESOURCE, [(0, [("rpd", 2)])], 4)

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
        # Written without `gc` (legacy shape), so the debit matches its stored count.
        assert debits == [
            QuotaDonorDebit(0, "session", 500_000, 1, None, now - 1000, legacy_shard_count=1)
        ]

    async def test_a_legacy_sibling_holding_more_than_a_share_covers_by_its_balance(self, limiter):
        """R7: a v0.14 item (no gc) at count 2 holding 700 was granted at count
        1 — no share at count 2 is that large — so it covers slot 1 and funds
        it by a move. Read at its stored count it would cover only slot 0 and
        the seed would mint a fresh 500 beside its 700 (1500 measured)."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 700_000, shard_count=2)
        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        assert grants["rpd"] == QuotaGrant(donor_shard=0, tokens_milli=500_000, donor_grant_count=1)
        assert debits == [QuotaDonorDebit(0, "rpd", 500_000, 1, ANY, None, legacy_shard_count=2)]
        await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, debits))
        assert await shard_balances(repo, "e1", "rpd", 1) == [200_000]

    async def test_a_legacy_sibling_within_its_share_keeps_its_stored_count(self, limiter):
        """R7 only broadens: 400 fits a share at count 2, so shard 0 covers slot
        0 only and slot 1 is granted fresh, as design §9 reads it."""
        repo = limiter._repository
        freeze(repo)
        await _write_shard(repo, "e1", self.QUOTA, 0, 400_000, shard_count=2)
        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], 1, 2, repo._now_ms()
        )
        assert grants["rpd"] == QuotaGrant(donor_shard=None, tokens_milli=500_000)
        assert debits == []

    async def test_raising_a_legacy_sibling_freezes_its_inferred_grant_size(self, limiter):
        """R7 + R6: a fresh grant for another quota raises the lagging legacy
        shard 0 to count 4; the raise stamps the grant size the planner
        inferred (1), not the stored count (2), and the move planned off it in
        the same pass still lands on the `gc = :gc` branch."""
        repo = limiter._repository
        freeze(repo)
        weekly = Limit.quota("rpw", 1000, cron="0 0 * * 1")
        await _write_shard(repo, "e1", self.QUOTA, 0, 700_000, shard_count=2)
        _count, grants, debits = await repo.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA, weekly], 3, 4, repo._now_ms()
        )
        assert grants["rpd"].donor_shard == 0 and grants["rpw"].donor_shard is None
        item = await _item(repo, "e1", 0)
        assert (item["shard_count"]["N"], item[self.GC]["N"]) == ("4", "1")
        await repo.transact_write(repo.build_quota_donor_debits("e1", RESOURCE, debits))
        assert await shard_balances(repo, "e1", "rpd", 1) == [450_000]

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


def _conflict() -> ClientError:
    """A transaction cancelled by a concurrent write to one of its items."""
    error = ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
            "CancellationReasons": [{"Code": "None"}, {"Code": "TransactionConflict"}],
        },
        "TransactWriteItems",
    )
    return error


async def _set_attr(repo, entity_id, shard_id, attr, value, resource=RESOURCE):
    """Overwrite one numeric attribute on a shard item, as a concurrent writer would."""
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
            "SK": {"S": schema.sk_state()},
        },
        UpdateExpression="SET #a = :v",
        ExpressionAttributeNames={"#a": attr},
        ExpressionAttributeValues={":v": {"N": str(value)}},
    )


class TestAcceptance:
    """Design §10 (ADR-145). C = 1000, clock frozen in one period."""

    QUOTA = Limit.quota("rpd", 1000, cron=QUOTA_CRON)

    async def _full_shard0_at_two(self, limiter, entity_id):
        """Shard 0 granted the whole quota at count 1, then a doubling to 2."""
        repo = limiter._repository
        freeze(repo)
        await seed_shard0(limiter, entity_id, self.QUOTA, repo._now_ms())
        assert await repo.bump_shard_count(entity_id, RESOURCE, 1) == 2
        return repo

    async def test_1_nobody_spends_walk_keeps_the_whole_quota(self, limiter):
        """#637: a full, unspent quota walked 1 -> 32 kept 187 spendable."""
        freeze(limiter._repository)
        await limiter.set_limits("e1", [self.QUOTA], resource=RESOURCE)
        count = await walk_doublings_no_spend(limiter, "e1", "rpd")
        assert count == 32
        assert await spendable(limiter._repository, "e1", "rpd", count, RESOURCE) == 1000

    async def test_4_587_walk_with_spending_never_exceeds(self, limiter):
        freeze(limiter._repository)
        await limiter.set_limits("e1", [self.QUOTA], resource=RESOURCE)
        assert await materialise(limiter, "e1", "rpd", 0, amount=0) == 1
        admitted, count, spends = await walk_doublings(limiter, "e1", "rpd")
        left = await spendable(limiter._repository, "e1", "rpd", count, RESOURCE)
        assert count == 32
        assert admitted + left <= 1000, (admitted, left, spends)

    async def test_7_a_split_conserves_the_total(self, limiter):
        repo = await self._full_shard0_at_two(limiter, "split")
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with RateLimiter(repository=repo, speculative_writes=False).acquire(
                "split", RESOURCE, {"rpd": 10}
            ):
                pass
        balances = await shard_balances(repo, "split", "rpd", 2)
        assert balances == [500_000, 490_000]
        assert sum(b for b in balances if b is not None) == 1_000_000 - 10_000

    @pytest.mark.parametrize("legacy", [False, True], ids=["gc", "no-gc"])
    @pytest.mark.parametrize("shard1_rf_after_midnight", [False, True])
    async def test_3_idle_across_the_reset_then_seed(
        self, limiter, shard1_rf_after_midnight, legacy
    ):
        """#642, the 1300 repro (rv633d): shard 0 was granted the whole quota
        at count 1 today and has spent 300; shard 1 lacks the quota and may
        have last materialised before midnight. Its seed is rejected (asks for
        600 against a move of 500), then everything is drained through both
        the fast and the slow path. Before: 1300 admitted in one period.

        ``no-gc`` writes shard 0 the way v0.14 left it (no grant count): R7
        infers the count-1 grant from its 700 held, rather than reading it as
        covering only its own slot (1500 measured before R7)."""
        rpm = Limit.per_minute("rpm", 100_000)
        repo = limiter._repository
        ns = repo._namespace_id
        eid = "pr"
        day = 86_400_000
        midnight = (T0 // day + 1) * day
        repo._now_ms = lambda: T0
        await repo.set_resource_defaults(RESOURCE, [rpm])
        for shard in (0, 1):
            states = [BucketState.from_limit(eid, RESOURCE, rpm, T0, shard_count=2)]
            if shard == 0:
                rpd = BucketState.from_limit(eid, RESOURCE, self.QUOTA, T0, shard_count=2)
                rpd.tokens_milli, rpd.total_consumed_milli = 700_000, 300_000
                rpd.grant_count = None if legacy else 1
                states.append(rpd)
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        eid, RESOURCE, states, T0, shard_id=shard, shard_count=2
                    )
                ]
            )
        for shard in (0, 1) if shard1_rf_after_midnight else (0,):
            await _set_attr(repo, eid, shard, "rf", midnight + 1_000)
        repo._entity_cache[(ns, eid)] = (False, None, {RESOURCE: 2})
        await repo.set_resource_defaults(RESOURCE, [rpm, self.QUOTA])
        await repo.invalidate_config_cache()
        slow = RateLimiter(repository=repo, speculative_writes=False)
        fast = RateLimiter(repository=repo)

        admitted = 300  # shard 0's spend today
        repo._now_ms = lambda: midnight + 2_000
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            with pytest.raises(RateLimitExceeded):
                async with slow.acquire(eid, RESOURCE, consume={"rpd": 600}):
                    pass
            try:
                async with fast.acquire(eid, RESOURCE, consume={"rpd": 200}):
                    admitted += 200
            except RateLimitExceeded:
                pass

        repo._now_ms = lambda: midnight + 3_000
        for shard in (1, 0, 1, 0):
            for lim in (fast, slow):
                while True:
                    try:
                        with patch("zae_limiter.repository.random.randrange", return_value=shard):
                            async with lim.acquire(eid, RESOURCE, consume={"rpd": 1}):
                                admitted += 1
                    except RateLimitExceeded:
                        break
        assert admitted == 1000

    async def test_5_seed_racing_a_doubling_never_exceeds(self, limiter):
        """R3 of #633 (rv633c/test_race.py): an unsharded seed of the quota on
        shard 0 reads count 1; an aggregator doubling lands between its read
        and its write, and Path 2 clones the pre-seed image as shard 1. The
        seed's pin fails and it grants nothing; every later pass seeds at the
        doubled count. Before the pin: 1500 against 1000."""
        rpm = Limit.per_minute("rpm", 100_000)
        repo = limiter._repository
        ns = repo._namespace_id
        eid = "race"
        repo._now_ms = lambda: T0
        await repo.set_resource_defaults(RESOURCE, [rpm])
        state = BucketState.from_limit(eid, RESOURCE, rpm, T0, shard_count=1)
        await repo.transact_write(
            [repo.build_composite_create(eid, RESOURCE, [state], T0, shard_id=0, shard_count=1)]
        )
        await repo.set_resource_defaults(RESOURCE, [rpm, self.QUOTA])
        await repo.invalidate_config_cache()
        repo._entity_cache[(ns, eid)] = (False, None, {RESOURCE: 1})
        slow = RateLimiter(repository=repo, speculative_writes=False)
        repo._now_ms = lambda: T0 + 1_000
        real = repo.transact_write
        raced: list[int] = []

        async def doubling_lands_first(items):
            if not raced:
                raced.append(1)
                await _set_attr(repo, eid, 0, "shard_count", 2)
                clone = BucketState.from_limit(eid, RESOURCE, rpm, T0, shard_count=2)
                await real(
                    [
                        repo.build_composite_create(
                            eid, RESOURCE, [clone], T0, shard_id=1, shard_count=2
                        )
                    ]
                )
            return await real(items)

        admitted = 0
        with patch.object(repo, "transact_write", doubling_lands_first):
            with pytest.raises(RateLimitExceeded):
                async with slow.acquire(eid, RESOURCE, consume={"rpd": 1}):
                    admitted += 1
        assert "b_rpd_tk" not in await _item(repo, eid, 0), "the pinned seed lost"

        repo._entity_cache[(ns, eid)] = (False, None, {RESOURCE: 2})
        repo._now_ms = lambda: T0 + 2_000
        fast = RateLimiter(repository=repo)
        with patch("zae_limiter.repository.random.randrange", return_value=0):
            try:
                async with fast.acquire(eid, RESOURCE, consume={"rpd": 700}):
                    admitted += 700
            except RateLimitExceeded:
                pass
        repo._now_ms = lambda: T0 + 3_000
        for shard in (1, 0, 1, 0):
            while True:
                try:
                    with patch("zae_limiter.repository.random.randrange", return_value=shard):
                        async with slow.acquire(eid, RESOURCE, consume={"rpd": 1}):
                            admitted += 1
                except RateLimitExceeded:
                    break
        assert admitted <= 1000
        assert admitted == 1000, "nothing lost either"

    async def test_rejected_acquire_keeps_the_move(self, limiter):
        """ADR-145 I5: a move is never lost. The create is committed with
        nothing consumed and the acquire is then rejected."""
        repo = await self._full_shard0_at_two(limiter, "keep")
        slow = RateLimiter(repository=repo, speculative_writes=False)
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            with pytest.raises(RateLimitExceeded):
                async with slow.acquire("keep", RESOURCE, {"rpd": 600}):
                    pass
        assert await shard_balances(repo, "keep", "rpd", 2) == [500_000, 500_000]
        shard1 = await _item(repo, "keep", 1)
        assert shard1["b_rpd_tc"]["N"] == "0"
        assert shard1["b_rpd_gc"]["N"] == "2"

    async def test_conflicted_move_retries_then_succeeds(self, limiter):
        """Review focus 1: the donor is the busiest shard, so a move's
        transaction can be cancelled by a concurrent write to it. It retries
        with jitter and lands."""
        repo = await self._full_shard0_at_two(limiter, "conflict")
        real = repo.transact_write
        calls: list[int] = []

        async def conflict_once(items):
            calls.append(len(items))
            if len(calls) == 1:
                raise _conflict()
            return await real(items)

        slow = RateLimiter(repository=repo, speculative_writes=False)
        with patch.object(repo, "transact_write", conflict_once):
            with patch("zae_limiter.repository.random.randrange", return_value=1):
                async with slow.acquire("conflict", RESOURCE, {"rpd": 1}):
                    pass
        assert calls == [2, 2], "the same move retried as-is"
        assert await shard_balances(repo, "conflict", "rpd", 2) == [500_000, 499_000]

    @pytest.mark.parametrize("mode", [OnUnavailable.BLOCK, OnUnavailable.ALLOW])
    async def test_a_move_that_keeps_conflicting_gets_zero_not_unavailable(self, limiter, mode):
        """R8, design §6 step 6: exhausted conflict retries lose the move, the
        re-plan loses it again, and the third pass grants the covered slot 0
        with no move. Never a raw `ClientError`, and never
        `RateLimiterUnavailable` — under ALLOW that would admit without limit:
        a request against the zero balance is rejected, a zero-cost one is
        admitted and creates the empty shard. The total never grows."""
        repo = await self._full_shard0_at_two(limiter, "conflict-all")
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.transact_write
        moves: list[int] = []

        async def moves_conflict(items):
            if len(items) > 1:  # only a transaction carrying a donor debit
                moves.append(1)
                raise _conflict()
            return await real(items)

        with patch.object(repo, "transact_write", moves_conflict):
            with patch("zae_limiter.lease.asyncio.sleep") as sleep:
                with patch("zae_limiter.repository.random.randrange", return_value=1):
                    with pytest.raises(RateLimitExceeded):
                        async with slow.acquire(
                            "conflict-all", RESOURCE, {"rpd": 1}, on_unavailable=mode
                        ):
                            pass
                    assert len(moves) == 2 * 4, "two attempts, each with 3 retries"
                    assert await shard_balances(repo, "conflict-all", "rpd", 2) == [
                        1_000_000,
                        None,
                    ]
                    async with slow.acquire(
                        "conflict-all", RESOURCE, {"rpd": 0}, on_unavailable=mode
                    ) as lease:
                        assert not lease.degraded
        # Full jitter: every delay is drawn in [0, base * 2**attempt].
        delays = [call.args[0] for call in sleep.call_args_list]
        assert delays and all(0 <= d <= 0.025 * 4 for d in delays)
        assert await shard_balances(repo, "conflict-all", "rpd", 2) == [1_000_000, 0]
        assert (await _item(repo, "conflict-all", 1))["b_rpd_gc"]["N"] == "2"

    async def test_lost_move_is_replanned_once(self, limiter):
        """Design §6 step 6: the donor is spent between the plan and the commit,
        the move's condition fails, the transaction rolls back whole, and the
        acquire re-plans from a fresh read and is admitted from what is left."""
        repo = await self._full_shard0_at_two(limiter, "lost")
        real = repo.transact_write
        calls: list[int] = []

        async def donor_spent_first(items):
            calls.append(len(items))
            if len(calls) == 1:
                await _set_attr(repo, "lost", 0, "b_rpd_tk", 300_000)
            return await real(items)

        slow = RateLimiter(repository=repo, speculative_writes=False)
        with patch.object(repo, "transact_write", donor_spent_first):
            with patch("zae_limiter.repository.random.randrange", return_value=1):
                async with slow.acquire("lost", RESOURCE, {"rpd": 100}):
                    pass
        assert calls == [2, 2]
        # The re-plan moved what shard 0 still held (300 < one share of 500).
        assert await shard_balances(repo, "lost", "rpd", 2) == [0, 200_000]

    async def test_cascade_child_and_parent_moves_share_one_transaction(self, limiter):
        """Review focus 2: a cascade creating a quota shard for both the child
        and the parent carries both moves in the acquire's one transaction —
        the two creates first, then both donor debits."""
        repo = limiter._repository
        freeze(repo)
        now = repo._now_ms()
        await limiter.create_entity("parent")
        await limiter.create_entity("child", parent_id="parent", cascade=True)
        await limiter.set_system_defaults([self.QUOTA])
        for eid in ("child", "parent"):
            state = BucketState.from_limit(eid, RESOURCE, self.QUOTA, now)
            vu, _reset = RateLimiter._materialisation_stamps(self.QUOTA, state, now)
            await repo.transact_write(
                [
                    repo.build_composite_create(
                        eid, RESOURCE, [state], now, shard_id=0, shard_count=1, vu=vu
                    )
                ]
            )
            assert await repo.bump_shard_count(eid, RESOURCE, 1) == 2
        real = repo.transact_write
        sent: list[list[str]] = []

        async def record(items):
            sent.append([next(iter(item)) for item in items])
            return await real(items)

        slow = RateLimiter(repository=repo, speculative_writes=False)
        with patch.object(repo, "transact_write", record):
            with patch("zae_limiter.repository.random.randrange", return_value=1):
                async with slow.acquire("child", RESOURCE, {"rpd": 1}):
                    pass
        assert sent == [["Put", "Put", "Update", "Update"]]
        for eid in ("child", "parent"):
            assert await shard_balances(repo, eid, "rpd", 2) == [500_000, 499_000]

    async def test_a_reset_pass_losing_its_pin_grants_nothing(self, limiter):
        """R5, carried from Task 5: a reset pass sized at count 1 loses its
        `shard_count <= 1` pin to a doubling that lands between its read and
        its write. It falls to the consumption-only retry, which grants
        nothing; the next pass resets at the doubled count."""
        repo = limiter._repository
        day = 86_400_000
        midnight = (T0 // day + 1) * day
        repo._now_ms = lambda: T0
        await seed_shard0(limiter, "r5", self.QUOTA, T0)
        await drain_shard(repo, "r5", "rpd", 0, floor=100)
        repo._now_ms = lambda: midnight + 1_000
        real = repo.transact_write
        raced: list[int] = []

        async def doubling_lands_first(items):
            if not raced:
                raced.append(1)
                await _set_attr(repo, "r5", 0, "shard_count", 2)
            return await real(items)

        slow = RateLimiter(repository=repo, speculative_writes=False)
        with patch.object(repo, "transact_write", doubling_lands_first):
            async with slow.acquire("r5", RESOURCE, {"rpd": 1}):
                pass
        item = await _item(repo, "r5", 0)
        assert item["b_rpd_tk"]["N"] == "99000", "the stale reset was not written"
        assert int(item["rf"]["N"]) < midnight, "nor was its rf"

        repo._entity_cache[(repo._namespace_id, "r5")] = (False, None, {RESOURCE: 2})
        with patch("zae_limiter.repository.random.randrange", return_value=0):
            async with slow.acquire("r5", RESOURCE, {"rpd": 1}):
                pass
        item = await _item(repo, "r5", 0)
        assert item["b_rpd_tk"]["N"] == "499000"
        assert item["b_rpd_gc"]["N"] == "2"
