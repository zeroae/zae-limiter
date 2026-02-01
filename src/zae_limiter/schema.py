"""DynamoDB schema definitions and key builders."""

from typing import Any

# Table and index names
DEFAULT_TABLE_NAME = "rate_limits"
GSI1_NAME = "GSI1"  # For parent -> children lookups
GSI2_NAME = "GSI2"  # For resource aggregation
GSI3_NAME = "GSI3"  # For entity config queries (sparse)

# Key prefixes
ENTITY_PREFIX = "ENTITY#"
PARENT_PREFIX = "PARENT#"
CHILD_PREFIX = "CHILD#"
RESOURCE_PREFIX = "RESOURCE#"
SYSTEM_PREFIX = "SYSTEM#"
ENTITY_CONFIG_PREFIX = "ENTITY_CONFIG#"  # For GSI3 sparse index

# Sort key prefixes
SK_META = "#META"
SK_BUCKET = "#BUCKET#"
SK_LIMIT = "#LIMIT#"
SK_RESOURCE = "#RESOURCE#"
SK_USAGE = "#USAGE#"
SK_VERSION = "#VERSION"
SK_AUDIT = "#AUDIT#"
SK_CONFIG = "#CONFIG"
SK_RESOURCES = "#RESOURCES"

# Partition key prefix for audit logs
AUDIT_PREFIX = "AUDIT#"

# Special resource for default limits
DEFAULT_RESOURCE = "_default_"

# Composite bucket attribute prefix and field suffixes (ADR-114)
BUCKET_ATTR_PREFIX = "b_"
BUCKET_FIELD_TK = "tk"  # tokens (millitokens)
BUCKET_FIELD_CP = "cp"  # capacity (millitokens)
BUCKET_FIELD_BX = "bx"  # burst maximum (millitokens)
BUCKET_FIELD_RA = "ra"  # refill amount (millitokens)
BUCKET_FIELD_RP = "rp"  # refill period (ms)
BUCKET_FIELD_TC = "tc"  # total consumed counter (millitokens)
BUCKET_FIELD_RF = "rf"  # shared refill timestamp (ms) — optimistic lock

# Composite limit config attribute prefix and field suffixes (ADR-114 for configs)
LIMIT_ATTR_PREFIX = "l_"
LIMIT_FIELD_CP = "cp"  # capacity
LIMIT_FIELD_BX = "bx"  # burst
LIMIT_FIELD_RA = "ra"  # refill_amount
LIMIT_FIELD_RP = "rp"  # refill_period_seconds


def bucket_attr(limit_name: str, field: str) -> str:
    """Build composite bucket attribute name: b_{limit_name}_{field}."""
    return f"{BUCKET_ATTR_PREFIX}{limit_name}_{field}"


def parse_bucket_attr(attr_name: str) -> tuple[str, str] | None:
    """Parse limit_name and field from a composite bucket attribute.

    Returns (limit_name, field) or None if not a bucket attribute.
    """
    if not attr_name.startswith(BUCKET_ATTR_PREFIX):
        return None
    rest = attr_name[len(BUCKET_ATTR_PREFIX) :]
    # Find the last underscore to split name from field
    idx = rest.rfind("_")
    if idx <= 0:
        return None
    return rest[:idx], rest[idx + 1 :]


def limit_attr(limit_name: str, field: str) -> str:
    """Build composite limit config attribute name: l_{limit_name}_{field}."""
    return f"{LIMIT_ATTR_PREFIX}{limit_name}_{field}"


def parse_limit_attr(attr_name: str) -> tuple[str, str] | None:
    """Parse limit_name and field from a composite limit config attribute.

    Returns (limit_name, field) or None if not a limit attribute.
    """
    if not attr_name.startswith(LIMIT_ATTR_PREFIX):
        return None
    rest = attr_name[len(LIMIT_ATTR_PREFIX) :]
    # Find the last underscore to split name from field
    idx = rest.rfind("_")
    if idx <= 0:
        return None
    return rest[:idx], rest[idx + 1 :]


