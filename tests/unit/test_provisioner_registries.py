"""The provisioner keeps the registries the CLI's list commands read.

`resource list`, `entity list-resources` and `entity list --with-custom-limits`
do not scan config items: they read the ``#RESOURCES`` set, the
``#ENTITY_CONFIG_RESOURCES`` ref counts and the sparse GSI3 entry on entity
config items. The `Repository` setters maintain all three; the provisioner
maintained none, so limits applied from a manifest were enforced but every
list command reported none.

An entity config item is counted exactly when it carries ``GSI3PK``, on both
sides, which is what lets an apply (and the API) repair items an older
provisioner wrote without either.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError

from zae_limiter import Limit
from zae_limiter.schema import pk_system, sk_entity_config_resources
from zae_limiter.sync_repository import SyncRepository
from zae_limiter_provisioner import applier
from zae_limiter_provisioner.applier import _build_change_item, apply_changes
from zae_limiter_provisioner.differ import Change
from zae_limiter_provisioner.handler import on_event

TABLE = "prov-registries"
RESOURCE = "gpt-4"
# `.`, `/` and `:` are legal in a resource name and illegal in expression text.
ODD_RESOURCE = "openai/gpt-4o.mini:2024"


@pytest.fixture
def repo(mock_dynamodb):
    setup = SyncRepository(name=TABLE, region="us-east-1", _skip_deprecation_warning=True)
    setup.create_table()
    setup._register_namespace("default")
    setup._register_namespace("tenant-x")
    scoped = setup.namespace("tenant-x")
    yield scoped
    setup.close()


def _rpm(capacity: int) -> dict[str, Any]:
    return {"limits": {"rpm": {"capacity": capacity}}}


def _manifest(resources=(RESOURCE,), entities: dict[str, tuple[str, ...]] | None = None):
    return {
        "namespace": "tenant-x",
        "resources": {name: _rpm(500) for name in resources},
        "entities": {
            entity: {"resources": {res: _rpm(1000) for res in held}}
            for entity, held in (entities or {}).items()
        },
    }


def _apply(repo: SyncRepository, manifest: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = on_event(
        {
            "action": "apply",
            "table_name": TABLE,
            "namespace_id": repo._namespace_id,
            "manifest": manifest,
            **extra,
        },
        MagicMock(),
    )
    assert result["errors"] == []
    return result


def _counts(repo: SyncRepository) -> dict[str, int]:
    """The raw #ENTITY_CONFIG_RESOURCES ref counts, zero counts included."""
    item = (
        boto3.client("dynamodb")
        .get_item(
            TableName=TABLE,
            Key={
                "PK": {"S": pk_system(repo._namespace_id)},
                "SK": {"S": sk_entity_config_resources()},
            },
            ConsistentRead=True,
        )
        .get("Item", {})
    )
    return {k: int(v["N"]) for k, v in item.items() if "N" in v}


def _entities(repo: SyncRepository, resource: str) -> list[str]:
    return sorted(repo.list_entities_with_custom_limits(resource)[0])


def _full_rpm(capacity: int) -> dict[str, Any]:
    """The same limit as `_rpm`, in the expanded form a `Change` carries."""
    return {
        "limits": {"rpm": {"capacity": capacity, "refill_amount": capacity, "refill_period": 60}}
    }


def _write_as_older_provisioner(repo: SyncRepository, change: Change) -> None:
    """A config item as a provisioner that kept no registry wrote it: no GSI3, no counts."""
    _pk, _sk, item = _build_change_item(repo._namespace_id, change)
    item.pop("GSI3PK", None)
    item.pop("GSI3SK", None)
    boto3.client("dynamodb").put_item(TableName=TABLE, Item=item)


