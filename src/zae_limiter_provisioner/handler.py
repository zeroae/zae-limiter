"""Lambda handler for declarative limits provisioner.

Handles two event types:
1. CLI invocations (action, manifest, table_name, namespace_id)
2. CloudFormation custom resource events (RequestType, ResourceProperties)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.request
from datetime import UTC, datetime
from typing import Any

import boto3

from zae_limiter.schema import RESERVED_NAMESPACE, pk_system, sk_namespace, sk_provisioner

from .applier import apply_changes
from .differ import Change, compute_diff
from .fanout import fanout_entity, fanout_resource
from .manifest import LimitsManifest

logger = logging.getLogger(__name__)

TABLE_NAME = os.environ.get("TABLE_NAME", "rate-limits")


def on_event(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point."""
    if "RequestType" in event:
        return _handle_cfn_with_response(event, context)
    return _handle_cli(event, context)


def _handle_cfn_with_response(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CFN custom resource event with response protocol.

    Wraps _handle_cfn to send SUCCESS/FAILED response to CloudFormation's
    pre-signed ResponseURL. Without this, CloudFormation will hang waiting
    for a response and eventually time out.
    """
    physical_resource_id = event.get("PhysicalResourceId", "")
    try:
        result = _handle_cfn(event, context)
        physical_resource_id = result.pop("physical_resource_id", physical_resource_id)
        if "ResponseURL" in event:
            _send_cfn_response(
                event,
                context,
                "SUCCESS",
                data=result,
                physical_resource_id=physical_resource_id,
            )
        return result
    except Exception as e:
        logger.exception("CFN handler failed")
        if "ResponseURL" in event:
            _send_cfn_response(
                event,
                context,
                "FAILED",
                reason=str(e),
                physical_resource_id=physical_resource_id or "failed",
            )
        raise


def _handle_cli(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Handle CLI invocation (plan or apply)."""
    action = event.get("action", "plan")
    table_name = event.get("table_name", TABLE_NAME)
    namespace_id = event.get("namespace_id", "")
    manifest_data = event.get("manifest", {})

    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    changes = compute_diff(manifest, previous)

    change_dicts = [{"action": c.action, "level": c.level, "target": c.target} for c in changes]

    if action == "plan":
        return {"status": "planned", "changes": change_dicts}

    # Apply
    result = apply_changes(changes, table_name, namespace_id)
    _fanout_disabled_changes(table_name, namespace_id, changes)

    # Update provisioner state
    manifest_hash = hashlib.sha256(
        json.dumps(manifest.to_dict(), sort_keys=True).encode()
    ).hexdigest()

    new_state = manifest.managed_set()
    new_state["last_applied"] = datetime.now(UTC).isoformat()
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
    if not namespace_id:
        namespace_name = properties.get("Namespace", "default")
        namespace_id = _resolve_namespace_id(table_name, namespace_name)

    physical_resource_id = f"{table_name}/{namespace_id}/limits-provisioner"

    if request_type == "Delete":
        # Empty manifest deletes all managed items
        manifest_data: dict[str, Any] = {"namespace": properties.get("Namespace", "deleted")}
    else:
        # Create or Update: convert CFN properties to manifest format
        manifest_data = _cfn_properties_to_manifest(properties)

    manifest = LimitsManifest.from_dict(manifest_data)
    previous = _read_provisioner_state(table_name, namespace_id)
    changes = compute_diff(manifest, previous)

    result = apply_changes(changes, table_name, namespace_id)
    _fanout_disabled_changes(table_name, namespace_id, changes)

    manifest_hash = hashlib.sha256(
        json.dumps(manifest.to_dict(), sort_keys=True).encode()
    ).hexdigest()

    new_state = manifest.managed_set()
    new_state["last_applied"] = datetime.now(UTC).isoformat()
    new_state["applied_hash"] = f"sha256:{manifest_hash}"
    _write_provisioner_state(table_name, namespace_id, new_state)

    return {
        "physical_resource_id": physical_resource_id,
        "status": "applied",
        "changes": [{"action": c.action, "level": c.level, "target": c.target} for c in changes],
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "errors": result.errors,
    }


def _fanout_disabled_changes(
    table_name: str,
    namespace_id: str,
    changes: list[Change],
) -> None:
    """Eagerly stamp bucket items for any change that sets `disabled` (ADR-125).

    Resource-level changes are applied before entity-level ones so that an
    entity-level carve-out re-stamps its own buckets last and wins — see
    ``fanout.py``'s module docstring for why the fan-out is order-dependent.
    Deletes never carry a `disabled` key (Change.data is None for deletes),
    so removing a managed item never touches bucket stamps; that mirrors
    delete_resource_defaults()/delete_limits() on the async Repository, which
    are likewise decoupled from disable_resource()/disable_entity().
    """
    candidates = [c for c in changes if c.data and "disabled" in c.data]
    if not candidates:
        return

    client = boto3.client("dynamodb")
    for change in sorted(candidates, key=lambda c: 0 if c.level == "resource" else 1):
        data = change.data or {}
        disabled = bool(data["disabled"])
        if change.level == "resource" and change.target:
            fanout_resource(client, table_name, namespace_id, change.target, disabled)
        elif change.level == "entity" and change.target:
            entity_id, resource = change.target.split("/", 1)
            fanout_entity(client, table_name, namespace_id, entity_id, resource, disabled)


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
            resource_entry: dict[str, Any] = {
                "limits": _cfn_limits_to_manifest(cfn_resource.get("Limits", {}))
            }
            # Tri-state: only set "disabled" when "Disabled" is present in the CFN
            # properties. An explicit False must survive (it's the carve-out value);
            # an absent key must NOT be coerced to False, or every apply would
            # re-enable anything the operator previously disabled out-of-band.
            if "Disabled" in cfn_resource:
                resource_entry["disabled"] = cfn_resource["Disabled"]
            resources[resource_name] = resource_entry
        manifest["resources"] = resources

    if "Entities" in properties:
        entities = {}
        for entity_id, cfn_entity in properties["Entities"].items():
            entity_resources = {}
            for resource_name, cfn_res in cfn_entity.get("Resources", {}).items():
                entity_resource_entry: dict[str, Any] = {
                    "limits": _cfn_limits_to_manifest(cfn_res.get("Limits", {}))
                }
                if "Disabled" in cfn_res:
                    entity_resource_entry["disabled"] = cfn_res["Disabled"]
                entity_resources[resource_name] = entity_resource_entry
            entities[entity_id] = {"resources": entity_resources}
        manifest["entities"] = entities

    return manifest


def _cfn_limits_to_manifest(cfn_limits: dict[str, Any]) -> dict[str, Any]:
    """Convert CFN PascalCase limits to manifest snake_case."""
    result = {}
    for name, cfn_limit in cfn_limits.items():
        limit: dict[str, Any] = {"capacity": cfn_limit["Capacity"]}
        if "RefillAmount" in cfn_limit:
            limit["refill_amount"] = cfn_limit["RefillAmount"]
        if "RefillPeriod" in cfn_limit:
            limit["refill_period"] = cfn_limit["RefillPeriod"]
        result[name] = limit
    return result


def _read_provisioner_state(table_name: str, namespace_id: str) -> dict[str, Any]:
    """Read the #PROVISIONER state record from DynamoDB."""
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
        "managed_resources": [r["S"] for r in item.get("managed_resources", {}).get("L", [])],
        "managed_entities": managed_entities,
    }


