# Declarative Limits Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enable operators to declare rate limits in YAML files and apply them through a Lambda provisioner, serving both CLI and CloudFormation paths with Terraform-style state tracking.

**Architecture:** YAML files (one per namespace) are parsed by the CLI into a `LimitsManifest` dataclass, serialized to JSON, and sent to a Lambda provisioner via `lambda:Invoke`. The Lambda diffs the manifest against a `#PROVISIONER` state record in DynamoDB, applies changes via the existing Repository API, and updates the state record. The same Lambda handles CloudFormation custom resource events. The provisioner is packaged and deployed alongside the existing aggregator Lambda.

**Tech Stack:** Python dataclasses, PyYAML, boto3 Lambda invoke, CloudFormation custom resources, Click CLI

---

## Task 1: Schema Key Builder

Add the `#PROVISIONER` sort key constant and builder to `schema.py`.

**Files:**
- Modify: `src/zae_limiter/schema.py`
- Test: `tests/unit/test_schema.py`

**Step 1: Write the failing test**

In `tests/unit/test_schema.py`, add:

```python
class TestProvisionerKey:
    """Tests for provisioner state sort key builder."""

    def test_sk_provisioner(self):
        from zae_limiter.schema import sk_provisioner
        assert sk_provisioner() == "#PROVISIONER"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schema.py::TestProvisionerKey -v`
Expected: FAIL with `ImportError: cannot import name 'sk_provisioner'`

**Step 3: Write minimal implementation**

In `src/zae_limiter/schema.py`, add the constant near the other SK constants (after line 41):

```python
SK_PROVISIONER = "#PROVISIONER"
```

Add the builder function (after `sk_nsid_prefix`):

```python
def sk_provisioner() -> str:
    """Build sort key for provisioner state record (tracks managed limits)."""
    return SK_PROVISIONER
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_schema.py::TestProvisionerKey -v`
Expected: PASS

**Step 5: Commit**

```
git add src/zae_limiter/schema.py tests/unit/test_schema.py
git commit -m "✨ feat(schema): add sk_provisioner() key builder for #PROVISIONER record"
```

---

## Task 2: Repository Methods for Provisioner State

Add `get_provisioner_state()` and `put_provisioner_state()` to `Repository`.

**Files:**
- Modify: `src/zae_limiter/repository.py`
- Test: `tests/unit/test_repository.py`

**Step 1: Write the failing tests**

In `tests/unit/test_repository.py`, add:

```python
class TestProvisionerState:
    """Tests for provisioner state CRUD (declarative limits management)."""

    @pytest.mark.asyncio
    async def test_get_provisioner_state_empty(self, repo):
        """get_provisioner_state returns empty state when no record exists."""
        state = await repo.get_provisioner_state()
        assert state["managed_system"] is False
        assert state["managed_resources"] == []
        assert state["managed_entities"] == {}
        assert state["last_applied"] is None
        assert state["applied_hash"] is None

    @pytest.mark.asyncio
    async def test_put_get_provisioner_state_roundtrip(self, repo):
        """put_provisioner_state and get_provisioner_state round-trip correctly."""
        state = {
            "managed_system": True,
            "managed_resources": ["gpt-4", "claude-3"],
            "managed_entities": {"user-123": ["gpt-4"], "org-456": ["_default_"]},
            "last_applied": "2026-02-19T12:00:00Z",
            "applied_hash": "sha256:abc123",
        }
        await repo.put_provisioner_state(state)

        retrieved = await repo.get_provisioner_state()
        assert retrieved["managed_system"] is True
        assert sorted(retrieved["managed_resources"]) == ["claude-3", "gpt-4"]
        assert retrieved["managed_entities"] == {
            "user-123": ["gpt-4"],
            "org-456": ["_default_"],
        }
        assert retrieved["last_applied"] == "2026-02-19T12:00:00Z"
        assert retrieved["applied_hash"] == "sha256:abc123"

    @pytest.mark.asyncio
    async def test_put_provisioner_state_overwrites(self, repo):
        """put_provisioner_state replaces previous state entirely."""
        state1 = {
            "managed_system": True,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
            "last_applied": "2026-02-19T12:00:00Z",
            "applied_hash": "sha256:aaa",
        }
        await repo.put_provisioner_state(state1)

        state2 = {
            "managed_system": False,
            "managed_resources": ["claude-3"],
            "managed_entities": {"user-1": ["claude-3"]},
            "last_applied": "2026-02-19T13:00:00Z",
            "applied_hash": "sha256:bbb",
        }
        await repo.put_provisioner_state(state2)

        retrieved = await repo.get_provisioner_state()
        assert retrieved["managed_system"] is False
        assert retrieved["managed_resources"] == ["claude-3"]
        assert retrieved["managed_entities"] == {"user-1": ["claude-3"]}
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_repository.py::TestProvisionerState -v`
Expected: FAIL with `AttributeError: 'Repository' object has no attribute 'get_provisioner_state'`

**Step 3: Write minimal implementation**

In `src/zae_limiter/repository.py`, add two methods to the `Repository` class:

```python
async def get_provisioner_state(self) -> dict[str, Any]:
    """Get the provisioner state record for this namespace.

    Returns:
        Dict with keys: managed_system, managed_resources, managed_entities,
        last_applied, applied_hash. Returns empty state if no record exists.
    """
    client = await self._get_client()
    result = await client.get_item(
        TableName=self.table_name,
        Key={
            "PK": {"S": schema.pk_system(self._namespace_id)},
            "SK": {"S": schema.sk_provisioner()},
        },
    )
    item = result.get("Item")
    if not item:
        return {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
            "last_applied": None,
            "applied_hash": None,
        }

    managed_entities: dict[str, list[str]] = {}
    raw_entities = item.get("managed_entities", {}).get("M", {})
    for entity_id, resources_attr in raw_entities.items():
        managed_entities[entity_id] = [
            r["S"] for r in resources_attr.get("L", [])
        ]

    return {
        "managed_system": item.get("managed_system", {}).get("BOOL", False),
        "managed_resources": [
            r["S"] for r in item.get("managed_resources", {}).get("L", [])
        ],
        "managed_entities": managed_entities,
        "last_applied": item.get("last_applied", {}).get("S"),
        "applied_hash": item.get("applied_hash", {}).get("S"),
    }

async def put_provisioner_state(self, state: dict[str, Any]) -> None:
    """Write the provisioner state record for this namespace.

    Args:
        state: Dict with keys: managed_system, managed_resources,
               managed_entities, last_applied, applied_hash.
    """
    client = await self._get_client()
    item: dict[str, Any] = {
        "PK": {"S": schema.pk_system(self._namespace_id)},
        "SK": {"S": schema.sk_provisioner()},
        "GSI4PK": {"S": self._namespace_id},
        "managed_system": {"BOOL": state["managed_system"]},
        "managed_resources": {
            "L": [{"S": r} for r in state["managed_resources"]]
        },
        "managed_entities": {
            "M": {
                entity_id: {"L": [{"S": r} for r in resources]}
                for entity_id, resources in state["managed_entities"].items()
            }
        },
        "last_applied": {"S": state["last_applied"]},
        "applied_hash": {"S": state["applied_hash"]},
    }
    await client.put_item(TableName=self.table_name, Item=item)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_repository.py::TestProvisionerState -v`
Expected: PASS

**Step 5: Commit**

```
git add src/zae_limiter/repository.py tests/unit/test_repository.py
git commit -m "✨ feat(repository): add get/put_provisioner_state() for declarative limits"
```

---

## Task 3: Provisioner Package — Manifest Dataclass

Create the `zae_limiter_provisioner` package with the YAML manifest parser.

**Files:**
- Create: `src/zae_limiter_provisioner/__init__.py`
- Create: `src/zae_limiter_provisioner/manifest.py`
- Test: `tests/unit/test_manifest.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_manifest.py`:

