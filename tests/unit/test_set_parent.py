"""Moving an entity to a new parent (ADR-150, #677)."""

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
            await real(pk, *args)

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
