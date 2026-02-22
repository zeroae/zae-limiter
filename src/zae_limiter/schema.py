"""DynamoDB schema definitions and key builders."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import Limit

# Table and index names
DEFAULT_TABLE_NAME = "rate_limits"
GSI1_NAME = "GSI1"  # For parent -> children lookups
GSI2_NAME = "GSI2"  # For resource aggregation
GSI3_NAME = "GSI3"  # For entity config queries (sparse)
GSI4_NAME = "GSI4"  # For namespace-scoped item discovery

# Namespace constants
RESERVED_NAMESPACE = "_"
DEFAULT_NAMESPACE = "default"

# Key prefixes
ENTITY_PREFIX = "ENTITY#"
PARENT_PREFIX = "PARENT#"
CHILD_PREFIX = "CHILD#"
RESOURCE_PREFIX = "RESOURCE#"
SYSTEM_PREFIX = "SYSTEM#"
ENTITY_CONFIG_PREFIX = "ENTITY_CONFIG#"  # For GSI3 sparse index

# Bucket PK prefix (pre-shard buckets, GHSA-76rv)
BUCKET_PREFIX = "BUCKET#"
SK_STATE = "#STATE"

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
SK_ENTITY_CONFIG_RESOURCES = "#ENTITY_CONFIG_RESOURCES"

# Namespace registry sort key prefixes
SK_NAMESPACE_PREFIX = "#NAMESPACE#"
SK_NSID_PREFIX = "#NSID#"

# Provisioner state sort key (declarative limits management)
SK_PROVISIONER = "#PROVISIONER"

# Partition key prefix for audit logs
AUDIT_PREFIX = "AUDIT#"

# Special resource for default limits
DEFAULT_RESOURCE = "_default_"

# Composite bucket attribute prefix and field suffixes (ADR-114)
BUCKET_ATTR_PREFIX = "b_"
BUCKET_FIELD_TK = "tk"  # tokens (millitokens)
BUCKET_FIELD_CP = "cp"  # capacity / ceiling (millitokens)
BUCKET_FIELD_RA = "ra"  # refill amount (millitokens)
BUCKET_FIELD_RP = "rp"  # refill period (ms)
BUCKET_FIELD_TC = "tc"  # total consumed counter (millitokens)
BUCKET_FIELD_RF = "rf"  # shared refill timestamp (ms) — optimistic lock

# Infrastructure limit: DynamoDB partition write capacity ceiling (GHSA-76rv)
# Auto-injected on every bucket to track per-partition write pressure.
# When exhausted, the client doubles shard_count to spread writes.
WCU_LIMIT_NAME = "wcu"
WCU_LIMIT_CAPACITY = 1000  # DynamoDB per-partition WCU/sec limit
WCU_LIMIT_REFILL_AMOUNT = 1000  # Refills to full capacity each second
WCU_LIMIT_REFILL_PERIOD_SECONDS = 1

# Composite limit config attribute prefix and field suffixes (ADR-114 for configs)
LIMIT_ATTR_PREFIX = "l_"
LIMIT_FIELD_CP = "cp"  # capacity (ceiling)
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


def pk_entity(namespace_id: str, entity_id: str) -> str:
    """Build partition key for an entity."""
    return f"{namespace_id}/{ENTITY_PREFIX}{entity_id}"


def pk_system(namespace_id: str) -> str:
    """Build partition key for system records (e.g., version)."""
    return f"{namespace_id}/{SYSTEM_PREFIX}"


def pk_resource(namespace_id: str, resource: str) -> str:
    """Build partition key for resource config records."""
    return f"{namespace_id}/{RESOURCE_PREFIX}{resource}"


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


def sk_entity_config_resources() -> str:
    """Build sort key for entity config resources registry (wide column with ref counts)."""
    return SK_ENTITY_CONFIG_RESOURCES


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


def gsi1_pk_parent(namespace_id: str, parent_id: str) -> str:
    """Build GSI1 partition key for parent lookup."""
    return f"{namespace_id}/{PARENT_PREFIX}{parent_id}"


def gsi1_sk_child(entity_id: str) -> str:
    """Build GSI1 sort key for child entry."""
    return f"{CHILD_PREFIX}{entity_id}"


def gsi2_pk_resource(namespace_id: str, resource: str) -> str:
    """Build GSI2 partition key for resource aggregation."""
    return f"{namespace_id}/{RESOURCE_PREFIX}{resource}"


def gsi2_sk_bucket(entity_id: str, shard_id: int = 0) -> str:
    """Build GSI2 sort key for a composite bucket entry.

    Args:
        entity_id: Entity owning the bucket
        shard_id: Shard index (default 0 for backward compatibility)

    Returns:
        GSI2SK string in format ``BUCKET#{entity_id}#{shard_id}``
    """
    return f"BUCKET#{entity_id}#{shard_id}"


def gsi2_sk_access(entity_id: str) -> str:
    """Build GSI2 sort key for access tracking entry."""
    return f"ACCESS#{entity_id}"


def gsi2_sk_usage(window_key: str, entity_id: str) -> str:
    """Build GSI2 sort key for usage snapshot entry."""
    return f"USAGE#{window_key}#{entity_id}"


def gsi3_pk_entity_config(namespace_id: str, resource: str) -> str:
    """Build GSI3 partition key for entity config lookup by resource."""
    return f"{namespace_id}/{ENTITY_CONFIG_PREFIX}{resource}"


def gsi3_sk_entity(entity_id: str) -> str:
    """Build GSI3 sort key for entity config (just entity_id)."""
    return entity_id


def pk_audit(namespace_id: str, entity_id: str) -> str:
    """Build partition key for audit log records."""
    return f"{namespace_id}/{AUDIT_PREFIX}{entity_id}"


def sk_audit(event_id: str) -> str:
    """Build sort key for audit log record."""
    return f"{SK_AUDIT}{event_id}"


def parse_namespace(key: str) -> tuple[str, str]:
    """Parse namespace_id and remainder from a namespaced key.

    Splits on the first '/' character.

    Args:
        key: A namespaced key like 'a7x3kq/ENTITY#user-123'

    Returns:
        Tuple of (namespace_id, remainder)

    Raises:
        ValueError: If key contains no '/'
    """
    idx = key.find("/")
    if idx < 0:
        raise ValueError(f"Key has no namespace separator '/': {key}")
    return key[:idx], key[idx + 1 :]


def sk_namespace(namespace_name: str) -> str:
    """Build sort key for namespace registry record (name -> nsid lookup)."""
    return f"{SK_NAMESPACE_PREFIX}{namespace_name}"


def sk_nsid(namespace_id: str) -> str:
    """Build sort key for namespace ID registry record (nsid -> name lookup)."""
    return f"{SK_NSID_PREFIX}{namespace_id}"


def sk_namespace_prefix() -> str:
    """Return the sort key prefix for namespace name queries."""
    return SK_NAMESPACE_PREFIX


def sk_nsid_prefix() -> str:
    """Return the sort key prefix for namespace ID queries."""
    return SK_NSID_PREFIX


def sk_provisioner() -> str:
    """Build sort key for provisioner state record (tracks managed limits)."""
    return SK_PROVISIONER


def parse_bucket_sk(sk: str) -> str:
    """Parse resource from composite bucket sort key."""
    # SK format: #BUCKET#{resource}
    if not sk.startswith(SK_BUCKET):
        raise ValueError(f"Invalid bucket SK: {sk}")
    resource = sk[len(SK_BUCKET) :]
    if not resource:
        raise ValueError(f"Invalid bucket SK format: {sk}")
    return resource


def pk_bucket(namespace_id: str, entity_id: str, resource: str, shard_id: int) -> str:
    """Build partition key for a pre-shard bucket item.

    Bucket items use per-(entity, resource, shard) partition keys to distribute
    writes across DynamoDB partitions, mitigating hot partition risk (GHSA-76rv).

    Args:
        namespace_id: Opaque namespace identifier
        entity_id: Entity owning the bucket
        resource: Resource name (e.g., "gpt-4")
        shard_id: Shard index (0-based)

    Returns:
        PK string in format ``{ns}/BUCKET#{entity_id}#{resource}#{shard_id}``
    """
    return f"{namespace_id}/{BUCKET_PREFIX}{entity_id}#{resource}#{shard_id}"


def sk_state() -> str:
    """Build sort key for bucket state (fixed)."""
    return SK_STATE


def parse_bucket_pk(pk: str) -> tuple[str, str, str, int]:
    """Parse namespace, entity_id, resource, and shard_id from a bucket PK.

    Inverse of :func:`pk_bucket`. Splits on the ``BUCKET#`` prefix and
    separates ``entity_id#resource#shard_id`` components.

    Args:
        pk: A bucket PK like ``'ns1/BUCKET#user-1#gpt-4#0'``

    Returns:
        Tuple of (namespace_id, entity_id, resource, shard_id)

    Raises:
        ValueError: If PK does not match the ``{ns}/BUCKET#{id}#{res}#{shard}`` format
    """
    namespace_id, remainder = parse_namespace(pk)
    if not remainder.startswith(BUCKET_PREFIX):
        raise ValueError(f"Not a bucket PK: {pk}")
    rest = remainder[len(BUCKET_PREFIX) :]
    # Split from the right: last # is shard_id
    parts = rest.rsplit("#", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid bucket PK format: {pk}")
    entity_resource, shard_str = parts
    shard_id = int(shard_str)
    # Split entity_id and resource: first # separates them
    er_parts = entity_resource.split("#", 1)
    if len(er_parts) != 2:
        raise ValueError(f"Invalid bucket PK format: {pk}")
    entity_id, resource = er_parts
    return namespace_id, entity_id, resource, shard_id


def gsi3_pk_entity(namespace_id: str, entity_id: str) -> str:
    """Build GSI3 partition key for entity bucket discovery.

    GSI3 is a KEYS_ONLY index used by ``get_buckets(entity_id)`` (resource=None)
    to discover all bucket PKs for an entity across resources and shards,
    then BatchGetItem fetches the full items from the main table.

    Args:
        namespace_id: Opaque namespace identifier
        entity_id: Entity whose buckets to discover

    Returns:
        GSI3PK string in format ``{ns}/ENTITY#{entity_id}``
    """
    return f"{namespace_id}/{ENTITY_PREFIX}{entity_id}"


def gsi3_sk_bucket(resource: str, shard_id: int) -> str:
    """Build GSI3 sort key for a bucket entry.

    Args:
        resource: Resource name (e.g., "gpt-4")
        shard_id: Shard index (0-based)

    Returns:
        GSI3SK string in format ``BUCKET#{resource}#{shard_id}``
    """
    return f"{BUCKET_PREFIX}{resource}#{shard_id}"


def gsi4_sk_bucket(entity_id: str, resource: str, shard_id: int) -> str:
    """Build GSI4 sort key for bucket item (namespace-scoped discovery).

    Args:
        entity_id: Entity owning the bucket
        resource: Resource name
        shard_id: Shard index (0-based)

    Returns:
        GSI4SK string in format ``BUCKET#{entity_id}#{resource}#{shard_id}``
    """
    return f"{BUCKET_PREFIX}{entity_id}#{resource}#{shard_id}"


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
            {"AttributeName": "GSI4PK", "AttributeType": "S"},
            {"AttributeName": "GSI4SK", "AttributeType": "S"},
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
            {
                "IndexName": GSI4_NAME,
                "KeySchema": [
                    {"AttributeName": "GSI4PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI4SK", "KeyType": "RANGE"},
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


def calculate_bucket_ttl_seconds(
    limits: "list[Limit]",
    multiplier: int,
) -> int | None:
    """
    Calculate bucket TTL in seconds based on time-to-fill (Issue #271, #296).

    For buckets using default limits (system/resource), a TTL allows
    DynamoDB to auto-expire unused buckets. The TTL is calculated as:
    max_time_to_fill × multiplier

    where time_to_fill = (capacity / refill_amount) × refill_period_seconds

    This ensures buckets have enough time to fully refill before expiring,
    even for slow-refill limits where capacity >> refill_amount.

    Args:
        limits: List of Limit objects to consider (must be non-empty)
        multiplier: Multiplier applied to max time-to-fill (default: 7)

    Returns:
        TTL in seconds, or None if multiplier <= 0 (disabled) or limits is empty
    """
    if multiplier <= 0 or not limits:
        return None

    # Time-to-fill = (capacity / refill_amount) × refill_period_seconds
    # Use max across all limits to ensure the slowest limit has time to refill
    max_time_to_fill = max(
        (limit.capacity / limit.refill_amount) * limit.refill_period_seconds for limit in limits
    )
    return int(max_time_to_fill * multiplier)


def calculate_bucket_ttl(
    now_ms: int,
    limits: "list[Limit]",
    multiplier: int,
) -> int | None:
    """
    Calculate bucket TTL timestamp based on time-to-fill (Issue #271, #296).

    For buckets using default limits (system/resource), a TTL allows
    DynamoDB to auto-expire unused buckets. The TTL is calculated as:
    now + (max_time_to_fill × multiplier)

    where time_to_fill = (capacity / refill_amount) × refill_period_seconds

    This ensures buckets have enough time to fully refill before expiring,
    even for slow-refill limits where capacity >> refill_amount.

    Args:
        now_ms: Current time in milliseconds
        limits: List of Limit objects to consider
        multiplier: Multiplier applied to max time-to-fill (default: 7)

    Returns:
        TTL timestamp in epoch seconds, or None if multiplier <= 0 (disabled)
    """
    ttl_seconds = calculate_bucket_ttl_seconds(limits, multiplier)
    if ttl_seconds is None:
        return None
    return (now_ms // 1000) + ttl_seconds
