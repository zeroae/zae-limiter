"""DynamoDB Stream processor for usage aggregation."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3  # type: ignore[import-untyped]

from ..schema import SK_BUCKET, gsi2_pk_resource, gsi2_sk_usage, pk_entity, sk_usage

logger = logging.getLogger(__name__)


@dataclass
class ProcessResult:
    """Result of processing stream records."""

    processed_count: int
    snapshots_updated: int
    errors: list[str]


@dataclass
class ConsumptionDelta:
    """Consumption delta extracted from stream record."""

    entity_id: str
    resource: str
    limit_name: str
    tokens_delta: int  # positive = consumed, negative = refilled/returned
    timestamp_ms: int


def process_stream_records(
    records: list[dict[str, Any]],
    table_name: str,
    windows: list[str],
    ttl_days: int = 90,
) -> ProcessResult:
    """
    Process DynamoDB stream records and update usage snapshots.

    1. Filter for BUCKET records (MODIFY events)
    2. Extract consumption deltas from old/new images
    3. Aggregate into hourly/daily snapshot records
    4. Write updates using atomic ADD operations

    Args:
        records: DynamoDB stream records
        table_name: Target table name
        windows: List of window types ("hourly", "daily")
        ttl_days: TTL for snapshot records

    Returns:
        ProcessResult with counts and errors
    """
    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    deltas: list[ConsumptionDelta] = []
    errors: list[str] = []

    # Extract deltas from records
    for record in records:
        if record.get("eventName") != "MODIFY":
            continue

        try:
            delta = extract_delta(record)
            if delta:
                deltas.append(delta)
        except Exception as e:
            error_msg = f"Error processing record: {e}"
            logger.warning(error_msg)
            errors.append(error_msg)

    if not deltas:
        return ProcessResult(len(records), 0, errors)

    # Update snapshots
    snapshots_updated = 0
    for delta in deltas:
        for window in windows:
            try:
                update_snapshot(table, delta, window, ttl_days)
                snapshots_updated += 1
            except Exception as e:
                error_msg = f"Error updating snapshot: {e}"
                logger.warning(error_msg)
                errors.append(error_msg)

    return ProcessResult(len(records), snapshots_updated, errors)


def extract_delta(record: dict[str, Any]) -> ConsumptionDelta | None:
    """
    Extract consumption delta from a stream record.

    Only processes BUCKET records where tokens decreased (consumption).

    Args:
        record: DynamoDB stream record

    Returns:
        ConsumptionDelta if this was a consumption event, None otherwise
    """
    dynamodb_data = record.get("dynamodb", {})
    new_image = dynamodb_data.get("NewImage", {})
    old_image = dynamodb_data.get("OldImage", {})

    # Only process bucket records
    sk = new_image.get("SK", {}).get("S", "")
    if not sk.startswith(SK_BUCKET):
        return None

    # Parse key: #BUCKET#{resource}#{limit_name}
    parts = sk[len(SK_BUCKET) :].split("#", 1)
    if len(parts) != 2:
        return None

    resource, limit_name = parts
    entity_id = new_image.get("entity_id", {}).get("S", "")

    if not entity_id:
        return None

    # Extract token values from data map
    new_data = new_image.get("data", {}).get("M", {})
    old_data = old_image.get("data", {}).get("M", {})

    new_tokens = int(new_data.get("tokens_milli", {}).get("N", "0"))
    old_tokens = int(old_data.get("tokens_milli", {}).get("N", "0"))
    new_refill_ms = int(new_data.get("last_refill_ms", {}).get("N", "0"))

    # Calculate delta: old - new = amount consumed
    # (tokens decrease when consumed)
    tokens_delta = old_tokens - new_tokens

    # We track all changes (consumption and refunds)
    # but skip pure refill events (no net consumption)
    if tokens_delta == 0:
        return None

    return ConsumptionDelta(
        entity_id=entity_id,
        resource=resource,
        limit_name=limit_name,
        tokens_delta=tokens_delta,  # positive = consumed, negative = returned
        timestamp_ms=new_refill_ms,
    )


def get_window_key(timestamp_ms: int, window: str) -> str:
    """
    Get the window key (ISO timestamp) for a given timestamp.

    Args:
        timestamp_ms: Epoch milliseconds
        window: Window type ("hourly", "daily", "monthly")

    Returns:
        ISO timestamp string for the window start
    """
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)

    if window == "hourly":
        return dt.strftime("%Y-%m-%dT%H:00:00Z")
    elif window == "daily":
        return dt.strftime("%Y-%m-%dT00:00:00Z")
    elif window == "monthly":
        return dt.strftime("%Y-%m-01T00:00:00Z")
    else:
        raise ValueError(f"Unknown window type: {window}")


def get_window_end(window_key: str, window: str) -> str:
    """
    Get the window end timestamp.

    Args:
        window_key: Window start (ISO timestamp)
        window: Window type

    Returns:
        ISO timestamp string for the window end
    """
    dt = datetime.fromisoformat(window_key.replace("Z", "+00:00"))

    if window == "hourly":
        end_dt = dt.replace(minute=59, second=59)
    elif window == "daily":
        end_dt = dt.replace(hour=23, minute=59, second=59)
    elif window == "monthly":
        # Last day of month
        if dt.month == 12:
            end_dt = dt.replace(year=dt.year + 1, month=1, day=1) - timedelta(seconds=1)
        else:
            end_dt = dt.replace(month=dt.month + 1, day=1) - timedelta(seconds=1)
    else:
        end_dt = dt

    return end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def calculate_snapshot_ttl(ttl_days: int) -> int:
    """Calculate TTL epoch seconds."""
    return int(datetime.now(UTC).timestamp()) + (ttl_days * 86400)


def update_snapshot(
    table: Any,
    delta: ConsumptionDelta,
    window: str,
    ttl_days: int,
) -> None:
    """
    Update a usage snapshot record atomically.

    Uses DynamoDB ADD operation to increment counters, creating
    the record if it doesn't exist. Uses if_not_exists to initialize
    the nested data map, preserving the original schema structure.

    Args:
        table: boto3 Table resource
        delta: Consumption delta to record
        window: Window type
        ttl_days: TTL in days
    """
    window_key = get_window_key(delta.timestamp_ms, window)

    # Convert millitokens to tokens for storage
    tokens_delta = delta.tokens_delta // 1000

    # Build update expression
    # Use if_not_exists to initialize the nested data map when item is first created.
    # This preserves the original schema while fixing the "invalid document path" error.
    # ADD then atomically increments the counters within the data map.
    table.update_item(
        Key={
            "PK": pk_entity(delta.entity_id),
            "SK": sk_usage(delta.resource, window_key),
        },
        UpdateExpression="""
            SET entity_id = :entity_id,
                #data = if_not_exists(#data, :initial_data),
                GSI2PK = :gsi2pk,
                GSI2SK = :gsi2sk,
                #ttl = :ttl
            ADD #data.#limit_name :delta,
                #data.total_events :one
        """,
        ExpressionAttributeNames={
            "#data": "data",
            "#limit_name": delta.limit_name,
            "#ttl": "ttl",
        },
        ExpressionAttributeValues={
            ":entity_id": delta.entity_id,
            ":initial_data": {
                "resource": delta.resource,
                "window": window,
                "window_start": window_key,
                delta.limit_name: 0,
                "total_events": 0,
            },
            ":gsi2pk": gsi2_pk_resource(delta.resource),
            ":gsi2sk": gsi2_sk_usage(window_key, delta.entity_id),
            ":ttl": calculate_snapshot_ttl(ttl_days),
            ":delta": tokens_delta,
            ":one": 1,
        },
    )