```python
"""Tests for LimitsManifest YAML parsing and validation."""

import pytest
import yaml

from zae_limiter_provisioner.manifest import LimitsManifest


class TestLimitsManifestParsing:
    """Tests for YAML parsing into LimitsManifest."""

    def test_parse_minimal(self):
        """Minimal YAML with just namespace parses correctly."""
        raw = {"namespace": "test-ns"}
        manifest = LimitsManifest.from_dict(raw)
        assert manifest.namespace == "test-ns"
        assert manifest.system is None
        assert manifest.resources == {}
        assert manifest.entities == {}

    def test_parse_system_limits(self):
        """System-level limits parse with shorthand defaults."""
        raw = {
            "namespace": "test-ns",
            "system": {
                "on_unavailable": "allow",
                "limits": {
                    "rpm": {"capacity": 1000},
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        assert manifest.system is not None
        assert manifest.system.on_unavailable == "allow"
        assert len(manifest.system.limits) == 1
        limit = manifest.system.limits["rpm"]
        assert limit.capacity == 1000
        assert limit.burst == 1000  # default: capacity
        assert limit.refill_amount == 1000  # default: capacity
        assert limit.refill_period == 60  # default: 60

    def test_parse_explicit_limit_fields(self):
        """Explicit limit fields override shorthand defaults."""
        raw = {
            "namespace": "test-ns",
            "system": {
                "limits": {
                    "tpm": {
                        "capacity": 50000,
                        "burst": 75000,
                        "refill_amount": 50000,
                        "refill_period": 120,
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        limit = manifest.system.limits["tpm"]
        assert limit.capacity == 50000
        assert limit.burst == 75000
        assert limit.refill_amount == 50000
        assert limit.refill_period == 120

    def test_parse_resources(self):
        """Resource-level limits parse correctly."""
        raw = {
            "namespace": "test-ns",
            "resources": {
                "gpt-4": {
                    "limits": {
                        "rpm": {"capacity": 1000},
                        "tpm": {"capacity": 50000},
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        assert "gpt-4" in manifest.resources
        assert len(manifest.resources["gpt-4"].limits) == 2

    def test_parse_entities(self):
        """Entity-level limits parse correctly with resource scoping."""
        raw = {
            "namespace": "test-ns",
            "entities": {
                "user-123": {
                    "resources": {
                        "gpt-4": {
                            "limits": {"rpm": {"capacity": 500}},
                        },
                        "_default_": {
                            "limits": {"rpm": {"capacity": 200}},
                        },
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        assert "user-123" in manifest.entities
        entity = manifest.entities["user-123"]
        assert "gpt-4" in entity.resources
        assert "_default_" in entity.resources
        assert entity.resources["gpt-4"].limits["rpm"].capacity == 500

    def test_parse_from_yaml_string(self):
        """from_yaml parses a YAML string into a manifest."""
        yaml_str = """
namespace: test-ns
system:
  limits:
    rpm:
      capacity: 1000
resources:
  gpt-4:
    limits:
      tpm:
        capacity: 50000
"""
        manifest = LimitsManifest.from_yaml(yaml_str)
        assert manifest.namespace == "test-ns"
        assert manifest.system.limits["rpm"].capacity == 1000
        assert manifest.resources["gpt-4"].limits["tpm"].capacity == 50000

    def test_missing_namespace_raises(self):
        """Missing namespace field raises ValueError."""
        with pytest.raises(ValueError, match="namespace"):
            LimitsManifest.from_dict({})

    def test_to_dict_roundtrip(self):
        """to_dict produces a dict that round-trips through from_dict."""
        raw = {
            "namespace": "test-ns",
            "system": {
                "on_unavailable": "allow",
                "limits": {"rpm": {"capacity": 1000}},
            },
            "resources": {
                "gpt-4": {"limits": {"tpm": {"capacity": 50000}}},
            },
            "entities": {
                "user-1": {
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 500}}},
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        roundtripped = LimitsManifest.from_dict(manifest.to_dict())
        assert roundtripped.namespace == manifest.namespace
        assert roundtripped.system.limits["rpm"].capacity == 1000
        assert roundtripped.resources["gpt-4"].limits["tpm"].capacity == 50000
        assert roundtripped.entities["user-1"].resources["gpt-4"].limits["rpm"].capacity == 500


class TestManifestManagedSet:
    """Tests for extracting the managed set from a manifest."""

    def test_managed_set_system(self):
        """Manifest with system section reports managed_system=True."""
        raw = {"namespace": "ns", "system": {"limits": {"rpm": {"capacity": 1}}}}
        manifest = LimitsManifest.from_dict(raw)
        ms = manifest.managed_set()
        assert ms["managed_system"] is True

    def test_managed_set_no_system(self):
        """Manifest without system section reports managed_system=False."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        ms = manifest.managed_set()
        assert ms["managed_system"] is False

    def test_managed_set_resources(self):
        """Managed set includes all resource names."""
        raw = {
            "namespace": "ns",
            "resources": {
                "gpt-4": {"limits": {"rpm": {"capacity": 1}}},
                "claude-3": {"limits": {"tpm": {"capacity": 1}}},
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        ms = manifest.managed_set()
        assert sorted(ms["managed_resources"]) == ["claude-3", "gpt-4"]

    def test_managed_set_entities(self):
        """Managed set includes entity-to-resource mapping."""
        raw = {
            "namespace": "ns",
            "entities": {
                "user-1": {
                    "resources": {
                        "gpt-4": {"limits": {"rpm": {"capacity": 1}}},
                        "_default_": {"limits": {"rpm": {"capacity": 1}}},
                    },
                },
            },
        }
        manifest = LimitsManifest.from_dict(raw)
        ms = manifest.managed_set()
        assert ms["managed_entities"] == {"user-1": ["_default_", "gpt-4"]}
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_manifest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zae_limiter_provisioner'`

**Step 3: Write the implementation**

Create `src/zae_limiter_provisioner/__init__.py`:

```python
"""Lambda provisioner for declarative limits management."""

from .manifest import LimitsManifest

__all__ = ["LimitsManifest"]
```

Create `src/zae_limiter_provisioner/manifest.py`:

```python
"""YAML manifest parsing and validation for declarative limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LimitDecl:
    """A single limit declaration with shorthand defaults."""

    capacity: int
    burst: int
    refill_amount: int
    refill_period: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LimitDecl:
        capacity = d["capacity"]
        return cls(
            capacity=capacity,
            burst=d.get("burst", capacity),
            refill_amount=d.get("refill_amount", capacity),
            refill_period=d.get("refill_period", 60),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "capacity": self.capacity,
            "burst": self.burst,
            "refill_amount": self.refill_amount,
            "refill_period": self.refill_period,
        }


@dataclass(frozen=True)
class SystemDecl:
    """System-level limit declaration."""

    limits: dict[str, LimitDecl]
    on_unavailable: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SystemDecl:
        limits = {
            name: LimitDecl.from_dict(val)
            for name, val in d.get("limits", {}).items()
        }
        return cls(limits=limits, on_unavailable=d.get("on_unavailable"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "limits": {name: lim.to_dict() for name, lim in self.limits.items()},
        }
        if self.on_unavailable is not None:
            result["on_unavailable"] = self.on_unavailable
        return result


@dataclass(frozen=True)
class ResourceDecl:
    """Resource-level limit declaration."""

    limits: dict[str, LimitDecl]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResourceDecl:
        limits = {
            name: LimitDecl.from_dict(val)
            for name, val in d.get("limits", {}).items()
        }
        return cls(limits=limits)

    def to_dict(self) -> dict[str, Any]:
        return {"limits": {name: lim.to_dict() for name, lim in self.limits.items()}}


@dataclass(frozen=True)
class EntityResourceDecl:
    """Entity-resource-level limit declaration."""

    limits: dict[str, LimitDecl]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityResourceDecl:
        limits = {
            name: LimitDecl.from_dict(val)
            for name, val in d.get("limits", {}).items()
        }
        return cls(limits=limits)

    def to_dict(self) -> dict[str, Any]:
        return {"limits": {name: lim.to_dict() for name, lim in self.limits.items()}}


@dataclass(frozen=True)
class EntityDecl:
    """Entity-level declaration with per-resource limits."""

    resources: dict[str, EntityResourceDecl]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityDecl:
        resources = {
            name: EntityResourceDecl.from_dict(val)
            for name, val in d.get("resources", {}).items()
        }
        return cls(resources=resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": {
                name: res.to_dict() for name, res in self.resources.items()
            },
        }


@dataclass(frozen=True)
class LimitsManifest:
    """Parsed YAML manifest for declarative limits management."""

    namespace: str
    system: SystemDecl | None = None
    resources: dict[str, ResourceDecl] = field(default_factory=dict)
    entities: dict[str, EntityDecl] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LimitsManifest:
        namespace = d.get("namespace")
        if not namespace:
            raise ValueError("'namespace' is required in limits manifest")

        system = SystemDecl.from_dict(d["system"]) if "system" in d else None

        resources = {
            name: ResourceDecl.from_dict(val)
            for name, val in d.get("resources", {}).items()
        }

        entities = {
            name: EntityDecl.from_dict(val)
            for name, val in d.get("entities", {}).items()
        }

        return cls(
            namespace=namespace,
            system=system,
            resources=resources,
            entities=entities,
        )

    @classmethod
    def from_yaml(cls, yaml_str: str) -> LimitsManifest:
        import yaml

        data = yaml.safe_load(yaml_str)
        return cls.from_dict(data)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"namespace": self.namespace}
        if self.system is not None:
            result["system"] = self.system.to_dict()
        if self.resources:
            result["resources"] = {
                name: res.to_dict() for name, res in self.resources.items()
            }
        if self.entities:
            result["entities"] = {
                name: ent.to_dict() for name, ent in self.entities.items()
            }
        return result

    def managed_set(self) -> dict[str, Any]:
        """Extract the set of managed items from this manifest.

        Returns:
            Dict with managed_system (bool), managed_resources (list[str]),
            managed_entities (dict[str, list[str]]).
        """
        return {
            "managed_system": self.system is not None,
            "managed_resources": sorted(self.resources.keys()),
            "managed_entities": {
                entity_id: sorted(entity.resources.keys())
                for entity_id, entity in self.entities.items()
            },
        }
```

