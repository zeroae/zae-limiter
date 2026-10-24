"""Moving an entity to a new parent (ADR-150, #677)."""

from unittest.mock import AsyncMock, patch

import pytest

from zae_limiter import RateLimiter, RateLimitExceeded, ResourceDisabled, __version__, schema
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

    async def test_a_failed_audit_never_hides_an_incomplete_move(self, limiter, caplog):
        """F9 (review of Stack B): the caller still learns to re-run the move."""
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _hierarchy(limiter)
        incomplete = FanoutIncomplete(1, RuntimeError("throttled"), entity_id="user")
        throttled = ClientError({"Error": {"Code": "ThrottlingException"}}, "PutItem")
        with (
            patch.object(repo, "_fanout_cascade", side_effect=incomplete),
            patch.object(repo, "_log_audit_event", side_effect=throttled),
        ):
            with pytest.raises(FanoutIncomplete) as raised:
                await repo.set_parent("user", "org-b")
        assert raised.value is incomplete
        assert "entity_parent_changed was not written" in caplog.text
        assert "ClientError" in caplog.text
        assert "user" not in caplog.text

    async def test_any_other_fan_out_failure_is_audited_and_kept(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        with patch.object(repo, "_fanout_cascade", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                await repo.set_parent("user", "org-b")
        events = await repo.get_audit_events("user")
        assert [e.details["buckets_stamped"] for e in events if e.action.endswith("changed")] == [0]

    async def test_a_failed_audit_after_a_complete_move_raises(self, limiter):
        """Like every other admin method: the move landed, its audit did not."""
        from botocore.exceptions import ClientError

        repo = limiter._repository
        await _hierarchy(limiter)
        throttled = ClientError({"Error": {"Code": "ThrottlingException"}}, "PutItem")
        with patch.object(repo, "_log_audit_event", side_effect=throttled):
            with pytest.raises(ClientError):
                await repo.set_parent("user", "org-b")
        assert (await repo.get_entity("user", consistent_read=True)).parent_id == "org-b"


class TestStaleMetaRead:
    """An eventually consistent META read from before a move cannot move the cache back.

    Without it the warm path debits the old parent, is overruled by the
    child's item and refunds it: +2 WCU per call, never an over-admission.
    """

    @staticmethod
    async def _moved_with_old_meta(limiter: RateLimiter) -> dict:
        """Move `user` from org-a to org-b; return its META from before the move."""
        repo = limiter._repository
        await _hierarchy(limiter)
        old_meta = await _meta(repo, "user")
        await repo.set_parent("user", "org-b")
        return old_meta

    @staticmethod
    def _cached_owner(repo: Repository) -> tuple[bool, str | None]:
        return repo._entity_cache[(repo._namespace_id, "user")][:2]

    @staticmethod
    async def _read_meta_as(repo: Repository, meta: dict) -> None:
        """`get_entity` answered by a replica holding ``meta``."""
        client = await repo._get_client()
        with patch.object(client, "get_item", AsyncMock(return_value={"Item": meta})):
            entity = await repo.get_entity("user")
        assert entity is not None

    @staticmethod
    async def _batch_read_meta_as(repo: Repository, meta: dict) -> None:
        """`batch_get_entity_and_buckets` answered by a replica holding ``meta``."""
        real = repo._batch_get_all

        async def stale(keys, **kwargs):
            items = await real(keys, **kwargs)
            return [meta if i["SK"]["S"] == schema.sk_meta() else i for i in items]

        with patch.object(repo, "_batch_get_all", side_effect=stale):
            entity, _buckets = await repo.batch_get_entity_and_buckets(
                "user", [("user", "gpt-4", 0)]
            )
        assert entity is not None

    @pytest.mark.parametrize("read", ["_read_meta_as", "_batch_read_meta_as"])
    async def test_an_older_replica_leaves_the_cache_on_the_new_parent(self, limiter, read):
        repo = limiter._repository
        old_meta = await self._moved_with_old_meta(limiter)
        cache_key = (repo._namespace_id, "user")
        shards = dict(repo._entity_cache[cache_key][2])
        newest = repo._owner_stamp_cache[cache_key]

        await getattr(self, read)(repo, old_meta)

        assert self._cached_owner(repo) == (True, "org-b")
        assert repo._entity_cache[cache_key][2] == shards
        assert repo._owner_stamp_cache[cache_key] == newest

    @pytest.mark.parametrize("read", ["_read_meta_as", "_batch_read_meta_as"])
    async def test_another_incarnation_still_replaces_it(self, limiter, read):
        """A recreated entity's META is not comparable, so it is never older."""
        repo = limiter._repository
        old_meta = await self._moved_with_old_meta(limiter)
        reborn = {**old_meta, schema.ENTITY_FIELD_PINC: {"S": "reborn"}}

        await getattr(self, read)(repo, reborn)

        assert self._cached_owner(repo) == (True, "org-a")
        assert repo._owner_stamp_cache[(repo._namespace_id, "user")] == ("reborn", 0)

    async def test_a_current_read_is_recorded_as_the_newest(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        repo._owner_stamp_cache.clear()

        await repo.get_entity("user")

        assert repo._owner_stamp_cache[(repo._namespace_id, "user")][1] == 0
        assert self._cached_owner(repo) == (True, "org-a")


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
                # Its owner pin is lost to the move's newer generation, so it
                # re-plans from a strongly consistent read and debits the new
                # parent (review P1), where it used to debit the old one.
                assert {e.entity_id for e in lease.entries} == {"user", "org-b"}

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
        future, and its bucket outlives the entity (a delete that missed it in
        GSI3; an acquire in flight across the delete can no longer leave one,
        since its create is pinned to META). The entity is then recreated,
        under org-b, by a correct clock (here generation 0): its generation is
        below the leftover's.
        """
        fast = other._repository
        for resource in ("gpt-4", "llm"):
            await fast.set_resource_defaults(resource, [Limit.per_minute("rpm", 3)])
        await fast.create_entity("org-a")
        await fast.create_entity("org-b")
        with patch.object(type(fast), "_creation_generation", return_value=10**13):
            await fast.create_entity("user", parent_id="org-a", cascade=True)
        slow = RateLimiter(repository=fast, speculative_writes=False)
        async with slow.acquire("user", "llm", consume={"rpm": 1}):
            pass
        client = await repo._get_client()
        await client.delete_item(  # the META goes; the bucket outlives it
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_entity(repo._namespace_id, "user")},
                "SK": {"S": schema.sk_meta()},
            },
        )
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

    @pytest.mark.parametrize("mode", ["block", "allow"])
    async def test_a_resent_write_that_keeps_conflicting_is_a_contended_rejection(
        self, limiter, mode
    ):
        """F4 (review of Stack B): the re-send after a claim retries a conflict.

        Sustained contention on it is the #724 contended 429, never a raw error
        that `on_unavailable=ALLOW` would turn into an unlimited degraded lease.
        """
        from botocore.exceptions import ClientError

        from zae_limiter import OnUnavailable

        repo = limiter._repository
        await _hierarchy(limiter)
        await self._foreign_stamp(repo)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.transact_write
        calls = []
        conflict = ClientError(
            {
                "Error": {"Code": "TransactionCanceledException", "Message": "conflict"},
                "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )

        async def conflicting_resend(items):
            calls.append(items)
            if len(calls) > 1:  # every send after the claim
                raise conflict
            return await real(items)

        with (
            patch.object(repo, "restamp_owner_if_current", return_value=True),
            patch.object(repo, "transact_write", conflicting_resend),
            patch("zae_limiter.lease.asyncio.sleep"),
        ):
            with pytest.raises(RateLimitExceeded) as raised:
                async with slow.acquire(
                    "user", "gpt-4", consume={"rpm": 1}, on_unavailable=OnUnavailable(mode)
                ):
                    pass
        assert raised.value.contended
        assert len(calls) == 1 + 4  # the first send, then the re-send and its 3 retries

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

    @staticmethod
    def _move_after_reading(repo, mover, entity_id: str, moved_to: str | None, moved: list):
        """Patch ``repo`` so the first read of ``entity_id``'s META is followed by its move."""
        real = repo.batch_get_entity_and_buckets

        async def read_then_move(owner, *args, **kwargs):
            result = await real(owner, *args, **kwargs)
            if owner == entity_id and not moved:
                moved.append(await mover.set_parent(entity_id, moved_to))
            return result

        return patch.object(repo, "batch_get_entity_and_buckets", read_then_move)

    @staticmethod
    def _record_sends(repo) -> list[list[str]]:
        """The item kinds of every transaction the repository sends."""
        sent: list[list[str]] = []
        real = repo.transact_write

        async def transact_write(items):
            sent.append([next(iter(item)) for item in items])
            return await real(items)

        repo.transact_write = transact_write
        return sent

    async def test_a_move_between_the_read_and_a_create_cancels_it(self, repo, other):
        """(a) Review of Stack B: the create is pinned to META in its own transaction.

        The pass read `user` under org-a; the move lands before the commit, so
        the META check cancels the transaction, the pass is re-planned from a
        strongly consistent read, and the bucket is born with the new parent —
        which is also the one debited. Nothing reached org-a.
        """
        mover = other._repository
        await _hierarchy(other)  # user has gpt-4 and llm buckets; none on "chat" yet
        await mover.set_resource_defaults("chat", [RPM])
        slow = RateLimiter(repository=repo, speculative_writes=False)
        moved: list[int] = []
        sent = self._record_sends(repo)

        with self._move_after_reading(repo, mover, "user", "org-b", moved):
            async with slow.acquire("user", "chat", consume={"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-b"}

        assert moved == [2]
        assert sent == [["Put", "Put", "ConditionCheck", "ConditionCheck"]] * 2
        assert _stamp(await _item(repo, "user", "chat")) == (True, "org-b", 1)
        assert await _item(repo, "org-a", "chat") is None
        assert await _consumed(repo, "org-b", "chat") == 1

    async def test_a_create_for_an_entity_with_no_meta_is_pinned_to_its_absence(self, limiter):
        """An entity created between the read (no META) and the create cancels it."""
        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [RPM])
        await repo.create_entity("org-a")
        slow = RateLimiter(repository=repo, speculative_writes=False)
        real = repo.batch_get_entity_and_buckets
        created: list[str] = []

        async def read_then_create(owner, *args, **kwargs):
            result = await real(owner, *args, **kwargs)
            if not created:
                created.append(owner)
                await repo.create_entity("user", parent_id="org-a", cascade=True)
            return result

        with patch.object(repo, "batch_get_entity_and_buckets", read_then_create):
            async with slow.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-a"}

        assert _stamp(await _item(repo, "user", "gpt-4"))[:2] == (True, "org-a")
        assert await _consumed(repo, "org-a") == 1

    async def test_a_cascade_parents_create_is_pinned_to_the_parents_meta(self, repo, other):
        """(b) The parent's own bucket carries the parent's own parent: pinned too."""
        mover = other._repository
        await mover.set_resource_defaults("chat", [RPM])
        for holding in ("holding-1", "holding-2"):
            await mover.create_entity(holding)
        await mover.create_entity("org-a", parent_id="holding-1")
        await mover.create_entity("user", parent_id="org-a", cascade=True)
        async with other.acquire("user", "chat", consume={"rpm": 1}):
            pass
        client = await repo._get_client()
        await client.delete_item(  # user keeps its bucket; org-a's must be created
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "org-a", "chat", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        slow = RateLimiter(repository=repo, speculative_writes=False)
        moved: list[int] = []
        sent = self._record_sends(repo)

        with self._move_after_reading(repo, mover, "org-a", "holding-2", moved):
            async with slow.acquire("user", "chat", consume={"rpm": 1}):
                pass

        assert moved == [0]  # org-a had no bucket to restamp yet
        assert sent == [["Update", "Put", "ConditionCheck"]] * 2
        assert _stamp(await _item(repo, "org-a", "chat")) == (False, "holding-2", 1)
        assert await _consumed(repo, "org-a", "chat") == 1

    async def test_a_multi_resource_acquire_pins_every_create(self, repo, other):
        """(c) `also=`: every part's create rides in the one pinned transaction."""
        mover = other._repository
        await _hierarchy(other)
        for resource in ("chat", "code"):
            await mover.set_resource_defaults(resource, [RPM])
        slow = RateLimiter(repository=repo, speculative_writes=False)
        moved: list[int] = []
        sent = self._record_sends(repo)
        real = repo.batch_get_entity_and_buckets
        reads: list[str] = []

        async def both_parts_read_then_move(owner, *args, **kwargs):
            result = await real(owner, *args, **kwargs)
            reads.append(owner)
            if reads.count("user") == 2 and not moved:  # each part read org-a
                moved.append(await mover.set_parent("user", "org-b"))
            return result

        with patch.object(repo, "batch_get_entity_and_buckets", both_parts_read_then_move):
            async with slow.acquire(
                "user", "chat", consume={"rpm": 1}, also={"code": {"rpm": 1}}
            ) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-b"}

        assert moved == [2]
        # One check per owner (user, its parent) for the four creates: cancelled
        # once on the META check, then re-planned.
        assert sent == [["Put", "Put", "Put", "Put", "ConditionCheck", "ConditionCheck"]] * 2
        for resource in ("chat", "code"):
            assert _stamp(await _item(repo, "user", resource)) == (True, "org-b", 1)
            assert await _item(repo, "org-a", resource) is None
            assert await _consumed(repo, "org-b", resource) == 1

    async def test_parts_that_read_different_meta_re_plan_before_sending(self, repo, other):
        """One part read before the move, the other after: no one check pins both."""
        mover = other._repository
        await _hierarchy(other)
        for resource in ("chat", "code"):
            await mover.set_resource_defaults(resource, [RPM])
        slow = RateLimiter(repository=repo, speculative_writes=False)
        moved: list[int] = []
        sent = self._record_sends(repo)
        real = repo.batch_get_entity_and_buckets
        reads: list[str] = []

        async def first_read_then_move(owner, *args, **kwargs):
            result = await real(owner, *args, **kwargs)
            reads.append(owner)
            if owner == "user" and not moved:
                moved.append(await mover.set_parent("user", "org-b"))
            return result

        with patch.object(repo, "batch_get_entity_and_buckets", first_read_then_move):
            async with slow.acquire(
                "user", "chat", consume={"rpm": 1}, also={"code": {"rpm": 1}}
            ) as lease:
                assert {e.entity_id for e in lease.entries} == {"user", "org-b"}

        assert reads.count("user") >= 2
        assert sent and all(kinds.count("ConditionCheck") == 2 for kinds in sent)
        for resource in ("chat", "code"):
            assert _stamp(await _item(repo, "user", resource)) == (True, "org-b", 1)
            assert await _item(repo, "org-a", resource) is None

    def test_creates_that_read_different_meta_cannot_share_one_check(self):
        from types import SimpleNamespace

        from zae_limiter.lease import LeaseEntry, OwnerStampLostError, _meta_checks
        from zae_limiter.models import BucketState

        def entry(resource: str, pgen: int) -> LeaseEntry:
            return LeaseEntry(
                entity_id="user",
                resource=resource,
                limit=RPM,
                state=BucketState.from_limit("user", resource, RPM, 0),
                consumed=1,
                _is_new=True,
                _owner_read=True,
                _stamp_owner=True,
                _parent_generation=pgen,
                _parent_incarnation="inc",
            )

        chat, code = entry("chat", 0), entry("code", 1)
        repo = SimpleNamespace(build_owner_check=lambda *a, **k: {"ConditionCheck": k})
        groups = [(("user", "chat", 0), [chat]), (("user", "code", 0), [code])]
        with pytest.raises(OwnerStampLostError) as raised:
            _meta_checks(repo, groups, [chat, code], 0)
        assert raised.value.contended
        assert _meta_checks(repo, groups[:1], [chat], 0) == [
            {"ConditionCheck": {"exists": True, "pgen": 0, "pinc": "inc"}}
        ]

    async def test_a_second_lost_check_is_a_contended_rejection(self, repo, other):
        """The re-plan loses again (another move): a short 429, never unavailable."""
        mover = other._repository
        await _hierarchy(other)
        await mover.set_resource_defaults("chat", [RPM])
        slow = RateLimiter(repository=repo, speculative_writes=False)
        moved: list[int] = []
        real_get = repo.get_entity

        async def consistent_read_then_move(entity_id, *, consistent_read=False):
            result = await real_get(entity_id, consistent_read=consistent_read)
            if entity_id == "user" and consistent_read and len(moved) == 1:
                moved.append(await mover.set_parent("user", "org-a"))
            return result

        with (
            self._move_after_reading(repo, mover, "user", "org-b", moved),
            patch.object(repo, "get_entity", consistent_read_then_move),
        ):
            with pytest.raises(RateLimitExceeded) as raised:
                async with slow.acquire("user", "chat", consume={"rpm": 1}):
                    pass
        assert raised.value.contended
        assert len(moved) == 2
        assert await _item(repo, "user", "chat") is None  # nothing was written

    async def test_a_parent_only_sub_lease_never_creates(self, limiter):
        """(d) The parent-only fallback writes only existing parent buckets.

        It returns None (the full slow path takes over) when the parent bucket
        is missing, so it has no create to pin; its rf-locked owner stamp is
        pinned on the parent generation like every other.
        """
        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_resource_defaults("chat", [RPM])  # no org-a bucket on it
        with patch.object(repo, "build_composite_create", side_effect=AssertionError("create")):
            assert (
                await limiter._try_parent_only_acquire(
                    "org-a", "chat", {"rpm": 1}, [], parent_shard=0, parent_shard_count=1
                )
                is None
            )

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

    async def test_an_aggregator_clone_from_before_the_move_is_skipped(self, limiter):
        """Review of Stack B: Path 2 clones copy shard 0's stream image, pinned to META.

        The image predates the move, so the clone's META check fails and the
        clone is not written; the client creates the shard lazily, from META.
        """
        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        record = await self._doubling_record(repo)
        await repo.set_parent("user", "org-b")  # lands before the lagging record

        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        assert propagate_shard_count(table, record) == 0
        assert await _item(repo, "user", "gpt-4", 1) is None

        third_repo = await _open_repo()
        async with RateLimiter(repository=third_repo) as third:
            org_a = await _consumed(repo, "org-a")
            for shard in (1, 1, 0):
                with patch("zae_limiter.repository.random.randrange", return_value=shard):
                    async with third.acquire("user", "gpt-4", consume={"rpm": 1}):
                        pass
        await third_repo.close()
        assert _stamp(await _item(repo, "user", "gpt-4", 1)) == (True, "org-b", 1)
        assert await _consumed(repo, "org-a") == org_a

    async def test_a_clone_after_a_move_to_no_parent_is_skipped_too(self, limiter):
        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        record = await self._doubling_record(repo)
        await repo.set_parent("user", None)
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        assert propagate_shard_count(table, record) == 0
        assert await _item(repo, "user", "gpt-4", 1) is None

    async def test_a_clone_with_a_current_stamp_is_created(self, limiter):
        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        await repo.set_parent("user", "org-b")
        record = await self._doubling_record(repo)  # taken after the move
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        assert propagate_shard_count(table, record) == 1
        assert _stamp(await _item(repo, "user", "gpt-4", 1)) == (True, "org-b", 1)

    async def test_an_unstamped_image_clones_only_while_nothing_moved(self, limiter):
        """An image from before ADR-150 carries no generation: META must carry none."""
        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        record = await self._doubling_record(repo)
        image = record["dynamodb"]["NewImage"]
        for attr in (schema.BUCKET_FIELD_PGEN, schema.BUCKET_FIELD_PINC):
            image.pop(attr)
        client = await repo._get_client()
        meta_key = {
            "PK": {"S": schema.pk_entity(repo._namespace_id, "user")},
            "SK": {"S": schema.sk_meta()},
        }
        await client.update_item(  # a pre-0.17 entity: neither attribute on META
            TableName=TABLE, Key=meta_key, UpdateExpression="REMOVE pgen, pinc"
        )
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        assert propagate_shard_count(table, record) == 1  # never moved: cloned

        await repo.set_parent("user", "org-b")  # pgen appears on META
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "2"}
        image["shard_count"] = {"N": "4"}
        assert propagate_shard_count(table, record) == 1  # Path 1 raises shard 1 only
        for shard in (2, 3):  # moved since: neither clone is written
            assert await _item(repo, "user", "gpt-4", shard) is None

    async def test_a_skipped_clone_is_logged_without_the_entity(self, limiter, capsys):
        import json

        import boto3

        from zae_limiter_aggregator.processor import propagate_shard_count

        repo = limiter._repository
        await _hierarchy(limiter)
        record = await self._doubling_record(repo)
        await repo.set_parent("user", "org-b")
        table = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)
        propagate_shard_count(table, record)
        logged = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.startswith("{")
        ]
        (skip,) = [e for e in logged if e["message"].startswith("Clone skipped - the owner")]
        assert skip["shard_id"] == 1
        assert "user" not in json.dumps(skip)

    def test_the_clone_owner_check_is_token_safe(self):
        from tests.unit.test_expression_tokens import assert_expression_safe
        from zae_limiter_aggregator.processor import _clone_owner_check

        for image in ({}, {"pgen": 0, "pinc": "-"}, {"pgen": 7, "pinc": "abc"}):
            check = _clone_owner_check("t", "ns", "user", image)["ConditionCheck"]
            assert_expression_safe(check)

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

    Review of #716 (3rd), finding 3: a trimming `vu = 0` (#679's credit clamp,
    the param sync) on one shard of a sharded entity sent the slow path to
    another shard, so the stamped one kept failing every fast-path write that
    drew it — and kept any stale owner stamp it carried.
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

    async def test_a_stale_stamped_shard_is_restamped_where_it_is(self, repo):
        """The vu0-sharded probe."""
        await self._two_shards(repo)
        await repo.set_parent("user", "org-b")
        # Shard 1: a stale owner stamp (a writer predating ADR-150) under a
        # trimming `vu = 0`.
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


