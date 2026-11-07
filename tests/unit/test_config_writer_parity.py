"""The two config writers leave the same table.

Limit config has two writers: the `Repository` setters (`set_system_defaults`,
`set_resource_defaults`, `set_limits` and their deletes) and the provisioner
Lambda's applier, which re-implements them with raw boto3 calls because the
Lambda zip carries only a stub of `zae_limiter`. Nothing compared the two, so
every side effect the setters had and the applier lacked was found one bug at a
time: `bx` (fd4beefe), schedules (#549), GSI4SK (#777), and the three registries
the list commands read (#797), which left a manifest's limits enforced and
listed nowhere for eight months.

This test writes the same declarations through both, one namespace each, and
compares every item the two namespaces hold, attribute by attribute, after every
step of a create / update / delete sequence. An item or attribute only one
writer produces fails it, unless `EXEMPT_ITEMS` / `EXEMPT_ATTRIBUTES` names it
with the reason the difference is intended. The list methods are compared too.

To extend coverage, add a step to `STEPS`: the API side is derived from the
manifest, so a new declaration needs no second spelling.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import timedelta
from typing import Any

import boto3
import pytest

import zae_limiter
from zae_limiter import Limit
from zae_limiter.schema import DEFAULT_RESOURCE
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version
from zae_limiter_provisioner.handler import on_event
from zae_limiter_provisioner.manifest import (
    EntityResourceDecl,
    LimitDecl,
    LimitsManifest,
    ResourceDecl,
    SystemDecl,
)

TABLE = "config-writer-parity"

# Items only one writer produces, by design. A pattern over the item's
# normalised SK (and PK, for the audit partition).
EXEMPT_ITEMS: dict[str, str] = {
    # The provisioner's own state: which levels a manifest manages, and its
    # pending fan-outs. The API manages no levels.
    r"^#PROVISIONER": "provisioner state, not config",
    # Audit events: the setters write LIMITS_SET / LIMITS_DELETED; the
    # provisioner writes none. A real gap (`audit list` never shows a manifest
    # change), but a compliance feature rather than config, tracked
    # separately; listed here so the test pins everything else.
    r"^#AUDIT#": "audit events: the provisioner writes none (tracked separately)",
}

# Attributes only one writer stamps on an item both write, by design.
EXEMPT_ATTRIBUTES: dict[str, str] = {
    # Written as 1 by the setters and never read anywhere in src/.
    "config_version": "write-only counter, read by nothing",
}


def _limit(name: str, decl: LimitDecl) -> Limit:
    return Limit(
        name=name,
        capacity=decl.capacity,
        refill_amount=decl.refill_amount,
        refill_period_seconds=decl.refill_period,
        schedule=decl.schedule,
        reset_schedule=decl.reset_schedule,
        reset_after=(
            None
            if decl.reset_after_seconds is None
            else timedelta(seconds=decl.reset_after_seconds)
        ),
        soft=decl.soft,
    )


def _limits(decls: dict[str, LimitDecl]) -> list[Limit]:
    return [_limit(name, decl) for name, decl in decls.items()]


def _flag(value: Any) -> dict[str, Any]:
    """A manifest omits a flag to clear it; the setters clear it with None."""
    return {"disabled": value.disabled, "cascade": value.cascade}


def _levels(manifest: LimitsManifest) -> set[tuple[str, str | None]]:
    levels: set[tuple[str, str | None]] = set()
    if manifest.system is not None:
        levels.add(("system", None))
    levels.update(("resource", name) for name in manifest.resources)
    for entity, decl in manifest.entities.items():
        levels.update(("entity", f"{entity}/{res}") for res in decl.resources)
    return levels


def _apply_through_the_api(
    repo: SyncRepository, manifest: LimitsManifest, previous: LimitsManifest | None
) -> None:
    """What an operator would type to reach the same state, level by level."""
    if manifest.system is not None:
        repo.set_system_defaults(
            _limits(manifest.system.limits), on_unavailable=manifest.system.on_unavailable
        )
    for name, decl in manifest.resources.items():
        repo.set_resource_defaults(name, _limits(decl.limits), **_flag(decl))
    for entity, entity_decl in manifest.entities.items():
        for res, decl in entity_decl.resources.items():
            repo.set_limits(entity, _limits(decl.limits), resource=res, **_flag(decl))
    if previous is None:
        return
    for level, target in _levels(previous) - _levels(manifest):
        if level == "system":
            repo.delete_system_defaults()
        elif level == "resource":
            assert target is not None
            repo.delete_resource_defaults(target)
        else:
            assert target is not None
            entity, res = target.split("/", 1)
            repo.delete_limits(entity, resource=res)


def _apply_through_the_provisioner(repo: SyncRepository, manifest: dict[str, Any]) -> None:
    result = on_event(
        {
            "action": "apply",
            "table_name": TABLE,
            "namespace_id": repo._namespace_id,
            "manifest": manifest,
        },
        None,
    )
    assert result["status"] == "applied", result
    assert result["errors"] == [], result


def _namespace_items(namespace_id: str) -> dict[tuple[str, str], dict[str, Any]]:
    """Every item of one namespace, with its id replaced so two can be compared."""
    client = boto3.client("dynamodb", region_name="us-east-1")
    marker = re.compile(re.escape(namespace_id))

    def normalise(value: Any) -> Any:
        if isinstance(value, str):
            return marker.sub("<ns>", value)
        if isinstance(value, list):
            return sorted((normalise(v) for v in value), key=repr)
        if isinstance(value, dict):
            return {normalise(k): normalise(v) for k, v in value.items()}
        return value

    items: dict[tuple[str, str], dict[str, Any]] = {}
    for page in client.get_paginator("scan").paginate(TableName=TABLE, ConsistentRead=True):
        for item in page["Items"]:
            pk = item["PK"]["S"]
            if not (
                pk.startswith(f"{namespace_id}/") or item.get("GSI4PK", {}).get("S") == namespace_id
            ):
                continue
            clean = normalise(item)
            key = (clean["PK"]["S"], clean["SK"]["S"])
            if any(re.search(p, key[1]) or re.search(p, key[0]) for p in EXEMPT_ITEMS):
                continue
            items[key] = {k: v for k, v in clean.items() if k not in EXEMPT_ATTRIBUTES}
    return items


def _listed(repo: SyncRepository, resources: set[str]) -> tuple[Any, ...]:
    return (
        sorted(repo.list_resources_with_defaults()),
        sorted(repo.list_resources_with_entity_configs()),
        {r: sorted(repo.list_entities_with_custom_limits(r)[0]) for r in sorted(resources)},
    )


def _rpm(capacity: int, **extra: Any) -> dict[str, Any]:
    return {"rpm": {"capacity": capacity, **extra}}


# Each step is a whole manifest; the API side reaches it by the calls an
# operator would make (sets, then deletes of the levels the step drops).
STEPS: list[tuple[str, dict[str, Any]]] = [
    (
        "create every level and limit shape",
        {
            "system": {"on_unavailable": "allow", "limits": _rpm(1000)},
            "resources": {
                "gpt-4": {
                    "limits": {
                        **_rpm(500),
                        "tpm": {
                            "capacity": 50000,
                            "schedule": [
                                {"cron": "* 9-17 * * 1-5", "tz": "America/New_York", "scale": 2}
                            ],
                        },
                    }
                },
                # `.`, `/` and `:` are legal in a resource name, illegal in expression text.
                "vendor/model.v1:2024": {
                    "limits": {
                        "day": {
                            "capacity": 100,
                            "reset_schedule": [{"cron": "0 0 * * *", "tz": "UTC"}],
                        }
                    }
                },
                "claude": {"limits": {"session": {"capacity": 50, "reset_after_seconds": 18000}}},
                "legacy": {"disabled": True, "limits": _rpm(10)},
                "cascading": {"cascade": True, "limits": _rpm(100, soft=True)},
            },
            "entities": {
                "user-1": {
                    "resources": {
                        "gpt-4": {"limits": _rpm(100)},
                        "legacy": {"disabled": False, "limits": _rpm(5)},
                    }
                },
                "user-2": {
                    "resources": {
                        "gpt-4": {"limits": _rpm(200)},
                        "vendor/model.v1:2024": {"disabled": "bypass", "limits": _rpm(20)},
                        DEFAULT_RESOURCE: {"cascade": False, "limits": _rpm(50)},
                    }
                },
            },
        },
    ),
    (
        "update limits and flip flags",
        {
            "system": {"limits": _rpm(2000)},
            "resources": {
                "gpt-4": {"limits": _rpm(600)},
                "vendor/model.v1:2024": {
                    "limits": {
                        "day": {
                            "capacity": 200,
                            "reset_schedule": [{"cron": "0 0 * * *", "tz": "UTC"}],
                        }
                    }
                },
                "claude": {"limits": {"session": {"capacity": 50, "reset_after_seconds": 18000}}},
                "legacy": {"limits": _rpm(10)},
                "cascading": {"limits": _rpm(100)},
            },
            "entities": {
                "user-1": {
                    "resources": {
                        "gpt-4": {"limits": _rpm(150)},
                        "legacy": {"limits": _rpm(5)},
                    }
                },
                "user-2": {
                    "resources": {
                        "gpt-4": {"limits": _rpm(200)},
                        "vendor/model.v1:2024": {"limits": _rpm(20)},
                        DEFAULT_RESOURCE: {"limits": _rpm(50)},
                    }
                },
            },
        },
    ),
    (
        "drop a resource, one of two entities on a resource, and a whole resource's entities",
        {
            "system": {"limits": _rpm(2000)},
            "resources": {
                "gpt-4": {"limits": _rpm(600)},
                "claude": {"limits": {"session": {"capacity": 50, "reset_after_seconds": 18000}}},
            },
            "entities": {
                "user-2": {"resources": {"gpt-4": {"limits": _rpm(200)}}},
            },
        },
    ),
    ("delete everything", {}),
]


@pytest.fixture
def namespaces(mock_dynamodb):
    setup = SyncRepository(name=TABLE, region="us-east-1", _skip_deprecation_warning=True)
    setup.create_table()
    # A deploy of this build: the soft, bypass, cascade and reset_after gates pass.
    setup.set_version_record(
        schema_version=get_schema_version(), lambda_version=zae_limiter.__version__
    )
    setup._register_namespace("api")
    setup._register_namespace("manifest")
    api = setup.namespace("api")
    manifest = setup.namespace("manifest")
    yield api, manifest
    setup.close()


def test_both_writers_leave_the_same_table(namespaces):
    api, provisioner = namespaces
    previous: LimitsManifest | None = None
    resources: set[str] = set()
    for step, body in STEPS:
        raw = {"namespace": "manifest", **body}
        manifest = LimitsManifest.from_dict(raw)
        _apply_through_the_api(api, manifest, previous)
        _apply_through_the_provisioner(provisioner, raw)
        resources |= set(manifest.resources) | {
            res for e in manifest.entities.values() for res in e.resources
        }

        api_items = _namespace_items(api._namespace_id)
        manifest_items = _namespace_items(provisioner._namespace_id)
        if body:
            assert api_items, f"{step}: compared nothing; is the namespace filter right?"
        only_api = sorted(set(api_items) - set(manifest_items))
        only_manifest = sorted(set(manifest_items) - set(api_items))
        assert (only_api, only_manifest) == ([], []), f"{step}: items one writer lacks"
        differing = {
            key: {
                attr: (api_items[key].get(attr), manifest_items[key].get(attr))
                for attr in set(api_items[key]) | set(manifest_items[key])
                if api_items[key].get(attr) != manifest_items[key].get(attr)
            }
            for key in api_items
            if api_items[key] != manifest_items[key]
        }
        assert differing == {}, f"{step}: (api, provisioner) attribute values differ"
        assert _listed(api, resources) == _listed(provisioner, resources), step
        previous = manifest


def test_every_declaration_field_is_exercised():
    """A field added to a manifest declaration needs a parity step.

    A new manifest feature is a new attribute both writers must agree on (how
    ``disabled`` reached manifests in #438 with nobody checking the applier).
    This fails until some step gives every field of every declaration type a
    non-default value, so the comparison above covers it.
    """
    seen: set[str] = set()

    def note(decl: Any) -> None:
        for f in dataclasses.fields(decl):
            value = getattr(decl, f.name)
            default = f.default if f.default is not dataclasses.MISSING else None
            if f.default_factory is not dataclasses.MISSING:
                default = f.default_factory()
            if value != default and value not in (None, (), {}):
                seen.add(f"{type(decl).__name__}.{f.name}")

    for _step, body in STEPS:
        manifest = LimitsManifest.from_dict({"namespace": "manifest", **body})
        if manifest.system is not None:
            note(manifest.system)
            for decl in manifest.system.limits.values():
                note(decl)
        for resource in manifest.resources.values():
            note(resource)
            for decl in resource.limits.values():
                note(decl)
        for entity in manifest.entities.values():
            for resource in entity.resources.values():
                note(resource)
                for decl in resource.limits.values():
                    note(decl)

    declared = {
        f"{cls.__name__}.{f.name}"
        for cls in (SystemDecl, ResourceDecl, EntityResourceDecl, LimitDecl)
        for f in dataclasses.fields(cls)
    }
    assert declared - seen == set(), "declaration fields no parity step exercises"
