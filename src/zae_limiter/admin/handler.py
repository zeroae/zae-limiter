"""Lambda handler for Admin API.

This module handles API Gateway requests for the admin REST API.
Uses boto3 directly (sync) since Lambda runtime doesn't include aioboto3.
"""

import json
import os
import time
import traceback
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError
from ulid import ULID

from .. import schema
from ..models import (
    AuditAction,
    AuditEvent,
    BucketState,
    Entity,
    Limit,
    validate_identifier,
)
from ..naming import normalize_stack_name

# Environment variables
TABLE_NAME = os.environ.get("TABLE_NAME", "")
STACK_NAME = os.environ.get("STACK_NAME", "")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


class StructuredLogger:
    """JSON-formatted logger for CloudWatch Logs Insights."""

    def __init__(self, name: str):
        self._name = name

    def _log(self, level: str, message: str, **extra: Any) -> None:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": level,
            "logger": self._name,
            "message": message,
            **extra,
        }
        print(json.dumps(log_entry))

    def info(self, message: str, **extra: Any) -> None:
        self._log("INFO", message, **extra)

    def warning(self, message: str, exc_info: bool = False, **extra: Any) -> None:
        if exc_info:
            extra["exception"] = traceback.format_exc()
        self._log("WARNING", message, **extra)

    def error(self, message: str, exc_info: bool = False, **extra: Any) -> None:
        if exc_info:
            extra["exception"] = traceback.format_exc()
        self._log("ERROR", message, **extra)


logger = StructuredLogger(__name__)


