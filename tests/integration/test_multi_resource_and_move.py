"""LocalStack coverage for multi-resource acquire (ADR-148, #675) and moving an
entity (ADR-150, #677).

moto evaluates transactions and GSIs with its own engine; these run the
merged multi-resource `TransactWriteItems` (cancellation reasons included),
GSI1 propagation after a move and a second process learning the move against
LocalStack's DynamoDB (validation of #675 / #677).
"""

import pytest

from tests.fixtures.repositories import make_test_repo
from zae_limiter import RateLimiter, RateLimitExceeded
from zae_limiter.models import Limit

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

DAY = 86_400


async def _consumed(repo, entity_id: str, resource: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for bucket in await repo.get_buckets(entity_id, resource=resource):
        totals[bucket.limit_name] = (
            totals.get(bucket.limit_name, 0) + (bucket.total_consumed_milli or 0) // 1000
        )
    return totals


async def _setup(repo) -> None:
    await repo.set_resource_defaults("search", [Limit.per_day("rpm", 100)])
    await repo.set_resource_defaults("budget", [Limit.per_day("weekly", 50)])
    await repo.set_resource_cascade("budget", False)
    await repo.create_entity("org")
    await repo.create_entity("user", parent_id="org", cascade=True)


async def test_the_merged_slow_commit_is_all_or_none(test_repo):
    """Every part on the slow path is one transaction; a rejected part leaves
    nothing debited on the others."""
    await _setup(test_repo)
    slow = RateLimiter(repository=test_repo, speculative_writes=False)
    async with slow.acquire("user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}):
        pass
    assert await _consumed(test_repo, "user", "search") == {"rpm": 1}
    assert await _consumed(test_repo, "org", "search") == {"rpm": 1}
    assert await _consumed(test_repo, "user", "budget") == {"weekly": 10}

    with pytest.raises(RateLimitExceeded):
        async with slow.acquire("user", "search", {"rpm": 1}, also={"budget": {"weekly": 100}}):
            pass
    assert await _consumed(test_repo, "user", "search") == {"rpm": 1}
    assert await _consumed(test_repo, "org", "search") == {"rpm": 1}
    assert await _consumed(test_repo, "user", "budget") == {"weekly": 10}


async def test_a_lost_create_race_inside_the_merged_commit(test_repo):
    """A rival creates one part's bucket between the plan and the merged commit:
    the transaction is cancelled on that item alone, the rest re-sent, and the
    ledger is exact on a real engine."""
    await _setup(test_repo)
    slow = RateLimiter(repository=test_repo, speculative_writes=False)
    async with slow.acquire("user", "search", {"rpm": 1}):
        pass  # search exists; budget is created by the merged commit
    rival = RateLimiter(repository=test_repo, speculative_writes=False)
    real = test_repo.transact_write
    raced: list[int] = []

    async def rival_creates_first(items):
        if len(items) > 1 and not raced:
            raced.append(1)
            async with rival.acquire("user", "budget", {"weekly": 5}):
                pass
        return await real(items)

    test_repo.transact_write = rival_creates_first
    try:
        async with slow.acquire("user", "search", {"rpm": 1}, also={"budget": {"weekly": 10}}):
            pass
    finally:
        test_repo.transact_write = real
    assert raced == [1]
    assert await _consumed(test_repo, "user", "budget") == {"weekly": 15}
    assert await _consumed(test_repo, "user", "search") == {"rpm": 2}


async def test_a_move_reaches_gsi1_and_another_process(
    test_repo, shared_minimal_stack, unique_namespace
):
    """After `set_parent`, the children index lists the child under its new
    parent, and a second process — whose cache still names the old parent —
    debits the new one on its next acquire."""
    await test_repo.set_resource_defaults("search", [Limit.per_day("rpm", 100)])
    for org in ("org-a", "org-b"):
        await test_repo.create_entity(org)
    await test_repo.create_entity("user", parent_id="org-a", cascade=True)

    parent, other_repo = await make_test_repo(shared_minimal_stack, unique_namespace)
    try:
        other = RateLimiter(repository=other_repo)
        for _ in range(2):  # learn the family: org-a cached
            async with other.acquire("user", "search", {"rpm": 1}):
                pass
        await test_repo.set_parent("user", "org-b")

        children_b = {e.id for e in await test_repo.get_children("org-b")}
        children_a = {e.id for e in await test_repo.get_children("org-a")}
        assert "user" in children_b and "user" not in children_a

        before = {o: await _consumed(test_repo, o, "search") for o in ("org-a", "org-b")}
        async with other.acquire("user", "search", {"rpm": 1}):
            pass
        after = {o: await _consumed(test_repo, o, "search") for o in ("org-a", "org-b")}
        assert after["org-b"].get("rpm", 0) - before["org-b"].get("rpm", 0) == 1
        assert after["org-a"] == before["org-a"]  # debited and refunded on the same call
    finally:
        await parent.close()
