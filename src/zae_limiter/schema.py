"""DynamoDB schema definitions and key builders."""

from typing import TYPE_CHECKING, Any

from .schedule import entry_params, parse_cron

if TYPE_CHECKING:
    from .models import Limit
    from .schedule import ScheduleEntry

# Table and index names
DEFAULT_TABLE_NAME = "rate_limits"
GSI1_NAME = "GSI1"  # For parent -> children lookups
GSI2_NAME = "GSI2"  # For resource aggregation
GSI3_NAME = "GSI3"  # For entity config queries (sparse)
GSI4_NAME = "GSI4"  # For namespace-scoped item discovery
LSI1_NAME = "LSI1"  # Reserved, ALL projection (ADR-123)
LSI2_NAME = "LSI2"  # Reserved, KEYS_ONLY projection (ADR-123)
LSI3_NAME = "LSI3"  # Reserved, ALL projection (ADR-123)
LSI4_NAME = "LSI4"  # Reserved, KEYS_ONLY projection (ADR-123)
LSI5_NAME = "LSI5"  # Reserved, ALL projection (ADR-123)

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

# Scheduled limits (#222, ADR-135). The compact encoding lives in
# ``zae_limiter.schedule``; these are the attribute names it is stored under.
#
# ``sched`` is the item-level *default* schedule and ``b_{name}_sched`` the
# per-limit override written only where a limit's schedule differs from it
# (§4.1). ``sched_tz`` is hoisted to one item-level attribute, so every entry
# on one bucket item shares a timezone.
#
# ``vu`` ("valid until", epoch ms) is the materialisation stamp: the fast path
# gates on ``vu > now`` so it can honour a schedule without ever evaluating
# one. ``vu = 0`` forces exactly one materialising pass.
# ``rsched`` / ``b_{name}_rsched`` carry the **reset** schedule (§3.6) under the
# same item-level-default rule and the same hoisted ``sched_tz``. A separate
# attribute rather than a tag inside ``sched``: the two tuples mean opposite
# things (a reset is edge-triggered and overrides no parameters, a parameter
# entry is level-triggered and overrides nothing else), and one list the reader
# has to partition into two meanings is exactly what §4.1 rejected.
BUCKET_FIELD_SCHED = "sched"  # item-level default schedule, compact-encoded
BUCKET_FIELD_RSCHED = "rsched"  # item-level default reset schedule (§3.6, §4.1)
BUCKET_FIELD_SCHED_TZ = "sched_tz"  # IANA name, hoisted out of every entry
BUCKET_FIELD_VU = "vu"  # valid-until, epoch ms — schedule materialisation stamp

# Disable flag (ADR-125). Tri-state on config items: absent = inherit,
# True/False = explicit. On bucket items the attribute is present only
# when the bucket is effectively disabled, so the speculative guard can
# stay `attribute_not_exists(disabled)`.
CONFIG_FIELD_DISABLED = "disabled"
BUCKET_FIELD_DISABLED = "disabled"

# Infrastructure limit: DynamoDB partition write capacity ceiling (GHSA-76rv)
# Auto-injected on every bucket to track per-partition write pressure.
# When exhausted, the client doubles shard_count to spread writes.
WCU_LIMIT_NAME = "wcu"
WCU_LIMIT_CAPACITY = 1000  # DynamoDB per-partition WCU/sec limit
WCU_LIMIT_REFILL_AMOUNT = 1000  # Refills to full capacity each second
WCU_LIMIT_REFILL_PERIOD_SECONDS = 1
# Log warning when shard count exceeds this (GHSA-76rv). Consumed only by the
# aggregator's proactive doubling, which is not bound by MAX_SHARD_COUNT; the
# client refuses to double at MAX_SHARD_COUNT and warns there instead, so a
# client-side check against this equal-valued threshold is unreachable (#439).
WCU_SHARD_WARN_THRESHOLD = 32
# Hard cap on shard_count (ADR-133). Bounds how small a per-shard share can get
# (capacity // shard_count) and how far an exhausted shard can drive doubling
# without the aggregator. Surfaced as a metric in #475.
MAX_SHARD_COUNT = 32

# Composite limit config attribute prefix and field suffixes (ADR-114 for configs)
LIMIT_ATTR_PREFIX = "l_"
LIMIT_FIELD_CP = "cp"  # capacity (ceiling)
LIMIT_FIELD_RA = "ra"  # refill_amount
LIMIT_FIELD_RP = "rp"  # refill_period_seconds
LIMIT_FIELD_SCHED = "sched"  # compact-encoded schedule (#222 §4.1)
LIMIT_FIELD_RSCHED = "rsched"  # compact-encoded reset schedule (#222 §4.1)

