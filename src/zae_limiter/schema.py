"""DynamoDB schema definitions and key builders."""

from typing import Any

# Table and index names
DEFAULT_TABLE_NAME = "rate_limits"
GSI1_NAME = "GSI1"  # For parent -> children lookups
GSI2_NAME = "GSI2"  # For resource aggregation

# Key prefixes
ENTITY_PREFIX = "ENTITY#"
PARENT_PREFIX = "PARENT#"
CHILD_PREFIX = "CHILD#"
RESOURCE_PREFIX = "RESOURCE#"
SYSTEM_PREFIX = "SYSTEM#"

# Sort key prefixes
SK_META = "#META"
SK_BUCKET = "#BUCKET#"
SK_LIMIT = "#LIMIT#"
SK_RESOURCE = "#RESOURCE#"
SK_USAGE = "#USAGE#"
SK_VERSION = "#VERSION"
SK_AUDIT = "#AUDIT#"

# Partition key prefix for audit logs
AUDIT_PREFIX = "AUDIT#"

# Special resource for default limits
DEFAULT_RESOURCE = "_default_"


def pk_entity(entity_id: str) -> str:
    """Build partition key for an entity."""
    return f"{ENTITY_PREFIX}{entity_id}"


def pk_system() -> str:
    """Build partition key for system records (e.g., version)."""
    return SYSTEM_PREFIX


def sk_version() -> str:
    """Build sort key for version record."""
    return SK_VERSION


def sk_meta() -> str:
    """Build sort key for entity metadata."""
    return SK_META


def sk_bucket(resource: str, limit_name: str) -> str:
    """Build sort key for a bucket."""
    return f"{SK_BUCKET}{resource}#{limit_name}"


def sk_limit(resource: str, limit_name: str) -> str:
    """Build sort key for a limit config."""
    return f"{SK_LIMIT}{resource}#{limit_name}"


def sk_limit_prefix(resource: str) -> str:
    """Build sort key prefix for querying limits by resource."""
    return f"{SK_LIMIT}{resource}#"


def sk_resource(resource: str) -> str:
    """Build sort key for resource access tracking."""
    return f"{SK_RESOURCE}{resource}"


def sk_usage(resource: str, window_key: str) -> str:
    """Build sort key for usage snapshot."""
    return f"{SK_USAGE}{resource}#{window_key}"


def gsi1_pk_parent(parent_id: str) -> str:
    """Build GSI1 partition key for parent lookup."""
    return f"{PARENT_PREFIX}{parent_id}"


def gsi1_sk_child(entity_id: str) -> str:
    """Build GSI1 sort key for child entry."""
    return f"{CHILD_PREFIX}{entity_id}"


def gsi2_pk_resource(resource: str) -> str:
    """Build GSI2 partition key for resource aggregation."""
    return f"{RESOURCE_PREFIX}{resource}"


def gsi2_sk_bucket(entity_id: str, limit_name: str) -> str:
    """Build GSI2 sort key for bucket entry."""
    return f"BUCKET#{entity_id}#{limit_name}"


def gsi2_sk_access(entity_id: str) -> str:
    """Build GSI2 sort key for access tracking entry."""
    return f"ACCESS#{entity_id}"


def gsi2_sk_usage(window_key: str, entity_id: str) -> str:
    """Build GSI2 sort key for usage snapshot entry."""
    return f"USAGE#{window_key}#{entity_id}"


def pk_audit(entity_id: str) -> str:
    """Build partition key for audit log records."""
    return f"{AUDIT_PREFIX}{entity_id}"


def sk_audit(event_id: str) -> str:
    """Build sort key for audit log record."""
    return f"{SK_AUDIT}{event_id}"


def parse_bucket_sk(sk: str) -> tuple[str, str]:
    """Parse resource and limit_name from bucket sort key."""
    # SK format: #BUCKET#{resource}#{limit_name}
    if not sk.startswith(SK_BUCKET):
        raise ValueError(f"Invalid bucket SK: {sk}")
    parts = sk[len(SK_BUCKET) :].split("#", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid bucket SK format: {sk}")
    return parts[0], parts[1]


def get_table_definition(table_name: str) -> dict[str, Any]:
    """
    Get the DynamoDB table definition for CreateTable.

    Returns a dictionary suitable for boto3 create_table().
    """
    return {
        "TableName": table_name,
        "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
        ],
        "KeySchema": [
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        "GlobalSecondaryIndexes": [
            {
                "IndexName": GSI1_NAME,
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": GSI2_NAME,
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        "StreamSpecification": {
            "StreamEnabled": True,
            "StreamViewType": "NEW_AND_OLD_IMAGES",
        },
    }


def calculate_ttl(now_ms: int, ttl_seconds: int = 86400) -> int:
    """Calculate TTL timestamp (epoch seconds)."""
    return (now_ms // 1000) + ttl_seconds