def _write_provisioner_state(
    table_name: str,
    namespace_id: str,
    state: dict[str, Any],
) -> None:
    """Write the #PROVISIONER state record to DynamoDB."""
    client = boto3.client("dynamodb")
    item: dict[str, Any] = {
        "PK": {"S": pk_system(namespace_id)},
        "SK": {"S": sk_provisioner()},
        "GSI4PK": {"S": namespace_id},
        "managed_system": {"BOOL": state.get("managed_system", False)},
        "managed_resources": {"L": [{"S": r} for r in state.get("managed_resources", [])]},
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


def _resolve_namespace_id(table_name: str, namespace_name: str) -> str:
    """Resolve namespace name to opaque ID via DynamoDB lookup.

    Args:
        table_name: DynamoDB table name.
        namespace_name: Human-readable namespace name (e.g., "default").

    Returns:
        Opaque namespace ID string.

    Raises:
        ValueError: If the namespace is not registered.
    """
    client = boto3.client("dynamodb")
    result = client.get_item(
        TableName=table_name,
        Key={
            "PK": {"S": pk_system(RESERVED_NAMESPACE)},
            "SK": {"S": sk_namespace(namespace_name)},
        },
    )
    item = result.get("Item")
    if not item:
        raise ValueError(f"Namespace '{namespace_name}' not found in table '{table_name}'")
    return item["namespace_id"]["S"]


def _send_cfn_response(
    event: dict[str, Any],
    context: Any,
    status: str,
    *,
    data: dict[str, Any] | None = None,
    physical_resource_id: str | None = None,
    reason: str | None = None,
) -> None:
    """Send response to CloudFormation pre-signed URL.

    CloudFormation custom resources require a PUT to the ResponseURL
    with status information. Without this, the stack operation hangs
    until timeout (default 1 hour).
    """
    response_url = event["ResponseURL"]

    log_stream = getattr(context, "log_stream_name", "unknown") if context else "unknown"

    response_body = {
        "Status": status,
        "Reason": reason or f"See CloudWatch Log Stream: {log_stream}",
        "PhysicalResourceId": physical_resource_id or log_stream,
        "StackId": event["StackId"],
        "RequestId": event["RequestId"],
        "LogicalResourceId": event["LogicalResourceId"],
        "Data": data or {},
    }

    body = json.dumps(response_body).encode()
    req = urllib.request.Request(
        response_url,
        data=body,
        headers={"Content-Type": "", "Content-Length": str(len(body))},
        method="PUT",
    )
    urllib.request.urlopen(req)
