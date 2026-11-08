"""The fill-instant module against an exact strict token bucket (#817, Refs #808).

Every function in ``zae_limiter.fill_instant`` is checked against a reference
bucket written the way ``docs/contributing/fill-instant-proofs.md`` §1 defines
it, in ``fractions.Fraction`` and in *balance* form (``b(t) = min(C, b(t0) +
r × (t − t0))``), so the reference shares no formula with the module it checks.
Seeded random sequences rather than ``hypothesis``, as in
``test_quota_conservation_fuzz.py``.
"""

from __future__ import annotations

import ast
import heapq
import random
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from fractions import Fraction
from functools import partial
from pathlib import Path

import pytest

from zae_limiter import fill_instant as fi
from zae_limiter.fill_instant import ShardParams, Side
from zae_limiter.schedule import (
    MAX_PERIOD_SECONDS,
    MAX_SCALE,
    MAX_STORED_MILLI,
    MAX_TOKENS,
    ScheduleEntry,
)

MODULE = Path(fi.__file__)
SRC = MODULE.parent.parent
T0 = int(datetime(2026, 11, 7, 12, 0, tzinfo=UTC).timestamp() * 1000)


class Strict:
    """The strict token bucket of proofs §1, exact, in balance form.

    ``capacity`` and ``rate`` are one shard's share (millitokens, millitokens per
    ms). Admissions and debits must arrive in non-decreasing time.
    """

    def __init__(self, capacity: Fraction, rate: Fraction, balance: Fraction, at: int) -> None:
        self.capacity, self.rate, self.b, self.at = capacity, rate, balance, at

    def balance(self, t: int) -> Fraction:
        assert t >= self.at, "the reference bucket runs forward only"
        if self.b >= self.capacity:
            return self.b if t == self.at else self.capacity
        return min(self.capacity, self.b + self.rate * (t - self.at))

    def _advance(self, t: int) -> None:
        self.b, self.at = self.balance(t), t

    def admit(self, t: int, c: int) -> bool:
        self._advance(t)
        if self.b >= c:
            self.b -= c
            return True
        return False

    def debit(self, t: int, c: int) -> None:
        self._advance(t)
        self.b -= c

    def give_back(self, t: int, c: int) -> None:
        self._advance(t)
        self.b = min(self.capacity, self.b + c)

    def reset(self, t: int) -> None:
        self._advance(t)
        self.b = self.capacity

    def reparam(self, t: int, capacity: Fraction, rate: Fraction) -> None:
        self._advance(t)
        self.capacity, self.rate, self.b = capacity, rate, min(self.b, capacity)


def share(p: ShardParams) -> Fraction:
    return Fraction(p.capacity_milli, p.shard_count)


def rate(p: ShardParams) -> Fraction:
    return Fraction(p.refill_amount_milli, p.shard_count * p.refill_period_ms)


def exact_balance(z: int, t: int, p: ShardParams) -> Fraction:
    return Fraction(fi.balance_units(z, t, p), p.units_per_milli)


def random_params(rng: random.Random, shard_count: int | None = None) -> ShardParams:
    return ShardParams(
        capacity_milli=rng.randint(1, 2_000) * 1000,
        refill_amount_milli=rng.randint(1, 2_000) * 1000 + rng.randint(0, 999),
        refill_period_ms=rng.choice([1_000, 60_000, 3_600_000, 14_400_000, 86_400_000]),
        shard_count=shard_count if shard_count is not None else rng.choice([1, 2, 4, 8, 16, 32]),
    )


def z_with_balance(t: int, b_units: int, p: ShardParams) -> int:
    """A fill instant whose balance at ``t`` is ``b_units`` (in the module's units)."""
    return fi.stamp(t, p) + p.capacity_units - b_units


def side_of(write: fi.Write | None) -> Side:
    assert write is not None
    return write.side


def admit_until_decided(
    z: int, t: int, c: int, p: ShardParams, write: fi.Write | None
) -> tuple[int, bool, int]:
    """Send ``write``, then retries from each failure's image; nothing lands in between."""
    writes = 0
    while write is not None:
        writes += 1
        if write.applies(z):
            return write.apply(z), True, writes
        write = fi.plan_retry(t, c, p, z)
    return z, False, writes


# ---------------------------------------------------------------------------
# What the module may depend on, and who may depend on it
# ---------------------------------------------------------------------------