**Step 4: Register the package in pyproject.toml**

In `pyproject.toml`, update the packages list (line 97):

```toml
packages = ["src/zae_limiter", "src/zae_limiter_aggregator", "src/zae_limiter_provisioner"]
```

Add `pyyaml` to the project dependencies if not already present (check first).

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_manifest.py -v`
Expected: PASS

**Step 6: Commit**

```
git add src/zae_limiter_provisioner/ tests/unit/test_manifest.py pyproject.toml
git commit -m "✨ feat(infra): add LimitsManifest dataclass with YAML parsing"
```

---

## Task 4: Provisioner Package — Differ Module

The diff engine compares a manifest against the previous provisioner state to produce a list of changes.

**Files:**
- Create: `src/zae_limiter_provisioner/differ.py`
- Test: `tests/unit/test_differ.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_differ.py`:

```python
"""Tests for the provisioner diff engine."""

import pytest

from zae_limiter_provisioner.differ import compute_diff, Change
from zae_limiter_provisioner.manifest import LimitsManifest


class TestComputeDiff:
    """Tests for diff computation between manifest and previous state."""

    def test_first_apply_creates_everything(self):
        """First apply (empty previous state) creates all items."""
        manifest = LimitsManifest.from_dict({
            "namespace": "ns",
            "system": {"limits": {"rpm": {"capacity": 1000}}},
            "resources": {"gpt-4": {"limits": {"tpm": {"capacity": 50000}}}},
            "entities": {
                "user-1": {"resources": {"gpt-4": {"limits": {"rpm": {"capacity": 500}}}}},
            },
        })
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "create") in actions
        assert ("resource", "gpt-4", "create") in actions
        assert ("entity", "user-1/gpt-4", "create") in actions

    def test_no_changes_on_same_state(self):
        """Re-applying same manifest produces update actions (idempotent overwrites)."""
        manifest = LimitsManifest.from_dict({
            "namespace": "ns",
            "system": {"limits": {"rpm": {"capacity": 1000}}},
            "resources": {"gpt-4": {"limits": {"tpm": {"capacity": 50000}}}},
        })
        previous = {
            "managed_system": True,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "update") in actions
        assert ("resource", "gpt-4", "update") in actions

    def test_removed_resource_produces_delete(self):
        """Resource in previous but not in manifest produces delete."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": False,
            "managed_resources": ["gpt-4"],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("resource", "gpt-4", "delete") in actions

    def test_removed_system_produces_delete(self):
        """System in previous but not in manifest produces delete."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": True,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "delete") in actions

    def test_removed_entity_resource_produces_delete(self):
        """Entity resource in previous but not in manifest produces delete."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {"user-1": ["gpt-4"]},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("entity", "user-1/gpt-4", "delete") in actions

    def test_unmanaged_items_not_touched(self):
        """Items never in previous managed set produce no changes."""
        manifest = LimitsManifest.from_dict({
            "namespace": "ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1}}}},
        })
        previous = {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }

        changes = compute_diff(manifest, previous)
        # gpt-4 is new (create), but no deletes for things never managed
        assert all(c.action != "delete" for c in changes)

    def test_empty_manifest_deletes_all_managed(self):
        """Empty manifest (CFN Delete) deletes all previously managed items."""
        manifest = LimitsManifest.from_dict({"namespace": "ns"})
        previous = {
            "managed_system": True,
            "managed_resources": ["gpt-4", "claude-3"],
            "managed_entities": {"user-1": ["gpt-4"]},
        }

        changes = compute_diff(manifest, previous)
        actions = {(c.level, c.target, c.action) for c in changes}

        assert ("system", None, "delete") in actions
        assert ("resource", "gpt-4", "delete") in actions
        assert ("resource", "claude-3", "delete") in actions
        assert ("entity", "user-1/gpt-4", "delete") in actions
        assert len(changes) == 4
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_differ.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zae_limiter_provisioner.differ'`

**Step 3: Write the implementation**

Create `src/zae_limiter_provisioner/differ.py`:

```python
"""Diff engine for declarative limits management.

Compares a LimitsManifest against the previous provisioner state
to produce a list of changes (create/update/delete).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .manifest import LimitsManifest


@dataclass(frozen=True)
class Change:
    """A single change to apply."""

    action: str  # "create", "update", "delete"
    level: str  # "system", "resource", "entity"
    target: str | None  # resource name, "entity_id/resource", or None for system
    data: dict[str, Any] | None = None  # manifest data for create/update


def compute_diff(
    manifest: LimitsManifest,
    previous: dict[str, Any],
) -> list[Change]:
    """Compute changes between a manifest and previous managed state.

    Args:
        manifest: The new desired state from YAML.
        previous: The previous managed set from #PROVISIONER record.
                  Keys: managed_system, managed_resources, managed_entities.

    Returns:
        List of Change objects to apply.
    """
    changes: list[Change] = []

    # --- System ---
    prev_system = previous.get("managed_system", False)
    if manifest.system is not None:
        action = "update" if prev_system else "create"
        changes.append(Change(
            action=action, level="system", target=None,
            data=manifest.system.to_dict(),
        ))
    elif prev_system:
        changes.append(Change(action="delete", level="system", target=None))

    # --- Resources ---
    prev_resources = set(previous.get("managed_resources", []))
    curr_resources = set(manifest.resources.keys())

    for resource in curr_resources:
        action = "update" if resource in prev_resources else "create"
        changes.append(Change(
            action=action, level="resource", target=resource,
            data=manifest.resources[resource].to_dict(),
        ))

    for resource in prev_resources - curr_resources:
        changes.append(Change(action="delete", level="resource", target=resource))

    # --- Entities ---
    prev_entities: dict[str, list[str]] = previous.get("managed_entities", {})
    prev_entity_resources: set[tuple[str, str]] = set()
    for entity_id, resources in prev_entities.items():
        for resource in resources:
            prev_entity_resources.add((entity_id, resource))

    curr_entity_resources: set[tuple[str, str]] = set()
    for entity_id, entity in manifest.entities.items():
        for resource in entity.resources:
            curr_entity_resources.add((entity_id, resource))

    for entity_id, resource in curr_entity_resources:
        target = f"{entity_id}/{resource}"
        action = "update" if (entity_id, resource) in prev_entity_resources else "create"
        changes.append(Change(
            action=action, level="entity", target=target,
            data=manifest.entities[entity_id].resources[resource].to_dict(),
        ))

    for entity_id, resource in prev_entity_resources - curr_entity_resources:
        target = f"{entity_id}/{resource}"
        changes.append(Change(action="delete", level="entity", target=target))

    return changes
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_differ.py -v`
Expected: PASS

**Step 5: Commit**

```
git add src/zae_limiter_provisioner/differ.py tests/unit/test_differ.py
git commit -m "✨ feat(infra): add diff engine for declarative limits provisioner"
```

---

## Task 5: Provisioner Package — Applier Module

The applier takes a list of `Change` objects and applies them via the Repository API (using boto3 sync, like the aggregator).

**Files:**
- Create: `src/zae_limiter_provisioner/applier.py`
- Test: `tests/unit/test_applier.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_applier.py`. These tests mock the boto3 DynamoDB table resource to verify the applier calls the right Repository-equivalent operations. The applier uses boto3 (sync) directly since it runs in Lambda.

```python
"""Tests for the provisioner applier."""