def pk_entity(entity_id: str) -> str:
    """Build partition key for an entity."""
    return f"{ENTITY_PREFIX}{entity_id}"


def pk_system() -> str:
    """Build partition key for system records (e.g., version)."""
    return SYSTEM_PREFIX


def pk_resource(resource: str) -> str:
    """Build partition key for resource config records."""
    return f"{RESOURCE_PREFIX}{resource}"


def sk_version() -> str:
    """Build sort key for version record."""
    return SK_VERSION


def sk_meta() -> str:
    """Build sort key for entity metadata."""
    return SK_META


def sk_bucket(resource: str) -> str:
    """Build sort key for a composite bucket (all limits for entity+resource)."""
    return f"{SK_BUCKET}{resource}"


def sk_limit(resource: str, limit_name: str) -> str:
    """Build sort key for an entity limit config (includes resource)."""
    return f"{SK_LIMIT}{resource}#{limit_name}"


def sk_limit_prefix(resource: str) -> str:
    """Build sort key prefix for querying entity limits by resource."""
    return f"{SK_LIMIT}{resource}#"


def sk_system_limit(limit_name: str) -> str:
    """Build sort key for a system-level limit (no resource)."""
    return f"{SK_LIMIT}{limit_name}"


def sk_system_limit_prefix() -> str:
    """Build sort key prefix for querying all system limits."""
    return SK_LIMIT


def sk_resource_limit(limit_name: str) -> str:
    """Build sort key for a resource-level limit (no resource in SK)."""
    return f"{SK_LIMIT}{limit_name}"


def sk_resource_limit_prefix() -> str:
    """Build sort key prefix for querying all resource limits."""
    return SK_LIMIT


def sk_resources() -> str:
    """Build sort key for resource registry record (tracks all resources with defaults)."""
    return SK_RESOURCES


def sk_config(resource: str | None = None) -> str:
    """Build sort key for config record.

    Args:
        resource: Resource name for entity-level configs. None for system/resource level.

    Returns:
        SK for config record: '#CONFIG' or '#CONFIG#{resource}'
    """
    if resource is not None:
        return f"{SK_CONFIG}#{resource}"
    return SK_CONFIG


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


def gsi2_sk_bucket(entity_id: str) -> str:
    """Build GSI2 sort key for composite bucket entry."""
    return f"BUCKET#{entity_id}"


def gsi2_sk_access(entity_id: str) -> str:
    """Build GSI2 sort key for access tracking entry."""
    return f"ACCESS#{entity_id}"


def gsi2_sk_usage(window_key: str, entity_id: str) -> str:
    """Build GSI2 sort key for usage snapshot entry."""
    return f"USAGE#{window_key}#{entity_id}"


def gsi3_pk_entity_config(resource: str) -> str:
    """Build GSI3 partition key for entity config lookup by resource."""
    return f"{ENTITY_CONFIG_PREFIX}{resource}"


def gsi3_sk_entity(entity_id: str) -> str:
    """Build GSI3 sort key for entity config (just entity_id)."""
    return entity_id


def pk_audit(entity_id: str) -> str:
    """Build partition key for audit log records."""
    return f"{AUDIT_PREFIX}{entity_id}"


def sk_audit(event_id: str) -> str:
    """Build sort key for audit log record."""
    return f"{SK_AUDIT}{event_id}"


def parse_bucket_sk(sk: str) -> str:
    """Parse resource from composite bucket sort key."""
    # SK format: #BUCKET#{resource}
    if not sk.startswith(SK_BUCKET):
        raise ValueError(f"Invalid bucket SK: {sk}")
    resource = sk[len(SK_BUCKET) :]
    if not resource:
        raise ValueError(f"Invalid bucket SK format: {sk}")
    return resource


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
            {"AttributeName": "GSI3PK", "AttributeType": "S"},
            {"AttributeName": "GSI3SK", "AttributeType": "S"},
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
            {
                "IndexName": GSI3_NAME,
                "KeySchema": [
                    {"AttributeName": "GSI3PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI3SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
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