class TestApplyKeepsTheRegistries:
    def test_applied_limits_are_listed(self, repo):
        _apply(
            repo,
            _manifest(
                resources=(RESOURCE, ODD_RESOURCE),
                entities={"user-1": (RESOURCE, "_default_"), "user-2": (ODD_RESOURCE,)},
            ),
        )

        assert repo.list_resources_with_defaults() == sorted([RESOURCE, ODD_RESOURCE])
        assert repo.list_resources_with_entity_configs() == sorted(
            ["_default_", RESOURCE, ODD_RESOURCE]
        )
        assert _entities(repo, RESOURCE) == ["user-1"]
        assert _entities(repo, ODD_RESOURCE) == ["user-2"]
        assert _entities(repo, "_default_") == ["user-1"]

    def test_an_update_does_not_count_again(self, repo):
        _apply(repo, _manifest(entities={"user-1": (RESOURCE,), "user-2": (RESOURCE,)}))
        updated = _manifest(entities={"user-1": (RESOURCE,), "user-2": (RESOURCE,)})
        updated["entities"]["user-1"]["resources"][RESOURCE] = _rpm(2000)
        result = _apply(repo, updated)

        assert result["updated"] == 1
        assert _counts(repo) == {RESOURCE: 2}

    def test_a_full_apply_does_not_count_again(self, repo):
        manifest = _manifest(entities={"user-1": (RESOURCE,)})
        _apply(repo, manifest)
        _apply(repo, manifest, full=True)

        assert _counts(repo) == {RESOURCE: 1}
        assert repo.list_resources_with_defaults() == [RESOURCE]

    def test_removed_levels_are_unlisted_once_nothing_holds_them(self, repo):
        _apply(repo, _manifest(entities={"user-1": (RESOURCE,), "user-2": (RESOURCE,)}))

        _apply(repo, _manifest(entities={"user-2": (RESOURCE,)}))
        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["user-2"]

        _apply(repo, {"namespace": "tenant-x"})
        assert _counts(repo) == {}
        assert repo.list_resources_with_entity_configs() == []
        assert repo.list_resources_with_defaults() == []
        assert _entities(repo, RESOURCE) == []

    def test_the_api_and_the_manifest_share_one_count(self, repo):
        repo.set_limits("api-user", [Limit.per_minute("rpm", 5)], resource=RESOURCE)
        _apply(repo, _manifest(entities={"user-1": (RESOURCE,)}))
        assert _counts(repo) == {RESOURCE: 2}

        _apply(repo, _manifest())
        assert repo.list_resources_with_entity_configs() == [RESOURCE]
        assert _entities(repo, RESOURCE) == ["api-user"]

        repo.delete_limits("api-user", resource=RESOURCE)
        assert repo.list_resources_with_entity_configs() == []


class TestItemsAnOlderProvisionerWrote:
    """Upgrading the Lambda must repair what it left behind, never double count."""

    def _seed(self, repo: SyncRepository) -> None:
        _write_as_older_provisioner(repo, Change("create", "resource", RESOURCE, _full_rpm(500)))
        _write_as_older_provisioner(
            repo, Change("create", "entity", f"user-1/{RESOURCE}", _full_rpm(1000))
        )

    def test_a_change_only_apply_repairs_entity_levels(self, repo):
        self._seed(repo)
        result = _apply(repo, _manifest(entities={"user-1": (RESOURCE,)}))

        # The entity item lacks only its GSI3 entry, so it differs and is
        # rewritten; the resource item is identical and is left alone.
        assert [(c["level"], c["target"]) for c in result["changes"]] == [
            ("entity", f"user-1/{RESOURCE}")
        ]
        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["user-1"]
        assert repo.list_resources_with_defaults() == []

    def test_a_full_apply_repairs_every_level(self, repo):
        self._seed(repo)
        _apply(repo, _manifest(entities={"user-1": (RESOURCE,)}), full=True)

        assert repo.list_resources_with_defaults() == [RESOURCE]
        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["user-1"]

    def test_deleting_an_uncounted_item_takes_nothing_off_the_count(self, repo):
        self._seed(repo)
        repo.set_limits("api-user", [Limit.per_minute("rpm", 5)], resource=RESOURCE)

        _apply(repo, _manifest())  # user-1 removed from the manifest

        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["api-user"]

    def test_the_api_counts_an_uncounted_item_it_rewrites(self, repo):
        self._seed(repo)
        repo.set_limits("user-1", [Limit.per_minute("rpm", 7)], resource=RESOURCE)

        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["user-1"]

    def test_the_api_counts_an_uncounted_item_it_disables(self, repo):
        self._seed(repo)
        repo.disable_entity("user-1", resource=RESOURCE)

        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["user-1"]

    def test_the_api_deletes_an_uncounted_item_without_a_decrement(self, repo):
        self._seed(repo)
        repo.set_limits("api-user", [Limit.per_minute("rpm", 5)], resource=RESOURCE)

        repo.delete_limits("user-1", resource=RESOURCE)

        assert repo.get_limits("user-1", resource=RESOURCE) == []
        assert _counts(repo) == {RESOURCE: 1}
        assert _entities(repo, RESOURCE) == ["api-user"]