class TestTheParentShardHint:
    """F2 (review of Stack B): a missing-shard hint belongs to the parent it came from."""

    QUOTA = Limit.quota("rpd", 100, cron="0 0 * * *")

    async def test_the_old_parents_hint_never_creates_a_new_parent_shard(self, limiter, other):
        """The cache names org-a at 4 shards; the fast path finds org-a's shard 3
        missing; META names org-b (1 shard). Honouring 3 there created org-b
        shard 3 at count 1 with a fresh full grant: 198 held + 2 used of 100."""
        repo = limiter._repository
        await repo.set_resource_defaults("gpt-4", [self.QUOTA])
        await repo.create_entity("org-a")
        await repo.create_entity("org-b")
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        async with limiter.acquire("user", "gpt-4", consume={"rpd": 1}):
            pass  # user cached under org-a
        async with other.acquire("org-b", "gpt-4", consume={"rpd": 1}):
            pass  # org-b's shard 0 exists
        ns = repo._namespace_id
        cascade, parent, shards = repo._entity_cache[(ns, "org-a")]
        repo._entity_cache[(ns, "org-a")] = (cascade, parent, {**shards, "gpt-4": 4})

        await other._repository.set_parent("user", "org-b")
        client = await repo._get_client()
        await client.update_item(  # the child's next fast-path write fails on its stamp
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(ns, "user", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET vu = :z",
            ExpressionAttributeValues={":z": {"N": "0"}},
        )
        # This process has not seen the move.
        repo._entity_cache[(ns, "user")] = (True, "org-a", repo._entity_cache[(ns, "user")][2])

        import zae_limiter.repository as repository_module

        real = repository_module.random.randrange

        def last(n, *args, **kwargs):
            return n - 1 if not args else real(n, *args, **kwargs)

        with patch.object(repository_module.random, "randrange", side_effect=last):
            async with limiter.acquire("user", "gpt-4", consume={"rpd": 1}) as lease:
                assert {(e.entity_id, e._shard_id) for e in lease.entries} == {
                    ("user", 0),
                    ("org-b", 0),
                }

        assert await _item(repo, "org-b", "gpt-4", 3) is None
        org_b = await _item(repo, "org-b", "gpt-4", 0)
        tk = int(org_b[schema.bucket_attr("rpd", "tk")]["N"])
        assert tk == 98_000, "org-b holds its allowance less the two it admitted"

    def test_a_hinted_shard_past_the_count_is_redrawn(self, limiter):
        repo = limiter._repository
        with patch("zae_limiter.repository.random.randrange", return_value=0):
            assert limiter._slow_path_shard("org-b", "gpt-4", 3) == (0, 1)
            assert limiter._slow_path_shard("org-b", "gpt-4", 3, 2) == (0, 2)
        assert limiter._slow_path_shard("org-b", "gpt-4", 3, 4) == (3, 4)  # a doubling's draw
        repo._entity_cache[(repo._namespace_id, "org-b")] = (False, None, {"gpt-4": 4})
        assert limiter._slow_path_shard("org-b", "gpt-4", 3) == (3, 4)  # ADR-133: in range


class TestTheDisabledLookAcrossResources:
    """F5 (review of Stack B): the multi-resource 403 names the parent META names now."""

    async def test_a_stale_read_of_the_old_parent_cannot_hide_the_new_ones_403(self, limiter):
        repo = limiter._repository
        for resource in ("chat", "code"):
            await repo.set_resource_defaults(resource, [RPM])
        await repo.create_entity("org-a")
        await repo.create_entity("org-b")
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        await repo.set_limits("user", [Limit.per_day("rpm", 1)], resource="chat")
        async with limiter.acquire("user", "chat", consume={"rpm": 1}):
            pass  # user/chat is now spent: a fast-path 429
        stale = await repo.get_entity("user", consistent_read=True)
        await repo.set_parent("user", "org-b")
        await repo.disable_entity("org-b", resource="code")
        real = repo.get_entity

        async def stale_unless_consistent(entity_id, *, consistent_read=False):
            if entity_id == "user" and not consistent_read:
                return stale  # an eventually consistent replica from before the move
            return await real(entity_id, consistent_read=consistent_read)

        with patch.object(repo, "get_entity", stale_unless_consistent):
            with pytest.raises(ResourceDisabled) as raised:
                async with limiter.acquire(
                    "user", "chat", consume={"rpm": 1}, also={"code": {"rpm": 1}}
                ):
                    pytest.fail("a disabled parent must not admit")
        assert (raised.value.entity_id, raised.value.resource) == ("org-b", "code")


class TestMetaWritesRetryAConflict:
    """Review P2: every bucket create holds a `ConditionCheck` on its owner's
    META in a transaction (ADR-150), so a META write on a busy entity can meet
    a `TransactionConflictException`. It did not apply; it is re-sent rather
    than surfaced raw."""

    @staticmethod
    def _conflict_once(real):
        from botocore.exceptions import ClientError

        calls: list[int] = []

        async def flaky(**kwargs):
            on_meta = kwargs.get("Key", kwargs.get("Item", {})).get("SK") == {"S": schema.sk_meta()}
            if not on_meta:
                return await real(**kwargs)
            calls.append(1)
            if len(calls) == 1:
                raise ClientError(
                    {"Error": {"Code": "TransactionConflictException", "Message": "busy"}},
                    "Write",
                )
            return await real(**kwargs)

        return flaky, calls

    async def test_set_parent_retries(self, limiter):
        await _hierarchy(limiter)
        repo = limiter._repository
        client = await repo._get_client()
        flaky, calls = self._conflict_once(client.update_item)
        with patch.object(client, "update_item", side_effect=flaky):
            await repo.set_parent("user", "org-b")
        assert len(calls) == 2  # the conflict, then the write that landed
        assert (await repo.get_entity("user", consistent_read=True)).parent_id == "org-b"

    async def test_create_entity_retries(self, repo):
        client = await repo._get_client()
        flaky, calls = self._conflict_once(client.put_item)
        with patch.object(client, "put_item", side_effect=flaky):
            await repo.create_entity("fresh")
        assert len(calls) == 2
        assert await repo.get_entity("fresh", consistent_read=True) is not None


class TestAStaleSlowPassAfterAMove:
    """Review P1 (#677 review): a slow pass whose eventually consistent META
    read predates a move lost its rf-locked owner pin — the bucket already
    carries the move's newer generation — and the consumption-only retry then
    debited the old parent, never the new one, in every process while META
    reads lagged. It now re-plans once from a strongly consistent read."""

    async def test_the_new_parent_is_debited(self, limiter, other):
        await _hierarchy(limiter)
        repo = limiter._repository
        stale = await repo.get_entity("user", consistent_read=True)
        await repo.set_parent("user", "org-b")
        other_repo = other._repository
        real = other_repo.batch_get_entity_and_buckets

        async def stale_meta(entity_id, bucket_keys):
            entity, buckets = await real(entity_id, bucket_keys)
            return (stale if entity_id == "user" else entity), buckets

        before = {org: await _consumed(repo, org) for org in ("org-a", "org-b")}
        slow = RateLimiter(repository=other_repo, speculative_writes=False)
        with patch.object(other_repo, "batch_get_entity_and_buckets", stale_meta):
            async with slow.acquire("user", "gpt-4", consume={"rpm": 5}):
                pass
        assert await _consumed(repo, "org-b") - before["org-b"] == 5
        assert await _consumed(repo, "org-a") == before["org-a"]
        assert _stamp(await _item(repo, "user", "gpt-4"))[1] == "org-b"


class TestAnOlderStampNeverMovesTheCacheBack:
    """F7 (review of Stack B): mid fan-out, shards carry two generations."""

    async def test_alternating_draws_stay_on_the_newest_parent(self, repo):
        await repo.set_resource_defaults("gpt-4", [RPM])
        for org in ("org-a", "org-b", "org-c"):
            await repo.create_entity(org)
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
            async with limiter.acquire("org-b", "gpt-4", consume={"rpm": 1}):
                pass  # org-b has a bucket: a debit to it would land
        await repo.bump_shard_count("user", "gpt-4", 1)
        with patch("zae_limiter.repository.random.randrange", return_value=1):
            async with RateLimiter(repository=repo) as limiter:
                async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                    pass
        await repo.set_parent("user", "org-b")  # generation 1 on both shards
        await repo.set_parent("user", "org-c")  # generation 2 on both shards

        fresh = await _open_repo()
        ns = fresh._namespace_id
        async with RateLimiter(repository=fresh) as limiter:
            for shard in (0, 1):  # this process learns generation 2, org-c
                with patch("zae_limiter.repository.random.randrange", return_value=shard):
                    async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                        pass
            assert fresh._entity_cache[(ns, "user")][1] == "org-c"
            client = await repo._get_client()
            await client.update_item(  # shard 0 as a fan-out that has not reached it
                TableName=TABLE,
                Key={
                    "PK": {"S": schema.pk_bucket(ns, "user", "gpt-4", 0)},
                    "SK": {"S": schema.sk_state()},
                },
                UpdateExpression="SET parent_id = :b, pgen = :g",
                ExpressionAttributeValues={":b": {"S": "org-b"}, ":g": {"N": "1"}},
            )
            org_b, org_c = await _consumed(repo, "org-b"), await _consumed(repo, "org-c")
            refunds: list[str] = []
            real = limiter._compensate_speculative

            async def spy(entity_id, *args, **kwargs):
                refunds.append(entity_id)
                return await real(entity_id, *args, **kwargs)

            with patch.object(limiter, "_compensate_speculative", spy):
                for shard in (0, 1, 0, 1):
                    with patch("zae_limiter.repository.random.randrange", return_value=shard):
                        async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                            pass

            assert refunds == [], "an older stamp is not a reason to refund"
            assert fresh._entity_cache[(ns, "user")][1] == "org-c"
            assert fresh._owner_stamp_cache[(ns, "user")][1] == 2
        await fresh.close()
        assert await _consumed(repo, "org-b") == org_b
        assert await _consumed(repo, "org-c") == org_c + 4

    async def test_another_incarnation_is_never_older(self, repo):
        repo._owner_stamp_cache[(repo._namespace_id, "user")] = ("inc-1", 5)
        assert repo._stamp_is_outdated("user", "inc-1", 4)
        assert not repo._stamp_is_outdated("user", "inc-1", 5)
        assert not repo._stamp_is_outdated("user", "inc-2", 0)
        assert not repo._stamp_is_outdated("user", "inc-1", None)
        assert not repo._stamp_is_outdated("nobody", "inc-1", 0)

    async def test_a_move_in_this_process_records_its_stamp_for_every_scope(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        scoped = await repo.namespace("default")
        assert scoped._owner_stamp_cache is repo._owner_stamp_cache
        await repo.set_parent("user", "org-b")
        entity = await repo.get_entity("user", consistent_read=True)
        assert scoped._owner_stamp_cache[(repo._namespace_id, "user")] == (
            entity.incarnation,
            1,
        )


def _cancelled(codes: list[str]):
    """A cancelled transaction with these per-item reasons."""
    from botocore.exceptions import ClientError

    return ClientError(
        {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": code} for code in codes],
        },
        "TransactWriteItems",
    )


def _meta_check_lost(items: list[dict]) -> list[str]:
    """Reasons for a transaction whose META checks failed and nothing else did."""
    return ["ConditionalCheckFailed" if "ConditionCheck" in item else "None" for item in items]


def _update_lost(items: list[dict]) -> list[str]:
    """Reasons for a transaction whose rf-locked update lost its lock."""
    return ["ConditionalCheckFailed" if "Update" in item else "None" for item in items]


class TestEverySendKeepsTheMetaPin:
    """Each send of a commit that creates a bucket routes a lost META check to a re-plan."""

    @staticmethod
    async def _child_kept_parent_created(limiter) -> Repository:
        """user/gpt-4 exists; org-a/gpt-4 must be created (a check on org-a)."""
        repo = limiter._repository
        await _hierarchy(limiter)
        client = await repo._get_client()
        await client.delete_item(
            TableName=TABLE,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, "org-a", "gpt-4", 0)},
                "SK": {"S": schema.sk_state()},
            },
        )
        return repo

    @staticmethod
    def _scripted(repo, *failures):
        """Fail the first sends with these reason builders, then send for real."""
        real = repo.transact_write
        sent: list[list[str]] = []

        async def transact_write(items):
            sent.append([next(iter(item)) for item in items])
            if len(sent) <= len(failures):
                raise _cancelled(failures[len(sent) - 1](items))
            return await real(items)

        return sent, patch.object(repo, "transact_write", transact_write)

    async def test_the_re_send_after_a_claim_keeps_its_pin(self, limiter):
        from unittest.mock import AsyncMock

        from zae_limiter.lease import Lease

        repo = await self._child_kept_parent_created(limiter)
        sent, scripted = self._scripted(repo, _update_lost, _meta_check_lost)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        with scripted, patch.object(Lease, "_claim_owner_stamps", AsyncMock(return_value=True)):
            async with slow.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
        # The send, the re-send after the claim (lost its META check), the re-plan.
        assert sent == [["Update", "Put", "ConditionCheck"]] * 3
        assert await _item(repo, "org-a", "gpt-4") is not None

    async def test_the_retry_keeps_its_pin(self, limiter):
        repo = await self._child_kept_parent_created(limiter)
        await repo.set_resource_defaults("chat", [RPM])
        # The child's chat bucket is created; the parent's exists.
        async with RateLimiter(repository=repo).acquire("org-a", "chat", consume={"rpm": 1}):
            pass
        sent, scripted = self._scripted(repo, _update_lost, _meta_check_lost)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        with scripted:
            async with slow.acquire("user", "chat", consume={"rpm": 1}):
                pass
        # The send (the parent's lock lost), the retry re-issuing the innocent
        # create with its check (lost), and the re-plan.
        assert sent[:2] == [
            ["Put", "Update", "ConditionCheck"],
            ["Put", "Update", "ConditionCheck"],
        ]
        assert len(sent) == 3
        assert await _item(repo, "user", "chat") is not None

    async def test_a_multi_resource_commit_that_loses_twice_is_contended(self, limiter):
        repo = limiter._repository
        await _hierarchy(limiter)
        for resource in ("chat", "code"):
            await repo.set_resource_defaults(resource, [RPM])
        sent, scripted = self._scripted(repo, _meta_check_lost, _meta_check_lost)
        slow = RateLimiter(repository=repo, speculative_writes=False)
        with scripted:
            with pytest.raises(RateLimitExceeded) as raised:
                async with slow.acquire(
                    "user", "chat", consume={"rpm": 1}, also={"code": {"rpm": 1}}
                ):
                    pytest.fail("a lost META check must not admit")
        assert raised.value.contended
        assert len(sent) == 2
        assert await _item(repo, "user", "chat") is None


