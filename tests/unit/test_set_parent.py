"""Moving an entity to a new parent (ADR-150, #677)."""

import asyncio
from unittest.mock import patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded, __version__, schema
from zae_limiter.exceptions import (
    EntityNotFoundError,
    FanoutIncomplete,
    ValidationError,
    VersionMismatchError,
)
from zae_limiter.models import AuditAction, Limit
from zae_limiter.repository import Repository
from zae_limiter.repository_protocol import SpeculativeResult
from zae_limiter.version import get_schema_version

TABLE = "test-set-parent"
RPM = Limit.per_day("rpm", 100)  # no refill to speak of during a test


async def _open_repo() -> Repository:
    """A Repository on the shared moto table: one more process."""
    return Repository(name=TABLE, region="us-east-1", _skip_deprecation_warning=True)


@pytest.fixture(autouse=True)
def generations_from_zero():
    """Entities created here start at generation 0, so a test reads moves as 1, 2.

    A real entity starts at its creation instant (ADR-150); only the
    differences matter. A test that needs the real start calls ``.stop()``.
    """
    from zae_limiter.sync_repository import SyncRepository

    patchers = [
        patch.object(cls, "_creation_generation", return_value=0)
        for cls in (Repository, SyncRepository)
    ]
    for patcher in patchers:
        patcher.start()

    class _Stopper:
        def stop(self) -> None:
            for patcher in patchers:
                patcher.stop()

    stopper = _Stopper()
    yield stopper
    stopper.stop()


@pytest.fixture
async def repo(mock_dynamodb):
    repo = await _open_repo()
    await repo.create_table()
    await repo._register_namespace("default")
    await repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    yield repo
    await repo.close()


@pytest.fixture
async def limiter(repo):
    async with RateLimiter(repository=repo) as limiter:
        yield limiter


@pytest.fixture
async def other(repo):
    """A second process's repository and limiter on the same table."""
    other_repo = await _open_repo()
    async with RateLimiter(repository=other_repo) as other_limiter:
        yield other_limiter
    await other_repo.close()


async def _hierarchy(limiter: RateLimiter) -> None:
    """org-a and org-b; `user` under org-a, cascading; buckets on two resources."""
    repo = limiter._repository
    for resource in ("gpt-4", "llm"):
        await repo.set_resource_defaults(resource, [RPM])
    await repo.create_entity("org-a")
    await repo.create_entity("org-b")
    await repo.create_entity("user", parent_id="org-a", cascade=True)
    for resource in ("gpt-4", "llm"):
        async with limiter.acquire("user", resource, consume={"rpm": 1}):
            pass


async def _item(repo: Repository, entity_id: str, resource: str, shard: int = 0) -> dict | None:
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


async def _meta(repo: Repository, entity_id: str) -> dict:
    client = await repo._get_client()
    response = await client.get_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_entity(repo._namespace_id, entity_id)},
            "SK": {"S": schema.sk_meta()},
        },
        ConsistentRead=True,
    )
    return response["Item"]


async def _consumed(repo: Repository, entity_id: str, resource: str = "gpt-4") -> int:
    item = await _item(repo, entity_id, resource)
    return 0 if item is None else int(item[schema.bucket_attr("rpm", "tc")]["N"]) // 1000


def _stamp(item: dict) -> tuple[bool, str | None, int | None]:
    pgen = item.get(schema.BUCKET_FIELD_PGEN, {}).get("N")
    return (
        item["cascade"]["BOOL"],
        item.get("parent_id", {}).get("S"),
        None if pgen is None else int(pgen),
    )


class TestMetaWrite:
    async def test_a_move_rewrites_the_parent_and_the_children_index(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)

        await repo.set_parent("user", "org-b")

        entity = await repo.get_entity("user")
        assert entity is not None
        assert (entity.parent_id, entity.parent_generation, entity.cascade) == ("org-b", 1, True)
        assert await repo.get_children("org-a") == []
        assert [e.id for e in await repo.get_children("org-b")] == ["user"]

    async def test_a_move_to_no_parent_leaves_the_children_index(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)

        await repo.set_parent("user", None)

        meta = await _meta(repo, "user")
        assert meta["parent_id"] == {"NULL": True}
        assert "GSI1PK" not in meta and "GSI1SK" not in meta
        assert await repo.get_children("org-a") == []
        assert (await repo.get_entity("user")).is_parent

    async def test_a_root_entity_moves_under_a_parent(self, limiter):
        repo = limiter._repository
        await repo.create_entity("org-a")
        await repo.create_entity("solo")

        assert await repo.set_parent("solo", "org-a") == 0  # no buckets yet

        assert [e.id for e in await repo.get_children("org-a")] == ["solo"]

    async def test_every_move_bumps_the_generation(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)

        await repo.set_parent("user", "org-b")
        await repo.set_parent("user", "org-b")  # a repeat is a move too: the repair path

        assert (await repo.get_entity("user")).parent_generation == 2

    async def test_a_missing_entity(self, limiter):
        repo = limiter._repository
        await repo.create_entity("org-a")
        with pytest.raises(EntityNotFoundError) as exc_info:
            await repo.set_parent("nobody", "org-a")
        assert exc_info.value.entity_id == "nobody"

    async def test_a_missing_parent_writes_nothing(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        with pytest.raises(EntityNotFoundError) as exc_info:
            await repo.set_parent("user", "org-typo")
        assert exc_info.value.entity_id == "org-typo"
        assert (await repo.get_entity("user")).parent_id == "org-a"

    async def test_any_other_error_on_the_meta_write_propagates(self, limiter):
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _hierarchy(limiter)
        client = await repo._get_client()
        throttled = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem"
        )
        with patch.object(client, "update_item", side_effect=throttled):
            with pytest.raises(ClientError):
                await repo._write_parent("user", "org-b")

    async def test_an_entity_cannot_be_its_own_parent(self, limiter):
        with pytest.raises(ValidationError, match="its own parent"):
            await limiter._repository.set_parent("user", "user")

    async def test_a_cycle_is_refused(self, limiter):
        repo = limiter._repository
        await repo.create_entity("top")
        await repo.create_entity("org-a", parent_id="top")
        await repo.create_entity("user", parent_id="org-a")

        with pytest.raises(ValidationError, match="ancestor of user"):
            await repo.set_parent("top", "user")  # user -> org-a -> top
        assert (await repo.get_entity("top")).parent_id is None

    async def test_a_dangling_ancestor_ends_the_walk(self, limiter):
        repo = limiter._repository
        await repo.create_entity("org-a", parent_id="deleted-top")
        await repo.create_entity("user")

        await repo.set_parent("user", "org-a")

        assert (await repo.get_entity("user")).parent_id == "org-a"

    async def test_a_chain_deeper_than_the_bound_is_refused(self, limiter):
        repo = limiter._repository
        await repo.create_entity("e0")
        for i in range(1, 34):
            await repo.create_entity(f"e{i}", parent_id=f"e{i - 1}")
        await repo.create_entity("user")

        with pytest.raises(ValidationError, match="deeper than 32"):
            await repo.set_parent("user", "e33")
        await repo.set_parent("user", "e32")  # exactly at the bound

    async def test_the_move_is_audited(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)

        await repo.set_parent("user", "org-b", principal="ops@example.com")

        events = await repo.get_audit_events("user")
        moves = [e for e in events if e.action == AuditAction.ENTITY_PARENT_CHANGED]
        assert len(moves) == 1
        assert moves[0].principal == "ops@example.com"
        assert moves[0].details == {
            "old_parent_id": "org-a",
            "parent_id": "org-b",
            "pgen": 1,
            "buckets_stamped": 2,
        }

    async def test_the_rate_limiter_delegates(self, limiter):
        await _hierarchy(limiter)
        assert await limiter.set_parent("user", "org-b") == 2
        assert (await limiter.get_entity("user")).parent_id == "org-b"


