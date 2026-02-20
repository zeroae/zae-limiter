"""YAML manifest parsing and validation for declarative limits."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LimitDecl:
    """A single limit declaration with shorthand defaults.

    ``capacity`` is the bucket ceiling (max tokens). The separate ``burst``
    field was removed in the Limit model refactor — callers that want burst
    behaviour should set ``capacity`` to the burst value directly.
    """

    capacity: int
    refill_amount: int
    refill_period: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LimitDecl:
        capacity = d["capacity"]
        # Accept "burst" from YAML for backwards-compat: use it as capacity
        if "burst" in d:
            capacity = d["burst"]
        return cls(
            capacity=capacity,
            refill_amount=d.get("refill_amount", capacity),
            refill_period=d.get("refill_period", 60),
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "capacity": self.capacity,
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
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
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
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
        return cls(limits=limits)

    def to_dict(self) -> dict[str, Any]:
        return {"limits": {name: lim.to_dict() for name, lim in self.limits.items()}}


@dataclass(frozen=True)
class EntityResourceDecl:
    """Entity-resource-level limit declaration."""

    limits: dict[str, LimitDecl]

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EntityResourceDecl:
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
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
            name: EntityResourceDecl.from_dict(val) for name, val in d.get("resources", {}).items()
        }
        return cls(resources=resources)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": {name: res.to_dict() for name, res in self.resources.items()},
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
            name: ResourceDecl.from_dict(val) for name, val in d.get("resources", {}).items()
        }

        entities = {name: EntityDecl.from_dict(val) for name, val in d.get("entities", {}).items()}

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
            result["resources"] = {name: res.to_dict() for name, res in self.resources.items()}
        if self.entities:
            result["entities"] = {name: ent.to_dict() for name, ent in self.entities.items()}
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