# IANA timezone name for every schedule on the item, hoisted out of the
# individual entries (#222 §4.1). One attribute per item, not per limit: it is
# the same 16-ish bytes for every entry and the design measured that repetition
# out. The corollary is that all scheduled limits on one config item must agree
# on a timezone; `models.hoisted_schedule_timezone()` enforces it at the write.
# It covers **both** tuples: a limit carrying a parameter schedule in one zone
# and a reset schedule in another has nowhere to store the second one, so
# `Limit.__post_init__` rejects the pair rather than letting storage pick.
CONFIG_FIELD_SCHED_TZ = "sched_tz"


def encode_disabled(value: bool | None) -> dict[str, Any] | None:
    """Encode a tri-state disabled value as a DynamoDB attribute.

    Args:
        value: True, False, or None (meaning "inherit from the level above").

    Returns:
        A DynamoDB BOOL attribute, or None when the value is "inherit"
        (the caller should omit the attribute entirely).
    """
    if value is None:
        return None
    return {"BOOL": value}


def decode_disabled(item: dict[str, Any]) -> bool | None:
    """Decode the tri-state disabled attribute from a DynamoDB item.

    Returns:
        True or False when explicitly set, None when the attribute is
        absent (meaning "inherit from the level above").
    """
    attr = item.get(CONFIG_FIELD_DISABLED)
    if not attr:
        return None
    return bool(attr.get("BOOL", False))


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
            {"AttributeName": "LSI1SK", "AttributeType": "S"},
            {"AttributeName": "LSI2SK", "AttributeType": "S"},
            {"AttributeName": "LSI3SK", "AttributeType": "S"},
            {"AttributeName": "LSI4SK", "AttributeType": "S"},
            {"AttributeName": "LSI5SK", "AttributeType": "S"},
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
        "LocalSecondaryIndexes": [
            {
                "IndexName": LSI1_NAME,
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "LSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": LSI2_NAME,
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "LSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
            {
                "IndexName": LSI3_NAME,
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "LSI3SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": LSI4_NAME,
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "LSI4SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "KEYS_ONLY"},
            },
            {
                "IndexName": LSI5_NAME,
                "KeySchema": [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "LSI5SK", "KeyType": "RANGE"},
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


# --------------------------------------------------------------------------
# Bucket TTL horizons (#271, #296, #532)
#
# The horizon of a cron pattern: how long it takes for the set of instants it
# matches to repeat. Read off the COARSEST field the pattern constrains, which
# is the opposite end from `schedule._granularity` (that one picks a scan step
# from the finest field). `0 0 * * *` constrains hour, so it repeats daily;
# `0 0 1 * *` constrains day-of-month, so it repeats monthly.
# --------------------------------------------------------------------------
_MINUTE_SECONDS = 60
_HOUR_SECONDS = 3_600
_DAY_SECONDS = 86_400
_WEEK_SECONDS = 7 * _DAY_SECONDS
# Long months and leap years, so the horizon is never short of a real gap.
_MONTH_SECONDS = 31 * _DAY_SECONDS
_YEAR_SECONDS = 366 * _DAY_SECONDS


def _reset_cycle_seconds(entry: "ScheduleEntry") -> int:
    """Upper bound on the gap between two consecutive edges of one reset entry.

    Derived from the coarsest cron field the entry constrains, because that is
    the cycle over which its firing set repeats. Every branch rounds **up** —
    31 days for a monthly pattern, 366 for an annual one — since a horizon that
    is too short is the harmful direction (see
    :func:`calculate_bucket_ttl_seconds`).

    Exact for every pattern whose firing set repeats within its own cycle,
    which is every practical quota schedule. The one class it understates is a
    pattern that skips whole years — ``0 0 29 2 *`` fires on Feb 29 and so has
    a real gap near four years against the one year reported here. The
    multiplier absorbs it: at the default 7 the resulting horizon is seven
    years, comfortably past the real gap, and the operator who writes a
    quadrennial quota has bigger questions than bucket expiry.

    Day-of-month is tested before day-of-week deliberately: when both are
    constrained cron ORs them, so the answer must be the *wider* of the two
    cycles, and a month is wider than a week.
    """
    parsed = parse_cron(entry.cron, entry.tz)
    if len(parsed.months) < 12:
        return _YEAR_SECONDS
    if len(parsed.days) < 31:
        return _MONTH_SECONDS
    if len(parsed.weekdays) < 7:
        return _WEEK_SECONDS
    if len(parsed.hours) < 24:
        return _DAY_SECONDS
    if len(parsed.minutes) < 60:
        return _HOUR_SECONDS
    return _MINUTE_SECONDS


def _time_to_fill_seconds(name: str, cp_milli: int, ra_milli: int, rp_ms: int) -> float:
    """Seconds to refill one set of dripping parameters from empty to full.

    Parameters arrive in milli-units rather than whole ones because that is
    where the schedule overrides reaching here are defined:
    ``schedule.entry_params`` floors a scaled rate at one **milli**-unit, and
    rounding that back to whole tokens would divide by zero. For the unscaled
    base the quotient is identical either way.
    """
    if ra_milli <= 0:
        # Unreachable: `Limit.__post_init__` rejects a zero rate unless a
        # `reset_schedule` pairs with it (and `is_quota` catches those before
        # this function is reached), rejects a negative one outright, and
        # `ScheduleEntry.__post_init__` requires every override to be
        # positive. Stated rather than divided by, so that a future
        # constructor bypassing validation surfaces as this sentence and not
        # as the `ZeroDivisionError` of #532.
        raise ValueError(
            f"limit {name!r} neither drips (refill_amount="
            f"{ra_milli / 1000:g}) nor resets (no reset_schedule), so it can "
            f"never recover and has no bucket TTL horizon (ADR-137)."
        )
    return (cp_milli / ra_milli) * (rp_ms / 1000)


def _recovery_seconds(limit: "Limit") -> float:
    """How long ``limit`` needs to bring a fully spent balance back to full.

    Two shapes, because ADR-137 gives a limit two ways to recover and exactly
    one of them is a rate (see :func:`calculate_bucket_ttl_seconds` for why the
    TTL is built on this quantity rather than on time-to-fill directly).

    A **quota** (``Limit.is_quota`` — no drip at all, ``refill_amount = 0``
    paired with a ``reset_schedule``) recovers in a lump at a calendar edge, so
    its horizon is the reset *period*: the longest it can wait for the next
    edge. Where several reset entries share a limit, the **tightest** cycle
    wins — the balance is restored by whichever entry fires first, so every
    entry's cycle is independently an upper bound and the smallest of them is
    the sharpest one that is still correct.

    Asking :attr:`Limit.is_quota`, the **structural** predicate, is deliberate.
    The temporal question — "is this rate adding tokens right now?", which
    ``models.is_accrual_rate`` and ``BucketState.accrues`` ask — is the wrong
    one here twice over: a TTL is a horizon rather than an instant, and the
    limits reaching this function are the *undivided* config limits (the slow
    path's ``LeaseEntry.limit``, ``_sync_bucket_params``'s resolved config,
    the provisioner's manifest decls), never a per-shard share that could have
    floored to zero on the way in.

    A dripping limit's horizon is the **worst case** across its base parameters
    and every window of its ``schedule`` (#557). The base is always in the set:
    it applies outside every window, and including it is also the rounding-up
    direction. An *absolute* entry — one overriding ``capacity``,
    ``refill_amount`` or ``refill_period_seconds`` rather than scaling — moves
    time-to-fill, and the night window of
    ``per_minute("rpm", 60).with_schedule((ScheduleEntry(cron="0 0-6 * * *",
    refill_amount=1),))`` needs 3600 s against the base's 60 s. Reading the base
    alone gave that bucket a 420 s TTL, so it was swept while still in debt and
    recreated at full capacity — the over-admission ADR-136 exempts
    custom-configured buckets from TTL to avoid. ``scale`` entries come out
    unchanged, as §1.1 intends: capacity and refill move together.

    Worst case rather than the current window because this function holds no
    clock (see :func:`calculate_bucket_ttl_seconds`) and so cannot know which
    window the expiry will land in — nor which windows the bucket will sit idle
    through on the way there.

    The walk is reached only by a limit that drips. A quota takes the branch
    above whatever its parameter schedule says, which is both safe and correct:
    a quota's rate is zero by ADR-137 and dividing by a window's version of it
    would be #532 again, while the reset restores the balance in full within
    the reset period no matter what the windows do to the ceiling.
    """
    if limit.is_quota:
        return float(min(_reset_cycle_seconds(entry) for entry in limit.reset_schedule))

    cp_milli = limit.capacity * 1000
    ra_milli = limit.refill_amount * 1000
    rp_ms = limit.refill_period_seconds * 1000
    horizons = [_time_to_fill_seconds(limit.name, cp_milli, ra_milli, rp_ms)]
    horizons.extend(
        _time_to_fill_seconds(limit.name, *entry_params(cp_milli, ra_milli, rp_ms, entry))
        for entry in limit.schedule
    )
    return max(horizons)


def calculate_bucket_ttl_seconds(
    limits: "list[Limit]",
    multiplier: int,
) -> int | None:
    """
    Calculate bucket TTL in seconds from the slowest recovery (#271, #296, #532, #557).

    For buckets using default limits (system/resource), a TTL allows DynamoDB
    to auto-expire unused buckets. The TTL is ``max_recovery × multiplier``,
    where the recovery horizon of each limit depends on how that limit
    recovers:

    ===================================  ==========================================
    Limit shape                          Recovery horizon
    ===================================  ==========================================
    Drips (``refill_amount > 0``)        ``(capacity / refill_amount) × refill_period_seconds``,
                                         taken at its **slowest** over the base
                                         parameters and every ``schedule`` window (#557)
    Quota (``is_quota``, ADR-137)        the reset period — the cycle of its ``reset_schedule``
    ===================================  ==========================================

    A single ``max`` still spans both shapes, so a composite bucket carrying a
    quota beside a dripping limit expires on whichever recovers more slowly.

    **Why a quota needs its own horizon, and why it is a period rather than a
    wait.** Time-to-fill divides by ``refill_amount``, which ADR-137 fixes at
    zero for every quota; #532 is the ``ZeroDivisionError`` that follows.
    ADR-136 confines the exposure — a bucket resolving its limits from *entity*
    configuration carries no TTL at all — so only resource- and system-level
    quotas reach here, but "no TTL for a quota" is not an available answer for
    exactly those levels: ADR-136 makes TTL the **propagation mechanism** for
    resource and system limits, which do not fan out on change and pick up new
    parameters only by expiring and being recreated. A quota with no TTL would
    enforce its original allowance forever.

    The reset **period** rather than the wait to the next edge, because this
    function holds no clock and every production caller
    (``lease._commit_initial``, ``Repository._sync_bucket_params``, the
    provisioner's ``bucket_sync``) calls it without one. The period bounds that
    wait from above at every instant, which keeps the signature and the three
    call sites unchanged.

    **Every approximation rounds the horizon up**, because the two error
    directions are not symmetric. Too long only delays propagation of a
    parameter change. Too short expires a bucket that is still carrying
    state — and a bucket recreated while in debt comes back at full capacity,
    which for a quota is an unscheduled reset and an over-admission of up to
    ``capacity``.

    Args:
        limits: List of Limit objects to consider (must be non-empty)
        multiplier: Multiplier applied to the max recovery horizon (default: 7)

    Returns:
        TTL in seconds, or None if multiplier <= 0 (disabled) or limits is empty

    Raises:
        ValueError: if a limit neither drips nor resets (unconstructible today;
            see :func:`_recovery_seconds`).
    """
    if multiplier <= 0 or not limits:
        return None

    # Max across all limits, so the slowest to recover governs the whole item.
    max_recovery = max(_recovery_seconds(limit) for limit in limits)
    return int(max_recovery * multiplier)


def calculate_bucket_ttl(
    now_ms: int,
    limits: "list[Limit]",
    multiplier: int,
) -> int | None:
    """
    Calculate bucket TTL timestamp from the slowest recovery (#271, #296, #532).

    ``now + calculate_bucket_ttl_seconds(limits, multiplier)``. See that
    function for the horizon each limit shape contributes — time-to-fill for a
    limit that drips, the reset period for a quota (ADR-137), and a single
    ``max`` across both.

    Args:
        now_ms: Current time in milliseconds
        limits: List of Limit objects to consider
        multiplier: Multiplier applied to the max recovery horizon (default: 7)

    Returns:
        TTL timestamp in epoch seconds, or None if multiplier <= 0 (disabled)
    """
    ttl_seconds = calculate_bucket_ttl_seconds(limits, multiplier)
    if ttl_seconds is None:
        return None
    return (now_ms // 1000) + ttl_seconds