class AdminRepository:
    """Sync DynamoDB repository for admin operations using boto3."""

    def __init__(self, table_name: str):
        self.table_name = normalize_stack_name(table_name) if table_name else ""
        self._client = boto3.client("dynamodb")

    def _now_ms(self) -> int:
        return int(time.time() * 1000)

    def _serialize_map(self, data: dict[str, Any]) -> dict[str, Any]:
        """Serialize a Python dict to DynamoDB map format."""
        result: dict[str, Any] = {}
        for key, value in data.items():
            result[key] = self._serialize_value(value)
        return result

    def _serialize_value(self, value: Any) -> dict[str, Any]:
        """Serialize a Python value to DynamoDB attribute format."""
        if value is None:
            return {"NULL": True}
        elif isinstance(value, bool):
            return {"BOOL": value}
        elif isinstance(value, str):
            return {"S": value}
        elif isinstance(value, int | float):
            return {"N": str(value)}
        elif isinstance(value, list):
            return {"L": [self._serialize_value(v) for v in value]}
        elif isinstance(value, dict):
            return {"M": self._serialize_map(value)}
        else:
            return {"S": str(value)}

    def _deserialize_value(self, attr: dict[str, Any]) -> Any:
        """Deserialize a DynamoDB attribute to Python value."""
        if "S" in attr:
            return attr["S"]
        elif "N" in attr:
            num_str = attr["N"]
            return int(num_str) if "." not in num_str else float(num_str)
        elif "BOOL" in attr:
            return attr["BOOL"]
        elif "NULL" in attr:
            return None
        elif "L" in attr:
            return [self._deserialize_value(v) for v in attr["L"]]
        elif "M" in attr:
            return {k: self._deserialize_value(v) for k, v in attr["M"].items()}
        return None

    def _deserialize_map(self, attr: dict[str, Any]) -> dict[str, Any]:
        """Deserialize a DynamoDB map to Python dict."""
        if "M" in attr:
            return {k: self._deserialize_value(v) for k, v in attr["M"].items()}
        return {}

    # -------------------------------------------------------------------------
    # Entity operations
    # -------------------------------------------------------------------------

    def get_entity(self, entity_id: str) -> Entity | None:
        """Get an entity by ID."""
        response = self._client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_meta()},
            },
        )
        item = response.get("Item")
        if not item:
            return None

        data = self._deserialize_map(item.get("data", {}))
        return Entity(
            id=entity_id,
            name=data.get("name", entity_id),
            parent_id=data.get("parent_id"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
        )

    def create_entity(
        self,
        entity_id: str,
        name: str | None = None,
        parent_id: str | None = None,
        metadata: dict[str, str] | None = None,
        principal: str | None = None,
    ) -> Entity:
        """Create a new entity."""
        validate_identifier(entity_id, "entity_id")
        if parent_id:
            validate_identifier(parent_id, "parent_id")

        created_at = datetime.now(UTC).isoformat()
        entity_name = name or entity_id

        item: dict[str, Any] = {
            "PK": {"S": schema.pk_entity(entity_id)},
            "SK": {"S": schema.sk_meta()},
            "entity_id": {"S": entity_id},
            "data": {
                "M": {
                    "name": {"S": entity_name},
                    "metadata": {"M": self._serialize_map(metadata or {})},
                    "created_at": {"S": created_at},
                }
            },
        }

        if parent_id:
            item["data"]["M"]["parent_id"] = {"S": parent_id}
            item["GSI1PK"] = {"S": schema.gsi1_pk_parent(parent_id)}
            item["GSI1SK"] = {"S": schema.gsi1_sk_child(entity_id)}

        self._client.put_item(
            TableName=self.table_name,
            Item=item,
            ConditionExpression="attribute_not_exists(PK)",
        )

        # Log audit event
        self._log_audit_event(
            action=AuditAction.ENTITY_CREATED,
            entity_id=entity_id,
            principal=principal,
            details={
                "name": entity_name,
                "parent_id": parent_id,
                "metadata": metadata or {},
            },
        )

        return Entity(
            id=entity_id,
            name=entity_name,
            parent_id=parent_id,
            metadata=metadata or {},
            created_at=created_at,
        )

    def update_entity(
        self,
        entity_id: str,
        name: str | None = None,
        metadata: dict[str, str] | None = None,
        parent_id: str | None = None,
        clear_parent: bool = False,
        principal: str | None = None,
    ) -> Entity:
        """Update an existing entity."""
        validate_identifier(entity_id, "entity_id")
        if parent_id:
            validate_identifier(parent_id, "parent_id")

        # Get existing entity
        existing = self.get_entity(entity_id)
        if existing is None:
            raise ValueError(f"Entity not found: {entity_id}")

        update_parts: list[str] = []
        remove_parts: list[str] = []
        expr_names: dict[str, str] = {"#data": "data"}
        expr_values: dict[str, Any] = {}
        changes: dict[str, Any] = {}

        new_name = existing.name
        new_metadata = existing.metadata
        new_parent_id = existing.parent_id

        if name is not None:
            update_parts.append("#data.#name = :name")
            expr_names["#name"] = "name"
            expr_values[":name"] = {"S": name}
            new_name = name
            changes["name"] = name

        if metadata is not None:
            update_parts.append("#data.#metadata = :metadata")
            expr_names["#metadata"] = "metadata"
            expr_values[":metadata"] = {"M": self._serialize_map(metadata)}
            new_metadata = metadata
            changes["metadata"] = metadata

        if clear_parent:
            update_parts.append("#data.#parent_id = :parent_id")
            expr_names["#parent_id"] = "parent_id"
            expr_values[":parent_id"] = {"NULL": True}
            remove_parts.append("#gsi1pk")
            remove_parts.append("#gsi1sk")
            expr_names["#gsi1pk"] = "GSI1PK"
            expr_names["#gsi1sk"] = "GSI1SK"
            new_parent_id = None
            changes["parent_id"] = {"old": existing.parent_id, "new": None}
        elif parent_id is not None and parent_id != existing.parent_id:
            update_parts.append("#data.#parent_id = :parent_id")
            expr_names["#parent_id"] = "parent_id"
            expr_values[":parent_id"] = {"S": parent_id}
            update_parts.append("#gsi1pk = :gsi1pk")
            update_parts.append("#gsi1sk = :gsi1sk")
            expr_names["#gsi1pk"] = "GSI1PK"
            expr_names["#gsi1sk"] = "GSI1SK"
            expr_values[":gsi1pk"] = {"S": schema.gsi1_pk_parent(parent_id)}
            expr_values[":gsi1sk"] = {"S": schema.gsi1_sk_child(entity_id)}
            new_parent_id = parent_id
            changes["parent_id"] = {"old": existing.parent_id, "new": parent_id}

        if not update_parts and not remove_parts:
            return existing

        update_expr = ""
        if update_parts:
            update_expr = "SET " + ", ".join(update_parts)
        if remove_parts:
            if update_expr:
                update_expr += " "
            update_expr += "REMOVE " + ", ".join(remove_parts)

        self._client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_meta()},
            },
            UpdateExpression=update_expr,
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values if expr_values else None,
            ConditionExpression="attribute_exists(PK)",
        )

        self._log_audit_event(
            action=AuditAction.ENTITY_UPDATED,
            entity_id=entity_id,
            principal=principal,
            details=changes,
        )

        return Entity(
            id=entity_id,
            name=new_name,
            parent_id=new_parent_id,
            metadata=new_metadata,
            created_at=existing.created_at,
        )

    def delete_entity(self, entity_id: str, principal: str | None = None) -> None:
        """Delete an entity and all related data."""
        # Query all items for this entity
        response = self._client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": {"S": schema.pk_entity(entity_id)}},
        )

        items = response.get("Items", [])
        if not items:
            return

        # Batch delete in chunks of 25
        for i in range(0, len(items), 25):
            chunk = items[i : i + 25]
            delete_requests = [
                {"DeleteRequest": {"Key": {"PK": item["PK"], "SK": item["SK"]}}} for item in chunk
            ]
            self._client.batch_write_item(RequestItems={self.table_name: delete_requests})

        self._log_audit_event(
            action=AuditAction.ENTITY_DELETED,
            entity_id=entity_id,
            principal=principal,
            details={"records_deleted": len(items)},
        )

    def get_children(self, parent_id: str) -> list[Entity]:
        """Get all children of a parent entity."""
        response = self._client.query(
            TableName=self.table_name,
            IndexName=schema.GSI1_NAME,
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": {"S": schema.gsi1_pk_parent(parent_id)}},
        )

        entities = []
        for item in response.get("Items", []):
            entity_id = item.get("entity_id", {}).get("S", "")
            if entity_id:
                entity = self.get_entity(entity_id)
                if entity:
                    entities.append(entity)
        return entities

    # -------------------------------------------------------------------------
    # Bucket operations
    # -------------------------------------------------------------------------

    def get_buckets(self, entity_id: str, resource: str | None = None) -> list[BucketState]:
        """Get all buckets for an entity."""
        sk_prefix = schema.SK_BUCKET + (f"{resource}#" if resource else "")

        response = self._client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": sk_prefix},
            },
        )

        buckets = []
        for item in response.get("Items", []):
            data = self._deserialize_map(item.get("data", {}))
            buckets.append(
                BucketState(
                    entity_id=entity_id,
                    resource=data.get("resource", ""),
                    limit_name=data.get("limit_name", ""),
                    tokens_milli=int(data.get("tokens_milli", 0)),
                    last_refill_ms=int(data.get("last_refill_ms", 0)),
                    capacity_milli=int(data.get("capacity_milli", 0)),
                    burst_milli=int(data.get("burst_milli", 0)),
                    refill_amount_milli=int(data.get("refill_amount_milli", 0)),
                    refill_period_ms=int(data.get("refill_period_ms", 0)),
                )
            )
        return buckets

    def reset_bucket(
        self,
        entity_id: str,
        resource: str,
        limit_name: str,
        principal: str | None = None,
    ) -> BucketState:
        """Reset a bucket to burst capacity."""
        # Get existing bucket
        response = self._client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_bucket(resource, limit_name)},
            },
        )
        item = response.get("Item")
        if not item:
            raise ValueError(
                f"Bucket not found: entity={entity_id}, resource={resource}, limit={limit_name}"
            )

        data = self._deserialize_map(item.get("data", {}))
        burst_milli = int(data.get("burst_milli", 0))
        previous_tokens = int(data.get("tokens_milli", 0))
        now_ms = self._now_ms()

        self._client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(entity_id)},
                "SK": {"S": schema.sk_bucket(resource, limit_name)},
            },
            UpdateExpression="SET #data.#tokens = :tokens, #data.#refill = :refill",
            ExpressionAttributeNames={
                "#data": "data",
                "#tokens": "tokens_milli",
                "#refill": "last_refill_ms",
            },
            ExpressionAttributeValues={
                ":tokens": {"N": str(burst_milli)},
                ":refill": {"N": str(now_ms)},
            },
        )

        self._log_audit_event(
            action=AuditAction.BUCKET_RESET,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details={
                "limit_name": limit_name,
                "previous_tokens_milli": previous_tokens,
                "reset_tokens_milli": burst_milli,
            },
        )

        return BucketState(
            entity_id=entity_id,
            resource=resource,
            limit_name=limit_name,
            tokens_milli=burst_milli,
            last_refill_ms=now_ms,
            capacity_milli=int(data.get("capacity_milli", 0)),
            burst_milli=burst_milli,
            refill_amount_milli=int(data.get("refill_amount_milli", 0)),
            refill_period_ms=int(data.get("refill_period_ms", 0)),
        )

    # -------------------------------------------------------------------------
    # Limits operations
    # -------------------------------------------------------------------------

    def get_limits(self, entity_id: str, resource: str) -> list[Limit]:
        """Get stored limits for an entity/resource."""
        response = self._client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": f"#LIMIT#{resource}#"},
            },
        )

        limits = []
        for item in response.get("Items", []):
            data = self._deserialize_map(item.get("data", {}))
            limits.append(
                Limit(
                    name=data.get("name", ""),
                    capacity=int(data.get("capacity", 0)),
                    burst=int(data.get("burst", 0)),
                    refill_amount=int(data.get("refill_amount", 0)),
                    refill_period_seconds=int(data.get("refill_period_seconds", 60)),
                )
            )
        return limits

    def set_limits(
        self,
        entity_id: str,
        limits: list[Limit],
        resource: str,
        principal: str | None = None,
    ) -> None:
        """Set stored limits for an entity/resource."""
        # Delete existing limits for this resource
        self.delete_limits(entity_id, resource)

        # Write new limits
        for limit in limits:
            self._client.put_item(
                TableName=self.table_name,
                Item={
                    "PK": {"S": schema.pk_entity(entity_id)},
                    "SK": {"S": f"#LIMIT#{resource}#{limit.name}"},
                    "entity_id": {"S": entity_id},
                    "data": {
                        "M": {
                            "name": {"S": limit.name},
                            "capacity": {"N": str(limit.capacity)},
                            "burst": {"N": str(limit.burst)},
                            "refill_amount": {"N": str(limit.refill_amount)},
                            "refill_period_seconds": {"N": str(limit.refill_period_seconds)},
                        }
                    },
                },
            )

        self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=entity_id,
            principal=principal,
            resource=resource,
            details={"limits": [asdict(lim) for lim in limits]},
        )

    def delete_limits(self, entity_id: str, resource: str, principal: str | None = None) -> None:
        """Delete stored limits for an entity/resource."""
        response = self._client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.pk_entity(entity_id)},
                ":sk_prefix": {"S": f"#LIMIT#{resource}#"},
            },
        )

        items = response.get("Items", [])
        if not items:
            return

        for item in items:
            self._client.delete_item(
                TableName=self.table_name,
                Key={"PK": item["PK"], "SK": item["SK"]},
            )

        if principal:
            self._log_audit_event(
                action=AuditAction.LIMITS_DELETED,
                entity_id=entity_id,
                principal=principal,
                resource=resource,
            )

    # -------------------------------------------------------------------------
    # Audit operations
    # -------------------------------------------------------------------------

    def get_audit_events(self, entity_id: str, limit: int = 100) -> list[AuditEvent]:
        """Get audit events for an entity."""
        response = self._client.query(
            TableName=self.table_name,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"AUDIT#{entity_id}"},
                ":sk_prefix": {"S": "#AUDIT#"},
            },
            ScanIndexForward=False,  # Most recent first
            Limit=limit,
        )

        events = []
        for item in response.get("Items", []):
            data = self._deserialize_map(item.get("data", {}))
            events.append(
                AuditEvent(
                    event_id=data.get("event_id", ""),
                    timestamp=data.get("timestamp", ""),
                    action=data.get("action", ""),
                    entity_id=data.get("entity_id", ""),
                    principal=data.get("principal"),
                    resource=data.get("resource"),
                    details=data.get("details", {}),
                )
            )
        return events

    def _log_audit_event(
        self,
        action: str,
        entity_id: str,
        principal: str | None = None,
        resource: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Log an audit event."""
        event_id = str(ULID())
        timestamp = datetime.now(UTC).isoformat()

        event_data: dict[str, Any] = {
            "event_id": {"S": event_id},
            "timestamp": {"S": timestamp},
            "action": {"S": action},
            "entity_id": {"S": entity_id},
        }
        if principal:
            event_data["principal"] = {"S": principal}
        if resource:
            event_data["resource"] = {"S": resource}
        if details:
            event_data["details"] = {"M": self._serialize_map(details)}

        self._client.put_item(
            TableName=self.table_name,
            Item={
                "PK": {"S": f"AUDIT#{entity_id}"},
                "SK": {"S": f"#AUDIT#{event_id}"},
                "data": {"M": event_data},
            },
        )

    # -------------------------------------------------------------------------
    # Resource capacity
    # -------------------------------------------------------------------------

    def get_resource_capacity(self, resource: str, limit_name: str) -> dict[str, Any]:
        """Get capacity information for a resource."""
        response = self._client.query(
            TableName=self.table_name,
            IndexName=schema.GSI2_NAME,
            KeyConditionExpression="GSI2PK = :pk AND begins_with(GSI2SK, :sk_prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": schema.gsi2_pk_resource(resource)},
                ":sk_prefix": {"S": "BUCKET#"},
            },
        )

        total_capacity = 0
        total_available = 0
        entities: list[dict[str, Any]] = []

        now_ms = self._now_ms()

        for item in response.get("Items", []):
            data = self._deserialize_map(item.get("data", {}))
            if data.get("limit_name") != limit_name:
                continue

            entity_id = item.get("entity_id", {}).get("S", "")
            capacity = int(data.get("capacity_milli", 0)) // 1000
            tokens = int(data.get("tokens_milli", 0))
            last_refill = int(data.get("last_refill_ms", 0))
            refill_rate = int(data.get("refill_amount_milli", 0)) / max(
                int(data.get("refill_period_ms", 1)), 1
            )
            burst = int(data.get("burst_milli", 0))

            # Calculate current available with refill
            elapsed_ms = now_ms - last_refill
            refilled = int(refill_rate * elapsed_ms)
            available = min(tokens + refilled, burst) // 1000

            total_capacity += capacity
            total_available += available

            entities.append(
                {
                    "entity_id": entity_id,
                    "capacity": capacity,
                    "available": available,
                    "utilization_pct": round((1 - available / capacity) * 100, 2)
                    if capacity > 0
                    else 0,
                }
            )

        return {
            "resource": resource,
            "limit_name": limit_name,
            "total_capacity": total_capacity,
            "total_available": total_available,
            "utilization_pct": round((1 - total_available / total_capacity) * 100, 2)
            if total_capacity > 0
            else 0,
            "entities": entities,
        }


# ---------------------------------------------------------------------------
# Request/Response helpers
# ---------------------------------------------------------------------------


def json_response(
    status_code: int, body: Any, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """Create an API Gateway response."""
    response_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }
    if headers:
        response_headers.update(headers)

    return {
        "statusCode": status_code,
        "headers": response_headers,
        "body": json.dumps(body, default=str),
    }


def error_response(status_code: int, error: str, message: str) -> dict[str, Any]:
    """Create an error response."""
    return json_response(status_code, {"error": error, "message": message})


def get_principal(event: dict[str, Any]) -> str | None:
    """Extract principal from API Gateway event."""
    request_context: dict[str, Any] = event.get("requestContext", {})
    identity: dict[str, Any] = request_context.get("identity", {})

    # IAM auth
    user_arn: str | None = identity.get("userArn")
    if user_arn:
        return user_arn

    # Cognito auth
    authorizer: dict[str, Any] = request_context.get("authorizer", {})
    claims: dict[str, Any] = authorizer.get("claims", {})
    if claims:
        sub_or_email: str | None = claims.get("sub") or claims.get("email")
        return sub_or_email

    return None


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def handle_list_entities(repo: AdminRepository, event: dict[str, Any]) -> dict[str, Any]:
    """GET /entities - List entities (via scan, limited use)."""
    # Note: This is a basic implementation. For production, consider pagination.
    # Currently returns children of a parent if parent_id query param is provided.
    params = event.get("queryStringParameters") or {}
    parent_id = params.get("parent_id")

    if parent_id:
        entities = repo.get_children(parent_id)
    else:
        # Return top-level entities (no scan, just return empty for now)
        return json_response(
            200,
            {
                "entities": [],
                "message": (
                    "Use parent_id query param to list children, "
                    "or GET /entities/{id} to fetch specific entity"
                ),
            },
        )

    return json_response(200, {"entities": [asdict(e) for e in entities], "count": len(entities)})


def handle_create_entity(repo: AdminRepository, event: dict[str, Any]) -> dict[str, Any]:
    """POST /entities - Create entity."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error_response(400, "invalid_json", "Request body must be valid JSON")

    entity_id = body.get("entity_id")
    if not entity_id:
        return error_response(400, "missing_field", "entity_id is required")

    try:
        entity = repo.create_entity(
            entity_id=entity_id,
            name=body.get("name"),
            parent_id=body.get("parent_id"),
            metadata=body.get("metadata"),
            principal=get_principal(event),
        )
        return json_response(201, asdict(entity))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return error_response(409, "entity_exists", f"Entity {entity_id} already exists")
        raise


def handle_get_entity(
    repo: AdminRepository, event: dict[str, Any], entity_id: str
) -> dict[str, Any]:
    """GET /entities/{entity_id} - Get entity details."""
    entity = repo.get_entity(entity_id)
    if not entity:
        return error_response(404, "not_found", f"Entity {entity_id} not found")

    result = asdict(entity)
    # Optionally include buckets
    params = event.get("queryStringParameters") or {}
    if params.get("include_buckets") == "true":
        buckets = repo.get_buckets(entity_id)
        result["buckets"] = [asdict(b) for b in buckets]

    return json_response(200, result)


def handle_update_entity(
    repo: AdminRepository, event: dict[str, Any], entity_id: str
) -> dict[str, Any]:
    """PATCH /entities/{entity_id} - Update entity."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error_response(400, "invalid_json", "Request body must be valid JSON")

    try:
        entity = repo.update_entity(
            entity_id=entity_id,
            name=body.get("name"),
            metadata=body.get("metadata"),
            parent_id=body.get("parent_id"),
            clear_parent=body.get("clear_parent", False),
            principal=get_principal(event),
        )
        return json_response(200, asdict(entity))
    except ValueError as e:
        return error_response(404, "not_found", str(e))


def handle_delete_entity(
    repo: AdminRepository, event: dict[str, Any], entity_id: str
) -> dict[str, Any]:
    """DELETE /entities/{entity_id} - Delete entity."""
    repo.delete_entity(entity_id, principal=get_principal(event))
    return json_response(204, None)


def handle_get_children(
    repo: AdminRepository, event: dict[str, Any], entity_id: str
) -> dict[str, Any]:
    """GET /entities/{entity_id}/children - Get children."""
    children = repo.get_children(entity_id)
    return json_response(200, {"children": [asdict(c) for c in children], "count": len(children)})


def handle_get_limits(
    repo: AdminRepository, event: dict[str, Any], entity_id: str, resource: str
) -> dict[str, Any]:
    """GET /entities/{entity_id}/limits/{resource} - Get limits."""
    limits = repo.get_limits(entity_id, resource)
    return json_response(200, {"limits": [asdict(lim) for lim in limits]})


def handle_set_limits(
    repo: AdminRepository, event: dict[str, Any], entity_id: str, resource: str
) -> dict[str, Any]:
    """PUT /entities/{entity_id}/limits/{resource} - Set limits."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return error_response(400, "invalid_json", "Request body must be valid JSON")

    limits_data = body.get("limits", [])
    if not limits_data:
        return error_response(400, "missing_field", "limits array is required")

    limits = []
    for lim in limits_data:
        # Default: refill full capacity over 60 seconds
        refill_amount = lim.get("refill_amount", lim["capacity"])
        refill_period_seconds = lim.get("refill_period_seconds", 60)
        limits.append(
            Limit(
                name=lim["name"],
                capacity=lim["capacity"],
                burst=lim.get("burst", lim["capacity"]),
                refill_amount=refill_amount,
                refill_period_seconds=refill_period_seconds,
            )
        )

    repo.set_limits(entity_id, limits, resource, principal=get_principal(event))
    return json_response(200, {"limits": [asdict(lim) for lim in limits]})


def handle_delete_limits(
    repo: AdminRepository, event: dict[str, Any], entity_id: str, resource: str
) -> dict[str, Any]:
    """DELETE /entities/{entity_id}/limits/{resource} - Delete limits."""
    repo.delete_limits(entity_id, resource, principal=get_principal(event))
    return json_response(204, None)


def handle_get_buckets(
    repo: AdminRepository, event: dict[str, Any], entity_id: str, resource: str | None
) -> dict[str, Any]:
    """GET /entities/{entity_id}/buckets[/{resource}] - Get buckets."""
    buckets = repo.get_buckets(entity_id, resource)
    return json_response(200, {"buckets": [asdict(b) for b in buckets]})


def handle_reset_bucket(
    repo: AdminRepository,
    event: dict[str, Any],
    entity_id: str,
    resource: str,
    limit_name: str,
) -> dict[str, Any]:
    """POST /entities/{entity_id}/buckets/{resource}/{limit_name}/reset - Reset bucket."""
    try:
        bucket = repo.reset_bucket(entity_id, resource, limit_name, principal=get_principal(event))
        return json_response(200, asdict(bucket))
    except ValueError as e:
        return error_response(404, "not_found", str(e))


def handle_get_audit(
    repo: AdminRepository, event: dict[str, Any], entity_id: str
) -> dict[str, Any]:
    """GET /entities/{entity_id}/audit - Get audit events."""
    params = event.get("queryStringParameters") or {}
    limit = int(params.get("limit", 100))

    events = repo.get_audit_events(entity_id, limit=limit)
    return json_response(200, {"events": [asdict(e) for e in events]})


def handle_get_resource_capacity(
    repo: AdminRepository, event: dict[str, Any], resource: str
) -> dict[str, Any]:
    """GET /resources/{resource}/capacity - Get resource capacity."""
    params = event.get("queryStringParameters") or {}
    limit_name = params.get("limit_name", "rpm")

    capacity = repo.get_resource_capacity(resource, limit_name)
    return json_response(200, capacity)


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Main Lambda handler for Admin API."""
    logger.info(
        "Request received",
        method=event.get("httpMethod"),
        path=event.get("path"),
        resource=event.get("resource"),
    )

    # Handle OPTIONS for CORS preflight
    if event.get("httpMethod") == "OPTIONS":
        return json_response(200, {})

    try:
        repo = AdminRepository(TABLE_NAME)

        method = event.get("httpMethod", "")
        resource = event.get("resource", "")
        path_params = event.get("pathParameters") or {}

        # Route based on resource pattern and method
        if resource == "/entities":
            if method == "GET":
                return handle_list_entities(repo, event)
            elif method == "POST":
                return handle_create_entity(repo, event)

        elif resource == "/entities/{entity_id}":
            entity_id = path_params.get("entity_id", "")
            if method == "GET":
                return handle_get_entity(repo, event, entity_id)
            elif method == "PATCH":
                return handle_update_entity(repo, event, entity_id)
            elif method == "DELETE":
                return handle_delete_entity(repo, event, entity_id)

        elif resource == "/entities/{entity_id}/children":
            entity_id = path_params.get("entity_id", "")
            if method == "GET":
                return handle_get_children(repo, event, entity_id)

        elif resource == "/entities/{entity_id}/limits/{resource}":
            entity_id = path_params.get("entity_id", "")
            res = path_params.get("resource", "")
            if method == "GET":
                return handle_get_limits(repo, event, entity_id, res)
            elif method == "PUT":
                return handle_set_limits(repo, event, entity_id, res)
            elif method == "DELETE":
                return handle_delete_limits(repo, event, entity_id, res)

        elif resource == "/entities/{entity_id}/buckets":
            entity_id = path_params.get("entity_id", "")
            if method == "GET":
                return handle_get_buckets(repo, event, entity_id, None)

        elif resource == "/entities/{entity_id}/buckets/{resource}":
            entity_id = path_params.get("entity_id", "")
            res = path_params.get("resource", "")
            if method == "GET":
                return handle_get_buckets(repo, event, entity_id, res)

        elif resource == "/entities/{entity_id}/buckets/{resource}/{limit_name}/reset":
            entity_id = path_params.get("entity_id", "")
            res = path_params.get("resource", "")
            limit_name = path_params.get("limit_name", "")
            if method == "POST":
                return handle_reset_bucket(repo, event, entity_id, res, limit_name)

        elif resource == "/entities/{entity_id}/audit":
            entity_id = path_params.get("entity_id", "")
            if method == "GET":
                return handle_get_audit(repo, event, entity_id)

        elif resource == "/resources/{resource}/capacity":
            res = path_params.get("resource", "")
            if method == "GET":
                return handle_get_resource_capacity(repo, event, res)

        return error_response(404, "not_found", f"Unknown route: {method} {resource}")

    except Exception as e:
        logger.error("Request failed", exc_info=True)
        return error_response(500, "internal_error", str(e))
