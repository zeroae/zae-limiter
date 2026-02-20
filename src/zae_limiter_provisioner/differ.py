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
        changes.append(
            Change(
                action=action,
                level="system",
                target=None,
                data=manifest.system.to_dict(),
            )
        )
    elif prev_system:
        changes.append(Change(action="delete", level="system", target=None))

    # --- Resources ---
    prev_resources = set(previous.get("managed_resources", []))
    curr_resources = set(manifest.resources.keys())

    for resource in sorted(curr_resources):
        action = "update" if resource in prev_resources else "create"
        changes.append(
            Change(
                action=action,
                level="resource",
                target=resource,
                data=manifest.resources[resource].to_dict(),
            )
        )

    for resource in sorted(prev_resources - curr_resources):
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

    for entity_id, resource in sorted(curr_entity_resources):
        target = f"{entity_id}/{resource}"
        action = "update" if (entity_id, resource) in prev_entity_resources else "create"
        changes.append(
            Change(
                action=action,
                level="entity",
                target=target,
                data=manifest.entities[entity_id].resources[resource].to_dict(),
            )
        )

    for entity_id, resource in sorted(prev_entity_resources - curr_entity_resources):
        target = f"{entity_id}/{resource}"
        changes.append(Change(action="delete", level="entity", target=target))

    return changes