class TestFanout:
    async def test_every_shard_of_every_resource_is_restamped(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.bump_shard_count("user", "gpt-4", 1)
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
        assert await _item(repo, "user", "gpt-4", 1) is not None

        assert await repo.set_parent("user", "org-b") == 3

        for resource, shard in (("gpt-4", 0), ("gpt-4", 1), ("llm", 0)):
            assert _stamp(await _item(repo, "user", resource, shard)) == (True, "org-b", 1)

    async def test_a_move_to_no_parent_stops_the_cascade(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)

        await repo.set_parent("user", None)

        assert _stamp(await _item(repo, "user", "gpt-4")) == (False, None, 1)
        before = await _consumed(repo, "org-a")
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 2}) as lease:
            assert {e.entity_id for e in lease.entries} == {"user"}
        assert await _consumed(repo, "org-a") == before

    async def test_a_move_follows_the_cascade_policy(self, limiter):
        """The new parent is debited only where the policy says (ADR-146, D3)."""
        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_entity_cascade("user", False, resource="llm")

        await repo.set_parent("user", "org-b")

        assert _stamp(await _item(repo, "user", "gpt-4")) == (True, "org-b", 1)
        assert _stamp(await _item(repo, "user", "llm")) == (False, "org-b", 1)

    async def test_this_process_learns_the_move_at_once(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)

        await repo.set_parent("user", "org-b")

        assert repo._entity_cache[(repo._namespace_id, "user")][:2] == (True, "org-b")
        writes = []
        real = repo._speculative_consume_single

        async def spy(entity_id, *args, **kwargs):
            writes.append(entity_id)
            return await real(entity_id, *args, **kwargs)

        repo._speculative_consume_single = spy
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass
        assert sorted(writes) == ["org-b", "user"]

    async def test_a_failed_stamp_reports_progress_and_a_rerun_finishes(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        real = repo._stamp_bucket_cascade
        calls = []

        async def flaky(pk, *args):
            calls.append(pk)
            if len(calls) == 2:
                raise RuntimeError("throttled")
            return await real(pk, *args)

        with patch.object(repo, "_stamp_bucket_cascade", flaky):
            with pytest.raises(FanoutIncomplete) as exc_info:
                await repo.set_parent("user", "org-b")
        assert exc_info.value.stamped == 1
        events = await repo.get_audit_events("user")
        assert [e.details["buckets_stamped"] for e in events if e.action.endswith("changed")] == [1]

        assert await repo.set_parent("user", "org-b") == 2
        for resource in ("gpt-4", "llm"):
            assert _stamp(await _item(repo, "user", resource)) == (True, "org-b", 2)


class TestStaleWriters:
    """A writer that read the old parent can lose, never undo a move (ADR-150 §4)."""

    async def test_a_slow_pass_that_read_before_the_move_cannot_undo_it(self, repo, other):
        await _hierarchy(other)
        mover = other._repository
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.batch_get_entity_and_buckets
        moved = []

        async def read_then_move(*args, **kwargs):
            result = await real(*args, **kwargs)
            if not moved:  # META read (org-a); the move and its fan-out land now
                moved.append(await mover.set_parent("user", "org-b"))
            return result

        with patch.object(repo, "batch_get_entity_and_buckets", read_then_move):
            async with slow.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-a"}

        assert moved == [2]
        assert _stamp(await _item(repo, "user", "gpt-4")) == (True, "org-b", 1)

    async def test_a_recreated_entity_outranks_a_bucket_its_predecessor_left(
        self, limiter, generations_from_zero
    ):
        """Review of #716, finding 4: generations survive delete and recreate."""
        generations_from_zero.stop()  # the real start: the creation instant
        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [RPM])
        for org in ("org-a", "org-b", "org-c"):
            await repo.create_entity(org)
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass
        await repo.set_parent("user", "org-b")
        client = await repo._get_client()
        await client.delete_item(  # the bucket outlives its entity
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, "user")},
                "SK": {"S": schema.sk_meta()},
            },
        )
        with patch.object(repo, "_now_ms", return_value=repo._now_ms() + 1):
            await repo.create_entity("user", parent_id="org-c", cascade=True)

        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass  # the owner stamp lands: the new entity's generation is higher

        assert _stamp(await _item(repo, "user", "gpt-4"))[:2] == (True, "org-c")
        repo._entity_cache.clear()
        org_b, org_c = await _consumed(repo, "org-b"), await _consumed(repo, "org-c")
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass
        assert await _consumed(repo, "org-b") == org_b
        assert await _consumed(repo, "org-c") == org_c + 1

    @staticmethod
    async def _leftover_from_a_fast_clock(repo, other) -> None:
        """A bucket outliving an entity created on a clock 60 s ahead (review of #716, 2nd).

        Process A creates `user` under org-a with a generation 60 s in the
        future; an acquire in flight across the delete lands its create `Put`
        after it. The entity is then recreated, under org-b, by a correct clock
        (here generation 0): its generation is below the leftover's.
        """
        fast = other._repository
        for resource in ("gpt-4", "llm"):
            await fast.set_resource_defaults(resource, [Limit.per_minute("rpm", 3)])
        await fast.create_entity("org-a")
        await fast.create_entity("org-b")
        with patch.object(type(fast), "_creation_generation", return_value=10**13):
            await fast.create_entity("user", parent_id="org-a", cascade=True)
        slow = RateLimiter(repository=fast, speculative_writes=False)
        real = fast.batch_get_entity_and_buckets
        done = []

        async def read_then_delete(*args, **kwargs):
            result = await real(*args, **kwargs)
            if not done:
                done.append(1)
                await repo.delete_entity("user")
            return result

        with patch.object(fast, "batch_get_entity_and_buckets", read_then_delete):
            async with slow.acquire("user", "llm", consume={"rpm": 1}):
                pass
        assert _stamp(await _item(repo, "user", "llm")) == (True, "org-a", 10**13)
        await repo.create_entity("user", parent_id="org-b", cascade=True)

    async def test_a_stale_replica_of_the_predecessor_cannot_restamp(
        self, repo, other, generations_from_zero
    ):
        """Review of #716 (3rd), finding 1: the stale-incarnation probe.

        A slow pass whose eventually consistent META read returns the deleted
        predecessor (old incarnation, org-a) must not overwrite the recreated
        entity's stamp (org-b). Its own acquire still debits org-a once.
        """
        generations_from_zero.stop()
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("org-a")
        await repo.create_entity("org-b")
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        old_meta = await repo.get_entity("user", consistent_read=True)
        await repo.delete_entity("user")
        with patch.object(repo, "_now_ms", return_value=repo._now_ms() + 10):
            await repo.create_entity("user", parent_id="org-b", cascade=True)
        async with RateLimiter(repository=repo, speculative_writes=False).acquire(
            "user", "llm", consume={"rpm": 1}
        ):
            pass
        current = _stamp(await _item(repo, "user", "llm"))
        assert current[:2] == (True, "org-b")

        stale = other._repository
        real = stale.batch_get_entity_and_buckets

        async def stale_meta(entity_id, bucket_keys):
            entity, buckets = await real(entity_id, bucket_keys)
            return (old_meta if entity_id == "user" else entity), buckets

        org_a = await _consumed(repo, "org-a", "llm")
        with patch.object(stale, "batch_get_entity_and_buckets", stale_meta):
            async with RateLimiter(repository=stale, speculative_writes=False).acquire(
                "user", "llm", consume={"rpm": 1}
            ) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-a"}
        assert await _consumed(repo, "org-a", "llm") == org_a + 1  # this lease, once

        assert _stamp(await _item(repo, "user", "llm")) == current  # the stamp stands
        third_repo = await _open_repo()
        async with RateLimiter(repository=third_repo) as third:
            org_a, org_b = (
                await _consumed(repo, "org-a", "llm"),
                await _consumed(repo, "org-b", "llm"),
            )
            for _ in range(5):
                async with third.acquire("user", "llm", consume={"rpm": 1}):
                    pass
        await third_repo.close()
        assert await _consumed(repo, "org-a", "llm") == org_a
        assert await _consumed(repo, "org-b", "llm") == org_b + 5

    async def test_a_claim_needs_this_incarnation_to_be_current(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        assert await repo.restamp_owner_if_current("user", "gpt-4", 0, "not-current") is False
        assert await repo.restamp_owner_if_current("nobody", "gpt-4", 0, "x") is False

    async def test_a_failed_claim_is_logged(self, repo, other, caplog):
        await self._leftover_from_a_fast_clock(repo, other)
        with patch.object(repo, "restamp_owner_if_current", side_effect=RuntimeError("x")):
            async with RateLimiter(repository=repo, speculative_writes=False).acquire(
                "user", "llm", consume={"rpm": 1}
            ):
                pass
        assert "could not claim a predecessor's bucket" in caplog.text

    @staticmethod
    async def _foreign_stamp(repo: Repository) -> None:
        """user's gpt-4 bucket carries another incarnation's stamp."""
        client = await repo._get_client()
        await client.update_item(
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET pinc = :other",
            ExpressionAttributeValues={":other": {"S": "someone-else"}},
        )

    async def test_a_resent_write_that_loses_again_takes_the_retry(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._foreign_stamp(repo)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        user = await _consumed(repo, "user")
        # A claim reported but not landed: the re-sent write loses the pin again.
        with patch.object(repo, "restamp_owner_if_current", return_value=True):
            async with slow.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
        assert await _consumed(repo, "user") == user + 1  # the consumption-only retry
        assert (await _item(repo, "user", "gpt-4"))["pinc"] == {"S": "someone-else"}

    async def test_a_resent_write_that_errors_surfaces(self, limiter):
        from botocore.exceptions import ClientError

        from zae_limiter.exceptions import RateLimiterUnavailable

        repo = limiter._repository
        await _hierarchy(limiter)
        await self._foreign_stamp(repo)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.transact_write
        calls = []

        async def flaky(items):
            calls.append(items)
            if len(calls) == 2:  # the re-send
                raise ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")
            return await real(items)

        with (
            patch.object(repo, "restamp_owner_if_current", return_value=True),
            patch.object(repo, "transact_write", flaky),
        ):
            with pytest.raises(RateLimiterUnavailable):
                async with slow.acquire("user", "gpt-4", consume={"rpm": 1}):
                    pass

    async def test_a_backend_without_the_claim_skips_it(self):
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.lease import Lease
        from zae_limiter.repository_protocol import RepositoryProtocol

        lease = Lease(repository=cast(RepositoryProtocol, SimpleNamespace()))
        await lease._claim_owner_stamps({}, RuntimeError("x"))

    async def test_a_predecessors_bucket_never_outranks_the_recreated_entity(self, repo, other):
        """Skew probe: its generation is higher, its incarnation is not this one."""
        await self._leftover_from_a_fast_clock(repo, other)

        slow = RateLimiter(repository=repo, speculative_writes=False)
        async with slow.acquire("user", "llm", consume={"rpm": 1}):
            pass  # the owner stamp lands, whatever generation the leftover holds

        inc = (await repo.get_entity("user")).incarnation
        item = await _item(repo, "user", "llm")
        assert _stamp(item)[:2] == (True, "org-b")
        assert item[schema.BUCKET_FIELD_PINC] == {"S": inc}
        third_repo = await _open_repo()
        async with RateLimiter(repository=third_repo) as third:
            org_a, org_b = (
                await _consumed(repo, "org-a", "llm"),
                await _consumed(repo, "org-b", "llm"),
            )
            async with third.acquire("user", "llm", consume={"rpm": 1}):
                pass
        await third_repo.close()
        assert await _consumed(repo, "org-a", "llm") == org_a
        assert await _consumed(repo, "org-b", "llm") == org_b + 1

    async def test_set_parent_restamps_a_predecessors_bucket(self, repo, other):
        await self._leftover_from_a_fast_clock(repo, other)

        assert await repo.set_parent("user", "org-b") == 1

        assert _stamp(await _item(repo, "user", "llm")) == (True, "org-b", 1)

    async def test_a_predecessors_bucket_refills_again(self, repo, other):
        """Skew-refill probe: the rf-locked write lands, so `rf` advances."""
        await self._leftover_from_a_fast_clock(repo, other)
        limiter = RateLimiter(repository=repo)
        async with limiter.acquire("user", "llm", consume={"rpm": 1}):
            pass  # the slow pass that advances rf
        base = repo._now_ms()
        for minutes in (2, 5, 10):
            with patch.object(type(repo), "_now_ms", return_value=base + minutes * 60_000):
                async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                    pass  # refilled: admitted

    async def test_set_parent_counts_only_buckets_it_stamped(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        client = await repo._get_client()
        await client.update_item(  # gpt-4 already carries a newer stamp of this incarnation
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET pgen = :g",
            ExpressionAttributeValues={":g": {"N": "100"}},
        )

        assert await repo.set_parent("user", "org-b") == 1  # llm only

    async def test_an_entity_from_before_incarnations_is_its_own(self, limiter):
        """No `pinc` on META reads as its `created_at`, stamped and pinned as one."""
        repo = limiter._repository
        await _hierarchy(limiter)
        client = await repo._get_client()
        await client.update_item(
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, "user")},
                "SK": {"S": schema.sk_meta()},
            },
            UpdateExpression="REMOVE pinc",
        )
        created_at = (await _meta(repo, "user"))["created_at"]["S"]
        assert (await repo.get_entity("user")).incarnation == created_at

        await repo.set_parent("user", "org-b")

        item = await _item(repo, "user", "gpt-4")
        assert item[schema.BUCKET_FIELD_PINC] == {"S": created_at}
        pk = schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)
        assert not await repo._stamp_bucket_cascade(
            pk, True, "org-a", 0, created_at
        )  # same incarnation, lower generation: refused

    @pytest.mark.parametrize(
        ("meta", "expected"),
        [
            ({"pinc": {"S": "abc"}, "created_at": {"S": "2026-01-01T00:00:00Z"}}, "abc"),
            ({"created_at": {"S": "2026-01-01T00:00:00Z"}}, "2026-01-01T00:00:00Z"),
            ({}, "-"),
            ({"pinc": {"S": ""}}, "-"),  # an empty token is no incarnation
            ({"pinc": "abc"}, "abc"),  # the Table resource's native values (aggregator)
            ({"created_at": "2026-01-01T00:00:00Z"}, "2026-01-01T00:00:00Z"),
        ],
    )
    def test_the_shared_incarnation_rule(self, meta, expected):
        assert schema.entity_incarnation(meta) == expected

    async def test_a_new_entity_starts_at_its_creation_instant(self, repo, generations_from_zero):
        generations_from_zero.stop()
        with patch.object(repo, "_now_ms", return_value=1_800_000_000_000):
            created = await repo.create_entity("user")
        assert created.parent_generation == 1_800_000_000_000
        assert (await repo.get_entity("user")).parent_generation == 1_800_000_000_000

    async def test_a_create_that_read_before_the_move_is_restamped(self, repo, other):
        """Review of #716, finding 2: a create Put can carry no pin, so it is checked."""
        mover = other._repository
        await _hierarchy(other)  # user has gpt-4 and llm buckets; none on "chat" yet
        await mover.set_resource_defaults("chat", [RPM])
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.batch_get_entity_and_buckets
        moved = []

        async def read_then_move(*args, **kwargs):
            result = await real(*args, **kwargs)
            if not moved:  # META read (org-a); the move's fan-out finds no chat bucket
                moved.append(await mover.set_parent("user", "org-b"))
            return result

        with patch.object(repo, "batch_get_entity_and_buckets", read_then_move):
            async with slow.acquire("user", "chat", consume={"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-a"}  # in flight

        assert moved == [2]
        assert _stamp(await _item(repo, "user", "chat")) == (True, "org-b", 1)
        third_repo = await _open_repo()
        async with RateLimiter(repository=third_repo) as third:
            org_a, org_b = (
                await _consumed(repo, "org-a", "chat"),
                await _consumed(repo, "org-b", "chat"),
            )
            for _ in range(3):
                async with third.acquire("user", "chat", consume={"rpm": 1}):
                    pass
        await third_repo.close()
        assert await _consumed(repo, "org-a", "chat") == org_a
        assert await _consumed(repo, "org-b", "chat") == org_b + 3

    async def test_a_create_with_a_current_stamp_writes_nothing_more(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        assert await repo.repair_created_owner_stamp("user", "gpt-4", 0, 0) is False
        assert await repo.repair_created_owner_stamp("nobody", "gpt-4", 0, 0) is False

    async def test_a_failed_owner_check_never_fails_the_acquire(self, limiter):
        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.create_entity("user")
        with patch.object(repo, "repair_created_owner_stamp", side_effect=RuntimeError("x")):
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert lease.consumed == {"rpm": 1}

        assert (await _item(repo, "user", "gpt-4"))[schema.BUCKET_FIELD_VU] == {"N": "0"}

    async def test_a_failed_owner_check_forces_the_next_pass_to_restamp(self, repo, other):
        """Review of #716 (2nd), finding 2: the repairfail probe."""
        mover = other._repository
        await _hierarchy(other)
        await mover.set_resource_defaults("chat", [RPM])
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.batch_get_entity_and_buckets
        moved = []

        async def read_then_move(*args, **kwargs):
            result = await real(*args, **kwargs)
            if not moved:
                moved.append(await mover.set_parent("user", "org-b"))
            return result

        with (
            patch.object(repo, "batch_get_entity_and_buckets", read_then_move),
            patch.object(repo, "repair_created_owner_stamp", side_effect=RuntimeError("throttled")),
        ):
            async with slow.acquire("user", "chat", consume={"rpm": 1}):
                pass

        item = await _item(repo, "user", "chat")
        assert _stamp(item)[1] == "org-a"  # the stale stamp the check could not fix...
        assert item[schema.BUCKET_FIELD_VU] == {"N": "0"}  # ...but the fast path is closed
        third_repo = await _open_repo()
        async with RateLimiter(repository=third_repo) as third:
            org_a = await _consumed(repo, "org-a", "chat")
            for _ in range(3):
                async with third.acquire("user", "chat", consume={"rpm": 1}):
                    pass
        await third_repo.close()
        assert _stamp(await _item(repo, "user", "chat"))[1] == "org-b"
        assert await _consumed(repo, "org-a", "chat") == org_a

    async def test_the_owner_check_runs_in_the_background_during_the_block(self, limiter):
        """F2 (option B): the check starts at commit and overlaps the caller's block."""
        repo = limiter._repository
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("user")
        checked = []

        async def check(*args):
            checked.append(args[:3])
            return False

        with patch.object(repo, "repair_created_owner_stamp", check):
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert lease._owner_task is not None  # started at commit
                for _ in range(100):  # a long caller block
                    if checked:
                        break
                    await asyncio.sleep(0.01)
                assert checked == [("user", "gpt-4", 0)]  # done before the block ends
            assert lease._owner_task is None  # joined on exit

            with pytest.raises(RuntimeError, match="the caller's work failed"):
                async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                    raise RuntimeError("the caller's work failed")
            assert checked[-1] == ("user", "llm", 0)  # the bucket exists all the same

    async def test_a_failed_background_check_is_logged_and_masks_nothing(self, limiter, caplog):
        repo = limiter._repository
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("user")
        from zae_limiter.lease import Lease

        with patch.object(
            Lease, "_repair_created_owner_stamps", side_effect=RuntimeError("check broke")
        ):
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert lease.consumed == {"rpm": 1}
            with pytest.raises(ValueError, match="the caller's"):
                async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                    raise ValueError("the caller's")
        assert "owner stamp checks failed" in caplog.text
        assert "user" not in [r.getMessage() for r in caplog.records]

    async def test_a_cancel_during_the_join_forces_the_slow_pass(self, limiter):
        """r4 #1: a cancelled join loses the check, so the bucket gets ``vu = 0``."""
        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.create_entity("user")
        started = asyncio.Event()
        outcome: list[str] = []

        async def slow_check(*args):
            started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                outcome.append("cancelled")
                raise
            return False

        async def run():
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass  # the block ends at once: the join is what waits

        with patch.object(repo, "repair_created_owner_stamp", slow_check):
            task = asyncio.create_task(run())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert outcome == ["cancelled"]  # the check itself never finished...
        item = await _item(repo, "user", "gpt-4")
        assert item[schema.BUCKET_FIELD_VU] == {"N": "0"}  # ...so the fast path is closed

    async def test_a_cancel_during_the_window_fan_out_leaves_no_task_behind(self, limiter):
        """r4 #2: the owner task starts after the commit's last await, so none is orphaned."""
        from zae_limiter.lease import Lease

        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.create_entity("user")
        started: list[object] = []
        real_start = repo.start_background
        in_fan_out = asyncio.Event()

        def start(work):
            task = real_start(work)
            started.append(task)
            return task

        async def slow_fan_out(self, *args):
            in_fan_out.set()
            await asyncio.sleep(5)

        async def run():
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass

        with (
            patch.object(repo, "start_background", start),
            patch.object(Lease, "_fan_out_windows", slow_fan_out),
        ):
            task = asyncio.create_task(run())
            await in_fan_out.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        assert started == []  # no owner task was ever started, so none can be pending
        item = await _item(repo, "user", "gpt-4")
        assert item[schema.BUCKET_FIELD_VU] == {"N": "0"}  # the check's safety net instead

    async def test_a_task_cancelled_by_someone_else_leaves_the_callers_exception(self, caplog):
        """r5 #3 (case1): a cancel not aimed at the caller is not raised into it."""
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.lease import Lease
        from zae_limiter.repository_protocol import RepositoryProtocol

        writes: list[tuple] = []

        async def slow(*args):
            await asyncio.sleep(5)

        async def write_each(items):
            writes.extend(items)

        backend = SimpleNamespace(
            start_background=lambda work: asyncio.create_task(work()),
            repair_created_owner_stamp=slow,
            build_vu_reset=lambda *a: ("vu=0", *a),
            write_each=write_each,
        )
        lease = Lease(repository=cast(RepositoryProtocol, backend))
        await lease._start_owner_checks([("user", "gpt-4", 0, 1, "inc")])
        await asyncio.sleep(0.01)
        assert lease._owner_task is not None
        lease._owner_task.cancel()  # e.g. a shutdown helper cancelling stray tasks

        with pytest.raises(ValueError, match="the caller's"):
            try:
                raise ValueError("the caller's")
            finally:
                await lease._await_owner_checks()
        assert writes == [("vu=0", "user", "gpt-4", 0)]
        assert "owner stamp checks were cancelled" in caplog.text

        # A normal exit stands too.
        await lease._start_owner_checks([("user", "gpt-4", 1, 1, "inc")])
        await asyncio.sleep(0.01)
        lease._owner_task.cancel()
        await lease._await_owner_checks()
        assert writes[-1] == ("vu=0", "user", "gpt-4", 1)

    @pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
    def test_an_interrupted_sync_join_skips_the_writes_and_reraises(self, interrupt):
        """r5 #5: Ctrl-C during the join must not wait on vu = 0 round trips."""
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.sync_lease import SyncLease
        from zae_limiter.sync_repository_protocol import SyncRepositoryProtocol

        cancelled: list[bool] = []

        def result():
            raise interrupt()

        handle = SimpleNamespace(result=result, cancel=lambda: cancelled.append(True))
        writes: list[tuple] = []
        backend = SimpleNamespace(
            start_background=lambda _work: handle,
            build_vu_reset=lambda *a: ("vu=0", *a),
            write_each=writes.extend,
        )
        lease = SyncLease(repository=cast(SyncRepositoryProtocol, backend))
        lease._start_owner_checks([("user", "gpt-4", 0, 1, "inc")])

        with pytest.raises(interrupt):
            lease._await_owner_checks()
        assert writes == []  # no network round trip on the way down
        assert cancelled == [True]
        assert lease._owner_task is None

    async def test_another_base_exception_in_the_join_still_forces_the_slow_pass(self):
        """Anything else that unwinds the join (a killed greenlet's GreenletExit) writes vu = 0."""
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.lease import Lease
        from zae_limiter.repository_protocol import RepositoryProtocol

        class Unwind(BaseException):
            pass

        class Handle:
            cancelled = False

            def cancel(self):
                self.cancelled = True

            def __await__(self):
                raise Unwind  # `await` calls this first, so it unwinds right there

        writes: list[tuple] = []

        async def write_each(items):
            writes.extend(items)

        backend = SimpleNamespace(
            start_background=lambda _work: Handle(),
            build_vu_reset=lambda *a: ("vu=0", *a),
            write_each=write_each,
        )
        lease = Lease(repository=cast(RepositoryProtocol, backend))
        await lease._start_owner_checks([("user", "gpt-4", 0, 1, "inc")])
        handle = lease._owner_task

        with pytest.raises(Unwind):
            await lease._await_owner_checks()
        assert writes == [("vu=0", "user", "gpt-4", 0)]
        assert handle.cancelled

    def test_the_sync_twin_never_reads_its_caller_as_cancelled(self):
        from zae_limiter.cancellation import caller_is_cancelled, sync_caller_is_cancelled

        assert sync_caller_is_cancelled() is False
        assert caller_is_cancelled() is False  # no running task at all

    async def test_a_cancelled_join_with_nothing_to_force_still_reraises(self):
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.lease import Lease
        from zae_limiter.repository_protocol import RepositoryProtocol

        async def never():
            await asyncio.sleep(5)

        lease = Lease(repository=cast(RepositoryProtocol, SimpleNamespace()))
        lease._owner_task = asyncio.create_task(never())
        join = asyncio.create_task(lease._await_owner_checks())
        await asyncio.sleep(0)
        join.cancel()
        with pytest.raises(asyncio.CancelledError):
            await join
        assert lease._owner_task is None
        assert lease._owner_checks == []

    async def test_a_backend_without_background_tasks_checks_inline(self):
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.lease import Lease
        from zae_limiter.repository_protocol import RepositoryProtocol

        checked = []

        async def repair(*args):
            checked.append(args)
            return False

        lease = Lease(
            repository=cast(RepositoryProtocol, SimpleNamespace(repair_created_owner_stamp=repair))
        )
        await lease._start_owner_checks([("user", "gpt-4", 0, 1, "abc")])
        assert checked == [("user", "gpt-4", 0, 1, "abc")]
        assert lease._owner_task is None
        await lease._start_owner_checks([])  # nothing to check
        await lease._await_owner_checks()  # nothing to join

    def test_the_sync_join_forces_the_slow_pass_for_a_cancelled_spawn(self):
        """A cancelled spawn's `.result()` raises an `Exception`; it still gets vu = 0.

        Nobody cancelled the sync caller, so the join does not raise either.
        """
        from concurrent.futures import Future
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.sync_lease import SyncLease
        from zae_limiter.sync_repository_protocol import SyncRepositoryProtocol

        cancelled: Future[None] = Future()
        cancelled.cancel()
        writes: list[tuple] = []
        backend = SimpleNamespace(
            start_background=lambda _work: cancelled,
            repair_created_owner_stamp=lambda *a: False,
            build_vu_reset=lambda *a: ("vu=0", *a),
            write_each=writes.extend,
        )
        lease = SyncLease(repository=cast(SyncRepositoryProtocol, backend))
        lease._start_owner_checks([("user", "gpt-4", 0, 1, "inc")])

        lease._await_owner_checks()  # returns: the cancel was not aimed at the caller
        assert writes == [("vu=0", "user", "gpt-4", 0)]
        assert lease._owner_task is None

    def test_the_sync_twin_runs_the_check_in_the_background(self, mock_dynamodb):
        """The generated SyncLease spawns the check (threadpool) during the block."""
        import time

        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(
            name="test-set-parent-bg",
            region="us-east-1",
            _skip_deprecation_warning=True,
            parallel_mode="threadpool",
        )
        repo.create_table()
        repo._register_namespace("default")
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
        repo.set_resource_defaults("gpt-4", [RPM])
        repo.create_entity("user")
        limiter = SyncRateLimiter(repository=repo)
        checked = []
        main_thread = __import__("threading").get_ident()

        def check(*args):
            checked.append((args[:3], __import__("threading").get_ident()))
            return False

        with patch.object(repo, "repair_created_owner_stamp", check):
            with limiter.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert lease._owner_task is not None
                for _ in range(100):  # a long caller block
                    if checked:
                        break
                    time.sleep(0.01)
                assert [c[0] for c in checked] == [("user", "gpt-4", 0)]
            assert lease._owner_task is None
        assert checked[0][1] != main_thread  # it ran on the pool, not inline
        repo.close()

    async def test_a_failed_slow_pass_stamp_is_logged(self, limiter, caplog):
        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.create_entity("user")
        with (
            patch.object(repo, "repair_created_owner_stamp", side_effect=RuntimeError("x")),
            patch.object(repo, "build_vu_reset", side_effect=RuntimeError("y")),
        ):
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
        assert "could not force a slow pass" in caplog.text

    async def test_a_backend_without_vu_reset_skips_it(self):
        from types import SimpleNamespace
        from typing import cast

        from zae_limiter.lease import Lease
        from zae_limiter.repository_protocol import RepositoryProtocol

        lease = Lease(repository=cast(RepositoryProtocol, SimpleNamespace()))
        await lease._force_owner_restamp("user", "gpt-4", 0)

    @staticmethod
    async def _doubling_record(repo: Repository) -> dict:
        """The stream record of shard 0's 1 -> 2 doubling, its image read now."""
        client = await repo._get_client()
        await client.update_item(  # what the aggregator's proactive shard writes
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET shard_count = :n",
            ExpressionAttributeValues={":n": {"N": "2"}},
        )
        new_image = await _item(repo, "user", "gpt-4")
        old_image = {**new_image, "shard_count": {"N": "1"}}
        return {"eventName": "MODIFY", "dynamodb": {"NewImage": new_image, "OldImage": old_image}}

    async def test_an_aggregator_clone_from_before_the_move_is_restamped(self, limiter):
        """Review of #716, finding 3: Path 2 clones copy shard 0's stream image."""
        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        record = await self._doubling_record(repo)
        await repo.set_parent("user", "org-b")  # lands before the lagging record

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        assert propagate_shard_count(table, record) == 1

        assert _stamp(await _item(repo, "user", "gpt-4", 1)) == (True, "org-b", 1)
        third_repo = await _open_repo()
        async with RateLimiter(repository=third_repo) as third:
            org_a = await _consumed(repo, "org-a")
            for _ in range(10):
                async with third.acquire("user", "gpt-4", consume={"rpm": 1}):
                    pass
        await third_repo.close()
        assert await _consumed(repo, "org-a") == org_a

    async def test_a_clone_resolves_the_policy_and_a_move_to_no_parent(self, limiter):
        import boto3

        from zae_limiter_aggregator.processor import (
            _clone_owner_stamp,
            propagate_shard_count,
        )

        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_entity_cascade("user", False)  # entity-wide policy: off
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        ns = repo._namespace_id
        inc = (await repo.get_entity("user")).incarnation
        assert _clone_owner_stamp(table, ns, "user", "gpt-4") == (False, "org-a", 0, inc)
        assert _clone_owner_stamp(table, ns, "user", schema.DEFAULT_RESOURCE) == (
            False,
            "org-a",
            0,
            inc,
        )
        await repo.clear_entity_cascade("user")
        await repo.set_resource_cascade("gpt-4", True)
        assert _clone_owner_stamp(table, ns, "user", "gpt-4") == (True, "org-a", 0, inc)
        assert _clone_owner_stamp(table, ns, "nobody", "gpt-4") is None

        record = await self._doubling_record(repo)
        await repo.set_parent("user", None)
        assert propagate_shard_count(table, record) == 1
        assert _stamp(await _item(repo, "user", "gpt-4", 1)) == (False, None, 1)

    async def test_a_clone_with_a_current_stamp_is_left_alone(self, limiter):
        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_parent("user", "org-b")
        record = await self._doubling_record(repo)  # taken after the move
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        with patch.object(table, "update_item", side_effect=AssertionError("restamped")):
            assert propagate_shard_count(table, record) == 1
        assert _stamp(await _item(repo, "user", "gpt-4", 1)) == (True, "org-b", 1)

    async def test_a_failed_clone_repair_is_swallowed(self, limiter):
        from botocore.exceptions import ClientError, EndpointConnectionError

        from zae_limiter_aggregator.processor import _repair_clone_owner_stamps

        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_parent("user", "org-b")
        ns = repo._namespace_id
        throttled = ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")
        gone = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        dropped = EndpointConnectionError(endpoint_url="http://dynamodb")

        class _Table:
            def __init__(self, get=None, update=None):
                self.get, self.update = get, update

            def get_item(self, **kwargs):
                if self.get is not None:
                    raise self.get
                return {"Item": {"parent_id": None, "pgen": 5}}

            def update_item(self, **kwargs):
                raise self.update

        _repair_clone_owner_stamps(_Table(get=throttled), ns, "user", "gpt-4", [1], 0)
        for error in (throttled, gone, dropped):
            _repair_clone_owner_stamps(_Table(update=error), ns, "user", "gpt-4", [1, 2], 0)

    async def test_the_clone_restamp_is_token_safe(self, limiter):
        import boto3

        from tests.unit.test_expression_tokens import assert_expression_safe
        from zae_limiter_aggregator.processor import _repair_clone_owner_stamps

        repo = limiter._repository
        await _hierarchy(limiter)
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        for parent in ("org-b", None):
            await repo.set_parent("user", parent)
            calls = []
            real = table.update_item

            def spy(_real=real, _calls=calls, **kwargs):
                _calls.append(kwargs)
                return _real(**kwargs)

            with patch.object(table, "update_item", spy):
                _repair_clone_owner_stamps(table, repo._namespace_id, "user", "gpt-4", [0], 0)
            assert len(calls) == 1
            assert_expression_safe(calls[0])

    async def test_a_stale_fanout_stamp_is_skipped(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_parent("user", "org-b")
        pk = schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)

        await repo._stamp_bucket_cascade(pk, True, "org-a", 0)

        assert _stamp(await _item(repo, "user", "gpt-4")) == (True, "org-b", 1)

    async def test_two_moves_converge_on_the_later(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_parent("user", "org-b")
        await repo.set_parent("user", "org-a")
        pk = schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)

        await repo._stamp_bucket_cascade(pk, True, "org-b", 1)  # the first move, late

        assert _stamp(await _item(repo, "user", "gpt-4")) == (True, "org-a", 2)

    async def test_the_provisioner_stamp_carries_and_respects_the_generation(self, limiter):
        import boto3

        from zae_limiter_provisioner.fanout import fanout_cascade, stamp_bucket_cascade

        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_parent("user", "org-b")
        client = boto3.client("dynamodb", region_name="us-east-1")
        pk = schema.pk_bucket(repo._namespace_id, "user", "gpt-4", 0)

        stamp_bucket_cascade(client, TABLE, pk, True, "org-a", 0)
        assert _stamp(await _item(repo, "user", "gpt-4")) == (True, "org-b", 1)

        assert fanout_cascade(client, TABLE, repo._namespace_id, entity_id="user") == 2
        assert _stamp(await _item(repo, "user", "llm")) == (True, "org-b", 1)


class TestAnotherProcess:
    """A process whose cache predates the move learns it from the child's item."""

    @staticmethod
    async def _warm(limiter: RateLimiter) -> None:
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass

    async def test_the_next_call_debits_the_new_parent_only(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        org_a, org_b = await _consumed(repo, "org-a"), await _consumed(repo, "org-b")

        await repo.set_parent("user", "org-b")

        async with other.acquire("user", "gpt-4", consume={"rpm": 2}) as lease:
            assert {e.entity_id for e in lease.entries} == {"user", "org-b"}
        assert await _consumed(repo, "org-a") == org_a
        assert await _consumed(repo, "org-b") == org_b + 2

        writes = []
        other_repo = other._repository
        real = other_repo._speculative_consume_single

        async def spy(entity_id, *args, **kwargs):
            writes.append(entity_id)
            return await real(entity_id, *args, **kwargs)

        other_repo._speculative_consume_single = spy
        async with other.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass
        assert sorted(writes) == ["org-b", "user"]  # the parallel path, to org-b
        assert await _consumed(repo, "org-a") == org_a

    async def test_a_move_to_no_parent_stops_the_cascade_there_too(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        org_a = await _consumed(repo, "org-a")

        await repo.set_parent("user", None)

        for _ in range(2):
            async with other.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user"}
        assert await _consumed(repo, "org-a") == org_a
        key = (other._repository._namespace_id, "user", "gpt-4")
        assert other._repository._cascade_cache[key] is False

    async def test_a_full_new_parent_rejects_and_nothing_is_kept(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        async with limiter.acquire("org-b", "gpt-4", consume={"rpm": 100}):
            pass  # org-b drained
        org_a, user = await _consumed(repo, "org-a"), await _consumed(repo, "user")

        await repo.set_parent("user", "org-b")

        with pytest.raises(RateLimitExceeded):
            async with other.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
        assert await _consumed(repo, "org-a") == org_a
        child = await _item(repo, "user", "gpt-4")
        assert int(child[schema.bucket_attr("rpm", "tk")]["N"]) >= (100 - user - 1) * 1000

    async def test_a_failed_child_teaches_the_new_parent(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        await repo.set_parent("user", "org-b")
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 95}):
            pass  # through this process, which knows the move: 3 left

        with pytest.raises(RateLimitExceeded):
            async with other.acquire("user", "gpt-4", consume={"rpm": 50}):
                pass

        other_repo = other._repository
        assert other_repo._entity_cache[(other_repo._namespace_id, "user")][1] == "org-b"

    async def test_a_new_parent_that_refill_would_help_is_the_one_debited(self, limiter, other):
        """Review of #716, finding 1: the parent-only slow path must name the new parent."""
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        async with limiter.acquire("org-b", "gpt-4", consume={"rpm": 100}):
            pass  # org-b drained...
        await _set(repo, "org-b", rf=repo._now_ms() - 86_400_000)  # ...a day ago: refill helps
        await repo.set_parent("user", "org-b")
        org_a, org_b = await _consumed(repo, "org-a"), await _consumed(repo, "org-b")

        async with other.acquire("user", "gpt-4", consume={"rpm": 3}) as lease:
            assert sorted(e.entity_id for e in lease.entries) == ["org-b", "user"]

        assert await _consumed(repo, "org-a") == org_a
        assert await _consumed(repo, "org-b") == org_b + 3

    async def test_a_disabled_new_parent_is_the_one_reported(self, limiter, other):
        from zae_limiter.exceptions import ResourceDisabled

        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        async with limiter.acquire("org-b", "gpt-4", consume={"rpm": 1}):
            pass
        await _set(repo, "org-b", disabled=True)
        await repo.set_parent("user", "org-b")
        org_a = await _consumed(repo, "org-a")

        with pytest.raises(ResourceDisabled) as exc_info:
            async with other.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass

        assert exc_info.value.entity_id == "org-b"
        assert await _consumed(repo, "org-a") == org_a

    async def test_a_hot_new_parent_is_the_one_doubled(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        async with limiter.acquire("org-b", "gpt-4", consume={"rpm": 1}):
            pass
        await _set(repo, "org-b", wcu_tk=-10_000_000, rf=repo._now_ms())  # hot for seconds
        await repo.set_parent("user", "org-b")
        org_a = await _consumed(repo, "org-a")

        async with other.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass

        assert int((await _item(repo, "org-b", "gpt-4"))["shard_count"]["N"]) == 2
        assert int((await _item(repo, "org-a", "gpt-4"))["shard_count"]["N"]) == 1
        assert await _consumed(repo, "org-a") == org_a

    async def test_a_disabled_old_parent_does_not_outrank_the_child(self, limiter, other):
        """Review of #716, finding 5: a parent the child has left cannot 403 it."""
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        await repo.set_parent("user", "org-b")
        await _set(repo, "org-a", disabled=True)
        await _set(repo, "user", rpm_tk=0, rf=repo._now_ms())  # the child is spent

        with pytest.raises(RateLimitExceeded) as exc_info:
            async with other.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass

        assert {s.entity_id for s in exc_info.value.violations} == {"user"}


async def _set(
    repo: Repository,
    entity_id: str,
    *,
    rf: int | None = None,
    disabled: bool | None = None,
    wcu_tk: int | None = None,
    rpm_tk: int | None = None,
) -> None:
    """Rewrite a few attributes of an entity's gpt-4 shard 0, as another writer would."""
    names: dict[str, str] = {}
    values: dict[str, dict] = {}
    sets = []
    for i, (attr, value) in enumerate(
        [
            ("rf", None if rf is None else {"N": str(rf)}),
            (schema.BUCKET_FIELD_DISABLED, None if disabled is None else {"BOOL": disabled}),
            (schema.bucket_attr("wcu", "tk"), None if wcu_tk is None else {"N": str(wcu_tk)}),
            (schema.bucket_attr("rpm", "tk"), None if rpm_tk is None else {"N": str(rpm_tk)}),
        ]
    ):
        if value is not None:
            names[f"#a{i}"] = attr
            values[f":v{i}"] = value
            sets.append(f"#a{i} = :v{i}")
    client = await repo._get_client()
    await client.update_item(
        TableName=repo.table_name,
        Key={
            "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, "gpt-4", 0)},
            "SK": {"S": schema.sk_state()},
        },
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


class TestAStampedShardIsMaterialisedWhereItIs:
    """A `vu = 0` on one shard of a sharded bucket is cleared on that shard.

    Review of #716 (3rd), finding 3: the owner-check fallback stamps `vu = 0`,
    but a sharded entity's slow path was sent to another shard, so the stamped
    one kept its stale owner and failed every fast-path write that drew it.
    """

    @staticmethod
    async def _two_shards(repo: Repository) -> None:
        for resource in ("gpt-4", "llm"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("org-a")
        await repo.create_entity("org-b")
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                pass
        await repo.bump_shard_count("user", "llm", 1)
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with RateLimiter(repository=repo) as limiter:
                async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                    pass
        assert await _item(repo, "user", "llm", 1) is not None

    @staticmethod
    async def _set_shard1(repo: Repository, expression: str, values: dict) -> None:
        client = await repo._get_client()
        await client.update_item(
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "user", "llm", 1)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )

    async def test_the_owner_fallback_restamps_a_sharded_cascading_child(self, repo):
        """The vu0-sharded probe."""
        await self._two_shards(repo)
        await repo.set_parent("user", "org-b")
        # Shard 1: a create that read META before the move, its owner check failed.
        await self._set_shard1(
            repo,
            "SET parent_id = :a, pgen = :g, vu = :z",
            {":a": {"S": "org-a"}, ":g": {"N": "0"}, ":z": {"N": "0"}},
        )
        fresh = await _open_repo()
        async with RateLimiter(repository=fresh) as limiter:
            org_a = await _consumed(repo, "org-a", "llm")
            with patch("zae_limiter.repository.random.randrange", return_value=1):
                for _ in range(3):  # the first learns the count; then shard 1 is drawn
                    async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                        pass
        await fresh.close()

        shard1 = await _item(repo, "user", "llm", 1)
        assert _stamp(shard1)[:2] == (True, "org-b")
        assert schema.BUCKET_FIELD_VU not in shard1
        assert await _consumed(repo, "org-a", "llm") == org_a

    async def test_a_trimming_vu_on_a_sharded_bucket_is_cleared_there(self, repo):
        """The same route for #679's credit clamp, which main shares (not fixed there)."""
        await self._two_shards(repo)
        await self._set_shard1(repo, "SET vu = :z", {":z": {"N": "0"}})
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with RateLimiter(repository=repo) as limiter:
                async with limiter.acquire("user", "llm", consume={"rpm": 1}):
                    pass
        assert schema.BUCKET_FIELD_VU not in await _item(repo, "user", "llm", 1)

    def test_a_short_balance_is_not_a_stamp_failure(self):
        from zae_limiter.limiter import _failed_only_on_its_stamp
        from zae_limiter.models import BucketState

        def state(name, tokens):
            return BucketState(
                entity_id="user",
                resource="llm",
                limit_name=name,
                tokens_milli=tokens,
                last_refill_ms=0,
                capacity_milli=100_000,
                refill_amount_milli=100_000,
                refill_period_ms=60_000,
            )

        consume = {"rpm": 1}
        ok = [state("rpm", 5_000), state("wcu", 5_000)]
        assert _failed_only_on_its_stamp(SpeculativeResult(success=False, old_buckets=ok), consume)
        for buckets in (
            [state("rpm", 500), state("wcu", 5_000)],  # rpm short
            [state("rpm", 5_000), state("wcu", 0)],  # wcu short
            [state("wcu", 5_000)],  # rpm missing from the item
            None,  # no image
        ):
            result = SpeculativeResult(success=False, old_buckets=buckets)
            assert not _failed_only_on_its_stamp(result, consume)


class TestTheItemOverrulesTheDebitedParent:
    """The warm-path decision, on the result alone."""

    @pytest.mark.parametrize(
        ("cascade", "parent", "pgen", "debited", "expected"),
        [
            (True, "org-a", None, "org-a", False),  # agrees
            (True, "org-b", None, "org-a", True),  # moved (a pre-ADR-150 stamp still names it)
            (True, "org-b", 1, "org-a", True),  # moved
            (False, "org-a", None, "org-a", True),  # ADR-146: the policy is off
            (False, None, 1, "org-a", True),  # moved to no parent
            (False, None, None, "org-a", False),  # a pre-#684 stamp teaches nothing
            (True, "org-b", 1, None, False),  # no parallel write to compare with
        ],
    )
    def test_decision(self, cascade, parent, pgen, debited, expected):
        from zae_limiter.limiter import _item_overrules_debited_parent

        result = SpeculativeResult(
            success=True, cascade=cascade, parent_id=parent, pgen=pgen, debited_parent_id=debited
        )
        assert _item_overrules_debited_parent(result) is expected

    async def test_a_generation_stamp_without_a_parent_is_a_policy(self, repo):
        repo._learn_shard_count("user", "llm", 1, meta=(False, None), pgen=1)
        assert repo._cascade_cache[(repo._namespace_id, "user", "llm")] is False


class TestInFlightLease:
    """Usage already debited stays where it was debited (ADR-150 §7)."""

    async def test_an_adjustment_lands_on_the_old_parent(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        org_a, org_b = await _consumed(repo, "org-a"), await _consumed(repo, "org-b")

        async with other.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
            await repo.set_parent("user", "org-b")
            await lease.adjust(rpm=3)

        assert await _consumed(repo, "org-a") == org_a + 4
        assert await _consumed(repo, "org-b") == org_b

    async def test_a_rollback_lands_on_the_old_parent(self, limiter, other):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._warm(other)
        org_a = await _consumed(repo, "org-a")

        with pytest.raises(RuntimeError):
            async with other.acquire("user", "gpt-4", consume={"rpm": 2}):
                await repo.set_parent("user", "org-b")
                raise RuntimeError("the work failed")

        assert await _consumed(repo, "org-a") == org_a

    @staticmethod
    async def _warm(limiter: RateLimiter) -> None:
        async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass


class TestVersionGate:
    """A move is gated on the readers' version (ADR-150, ADR-141)."""

    @staticmethod
    async def _stamp(repo, lambda_version):
        await repo.set_version_record(
            schema_version=get_schema_version(),
            lambda_version=lambda_version,
            client_min_version="0.0.0",
        )

    async def test_refused_while_the_lambdas_predate_it(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._stamp(repo, "0.16.0")
        with patch("zae_limiter.__version__", "0.17.0"):
            with pytest.raises(VersionMismatchError) as exc_info:
                await repo.set_parent("user", "org-b")
        assert "new parent" in str(exc_info.value)
        assert exc_info.value.can_auto_update is True
        assert (await repo.get_entity("user")).parent_id == "org-a"

    async def test_admitted_and_ratcheted(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        await self._stamp(repo, "0.17.0")
        with patch("zae_limiter.__version__", "0.17.1"):
            await repo.set_parent("user", "org-b")
        assert (await repo.get_version_record())["client_min_version"] == "0.17.0"

    async def test_a_dev_build_proves_itself(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        dev = "0.16.1.dev3+gabc1234"
        await self._stamp(repo, dev)
        with patch("zae_limiter.__version__", dev):
            await repo.set_parent("user", "org-b")
        assert (await repo.get_version_record())["client_min_version"] == dev

    @pytest.mark.parametrize(
        ("found", "version", "words", "auto"),
        [
            (False, None, "no version record", False),
            (True, None, "does not say", False),
            (True, "0.16.0", "predate 0.17.0", True),
        ],
    )
    def test_the_refusal_names_the_remedy(self, found, version, words, auto):
        from zae_limiter.version import parent_move_refusal

        message, can_auto_update = parent_move_refusal(found, version)
        assert words in message
        assert can_auto_update is auto


class TestSyncTwin:
    def test_the_sync_repository_moves_an_entity(self, mock_dynamodb):
        from zae_limiter import SyncRateLimiter
        from zae_limiter.sync_repository import SyncRepository

        repo = SyncRepository(
            name="test-set-parent-sync", region="us-east-1", _skip_deprecation_warning=True
        )
        repo.create_table()
        repo._register_namespace("default")
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
        repo.set_resource_defaults("gpt-4", [RPM])
        repo.create_entity("org-a")
        repo.create_entity("org-b")
        repo.create_entity("user", parent_id="org-a", cascade=True)
        limiter = SyncRateLimiter(repository=repo)
        with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
            pass

        assert limiter.set_parent("user", "org-b") == 1

        assert [e.id for e in repo.get_children("org-b")] == ["user"]
        with limiter.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
            assert {e.entity_id for e in lease.entries} == {"user", "org-b"}
        repo.close()


async def test_a_backend_without_the_owner_check_is_skipped():
    """A third-party backend stamps no generation: nothing to check after a create."""
    from types import SimpleNamespace
    from typing import cast

    from zae_limiter.lease import Lease
    from zae_limiter.repository_protocol import RepositoryProtocol

    lease = Lease(repository=cast(RepositoryProtocol, SimpleNamespace()))
    await lease._repair_created_owner_stamps([("user", "gpt-4", 0, 0)])