class TestModuleBoundary:
    def test_imports_only_the_standard_library_and_schedule(self) -> None:
        tree = ast.parse(MODULE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.module == "schedule", f"relative import of {node.module}"
                elif node.module == "zae_limiter.schedule":
                    continue
                else:
                    assert node.module is not None
                    assert node.module.split(".")[0] in sys.stdlib_module_names, node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] in sys.stdlib_module_names, alias.name

    def test_no_float_or_fraction_arithmetic(self) -> None:
        tree = ast.parse(MODULE.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.BinOp, ast.AugAssign)):
                assert not isinstance(node.op, ast.Div), f"true division at line {node.lineno}"
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), f"float literal at line {node.lineno}"
            if isinstance(node, ast.Name):
                assert node.id not in {"float", "Fraction", "Decimal"}, node.id

    def test_nothing_else_in_src_imports_it(self) -> None:
        users = [
            path
            for path in SRC.rglob("*.py")
            if path != MODULE and "fill_instant" in path.read_text()
        ]
        assert users == []


# ---------------------------------------------------------------------------
# The two writes (Lemma 1) and the side planner
# ---------------------------------------------------------------------------


class TestTwoWrites:
    @pytest.mark.parametrize("seed", range(6))
    def test_exactly_one_write_applies_on_every_admissible_state(self, seed: int) -> None:
        rng = random.Random(seed)
        for _ in range(400):
            p = random_params(rng)
            t = T0 + rng.randint(-(10**7), 10**7)
            c = rng.randint(0, p.capacity_milli // p.shard_count)
            b = rng.randint(-2 * p.capacity_units, p.capacity_units)
            z = z_with_balance(t, b, p)
            if rng.random() < 0.3:  # a full bucket whose fill instant is long past
                z = fi.stamp(t, p) - rng.randint(1, 10**9)
            nf, full = fi.not_full_write(t, c, p), fi.full_write(t, c, p)
            reference = Strict(share(p), rate(p), exact_balance(z, t, p), t)
            admissible = reference.admit(t, c)
            assert [nf.applies(z), full.applies(z)].count(True) == (1 if admissible else 0)
            if admissible:
                written = (nf if nf.applies(z) else full).apply(z)
                assert exact_balance(written, t, p) == reference.balance(t)

    def test_the_planner_never_sends_the_full_write_for_an_oversized_hard_request(self) -> None:
        rng = random.Random(7)
        for _ in range(500):
            p = random_params(rng)
            t = T0
            c = p.capacity_milli // p.shard_count + rng.randint(1, 10_000)
            if not fi.exceeds_share(c, p):
                continue
            z = fi.stamp(t, p) - rng.randint(0, 10**9)  # full: the full write would apply
            for image in (None, z, z + 10**12):
                for u in (None, 0, 900):
                    assert fi.plan_first(t, c, p, image=image, utilisation_permille=u) is None
            assert fi.plan_retry(t, c, p, z) is None
            soft = fi.plan_first(t, c, p, soft=True)
            assert soft is not None and soft.applies(z)

    def test_the_planner_trusts_a_not_full_image_and_weighs_a_full_one(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 1)
        t, c = T0, 1_000
        not_full = fi.stamp(t, p) + 5
        full = fi.stamp(t, p) - 5
        assert side_of(fi.plan_first(t, c, p, image=not_full)) is Side.NOT_FULL
        assert side_of(fi.plan_first(t, c, p, image=full)) is Side.FULL
        assert side_of(fi.plan_first(t, c, p)) is Side.FULL
        assert (
            side_of(fi.plan_first(t, c, p, image=full, utilisation_permille=700)) is Side.NOT_FULL
        )
        assert (
            side_of(fi.plan_first(t, c, p, utilisation_permille=700, full_first=True)) is Side.FULL
        )
        assert side_of(fi.plan_first(t, c, p, image=full, utilisation_permille=500)) is Side.FULL

    def test_a_retry_after_a_failure_past_the_slack_rejects(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 1)
        t, c = T0, 1_000
        past = fi.not_full_write(t, c, p).upper
        assert past is not None
        assert fi.plan_retry(t, c, p, past + 1) is None
        assert side_of(fi.plan_retry(t, c, p, past)) is Side.NOT_FULL


# ---------------------------------------------------------------------------
# Exact sequences: writes, debits, returns, top-ups, resets against the reference
# ---------------------------------------------------------------------------


class TestExactSequences:
    @pytest.mark.parametrize("seed", range(8))
    def test_one_consumer_matches_the_strict_bucket_exactly(self, seed: int) -> None:
        rng = random.Random(100 + seed)
        p = random_params(rng)
        t = T0
        z = fi.created_full(t, p)
        reference = Strict(share(p), rate(p), share(p), t)
        for _ in range(300):
            t += rng.choice(
                [0, 0, 1, 37, 1_000, 60_000, p.refill_period_ms, 10 * p.refill_period_ms]
            )
            amount = rng.randint(0, max(1, p.capacity_milli // p.shard_count))
            op = rng.random()
            if op < 0.55:
                image = rng.choice([None, z, z - rng.randint(0, 10**8)])
                write = fi.plan_first(
                    t, amount, p, image=image, utilisation_permille=rng.choice([None, 200, 800])
                )
                z, admitted, _ = admit_until_decided(z, t, amount, p, write)
                assert admitted == reference.admit(t, amount)
            elif op < 0.7:
                write = fi.debit_write(t, amount, p, image=rng.choice([None, z]))
                z = (write if write.applies(z) else fi.debit_write(t, amount, p, image=z)).apply(z)
                reference.debit(t, amount)
            elif op < 0.88:
                z = fi.returned(z, amount, p)
                reference.give_back(t, amount)
            elif op < 0.96:
                z = fi.topped_up(z, amount, p)
                reference.give_back(t, amount)
            else:
                z = fi.reset(t, p)
                reference.reset(t)
            assert exact_balance(z, t, p) == reference.balance(t)
            probe = t + rng.randint(0, 3 * p.refill_period_ms)
            assert exact_balance(z, probe, p) == reference.balance(probe)

    def test_a_reset_forgives_debt_and_a_return_caps_at_the_share(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 4)
        full = fi.created_full(T0, p)
        # Created at T0, the fill instant is T0 itself: the not-full side, which
        # records the same `max(z, t) + d` the full side would.
        assert not fi.full_write(T0, 900_000, p).applies(full)
        z = fi.debit_write(T0, 900_000, p, image=full).apply(full)
        assert exact_balance(z, T0, p) == share(p) - 900_000
        assert exact_balance(z, T0, p) < 0
        assert exact_balance(fi.reset(T0, p), T0, p) == share(p)
        assert exact_balance(fi.returned(fi.created_full(T0, p), 50_000, p), T0, p) == share(p)


class TestConversion:
    @pytest.mark.parametrize("seed", range(4))
    def test_a_legacy_shard_keeps_its_balance(self, seed: int) -> None:
        rng = random.Random(200 + seed)
        for _ in range(300):
            p = random_params(rng)
            rf = T0 + rng.randint(-(10**8), 0)
            tk = rng.randint(-3 * p.capacity_milli, 2 * p.capacity_milli)
            z = fi.from_tokens(tk, rf, p)
            reference = Strict(share(p), rate(p), min(share(p), Fraction(tk)), rf)
            for later in (rf, rf + 1, rf + rng.randint(0, 4 * p.refill_period_ms)):
                assert exact_balance(z, later, p) == reference.balance(later)


class TestSplit:
    @pytest.mark.parametrize("shard_count", [1, 2, 4, 8, 16])
    def test_a_split_conserves_the_total_and_respects_the_new_share(self, shard_count: int) -> None:
        rng = random.Random(300 + shard_count)
        for _ in range(300):
            p = random_params(rng, shard_count)
            t = T0 + rng.randint(0, 10**6)
            z = z_with_balance(t, rng.randint(-p.capacity_units, p.capacity_units), p)
            if rng.random() < 0.3:
                z = fi.stamp(t, p) - rng.randint(1, 10**9)
            new, kept, moved = fi.split(z, t, p)
            assert new.shard_count == 2 * shard_count
            before = exact_balance(z, t, p)
            after_kept, after_moved = exact_balance(kept, t, new), exact_balance(moved, t, new)
            assert after_kept + after_moved == before
            assert after_kept <= share(new) and after_moved <= share(new)
            assert after_moved >= 0
            # Both then refill at the new share's rate; together, the old rate.
            later = t + rng.randint(1, p.refill_period_ms)
            assert rate(new) * 2 == rate(p)
            assert exact_balance(kept, later, new) == min(
                share(new), after_kept + rate(new) * (later - t)
            )

    def test_shards_created_empty_or_full(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 4)
        assert exact_balance(fi.created_empty(T0, p), T0, p) == 0
        assert exact_balance(fi.created_full(T0, p), T0, p) == share(p)


# ---------------------------------------------------------------------------
# Re-expression, and the piecewise walk across schedule boundaries
# ---------------------------------------------------------------------------


class TestReExpression:
    @pytest.mark.parametrize("seed", range(6))
    def test_keeps_or_trims_the_balance_and_rounds_toward_less(self, seed: int) -> None:
        rng = random.Random(400 + seed)
        for _ in range(400):
            old = random_params(rng)
            new = random_params(rng)
            tau = T0 + rng.randint(0, 10**7)
            z = z_with_balance(tau, rng.randint(-2 * old.capacity_units, old.capacity_units), old)
            if rng.random() < 0.3:
                z = fi.stamp(tau, old) - rng.randint(1, 10**9)
            expected = min(exact_balance(z, tau, old), share(new))
            re_expressed = fi.re_express(z, tau, old, new)
            got = exact_balance(re_expressed, tau, new)
            assert got <= expected
            assert expected - got < Fraction(1, new.units_per_milli)
            # A balance above the share would read as full anyway; trimming it keeps
            # the stored value from drifting before the instant (ADR-154 headroom).
            assert re_expressed >= fi.stamp(tau, new)

    def test_unchanged_parameters_change_no_balance(self) -> None:
        rng = random.Random(9)
        for _ in range(200):
            p = random_params(rng)
            tau = T0
            z = z_with_balance(tau, rng.randint(-p.capacity_units, p.capacity_units), p)
            again = fi.re_express(z, tau, p, p)
            for later in (tau, tau + rng.randint(1, 3 * p.refill_period_ms)):
                assert exact_balance(again, later, p) == exact_balance(z, later, p)

    def test_a_rate_raising_boundary_admits_no_more_than_the_reference(self) -> None:
        """1000/min, scaled 0.1x from 00:00 to 05:59 UTC; drained at 05:59:00.

        At 06:00:01 the strict bucket with time-varying parameters holds 100 +
        1000/60 = 116.67 tokens. Today's refill, which credits the whole interval
        at the rate in force at its end, admitted 1000.
        """
        base = (1_000_000, 1_000_000, 60_000)
        sched = (ScheduleEntry(cron="* 0-5 * * *", scale=0.1),)
        drained = int(datetime(2026, 11, 7, 5, 59, 0, tzinfo=UTC).timestamp() * 1000)
        now = int(datetime(2026, 11, 7, 6, 0, 1, tzinfo=UTC).timestamp() * 1000)
        night = fi.params_at(drained, *base, sched, 1)
        assert night.capacity_milli == 100_000
        z = fi.created_empty(drained, night)

        z, expressed = fi.re_express_across(z, drained, now, *base, sched, 1)
        boundary = int(datetime(2026, 11, 7, 6, 0, 0, tzinfo=UTC).timestamp() * 1000)
        assert expressed == boundary
        day = fi.params_at(now, *base, sched, 1)

        reference = Strict(share(night), rate(night), Fraction(0), drained)
        reference.reparam(boundary, share(day), rate(day))
        assert reference.balance(now) == Fraction(100_000) + Fraction(1_000_000, 60)

        admitted = 0
        while True:
            write = fi.plan_retry(now, 1_000, day, z)
            if write is None or not write.applies(z):
                break
            z = write.apply(z)
            admitted += 1
        assert admitted == 116
        assert admitted * 1000 <= reference.balance(now)

    def test_no_boundary_leaves_the_fill_instant_alone(self) -> None:
        base = (1_000_000, 1_000_000, 60_000)
        p = fi.params_at(T0, *base, (), 1)
        z = fi.created_empty(T0, p)
        assert fi.re_express_across(z, T0, T0 + 10**7, *base, (), 1) == (z, T0)


# ---------------------------------------------------------------------------
# Theorem 1: many consumers, clock offsets, the sliding-window bound
# ---------------------------------------------------------------------------


class _Reference:
    """Proofs Theorem 1's ``Y``: every applied write at the instant it lands, unchecked."""

    def __init__(self, capacity: Fraction, r: Fraction, at: int) -> None:
        self.bucket = Strict(capacity, r, capacity, at)

    def grant_of(self, apply_change: Callable[[int], None], t: int) -> Fraction:
        before = self.bucket.balance(t)
        apply_change(t)
        return self.bucket.balance(t) - before


@pytest.mark.parametrize("seed", range(4))
def test_many_consumers_never_exceed_the_sliding_window_bound(seed: int) -> None:
    rng = random.Random(500 + seed)
    p = ShardParams(100_000, 100_000, 60_000, 1)  # 100 tokens, 100 per minute
    r = rate(p)
    offsets = [rng.randint(-2_000, 2_000) for _ in range(4)]
    z = fi.created_full(T0, p)
    reference = _Reference(share(p), r, T0)
    events: list[
        tuple[int, int, str, int, int, int]
    ] = []  # (lands, seq, kind, consumer, amount, stamp)
    seq = 0
    clock = T0
    for _ in range(260):
        clock += rng.choice([0, 1, 5, 40, 400, 3_000, 30_000, 90_000])
        consumer = rng.randrange(len(offsets))
        kind = rng.choices(["admit", "debit", "return", "topup", "reset"], [70, 10, 12, 5, 3])[0]
        amount = rng.randint(1, 40) * 1000
        lands = clock + rng.randint(0, 300)
        heapq.heappush(events, (lands, seq, kind, consumer, amount, clock + offsets[consumer]))
        seq += 1

    applied: list[tuple[int, str, Fraction]] = []  # (lands, kind, tokens) in landing order
    gap = 0
    attempts: dict[int, int] = {}
    while events:
        lands, n, kind, consumer, amount, stamp = heapq.heappop(events)
        if kind in ("admit", "debit"):
            if kind == "debit" and attempts.get(n):
                stamp = lands + offsets[consumer] - rng.randint(0, 300)  # a fresh reading
            attempts[n] = attempts.get(n, 0) + 1
            if kind == "admit":
                write = (
                    fi.plan_first(
                        stamp, amount, p, image=None, utilisation_permille=rng.choice([None, 800])
                    )
                    if attempts[n] == 1
                    else fi.plan_retry(stamp, amount, p, z)
                )
                if write is None or attempts[n] > 8:
                    continue  # rejected, or answered as contended
            else:
                write = fi.debit_write(stamp, amount, p, image=z if attempts[n] > 1 else None)
            if not write.applies(z):
                heapq.heappush(
                    events, (lands + rng.randint(1, 300), n, kind, consumer, amount, stamp)
                )
                continue
            z = write.apply(z)
            gap = max(gap, abs(stamp - lands))
            reference.bucket.debit(lands, amount)
            applied.append((lands, kind, Fraction(amount)))
        elif kind == "return":
            z = fi.returned(z, amount, p)
            reference.bucket.give_back(lands, amount)
            applied.append((lands, "return", Fraction(amount)))
        elif kind == "topup":
            z = fi.topped_up(z, amount, p)
            grant = reference.grant_of(partial(reference.bucket.give_back, c=amount), lands)
            applied.append((lands, "grant", grant))
        else:
            z = fi.reset(stamp, p)
            gap = max(gap, abs(stamp - lands))
            grant = reference.grant_of(reference.bucket.reset, lands)
            applied.append((lands, "grant", grant))

    for end, (t_end, kind_end, _) in enumerate(applied):
        if kind_end != "admit":
            continue
        net = Fraction(0)
        for start in range(end, -1, -1):
            t_start, kind, tokens = applied[start]
            if kind in ("admit", "debit"):
                net += tokens
            else:
                net -= tokens  # a return, or a grant raising the bound by what it granted
            assert net <= share(p) + r * (t_end - t_start) + 2 * r * gap


# ---------------------------------------------------------------------------
# Theorem 4: decided within L + 2 writes (L + 1 full-first) while others only admit
# ---------------------------------------------------------------------------


def _decide(rng: random.Random, limits: int, full_first: bool, interleave: bool) -> int:
    params = [random_params(rng, 1) for _ in range(limits)]
    t = T0
    zs = [
        z_with_balance(t, rng.randint(-p.capacity_units // 4, p.capacity_units), p)
        if rng.random() < 0.6
        else fi.stamp(t, p) - rng.randint(1, 10**6)
        for p in params
    ]
    costs = [rng.randint(0, p.capacity_milli // 3) for p in params]
    images = [z - rng.randint(0, 10**9) if rng.random() < 0.5 else None for z in zs]
    writes = [
        fi.plan_first(
            t,
            c,
            p,
            image=img,
            utilisation_permille=rng.choice([None, 300, 700]),
            full_first=full_first,
        )
        for c, p, img in zip(costs, params, images, strict=True)
    ]
    sent = 0
    while True:
        if interleave:
            for _ in range(rng.randint(0, 3)):  # another consumer's admission lands first
                other_t = t + rng.randint(-5_000, 5_000)
                other = [
                    fi.plan_retry(other_t, rng.randint(0, p.capacity_milli // 5), p, z)
                    for p, z in zip(params, zs, strict=True)
                ]
                if all(w is not None and w.applies(z) for w, z in zip(other, zs, strict=True)):
                    zs = [w.apply(z) for w, z in zip(other, zs, strict=True)]  # type: ignore[union-attr]
        sent += 1
        assert all(w is not None for w in writes)
        if all(w.applies(z) for w, z in zip(writes, zs, strict=True)):  # type: ignore[union-attr]
            return sent
        retries = [fi.plan_retry(t, c, p, z) for c, p, z in zip(costs, params, zs, strict=True)]
        if any(w is None for w in retries):
            return sent  # rejected: some limit is past its slack
        writes = retries


@pytest.mark.parametrize("limits", [1, 2, 3, 4, 5, 6])
def test_a_request_is_decided_within_l_plus_two_writes(limits: int) -> None:
    rng = random.Random(600 + limits)
    worst = {"predict": 0, "full_first": 0, "alone": 0}
    for _ in range(400):
        worst["predict"] = max(worst["predict"], _decide(rng, limits, False, True))
        worst["full_first"] = max(worst["full_first"], _decide(rng, limits, True, True))
        worst["alone"] = max(worst["alone"], _decide(rng, limits, False, False))
    assert worst["predict"] <= limits + 2
    assert worst["full_first"] <= limits + 1
    assert worst["alone"] <= 2


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


class TestBounds:
    def test_an_amount_above_max_tokens_is_refused(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 1)
        too_much = MAX_TOKENS * 1000 + 1
        for call in (
            lambda: fi.plan_first(T0, too_much, p, soft=True),
            lambda: fi.debit_write(T0, too_much, p),
            lambda: fi.returned(fi.created_full(T0, p), too_much, p),
            lambda: fi.topped_up(fi.created_full(T0, p), too_much, p),
        ):
            with pytest.raises(ValueError, match="MAX_TOKENS"):
                call()
        with pytest.raises(ValueError, match="non-negative"):
            fi.span(-1, p)

    def test_a_2100_stamp_at_the_570_ceilings_fits_dynamodb(self) -> None:
        ra = MAX_TOKENS * 1000 * MAX_SCALE  # the largest refill amount in force, 1e24
        rp = MAX_PERIOD_SECONDS * 1000
        p = ShardParams(ra, ra, rp, 32)
        year_2100 = int(datetime(2100, 1, 1, tzinfo=UTC).timestamp() * 1000)
        stamped = fi.stamp(year_2100, p)
        assert 4.0e36 < stamped < 4.2e36 <= MAX_STORED_MILLI
        assert fi.created_empty(year_2100, p) <= MAX_STORED_MILLI
        assert (
            fi.not_full_write(year_2100, MAX_TOKENS * 1000, ShardParams(ra, ra, rp, 1)).upper
            is not None
        )

    def test_a_value_past_dynamodb_s_exact_range_is_refused(self) -> None:
        ra = MAX_TOKENS * 1000 * MAX_SCALE
        p = ShardParams(ra, ra, 60_000, 1)
        with pytest.raises(ValueError, match="exact range"):
            fi.stamp(MAX_STORED_MILLI // ra + 1, p)

    def test_a_quota_has_no_fill_instant(self) -> None:
        with pytest.raises(ValueError, match="dripping"):
            ShardParams(100_000, 0, 60_000, 1)

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            ((0, 100_000, 60_000, 1), "capacity"),
            ((100_000, 100_000, 0, 1), "refill period"),
            ((100_000, 100_000, 60_000, 0), "shard count"),
        ],
    )
    def test_shard_params_are_validated(
        self, args: tuple[int, int, int, int], message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            ShardParams(*args)

    def test_a_write_refuses_a_fill_instant_its_condition_rejects(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 1)
        full = fi.full_write(T0, 1_000, p)
        with pytest.raises(ValueError, match="does not apply"):
            full.apply(fi.stamp(T0, p))
        with pytest.raises(ValueError, match="utilisation"):
            fi.plan_first(T0, 1_000, p, utilisation_permille=-1)

    def test_the_millitoken_balance_rounds_down(self) -> None:
        p = ShardParams(100_000, 100_000, 60_000, 3)  # a share of 33,333.3 millitokens
        z = fi.created_full(T0, p)
        assert exact_balance(z, T0, p) == Fraction(100_000, 3)
        assert fi.balance_milli(z, T0, p) == 33_333