def _client_error(code: str, *reasons: str) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": code}}
    if reasons:
        response["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(response, "Write")


_ENTITY = Change("update", "entity", f"user-1/{RESOURCE}", _full_rpm(10))
_COUNTED = {"GSI3PK": {"S": "ns/ENTITY_CONFIG#gpt-4"}, "l_rpm_cp": {"N": "5"}}


class TestCountedWritesUnderContention:
    """Lost conditions re-read and retry; anything else is reported, never swallowed."""

    def test_a_create_that_loses_to_a_counted_one_replaces_it_uncounted(self):
        client = MagicMock()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException", "ConditionalCheckFailed", "None"
        )
        client.get_item.return_value = {"Item": _COUNTED}
        client.put_item.return_value = {"Attributes": _COUNTED}

        result = apply_changes([_ENTITY], TABLE, "ns", client, stored={id(_ENTITY): {}})

        assert result.errors == []
        assert client.transact_write_items.call_count == 1
        assert client.put_item.call_args.kwargs["ConditionExpression"] == "attribute_exists(#g3)"

    def test_a_transaction_conflict_is_retried(self):
        client = MagicMock()
        client.transact_write_items.side_effect = [
            _client_error("TransactionCanceledException", "None", "TransactionConflict"),
            {},
        ]
        client.get_item.return_value = {}

        result = apply_changes([_ENTITY], TABLE, "ns", client, stored={id(_ENTITY): {}})

        assert result.errors == []
        assert client.transact_write_items.call_count == 2

    @pytest.mark.parametrize(
        "error",
        [
            _client_error("TransactionCanceledException", "ValidationError", "None"),
            _client_error("TransactionCanceledException"),
            _client_error("ProvisionedThroughputExceededException"),
        ],
    )
    def test_any_other_failure_is_reported(self, error):
        client = MagicMock()
        client.transact_write_items.side_effect = error

        result = apply_changes([_ENTITY], TABLE, "ns", client, stored={id(_ENTITY): {}})

        assert len(result.errors) == 1
        assert client.transact_write_items.call_count == 1

    def test_a_write_that_never_settles_gives_up_and_says_so(self):
        client = MagicMock()
        client.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException", "ConditionalCheckFailed", "None"
        )
        # Each read says the other path applies: counted, then not.
        client.get_item.side_effect = [{}, {"Item": _COUNTED}, {}]

        result = apply_changes([_ENTITY], TABLE, "ns", client)

        assert result.errors == [
            f"update entity user-1/{RESOURCE}: entity config user-1/{RESOURCE} kept "
            "changing under the write; nothing was written, re-run the apply"
        ]

    def test_a_counted_delete_that_finds_the_item_gone_deletes_nothing(self):
        delete = Change("delete", "entity", f"user-1/{RESOURCE}")
        client = MagicMock()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException", "ConditionalCheckFailed", "None"
        )
        client.get_item.return_value = {}
        client.delete_item.return_value = {}

        result = apply_changes([delete], TABLE, "ns", client, stored={id(delete): _COUNTED})

        assert result.errors == []
        assert client.delete_item.call_count == 1
        client.update_item.assert_not_called()  # no decrement, no cleanup

    def test_a_delete_that_never_settles_gives_up_and_says_so(self):
        delete = Change("delete", "entity", f"user-1/{RESOURCE}")
        client = MagicMock()
        client.delete_item.side_effect = _client_error("ConditionalCheckFailedException")
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException", "ConditionalCheckFailed", "None"
        )
        client.get_item.side_effect = [{"Item": _COUNTED}, {}, {"Item": _COUNTED}]

        result = apply_changes([delete], TABLE, "ns", client)

        assert result.errors == [
            f"delete entity user-1/{RESOURCE}: entity config user-1/{RESOURCE} kept "
            "changing under the delete; nothing was deleted, re-run the apply"
        ]

    def test_a_delete_that_raises_otherwise_is_reported(self):
        delete = Change("delete", "entity", f"user-1/{RESOURCE}")
        client = MagicMock()
        client.transact_write_items.side_effect = _client_error("InternalServerError")

        result = apply_changes([delete], TABLE, "ns", client, stored={id(delete): _COUNTED})

        assert len(result.errors) == 1

    def test_a_failed_registry_tidy_is_logged_not_reported(self, caplog):
        delete = Change("delete", "entity", f"user-1/{RESOURCE}")
        client = MagicMock()
        client.update_item.side_effect = _client_error("InternalServerError")

        with caplog.at_level("WARNING"):
            result = apply_changes([delete], TABLE, "ns", client, stored={id(delete): _COUNTED})

        assert result.errors == []
        assert result.deleted == 1
        assert "Could not tidy the entity config registry" in caplog.text

    def test_a_tidy_that_finds_the_count_still_positive_is_quiet(self, caplog):
        delete = Change("delete", "entity", f"user-1/{RESOURCE}")
        client = MagicMock()
        client.update_item.side_effect = _client_error("ConditionalCheckFailedException")

        with caplog.at_level("WARNING"):
            result = apply_changes([delete], TABLE, "ns", client, stored={id(delete): _COUNTED})

        assert result.errors == []
        assert "Could not tidy" not in caplog.text


