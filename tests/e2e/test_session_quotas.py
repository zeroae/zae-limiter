"""End-to-end tests for session quotas: ``Limit.quota(..., reset_after=...)`` (ADR-139).

Unit tests drive a frozen clock through the window arithmetic. These use real
elapsed time against LocalStack, with two-second windows, for what only a real
clock shows: a window anchored at an entity's own first use, reported the same
way by a rejection and by ``check_availability``, restored when it elapses, and
restarted by the next use rather than tiled forward on a grid.

The minimal stack has no aggregator, so everything there is the client's own
work (the ``--no-aggregator`` case): the roll, the fan-out across shards, and
the reporting. The aggregator case lives on the shared aggregator stack.

To run locally::

    zae-limiter local up
    export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \\
           AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
    uv run pytest tests/e2e/test_session_quotas.py -v
"""

import asyncio
import time
from datetime import timedelta

import pytest
import pytest_asyncio  # type: ignore[import-untyped]

from tests.fixtures.repositories import make_test_limiter
from tests.fixtures.sharding import pinned_shard
from zae_limiter import Limit, RateLimitExceeded, schema

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

RESOURCE = "gpt-4"
WINDOW = timedelta(seconds=2)
WINDOW_MS = 2_000
SLACK_S = 0.3
"""Margin past a window's end before acting on it, for LocalStack round trips."""


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _raw(repo, entity_id: str, shard: int = 0) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, RESOURCE, shard)},
            "SK": {"S": schema.sk_state()},
        },
    )
    return response.get("Item") or {}


async def _ws(repo, entity_id: str, shard: int = 0) -> int | None:
    raw = (await _raw(repo, entity_id, shard)).get(schema.bucket_attr("session", "ws"))
    return None if raw is None else int(raw["N"])


async def _tk(repo, entity_id: str, shard: int = 0) -> int:
    item = await _raw(repo, entity_id, shard)
    return int(item[schema.bucket_attr("session", schema.BUCKET_FIELD_TK)]["N"])


async def _sleep_until(instant_ms: int) -> None:
    await asyncio.sleep(max(0.0, (instant_ms - _now_ms()) / 1000 + SLACK_S))


async def _configure(limiter, entity_id: str, capacity: int, window=WINDOW) -> None:
    await limiter.set_limits(
        entity_id, [Limit.quota("session", capacity, reset_after=window)], resource=RESOURCE
    )


async def _spend(limiter, entity_id: str, amount: int = 1) -> None:
    async with limiter.acquire(entity_id, RESOURCE, consume={"session": amount}):
        pass


class TestSessionQuotaWithoutTheAggregator:
    """The client owns the whole feature: no Lambda runs on this stack."""

    @pytest.mark.asyncio
    async def test_an_exhausted_session_reports_its_end_then_restores(self, localstack_limiter):
        """Spend the allowance; the rejection and ``check_availability`` both
        name the window's end, and after it the allowance is back in full."""
        limiter = localstack_limiter
        await _configure(limiter, "spender", 3)
        await _spend(limiter, "spender", 3)
        ws = await _ws(limiter._repository, "spender")
        assert ws is not None
        end = ws + WINDOW_MS

        with pytest.raises(RateLimitExceeded) as caught:
            await _spend(limiter, "spender")
        rejected_at = _now_ms()
        (violation,) = caught.value.violations
        assert violation.limit_name == "session"
        assert violation.resets_at_ms == end
        assert 0 < caught.value.retry_after_seconds <= WINDOW_MS / 1000
        assert caught.value.retry_after_seconds == pytest.approx(
            (end - rejected_at) / 1000, abs=0.5
        )
        (body,) = caught.value.as_dict()["limits"]
        assert body["kind"] == "quota"
        assert body["resets_at_ms"] == end

        availability = await limiter.check_availability("spender", RESOURCE)
        assert availability.status("session").resets_at_ms == end
        assert availability.available == {"session": 0}

        await _sleep_until(end)
        await _spend(limiter, "spender")
        assert await _ws(limiter._repository, "spender") >= end
        after = await limiter.check_availability("spender", RESOURCE)
        assert after.available == {"session": 2}

    @pytest.mark.asyncio
    async def test_an_idle_entity_restarts_at_its_next_use(self, localstack_limiter):
        """A window that elapses while the entity is quiet is simply over: the
        next use anchors a fresh one at THAT call, not at the next grid tile
        ``ws + k * W`` a tiling window would have reached."""
        limiter = localstack_limiter
        repo = limiter._repository
        await _configure(limiter, "idler", 2)
        await _spend(limiter, "idler")
        first = await _ws(repo, "idler")
        assert first is not None

        await asyncio.sleep(WINDOW_MS / 1000 + 1.0)  # a full second idle past the end
        called_at = _now_ms()
        await _spend(limiter, "idler")
        second = await _ws(repo, "idler")

        assert second is not None
        assert second >= called_at, "anchored at the call that restarted it"
        assert second - first >= WINDOW_MS + 900, "not the grid tile at first + W"
        availability = await limiter.check_availability("idler", RESOURCE)
        assert availability.available == {"session": 1}
        assert availability.status("session").resets_at_ms == second + WINDOW_MS

    @pytest.mark.asyncio
    async def test_two_entities_get_independent_windows(self, localstack_limiter):
        """The feature in one test. A calendar quota would reset both entities
        at one instant; a session quota resets each on its own first use.

        A three-second window, so the second entity's window is still live
        when the first one's has ended whatever LocalStack's latency adds to
        the one-second gap between the two first uses.
        """
        limiter = localstack_limiter
        repo = limiter._repository
        for entity_id in ("early", "late"):
            await _configure(limiter, entity_id, 1, window=timedelta(seconds=3))
        await _spend(limiter, "early")
        await asyncio.sleep(1.0)
        await _spend(limiter, "late")

        early_end = (await limiter.check_availability("early", RESOURCE)).status("session")
        late_end = (await limiter.check_availability("late", RESOURCE)).status("session")
        assert early_end.resets_at_ms == await _ws(repo, "early") + 3_000
        assert late_end.resets_at_ms == await _ws(repo, "late") + 3_000
        assert 1_000 <= late_end.resets_at_ms - early_end.resets_at_ms < 2_000

        await _sleep_until(early_end.resets_at_ms)
        await _spend(limiter, "early")  # its window is over
        with pytest.raises(RateLimitExceeded) as caught:
            await _spend(limiter, "late")  # its window is not
        assert caught.value.violations[0].resets_at_ms == late_end.resets_at_ms

    @pytest.mark.asyncio
    async def test_a_sharded_entity_rolls_onto_one_window(self, localstack_limiter):
        """Four shards, a real rollover on one of them, and one answer from
        ``check_availability``. The fan-out is what makes that answer honest;
        without it each shard would roll when next drawn, on its own clock."""
        limiter = localstack_limiter
        repo = limiter._repository
        await _configure(limiter, "sharded", 40)
        await _spend(limiter, "sharded", 0)  # anchor without spending
        count = 1
        while count < 4:
            count = await repo.bump_shard_count("sharded", RESOURCE, count)
        for shard in range(1, 4):
            with pinned_shard(shard):
                await _spend(limiter, "sharded", 0)
        starts = [await _ws(repo, "sharded", s) for s in range(4)]
        assert None not in starts

        await _sleep_until(max(starts) + WINDOW_MS)
        with pinned_shard(2):
            await _spend(limiter, "sharded")
        rolled = await _ws(repo, "sharded", 2)
        assert [await _ws(repo, "sharded", s) for s in range(4)] == [rolled] * 4

        availability = await limiter.check_availability("sharded", RESOURCE)
        assert availability.status("session").resets_at_ms == rolled + WINDOW_MS
        # Shard 2 applied its roll and spent 1; its siblings read as restored.
        assert availability.available == {"session": 39}
        for shard in (0, 1, 3):
            with pinned_shard(shard):
                await _spend(limiter, "sharded")
        assert [await _tk(repo, "sharded", s) for s in range(4)] == [9_000] * 4