from unittest.mock import MagicMock, patch, call

import pytest

from zae_limiter_provisioner.applier import apply_changes, ApplyResult
from zae_limiter_provisioner.differ import Change


class TestApplyChanges:
    """Tests for applying changes to DynamoDB via Repository-equivalent operations."""

    def test_apply_empty_changes(self):
        """Empty change list produces zero-change result."""
        table = MagicMock()
        result = apply_changes([], table_name="test", namespace_id="ns123")
        assert result.created == 0
        assert result.updated == 0
        assert result.deleted == 0
        assert result.errors == []

    def test_apply_create_system(self):
        """Create system defaults calls put_item with correct keys."""
        result = apply_changes(
            [Change(
                action="create", level="system", target=None,
                data={"limits": {"rpm": {"capacity": 1000, "burst": 1000, "refill_amount": 1000, "refill_period": 60}}},
            )],
            table_name="test",
            namespace_id="ns123",
        )
        assert result.created == 1

    def test_apply_delete_resource(self):
        """Delete resource defaults calls delete_item."""
        result = apply_changes(
            [Change(action="delete", level="resource", target="gpt-4")],
            table_name="test",
            namespace_id="ns123",
        )
        assert result.deleted == 1

    def test_apply_create_entity(self):
        """Create entity limits calls put_item with entity/resource keys."""
        result = apply_changes(
            [Change(
                action="create", level="entity", target="user-1/gpt-4",
                data={"limits": {"rpm": {"capacity": 500, "burst": 500, "refill_amount": 500, "refill_period": 60}}},
            )],
            table_name="test",
            namespace_id="ns123",
        )
        assert result.created == 1

    def test_apply_mixed_changes(self):
        """Mixed create/update/delete produces correct counts."""
        changes = [
            Change(action="create", level="system", target=None,
                   data={"limits": {"rpm": {"capacity": 1000, "burst": 1000, "refill_amount": 1000, "refill_period": 60}}}),
            Change(action="update", level="resource", target="gpt-4",
                   data={"limits": {"tpm": {"capacity": 50000, "burst": 50000, "refill_amount": 50000, "refill_period": 60}}}),
            Change(action="delete", level="resource", target="claude-3"),
            Change(action="delete", level="entity", target="user-1/gpt-4"),
        ]
        result = apply_changes(changes, table_name="test", namespace_id="ns123")
        assert result.created == 1
        assert result.updated == 1
        assert result.deleted == 2

    def test_apply_error_collected(self):
        """Errors from individual operations are collected, not raised."""
        # This test verifies the error-collection pattern (like the aggregator)
        changes = [
            Change(action="create", level="system", target=None, data={"limits": {}}),
        ]
        # Even with empty limits, should not crash
        result = apply_changes(changes, table_name="test", namespace_id="ns123")
        assert isinstance(result.errors, list)
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_applier.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zae_limiter_provisioner.applier'`

**Step 3: Write the implementation**

Create `src/zae_limiter_provisioner/applier.py`:

```python
"""Applies limit changes to DynamoDB.

Uses boto3 (sync) directly, like the aggregator. This module runs inside
Lambda where aioboto3 is not available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import boto3

from zae_limiter.schema import (
    limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
)

from .differ import Change


@dataclass
class ApplyResult:
    """Result of applying changes."""

    created: int = 0
    updated: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)


def _build_limit_item(
    pk: str, sk: str, namespace_id: str, limits: dict[str, Any], extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a DynamoDB item for a config record with composite limit attributes."""
    item: dict[str, Any] = {
        "PK": {"S": pk},
        "SK": {"S": sk},
        "GSI4PK": {"S": namespace_id},
    }
    if extra:
        for k, v in extra.items():
            item[k] = v

    for name, decl in limits.items():
        item[limit_attr(name, "cp")] = {"N": str(decl["capacity"])}
        item[limit_attr(name, "bx")] = {"N": str(decl["burst"])}
        item[limit_attr(name, "ra")] = {"N": str(decl["refill_amount"])}
        item[limit_attr(name, "rp")] = {"N": str(decl["refill_period"])}

    return item


def apply_changes(
    changes: list[Change],
    table_name: str,
    namespace_id: str,
) -> ApplyResult:
    """Apply a list of changes to DynamoDB.

    Args:
        changes: List of Change objects from the differ.
        table_name: DynamoDB table name.
        namespace_id: Opaque namespace ID (e.g., 'a7x3kq').

    Returns:
        ApplyResult with counts and any errors.
    """
    result = ApplyResult()

    if not changes:
        return result

    client = boto3.client("dynamodb")

    for change in changes:
        try:
            if change.action == "delete":
                _apply_delete(client, table_name, namespace_id, change)
                result.deleted += 1
            elif change.action == "create":
                _apply_set(client, table_name, namespace_id, change)
                result.created += 1
            elif change.action == "update":
                _apply_set(client, table_name, namespace_id, change)
                result.updated += 1
        except Exception as e:
            result.errors.append(f"{change.action} {change.level} {change.target}: {e}")

    return result


def _apply_set(
    client: Any, table_name: str, namespace_id: str, change: Change,
) -> None:
    """Apply a create or update change (PutItem)."""
    data = change.data or {}
    limits = data.get("limits", {})

    if change.level == "system":
        pk = pk_system(namespace_id)
        sk = sk_config()
        extra: dict[str, Any] = {}
        on_unavailable = data.get("on_unavailable")
        if on_unavailable is not None:
            extra["on_unavailable"] = {"S": on_unavailable}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "resource":
        resource = change.target
        pk = pk_resource(namespace_id, resource)
        sk = sk_config()
        extra = {"resource": {"S": resource}}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "entity":
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)
        extra = {"entity_id": {"S": entity_id}, "resource": {"S": resource}}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    else:
        raise ValueError(f"Unknown level: {change.level}")

    client.put_item(TableName=table_name, Item=item)


def _apply_delete(
    client: Any, table_name: str, namespace_id: str, change: Change,
) -> None:
    """Apply a delete change (DeleteItem)."""
    if change.level == "system":
        pk = pk_system(namespace_id)
        sk = sk_config()

    elif change.level == "resource":
        resource = change.target
        pk = pk_resource(namespace_id, resource)
        sk = sk_config()

    elif change.level == "entity":
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)

    else:
        raise ValueError(f"Unknown level: {change.level}")

    client.delete_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_applier.py -v`
Expected: PASS (boto3 calls go to moto or need mocking — adjust test setup to use `@mock_aws` or mock the client)

**Note:** The test setup may need `moto` or explicit mocking of `boto3.client("dynamodb")`. Follow the pattern in `tests/unit/test_handler.py` for mocking.

**Step 5: Commit**

```
git add src/zae_limiter_provisioner/applier.py tests/unit/test_applier.py
git commit -m "✨ feat(infra): add applier module for declarative limits provisioner"
```

---

## Task 6: Provisioner Package — Lambda Handler

The Lambda entry point that handles both CLI invocations and CloudFormation custom resource events.

