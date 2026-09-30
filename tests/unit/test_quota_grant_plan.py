"""The ADR-145 grant decision and its reference model (Refs #637, Refs #642)."""

import random
from datetime import timedelta

from tests.fixtures.quota_model import QuotaModel
from zae_limiter import Limit
from zae_limiter.models import QuotaSibling, plan_quota_grant, quota_grant_is_current

C = 1_000_000  # 1000 tokens, milli


def sib(i, tk, gc, current=True):
    return QuotaSibling(shard_id=i, tokens_milli=tk, grant_count=gc, current=current)


class TestPlanQuotaGrant:
    def test_no_sibling_is_a_fresh_share(self):
        g = plan_quota_grant([], shard_id=0, shard_count=1, share_milli=C)
        assert (g.donor_shard, g.tokens_milli) == (None, C)

    def test_split_moves_from_the_parent(self):
        g = plan_quota_grant([sib(0, C, 1)], shard_id=1, shard_count=2, share_milli=C // 2)
        assert (g.donor_shard, g.tokens_milli, g.donor_grant_count) == (0, C // 2, 1)

    def test_parent_spent_gives_nothing(self):  # the #642 case
        g = plan_quota_grant([sib(1, 0, 2), sib(0, C // 2, 2)], 3, 4, C // 4)
        assert (g.donor_shard, g.tokens_milli) == (1, 0)

    def test_after_a_reset_an_uncovered_slot_is_fresh(self):
        g = plan_quota_grant([sib(0, C // 4, 4)], 3, 4, C // 4)
        assert (g.donor_shard, g.tokens_milli) == (None, C // 4)

    def test_stale_sibling_never_covers(self):  # the 1300 case
        g = plan_quota_grant([sib(1, C // 2, 2, current=False)], 3, 4, C // 4)
        assert g.donor_shard is None

    def test_closest_ancestor_wins(self):
        g = plan_quota_grant([sib(0, C, 1), sib(1, C // 2, 2)], 3, 4, C // 4)
        assert g.donor_shard == 1

    def test_lazy_creation_reaches_the_root(self):
        # shard 5 at count 8 before shard 1 exists: shard 0 (gc 1) covers it
        g = plan_quota_grant([sib(0, C, 1)], 5, 8, C // 8)
        assert (g.donor_shard, g.tokens_milli) == (0, C // 8)

    def test_tie_takes_lowest_shard_id(self):
        g = plan_quota_grant([sib(3, C // 4, 2), sib(1, C // 4, 2)], 5, 8, C // 8)
        assert g.donor_shard == 1

    def test_a_donor_in_debt_moves_nothing_and_is_not_charged(self):
        """A covering donor below zero (adjust debt) still covers the slot, so
        nothing is minted, and the move is floored at zero rather than
        handing the debt on as a negative grant."""
        g = plan_quota_grant([sib(0, -5_000, 1)], 1, 2, C // 2)
        assert (g.donor_shard, g.tokens_milli, g.donor_grant_count) == (0, 0, 1)


class TestQuotaGrantIsCurrent:
    def test_session_unmarked_item_falls_back_to_rf(self):
        """No ``wa`` (an item no v0.15 writer has marked): applied means ``ws <= rf``."""
        limit = Limit.quota("s", 10, reset_after=timedelta(hours=5))
        ws = 1_757_000_000_000
        assert quota_grant_is_current(limit, ws, ws, None, ws + 1_000)
        assert not quota_grant_is_current(limit, ws - 1, ws, None, ws + 1_000)

    def test_session_without_a_window_is_never_current(self):
        limit = Limit.quota("s", 10, reset_after=timedelta(hours=5))
        assert not quota_grant_is_current(limit, 1_757_000_000_000, None, None, 1_757_000_001_000)

    def test_calendar_with_no_edge_in_reach_is_current(self):
        """``0 0 29 2 *`` from mid-2027: the last edge (2024) is beyond the
        backwards scan, so no reset can be pending — the grant stands."""
        limit = Limit.quota("q", 10, cron="0 0 29 2 *")
        now = 1_811_808_000_000  # 2027-06-01T00:00Z
        assert quota_grant_is_current(limit, 0, None, None, now)

    def test_calendar_current_when_rf_at_or_after_last_edge(self):
        limit = Limit.quota("rpd", 10, cron="0 0 * * *")
        midnight = 1_757_030_400_000  # 2025-09-05T00:00Z
        assert quota_grant_is_current(limit, midnight, None, None, midnight + 5_000)
        assert not quota_grant_is_current(limit, midnight - 1, None, None, midnight + 5_000)

    def test_session_current_when_window_live_and_applied(self):
        limit = Limit.quota("s", 10, reset_after=timedelta(hours=5))
        ws = 1_757_000_000_000
        assert quota_grant_is_current(limit, ws, ws, ws, ws + 1_000)
        assert not quota_grant_is_current(limit, ws, ws, ws - 1, ws + 1_000)  # pending roll
        assert not quota_grant_is_current(limit, ws, ws, ws, ws + 5 * 3_600_000)  # ended


class TestPlannerMatchesModel:
    """The differential guard: the shipped planner and the design's oracle.

    ``QuotaModel(rule="NEW")`` implements the ADR-145 rule independently. The
    same seeded sequences of spend / double / late seed / next period drive it,
    and before every grant the planner is asked the same question on the
    model's own shards; the two must agree on the donor and the amount, and the
    model must account for exactly ``C`` after every step (I8).
    """

    CAP = 1024

    def _planned(self, model, j):
        siblings = [
            QuotaSibling(shard_id=i, tokens_milli=q.tk, grant_count=q.gc, current=model.current(i))
            for i, q in model.shards.items()
        ]
        return plan_quota_grant(siblings, j, model.S, model.share())

    def _grant(self, model, j, trace):
        """Grant slot ``j`` in the model, checking the planner agrees first."""
        plan = self._planned(model, j)
        donor = model.cover(j)
        before = model.shards[donor].tk if donor is not None else None
        model.grant(j)
        assert plan.donor_shard == donor, trace
        assert plan.tokens_milli == model.shards[j].tk, trace
        if donor is not None:
            assert plan.donor_grant_count == model.shards[donor].gc, trace
            assert model.shards[donor].tk == before - plan.tokens_milli, trace

    def test_planner_and_model_agree_on_every_grant(self):
        grants = 0
        for seed in range(300):
            rng = random.Random(seed)
            model = QuotaModel(self.CAP, rule="NEW")
            trace: list[str] = []
            for _ in range(60):
                roll = rng.random()
                if roll < 0.2:
                    model.double()
                    trace.append("double")
                elif roll < 0.27:
                    model.next_period()
                    trace.append("next")
                elif roll < 0.42:
                    missing = [j for j in range(model.S) if j not in model.shards]
                    if missing:
                        j = rng.choice(missing)
                        self._grant(model, j, (seed, trace))
                        grants += 1
                        trace.append(f"seed({j})")
                else:
                    i = rng.randrange(model.S)
                    n = rng.choice([1, 3, 10, 40, 100, 300])
                    if i not in model.shards:
                        self._grant(model, i, (seed, trace))
                        grants += 1
                    model.spend(i, n)
                    trace.append(f"spend({i},{n})")
                assert model.accounted() == self.CAP, (seed, trace)
                assert model.admitted.get(model.period, 0) <= self.CAP, (seed, trace)
        assert grants > 1_000, "the sequences must actually exercise the planner"


class TestReferenceModel:
    def test_old_rule_reproduces_637(self):
        rng = random.Random(0)
        m = QuotaModel(1024, rule="OLD")
        m.spend(0, 0)
        while m.S < 32:
            m.double()
            m.grant(rng.choice([j for j in range(m.S) if j not in m.shards]))
        for j in range(m.S):
            if j not in m.shards:
                m.grant(j)
        assert sum(q.tk for q in m.shards.values()) == 192

    def test_new_rule_conserves_on_every_order(self):
        for seed in range(50):
            rng = random.Random(seed)
            m = QuotaModel(1024, rule="NEW")
            m.spend(0, 0)
            while m.S < 32:
                m.double()
                m.grant(rng.choice([j for j in range(m.S) if j not in m.shards]))
            for j in rng.sample(range(m.S), m.S):
                if j not in m.shards:
                    m.grant(j)
            assert sum(q.tk for q in m.shards.values()) == 1024