@pytest_asyncio.fixture
async def aggregator_limiter(shared_aggregator_stack, unique_namespace):
    """A limiter on the shared aggregator stack, in its own namespace."""
    parent, limiter = await make_test_limiter(shared_aggregator_stack, unique_namespace)
    async with limiter:
        yield limiter
    await parent.close()


class TestSessionQuotaWithTheAggregator:
    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_the_aggregator_applies_a_fanned_out_roll(self, aggregator_limiter):
        """A roll fanned out to a sibling is applied from the stream, with no
        client drawing that sibling. An optimisation on top of convergence --
        each sibling would apply it when next drawn -- so this asserts it
        HAPPENS, which is also the only way to see it.

        The aggregator applies a window only while it is live, and stream to
        Lambda latency on LocalStack is seconds, so this one uses a five-minute
        window. Its first window is anchored in the past through the client's
        clock hook so the rollover does not need a five-minute wait.
        """
        limiter = aggregator_limiter
        repo = limiter._repository
        window = timedelta(minutes=5)
        await limiter.set_limits(
            "aggr-session",
            [Limit.quota("session", 40, reset_after=window)],
            resource=RESOURCE,
        )
        past = _now_ms() - int(window.total_seconds() * 1000) - 5_000
        real_clock = repo._now_ms
        repo._now_ms = lambda: past
        try:
            await _spend(limiter, "aggr-session", 5)
            count = 1
            while count < 4:
                count = await repo.bump_shard_count("aggr-session", RESOURCE, count)
            for shard in range(1, 4):
                with pinned_shard(shard):
                    await _spend(limiter, "aggr-session", 0)
        finally:
            repo._now_ms = real_clock
        # The #587 transfer left the later siblings below their share of 10.
        assert await _tk(repo, "aggr-session", 3) < 10_000

        with pinned_shard(1):
            await _spend(limiter, "aggr-session")
        rolled = await _ws(repo, "aggr-session", 1)
        assert rolled is not None and rolled > past + int(window.total_seconds() * 1000)
        sibling = await _raw(repo, "aggr-session", 3)
        assert int(sibling[schema.BUCKET_FIELD_RF]["N"]) < rolled, "precondition: unapplied"

        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            sibling = await _raw(repo, "aggr-session", 3)
            if int(sibling[schema.BUCKET_FIELD_RF]["N"]) >= rolled:
                break
            await asyncio.sleep(1.0)
        assert int(sibling[schema.BUCKET_FIELD_RF]["N"]) >= rolled, "the aggregator never rolled"
        assert int(sibling[schema.bucket_attr("session", schema.BUCKET_FIELD_TK)]["N"]) == 10_000
        assert int(sibling[schema.bucket_attr("session", "ws")]["N"]) == rolled