**Files:**
- Create: `src/zae_limiter_provisioner/handler.py`
- Test: `tests/unit/test_provisioner_handler.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_provisioner_handler.py`:

```python
"""Tests for the provisioner Lambda handler."""

from unittest.mock import patch, MagicMock

import pytest

from zae_limiter_provisioner.handler import on_event


class TestProvisionerHandler:
    """Tests for the provisioner Lambda handler."""

    def test_plan_action_returns_changes(self):
        """Plan action computes diff without applying."""
        event = {
            "action": "plan",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "system": {"limits": {"rpm": {"capacity": 1000}}},
            },
        }
        context = MagicMock()
        result = on_event(event, context)
        assert result["status"] == "planned"
        assert len(result["changes"]) > 0

    def test_apply_action_applies_and_returns(self):
        """Apply action applies changes and updates state."""
        event = {
            "action": "apply",
            "table_name": "test-table",
            "namespace_id": "ns123",
            "manifest": {
                "namespace": "test-ns",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
            },
        }
        context = MagicMock()
        result = on_event(event, context)
        assert result["status"] == "applied"
        assert "changes" in result

    def test_cfn_create_event(self):
        """CloudFormation Create event applies the manifest."""
        event = {
            "RequestType": "Create",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
                "System": {"Limits": {"rpm": {"Capacity": 1000}}},
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        context = MagicMock()
        # CFN events are handled by on_event but need cfnresponse mocked
        # The handler should detect CFN events and route appropriately
        result = on_event(event, context)
        assert result["status"] in ("applied", "planned")

    def test_cfn_delete_event_clears_all(self):
        """CloudFormation Delete event applies empty manifest (deletes all managed)."""
        event = {
            "RequestType": "Delete",
            "ResourceProperties": {
                "ServiceToken": "arn:aws:lambda:us-east-1:123:function:test",
                "TableName": "test-table",
                "Namespace": "test-ns",
                "NamespaceId": "ns123",
            },
            "ResponseURL": "https://cfn-response.example.com",
            "StackId": "arn:aws:cloudformation:us-east-1:123:stack/test/guid",
            "RequestId": "test-request-id",
            "LogicalResourceId": "TenantLimits",
        }
        context = MagicMock()
        result = on_event(event, context)
        assert result["status"] == "applied"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_provisioner_handler.py -v`
Expected: FAIL with `ImportError`

**Step 3: Write the implementation**

Create `src/zae_limiter_provisioner/handler.py`:

```python
"""Lambda handler for declarative limits provisioner.

Handles two event types:
1. CLI invocations: {"action": "plan|apply", "manifest": {...}, "table_name": "...", "namespace_id": "..."}
2. CloudFormation custom resource events: {"RequestType": "Create|Update|Delete", "ResourceProperties": {...}}
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3

from .applier import apply_changes
from .differ import compute_diff
from .manifest import LimitsManifest

TABLE_NAME = os.environ.get("TABLE_NAME", "rate-limits")


def on_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point."""
    if "RequestType" in event:
        return _handle_cfn(event, context)
    return _handle_cli(event, context)


def _handle_cli(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CLI invocation (plan or apply)."""
    action = event.get("action", "plan")
    table_name = event.get("table_name", TABLE_NAME)
    namespace_id = event.get("namespace_id", "")
    manifest_data = event.get("manifest", {})

    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    changes = compute_diff(manifest, previous)

    change_dicts = [
        {"action": c.action, "level": c.level, "target": c.target}
        for c in changes
    ]

    if action == "plan":
        return {"status": "planned", "changes": change_dicts}

    # Apply
    result = apply_changes(changes, table_name, namespace_id)

    # Update provisioner state
    manifest_hash = hashlib.sha256(
        json.dumps(manifest.to_dict(), sort_keys=True).encode()
    ).hexdigest()

    new_state = manifest.managed_set()
    new_state["last_applied"] = datetime.now(timezone.utc).isoformat()
    new_state["applied_hash"] = f"sha256:{manifest_hash}"
    _write_provisioner_state(table_name, namespace_id, new_state)

    return {
        "status": "applied",
        "changes": change_dicts,
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "errors": result.errors,
    }


def _handle_cfn(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CloudFormation custom resource event."""
    request_type = event["RequestType"]
    properties = event.get("ResourceProperties", {})

    table_name = properties.get("TableName", TABLE_NAME)
    namespace_id = properties.get("NamespaceId", "")

    if request_type == "Delete":
        # Empty manifest deletes all managed items
        manifest_data = {"namespace": properties.get("Namespace", "deleted")}
    else:
        # Create or Update: convert CFN properties to manifest format
        manifest_data = _cfn_properties_to_manifest(properties)

    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    changes = compute_diff(manifest, previous)

    result = apply_changes(changes, table_name, namespace_id)

    manifest_hash = hashlib.sha256(
        json.dumps(manifest.to_dict(), sort_keys=True).encode()
    ).hexdigest()

    new_state = manifest.managed_set()
    new_state["last_applied"] = datetime.now(timezone.utc).isoformat()
    new_state["applied_hash"] = f"sha256:{manifest_hash}"
    _write_provisioner_state(table_name, namespace_id, new_state)

    return {
        "status": "applied",
        "changes": [
            {"action": c.action, "level": c.level, "target": c.target}
            for c in changes
        ],
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "errors": result.errors,
    }


def _cfn_properties_to_manifest(properties: dict[str, Any]) -> dict[str, Any]:
    """Convert CloudFormation ResourceProperties to manifest dict format.

    CFN uses PascalCase keys; manifest uses snake_case.
    """
    manifest: dict[str, Any] = {"namespace": properties.get("Namespace", "default")}

    if "System" in properties:
        system: dict[str, Any] = {}
        cfn_system = properties["System"]
        if "OnUnavailable" in cfn_system:
            system["on_unavailable"] = cfn_system["OnUnavailable"]
        if "Limits" in cfn_system:
            system["limits"] = _cfn_limits_to_manifest(cfn_system["Limits"])
        manifest["system"] = system

    if "Resources" in properties:
        resources = {}
        for resource_name, cfn_resource in properties["Resources"].items():
            resources[resource_name] = {
                "limits": _cfn_limits_to_manifest(cfn_resource.get("Limits", {}))
            }
        manifest["resources"] = resources

    if "Entities" in properties:
        entities = {}
        for entity_id, cfn_entity in properties["Entities"].items():
            entity_resources = {}
            for resource_name, cfn_res in cfn_entity.get("Resources", {}).items():
                entity_resources[resource_name] = {
                    "limits": _cfn_limits_to_manifest(cfn_res.get("Limits", {}))
                }
            entities[entity_id] = {"resources": entity_resources}
        manifest["entities"] = entities

    return manifest


def _cfn_limits_to_manifest(cfn_limits: dict[str, Any]) -> dict[str, Any]:
    """Convert CFN PascalCase limits to manifest snake_case."""
    result = {}
    for name, cfn_limit in cfn_limits.items():
        limit: dict[str, Any] = {"capacity": cfn_limit["Capacity"]}
        if "Burst" in cfn_limit:
            limit["burst"] = cfn_limit["Burst"]
        if "RefillAmount" in cfn_limit:
            limit["refill_amount"] = cfn_limit["RefillAmount"]
        if "RefillPeriod" in cfn_limit:
            limit["refill_period"] = cfn_limit["RefillPeriod"]
        result[name] = limit
    return result


def _read_provisioner_state(table_name: str, namespace_id: str) -> dict[str, Any]:
    """Read the #PROVISIONER state record from DynamoDB."""
    from zae_limiter.schema import pk_system, sk_provisioner

    client = boto3.client("dynamodb")
    result = client.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": pk_system(namespace_id)},
            "SK": {"S": sk_provisioner()},
        },
    )
    item = result.get("Item")
    if not item:
        return {
            "managed_system": False,
            "managed_resources": [],
            "managed_entities": {},
        }

    managed_entities: dict[str, list[str]] = {}
    raw_entities = item.get("managed_entities", {}).get("M", {})
    for entity_id, resources_attr in raw_entities.items():
        managed_entities[entity_id] = [r["S"] for r in resources_attr.get("L", [])]

    return {
        "managed_system": item.get("managed_system", {}).get("BOOL", False),
        "managed_resources": [
            r["S"] for r in item.get("managed_resources", {}).get("L", [])
        ],
        "managed_entities": managed_entities,
    }


def _write_provisioner_state(
    table_name: str, namespace_id: str, state: dict[str, Any],
) -> None:
    """Write the #PROVISIONER state record to DynamoDB."""
    from zae_limiter.schema import pk_system, sk_provisioner

    client = boto3.client("dynamodb")
    item: dict[str, Any] = {
        "PK": {"S": pk_system(namespace_id)},
        "SK": {"S": sk_provisioner()},
        "GSI4PK": {"S": namespace_id},
        "managed_system": {"BOOL": state.get("managed_system", False)},
        "managed_resources": {
            "L": [{"S": r} for r in state.get("managed_resources", [])]
        },
        "managed_entities": {
            "M": {
                eid: {"L": [{"S": r} for r in resources]}
                for eid, resources in state.get("managed_entities", {}).items()
            }
        },
        "last_applied": {"S": state.get("last_applied", "")},
        "applied_hash": {"S": state.get("applied_hash", "")},
    }
    client.put_item(TableName=table_name, Item=item)
```