class _ConflictingClient:
    """A DynamoDB client that cancels overlapping transactions, as DynamoDB does.

    Every counted transaction updates the namespace's one registry item, and
    DynamoDB cancels a transaction on an item another one holds
    (`TransactionConflict`). moto never does, so this stands in for it.
    """

    def __init__(self, inner: Any, *, wait_for_peer: bool = False) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._in_flight = 0
        self.conflicts = 0
        # Hold the first transaction until a second arrives, so unserialized
        # writers overlap by construction rather than by timing.
        self._wait_for_peer = wait_for_peer
        self._peer_arrived = threading.Event()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def transact_write_items(self, **kwargs: Any) -> Any:
        with self._lock:
            self._in_flight += 1
            overlapping = self._in_flight > 1
            if overlapping:
                self._peer_arrived.set()
        try:
            if self._wait_for_peer and not overlapping:
                self._peer_arrived.wait(timeout=5)
            time.sleep(0.005)
            if overlapping:
                with self._lock:
                    self.conflicts += 1
                raise _client_error("TransactionCanceledException", "None", "TransactionConflict")
            return self._inner.transact_write_items(**kwargs)
        finally:
            with self._lock:
                self._in_flight -= 1


def _entity_creates(n: int) -> list[Change]:
    return [
        Change(
            "create", "entity", f"user-{i}/{RESOURCE if i % 2 else ODD_RESOURCE}", _full_rpm(i + 1)
        )
        for i in range(n)
    ]


