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

from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest

from zae_limiter import Limit
from zae_limiter.schema import pk_system, sk_entity_config_resources
from zae_limiter.sync_repository import SyncRepository
from zae_limiter_provisioner.applier import _build_change_item
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