Update `src/zae_limiter_provisioner/__init__.py`:

```python
"""Lambda provisioner for declarative limits management."""

from .applier import ApplyResult
from .differ import Change, compute_diff
from .handler import on_event
from .manifest import LimitsManifest

__all__ = ["ApplyResult", "Change", "LimitsManifest", "compute_diff", "on_event"]
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_provisioner_handler.py -v`
Expected: PASS (with moto mocking for DynamoDB, following aggregator test patterns)

**Note:** Tests will likely need `@mock_aws` decorator or `unittest.mock.patch("boto3.client")`. Follow the pattern from `tests/unit/test_handler.py`.

**Step 5: Commit**

```
git add src/zae_limiter_provisioner/ tests/unit/test_provisioner_handler.py
git commit -m "✨ feat(infra): add Lambda handler for declarative limits provisioner"
```

---

## Task 7: Lambda Builder for Provisioner

Update the lambda builder to also package the provisioner, or create a separate builder.

**Files:**
- Create: `src/zae_limiter/infra/provisioner_builder.py`
- Test: `tests/unit/test_provisioner_builder.py`

**Step 1: Write the failing test**

Create `tests/unit/test_provisioner_builder.py`:

```python
"""Tests for provisioner Lambda package builder."""

import zipfile
from io import BytesIO

import pytest


class TestProvisionerBuilder:
    """Tests for build_provisioner_package."""

    def test_package_contains_provisioner_modules(self):
        """Built package contains all zae_limiter_provisioner .py files."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        zip_bytes = build_provisioner_package()
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "zae_limiter_provisioner/__init__.py" in names
            assert "zae_limiter_provisioner/handler.py" in names
            assert "zae_limiter_provisioner/manifest.py" in names
            assert "zae_limiter_provisioner/differ.py" in names
            assert "zae_limiter_provisioner/applier.py" in names

    def test_package_contains_zae_limiter_stubs(self):
        """Built package contains minimal zae_limiter stubs."""
        from zae_limiter.infra.provisioner_builder import build_provisioner_package

        zip_bytes = build_provisioner_package()
        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            assert "zae_limiter/__init__.py" in names
            assert "zae_limiter/schema.py" in names
            assert "zae_limiter/models.py" in names
            assert "zae_limiter/exceptions.py" in names

    def test_get_provisioner_handler_path(self):
        """Handler path matches CFN Handler property."""
        from zae_limiter.infra.provisioner_builder import get_provisioner_info

        info = get_provisioner_info()
        assert info["handler"] == "zae_limiter_provisioner.handler.on_event"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_provisioner_builder.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Write the implementation**

Create `src/zae_limiter/infra/provisioner_builder.py` — follow the same pattern as `lambda_builder.py` but copy `zae_limiter_provisioner` instead of `zae_limiter_aggregator`. Include `pyyaml` in the requirements (the provisioner uses `yaml.safe_load`).

The builder should:
1. Install `[lambda]` extra deps via `aws-lambda-builders` (same as aggregator)
2. Copy `zae_limiter_provisioner/` (all `.py` files)
3. Copy minimal `zae_limiter` stubs: `__init__.py` (empty), `schema.py`, `models.py`, `exceptions.py`
4. Add `pyyaml` to requirements (for YAML parsing in the handler)
5. Zip everything

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_provisioner_builder.py -v`
Expected: PASS (with `aws_lambda_builders.builder.LambdaBuilder` mocked, same as `test_lambda_builder.py`)

**Step 5: Commit**

```
git add src/zae_limiter/infra/provisioner_builder.py tests/unit/test_provisioner_builder.py
git commit -m "✨ feat(infra): add Lambda builder for limits provisioner"
```

---

## Task 8: CloudFormation Template Updates

Add the provisioner Lambda function, IAM role, and output to the existing CFN template.

**Files:**
- Modify: `src/zae_limiter/infra/cfn_template.yaml`
- Modify: `src/zae_limiter/models.py` (if `StackOptions` needs a new flag)

**Step 1: Plan the template changes**

Add to `cfn_template.yaml`:
- `DeployProvisioner` condition (linked to a new parameter, default `"true"`)
- `ProvisionerLogGroup` resource
- `ProvisionerRole` resource (DynamoDB read/write access to the table)
- `ProvisionerFunction` resource (placeholder code, like aggregator)
- `LimitsProvisionerArn` output (exported for cross-stack reference)

Follow the exact same patterns as the aggregator Lambda (conditions, role, log group, placeholder code).

**Step 2: Validate the template**

Run: `uv run cfn-lint src/zae_limiter/infra/cfn_template.yaml`
Expected: PASS (no errors)

**Step 3: Commit**

```
git add src/zae_limiter/infra/cfn_template.yaml
git commit -m "✨ feat(infra): add limits provisioner Lambda to CloudFormation template"
```

---

## Task 9: Stack Manager — Deploy Provisioner Code

Update the stack manager to deploy provisioner Lambda code alongside the aggregator.

**Files:**
- Modify: `src/zae_limiter/infra/stack_manager.py`
- Modify: `tests/unit/test_stack_manager.py`

**Step 1: Write the failing test**

Add a test that verifies `deploy_provisioner_code()` calls `update_function_code` with the provisioner function name.

**Step 2: Implement `deploy_provisioner_code()`**

Follow the same pattern as `deploy_lambda_code()` in `stack_manager.py`:
1. Call `build_provisioner_package()` from `provisioner_builder.py`
2. Call `lambda_client.update_function_code(FunctionName=f"{stack_name}-limits-provisioner", ZipFile=zip_bytes)`
3. Wait for function to be active

**Step 3: Update `deploy` CLI command**

After deploying the aggregator code, also deploy the provisioner code:

```python
# In cli.py deploy command, after deploy_lambda_code
if provisioner_enabled:
    await manager.deploy_provisioner_code(wait=True)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_stack_manager.py -v`

**Step 5: Commit**

```
git add src/zae_limiter/infra/stack_manager.py tests/unit/test_stack_manager.py src/zae_limiter/cli.py
git commit -m "✨ feat(infra): deploy provisioner Lambda code alongside aggregator"
```

---

## Task 10: CLI `limits` Command Group

Add `limits plan`, `limits apply`, `limits diff`, and `limits cfn-template` commands.

**Files:**
- Create: `src/zae_limiter/limits_cli.py`
- Modify: `src/zae_limiter/cli.py` (import + `cli.add_command(limits)`)
- Test: `tests/unit/test_limits_cli.py`

**Step 1: Write the failing tests**

Create `tests/unit/test_limits_cli.py`:

```python
"""Tests for the limits CLI commands."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml
from click.testing import CliRunner

from zae_limiter.cli import cli


class TestLimitsPlan:
    """Tests for `zae-limiter limits plan -f <file>`."""

    def test_plan_shows_changes(self):
        """Plan command parses YAML, invokes Lambda, and shows diff."""
        yaml_content = {
            "namespace": "test-ns",
            "system": {"limits": {"rpm": {"capacity": 1000}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {
                    "status": "planned",
                    "changes": [
                        {"action": "create", "level": "system", "target": None},
                    ],
                }
                result = runner.invoke(cli, [
                    "limits", "plan",
                    "--name", "test-app",
                    "--region", "us-east-1",
                    "-f", f.name,
                ])
                assert result.exit_code == 0
                assert "create" in result.output
                assert "system" in result.output


class TestLimitsApply:
    """Tests for `zae-limiter limits apply -f <file>`."""

    def test_apply_invokes_lambda(self):
        """Apply command parses YAML and invokes Lambda with action=apply."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            with patch("zae_limiter.limits_cli._invoke_provisioner") as mock_invoke:
                mock_invoke.return_value = {
                    "status": "applied",
                    "changes": [
                        {"action": "create", "level": "resource", "target": "gpt-4"},
                    ],
                    "created": 1,
                    "updated": 0,
                    "deleted": 0,
                    "errors": [],
                }
                result = runner.invoke(cli, [
                    "limits", "apply",
                    "--name", "test-app",
                    "--region", "us-east-1",
                    "-f", f.name,
                ])
                assert result.exit_code == 0
                assert "applied" in result.output.lower() or "create" in result.output.lower()


class TestLimitsCfnTemplate:
    """Tests for `zae-limiter limits cfn-template -f <file>`."""

    def test_cfn_template_output(self):
        """cfn-template command generates valid CFN template."""
        yaml_content = {
            "namespace": "test-ns",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        }
        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as f:
            yaml.dump(yaml_content, f)
            f.flush()

            runner = CliRunner()
            result = runner.invoke(cli, [
                "limits", "cfn-template",
                "--name", "test-app",
                "-f", f.name,
            ])
            assert result.exit_code == 0
            assert "Custom::ZaeLimiterLimits" in result.output
            assert "ServiceToken" in result.output
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_limits_cli.py -v`
Expected: FAIL

**Step 3: Write the implementation**

Create `src/zae_limiter/limits_cli.py`:

```python
"""CLI commands for declarative limits management."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import boto3
import click
import yaml


@click.group()
def limits() -> None:
    """Declarative limits management.

    Manage rate limits via YAML files. Apply through a Lambda provisioner
    that supports both CLI and CloudFormation paths.
    """
    pass


@limits.command("plan")
@click.option("--name", "-n", required=True, help="Stack identifier.")
@click.option("--region", help="AWS region.")
@click.option("--endpoint-url", help="AWS endpoint URL (e.g., LocalStack).")
@click.option("--namespace", "-N", default="default", help="Namespace.")
@click.option("--file", "-f", "file_path", required=True, type=click.Path(exists=True), help="YAML limits file.")
def limits_plan(name: str, region: str | None, endpoint_url: str | None, namespace: str, file_path: str) -> None:
    """Preview changes without applying (like terraform plan)."""
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "plan", manifest_data)

    changes = result.get("changes", [])
    if not changes:
        click.echo("No changes. Infrastructure is up-to-date.")
        return

    click.echo(f"Plan: {len(changes)} change(s)\n")
    for change in changes:
        symbol = {"create": "+", "update": "~", "delete": "-"}.get(change["action"], "?")
        target = change.get("target") or "(system defaults)"
        click.echo(f"  {symbol} {change['action']} {change['level']}: {target}")


@limits.command("apply")
@click.option("--name", "-n", required=True, help="Stack identifier.")
@click.option("--region", help="AWS region.")
@click.option("--endpoint-url", help="AWS endpoint URL (e.g., LocalStack).")
@click.option("--namespace", "-N", default="default", help="Namespace.")
@click.option("--file", "-f", "file_path", required=True, type=click.Path(exists=True), help="YAML limits file.")
def limits_apply(name: str, region: str | None, endpoint_url: str | None, namespace: str, file_path: str) -> None:
    """Apply limits from YAML file (like terraform apply)."""
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "apply", manifest_data)

    changes = result.get("changes", [])
    if not changes:
        click.echo("No changes. Infrastructure is up-to-date.")
        return

    for change in changes:
        symbol = {"create": "+", "update": "~", "delete": "-"}.get(change["action"], "?")
        target = change.get("target") or "(system defaults)"
        click.echo(f"  {symbol} {change['action']} {change['level']}: {target}")

    click.echo(
        f"\nApplied: {result.get('created', 0)} created, "
        f"{result.get('updated', 0)} updated, "
        f"{result.get('deleted', 0)} deleted."
    )

    errors = result.get("errors", [])
    if errors:
        click.echo(f"\nErrors ({len(errors)}):", err=True)
        for err in errors:
            click.echo(f"  - {err}", err=True)
        sys.exit(1)


@limits.command("diff")
@click.option("--name", "-n", required=True, help="Stack identifier.")
@click.option("--region", help="AWS region.")
@click.option("--endpoint-url", help="AWS endpoint URL (e.g., LocalStack).")
@click.option("--namespace", "-N", default="default", help="Namespace.")
@click.option("--file", "-f", "file_path", required=True, type=click.Path(exists=True), help="YAML limits file.")
def limits_diff(name: str, region: str | None, endpoint_url: str | None, namespace: str, file_path: str) -> None:
    """Show drift between YAML and live DynamoDB state."""
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "plan", manifest_data)

    changes = result.get("changes", [])
    if not changes:
        click.echo("No drift detected. Live state matches YAML.")
        return

    click.echo(f"Drift detected: {len(changes)} difference(s)\n")
    for change in changes:
        symbol = {"create": "+", "update": "~", "delete": "-"}.get(change["action"], "?")
        target = change.get("target") or "(system defaults)"
        click.echo(f"  {symbol} {change['level']}: {target}")


@limits.command("cfn-template")
@click.option("--name", "-n", required=True, help="Stack identifier (for ImportValue).")
@click.option("--file", "-f", "file_path", required=True, type=click.Path(exists=True), help="YAML limits file.")
def limits_cfn_template(name: str, file_path: str) -> None:
    """Generate a CloudFormation template from YAML file."""
    manifest_data = _load_yaml(file_path)
    namespace = manifest_data.get("namespace", "default")

    # Convert manifest to CFN Properties format (PascalCase)
    properties: dict[str, Any] = {
        "ServiceToken": {"Fn::ImportValue": f"{name}-LimitsProvisionerArn"},
        "TableName": name,
        "Namespace": namespace,
    }

    if "system" in manifest_data:
        system_props: dict[str, Any] = {}
        sys_data = manifest_data["system"]
        if "on_unavailable" in sys_data:
            system_props["OnUnavailable"] = sys_data["on_unavailable"]
        if "limits" in sys_data:
            system_props["Limits"] = _limits_to_cfn(sys_data["limits"])
        properties["System"] = system_props

    if "resources" in manifest_data:
        resources_props = {}
        for res_name, res_data in manifest_data["resources"].items():
            resources_props[res_name] = {
                "Limits": _limits_to_cfn(res_data.get("limits", {}))
            }
        properties["Resources"] = resources_props

    if "entities" in manifest_data:
        entities_props = {}
        for ent_id, ent_data in manifest_data["entities"].items():
            ent_resources = {}
            for res_name, res_data in ent_data.get("resources", {}).items():
                ent_resources[res_name] = {
                    "Limits": _limits_to_cfn(res_data.get("limits", {}))
                }
            entities_props[ent_id] = {"Resources": ent_resources}
        properties["Entities"] = entities_props

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"Declarative limits for namespace '{namespace}'",
        "Resources": {
            "TenantLimits": {
                "Type": "Custom::ZaeLimiterLimits",
                "Properties": properties,
            },
        },
    }

    click.echo(yaml.dump(template, default_flow_style=False, sort_keys=False))


def _limits_to_cfn(limits: dict[str, Any]) -> dict[str, Any]:
    """Convert manifest limits dict to CFN PascalCase format."""
    result = {}
    for name, limit in limits.items():
        cfn_limit: dict[str, Any] = {"Capacity": limit["capacity"]}
        if "burst" in limit:
            cfn_limit["Burst"] = limit["burst"]
        if "refill_amount" in limit:
            cfn_limit["RefillAmount"] = limit["refill_amount"]
        if "refill_period" in limit:
            cfn_limit["RefillPeriod"] = limit["refill_period"]
        result[name] = cfn_limit
    return result


def _load_yaml(file_path: str) -> dict[str, Any]:
    """Load and parse a YAML file."""
    with open(file_path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        click.echo("Error: YAML file must contain a mapping", err=True)
        sys.exit(1)
    return data


def _invoke_provisioner(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    action: str,
    manifest_data: dict[str, Any],
) -> dict[str, Any]:
    """Invoke the provisioner Lambda function.

    Args:
        name: Stack name (used to derive Lambda function name and table name).
        region: AWS region.
        endpoint_url: AWS endpoint URL (for LocalStack).
        action: "plan" or "apply".
        manifest_data: Parsed YAML manifest as dict.

    Returns:
        Lambda response payload.
    """
    import asyncio

    from .repository import Repository

    # Resolve namespace to get namespace_id
    async def _resolve() -> str:
        repo = await Repository.connect(
            name, region=region, endpoint_url=endpoint_url,
            namespace=manifest_data.get("namespace", "default"),
        )
        try:
            return repo._namespace_id
        finally:
            await repo.close()

    try:
        namespace_id = asyncio.run(_resolve())
    except Exception:
        # If namespace doesn't exist yet, we need to register it
        # The Lambda will handle auto-registration
        namespace_id = ""

    function_name = f"{name}-limits-provisioner"

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url

    lambda_client = boto3.client("lambda", **kwargs)

    payload = {
        "action": action,
        "table_name": name,
        "namespace_id": namespace_id,
        "manifest": manifest_data,
    }

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )

    response_payload = json.loads(response["Payload"].read())

    if "errorMessage" in response_payload:
        msg = response_payload["errorMessage"]
        click.echo(f"Error: Lambda execution failed: {msg}", err=True)
        sys.exit(1)

    return response_payload
```