class TestAnOlderStampOnTheSinglePath:
    """F7 on the single-item path: an outdated stamp never names a parent to debit."""

    async def test_a_move_to_no_parent_is_not_undone_by_an_older_shard(self, repo):
        await repo.set_resource_defaults("gpt-4", [RPM])
        for org in ("org-a", "org-b"):
            await repo.create_entity(org)
        await repo.create_entity("user", parent_id="org-a", cascade=True)
        async with RateLimiter(repository=repo) as limiter:
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass
            async with limiter.acquire("org-b", "gpt-4", consume={"rpm": 1}):
                pass
        await repo.set_parent("user", "org-b")  # generation 1
        await repo.set_parent("user", None)  # generation 2: no parent, no cascade

        fresh = await _open_repo()
        ns = fresh._namespace_id
        async with RateLimiter(repository=fresh) as limiter:
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}):
                pass  # learns generation 2, no parent
            assert fresh._entity_cache[(ns, "user")][:2] == (False, None)
            client = await repo._get_client()
            await client.update_item(  # as a fan-out that has not reached it yet
                TableName=TABLE,
                Key={
                    "PK": {"S": schema.pk_bucket(ns, "user", "gpt-4", 0)},
                    "SK": {"S": schema.sk_state()},
                },
                UpdateExpression="SET #c = :t, parent_id = :b, pgen = :g",
                ExpressionAttributeNames={"#c": "cascade"},
                ExpressionAttributeValues={
                    ":t": {"BOOL": True},
                    ":b": {"S": "org-b"},
                    ":g": {"N": "1"},
                },
            )
            org_b = await _consumed(repo, "org-b")
            async with limiter.acquire("user", "gpt-4", consume={"rpm": 1}) as lease:
                assert {e.entity_id for e in lease.entries} == {"user"}
            assert fresh._entity_cache[(ns, "user")][:2] == (False, None)
        await fresh.close()
        assert await _consumed(repo, "org-b") == org_b