class TestConcurrentCountedWrites:
    """An apply's 8 config writers share one registry item (measured on AWS: 134 of
    640 entity creates gave up before counted transactions were serialized)."""

    def test_a_concurrent_initial_apply_counts_every_entity(self, repo, monkeypatch):
        monkeypatch.setattr(applier, "_WRITE_WORKERS", 8)
        client = _ConflictingClient(boto3.client("dynamodb"))
        changes = _entity_creates(40)

        result = apply_changes(
            changes, TABLE, repo._namespace_id, client, stored={id(c): {} for c in changes}
        )

        assert result.errors == []
        assert result.created == 40
        assert client.conflicts == 0
        assert _counts(repo) == {RESOURCE: 20, ODD_RESOURCE: 20}
        assert len(_entities(repo, RESOURCE)) == 20

    def test_the_simulator_cancels_unserialized_transactions(self, repo, monkeypatch):
        """Without the lock the same apply conflicts, so the test above can fail."""
        monkeypatch.setattr(applier, "_WRITE_WORKERS", 8)
        monkeypatch.setattr(applier, "_COUNTED_TRANSACTION_LOCK", contextlib.nullcontext())
        monkeypatch.setattr(applier.time, "sleep", lambda _s: None)
        client = _ConflictingClient(boto3.client("dynamodb"), wait_for_peer=True)
        changes = _entity_creates(40)

        apply_changes(
            changes, TABLE, repo._namespace_id, client, stored={id(c): {} for c in changes}
        )

        assert client.conflicts > 0


class TestCountedTransact:
    """Conflicts with other writers are retried with jitter; anything else is not."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        self.sleeps: list[float] = []
        monkeypatch.setattr(applier.time, "sleep", self.sleeps.append)

    def test_a_conflict_is_retried_with_backoff(self):
        client = MagicMock()
        conflict = _client_error("TransactionCanceledException", "None", "TransactionConflict")
        client.transact_write_items.side_effect = [conflict, conflict, {}]

        applier._counted_transact(client, [])

        assert client.transact_write_items.call_count == 3
        assert len(self.sleeps) == 2
        assert all(0 <= s <= applier._CONFLICT_BACKOFF_CAP_S for s in self.sleeps)

    def test_a_lost_condition_is_not_retried_here(self):
        client = MagicMock()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException", "ConditionalCheckFailed", "TransactionConflict"
        )

        with pytest.raises(ClientError):
            applier._counted_transact(client, [])
        assert client.transact_write_items.call_count == 1

    def test_a_conflict_that_persists_is_raised(self):
        client = MagicMock()
        client.transact_write_items.side_effect = _client_error(
            "TransactionCanceledException", "None", "TransactionConflict"
        )

        with pytest.raises(ClientError):
            applier._counted_transact(client, [])
        assert client.transact_write_items.call_count == applier._CONFLICT_RETRIES + 1

    def test_a_throttled_transaction_is_retried(self):
        """botocore does not retry a cancellation, so a throttle would fail the change."""
        client = MagicMock()
        client.transact_write_items.side_effect = [
            _client_error("TransactionCanceledException", "None", "ThrottlingError"),
            {},
        ]

        applier._counted_transact(client, [])

        assert client.transact_write_items.call_count == 2

    def test_the_follow_up_runs_once_the_transaction_lands(self):
        client = MagicMock()
        ran: list[bool] = []

        applier._counted_transact(client, [], then=lambda: ran.append(True))

        assert ran == [True]

    def test_a_tidy_that_fails_any_way_after_the_delete_is_logged(self, caplog):
        """The delete and its −1 landed; a network error tidying up is not its failure."""
        client = MagicMock()
        client.update_item.side_effect = ConnectionError("reset by peer")

        applier._cleanup_entity_registry(client, TABLE, "ns", RESOURCE)

        assert "Could not tidy the entity config registry" in caplog.text