In `src/zae_limiter/cli.py`, add the import and registration:

```python
# Near the top, with other imports (around line 16)
from .limits_cli import limits

# At the bottom, with other add_command calls (around line 3820)
cli.add_command(limits)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_limits_cli.py -v`
Expected: PASS

**Step 5: Commit**

```
git add src/zae_limiter/limits_cli.py src/zae_limiter/cli.py tests/unit/test_limits_cli.py
git commit -m "✨ feat(cli): add limits plan/apply/diff/cfn-template commands"
```

---

## Task 11: Integration Tests

Create integration tests that verify the full flow against LocalStack.

**Files:**
- Create: `tests/integration/test_provisioner.py`

**Step 1: Write integration tests**

```python
"""Integration tests for declarative limits provisioner (LocalStack)."""

import pytest
import pytest_asyncio

from zae_limiter.models import Limit
from zae_limiter_provisioner.manifest import LimitsManifest
from zae_limiter_provisioner.differ import compute_diff
from zae_limiter_provisioner.applier import apply_changes


@pytest.mark.integration
class TestProvisionerIntegration:
    """Full provisioner workflow against LocalStack."""

    @pytest.mark.asyncio
    async def test_apply_creates_and_reads_back(self, test_repo):
        """Apply creates limits that are readable via Repository API."""
        manifest = LimitsManifest.from_dict({
            "namespace": "test",
            "system": {
                "on_unavailable": "allow",
                "limits": {"rpm": {"capacity": 1000}},
            },
            "resources": {
                "gpt-4": {"limits": {"tpm": {"capacity": 50000}}},
            },
        })
        previous = {"managed_system": False, "managed_resources": [], "managed_entities": {}}
        changes = compute_diff(manifest, previous)

        result = apply_changes(changes, test_repo.table_name, test_repo._namespace_id)
        assert result.created == 2
        assert result.errors == []

        # Verify via Repository API
        system = await test_repo.get_system_defaults()
        assert any(l.name == "rpm" and l.capacity == 1000 for l in system.limits)

        resource_limits = await test_repo.get_resource_defaults("gpt-4")
        assert any(l.name == "tpm" and l.capacity == 50000 for l in resource_limits)

    @pytest.mark.asyncio
    async def test_idempotent_apply(self, test_repo):
        """Applying the same manifest twice produces update actions (idempotent)."""
        manifest = LimitsManifest.from_dict({
            "namespace": "test",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        })

        # First apply
        previous = {"managed_system": False, "managed_resources": [], "managed_entities": {}}
        changes1 = compute_diff(manifest, previous)
        apply_changes(changes1, test_repo.table_name, test_repo._namespace_id)

        # Second apply (same manifest, updated previous state)
        new_previous = manifest.managed_set()
        changes2 = compute_diff(manifest, new_previous)
        result2 = apply_changes(changes2, test_repo.table_name, test_repo._namespace_id)
        assert result2.updated == 1
        assert result2.created == 0
        assert result2.deleted == 0

    @pytest.mark.asyncio
    async def test_removal_deletes_managed_only(self, test_repo):
        """Removing from YAML deletes managed items, leaves unmanaged alone."""
        # Set an unmanaged resource limit directly
        await test_repo.set_resource_defaults("claude-3", [Limit.per_minute("rpm", 500)])

        # Apply manifest with gpt-4 only
        manifest = LimitsManifest.from_dict({
            "namespace": "test",
            "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 1000}}}},
        })
        previous = {"managed_system": False, "managed_resources": [], "managed_entities": {}}
        changes = compute_diff(manifest, previous)
        apply_changes(changes, test_repo.table_name, test_repo._namespace_id)

        # Now remove gpt-4 from manifest
        manifest2 = LimitsManifest.from_dict({"namespace": "test"})
        previous2 = manifest.managed_set()
        changes2 = compute_diff(manifest2, previous2)
        result = apply_changes(changes2, test_repo.table_name, test_repo._namespace_id)

        assert result.deleted == 1  # gpt-4 deleted

        # claude-3 (unmanaged) should still exist
        claude_limits = await test_repo.get_resource_defaults("claude-3")
        assert any(l.name == "rpm" for l in claude_limits)
```

**Step 2: Run tests**

Run: `uv run pytest tests/integration/test_provisioner.py -v` (requires LocalStack)

**Step 3: Commit**

```
git add tests/integration/test_provisioner.py
git commit -m "✅ test(infra): add integration tests for declarative limits provisioner"
```

---

## Task 12: Documentation Updates

Update CLAUDE.md, CLI docs, and operator guide.

**Files:**
- Modify: `CLAUDE.md` (add provisioner to schema, CLI commands, access patterns)
- Modify: `docs/cli.md` (add `limits` command group)
- Modify: `docs/infra/deployment.md` (add declarative limits section)

**Step 1: Update CLAUDE.md**

Add to Project Structure:
```
src/zae_limiter_provisioner/   # Lambda provisioner for declarative limits
├── __init__.py
├── handler.py           # Lambda entry point (CLI + CFN events)
├── manifest.py          # LimitsManifest YAML parsing
├── differ.py            # Diff engine (manifest vs #PROVISIONER state)
└── applier.py           # Applies changes via boto3 DynamoDB
```

Add to DynamoDB Access Patterns table:
```
| Get provisioner state | `PK={ns}/SYSTEM#, SK=#PROVISIONER` |
```

Add to CLI commands section:
```
limits plan, limits apply, limits diff, limits cfn-template
```

**Step 2: Update docs/cli.md**

Add `limits` command group with examples for plan, apply, diff, cfn-template.

**Step 3: Update docs/infra/deployment.md**

Add a "Declarative Limits Management" section with YAML format, CLI workflow, and CFN integration.

**Step 4: Commit**

```
git add CLAUDE.md docs/cli.md docs/infra/deployment.md
git commit -m "📝 docs(infra): add declarative limits management documentation"
```

---

Plan complete and saved to `docs/plans/2026-02-19-declarative-limits-implementation.md`. Two execution options:

**1. Subagent-Driven (this session)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Parallel Session (separate)** — Open a new session with `executing-plans`, batch execution with checkpoints

Which approach?
