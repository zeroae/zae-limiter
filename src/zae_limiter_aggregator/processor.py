"""DynamoDB Stream processor for usage aggregation."""

import json
import time as time_module
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

from zae_limiter.schema import SK_BUCKET, gsi2_pk_resource, gsi2_sk_usage, pk_entity, sk_usage


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

    def debug(self, message: str, **extra: Any) -> None:
        self._log("DEBUG", message, **extra)

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
    start_time = time_module.perf_counter()

    logger.info(
        "Batch processing started",
        record_count=len(records),
        windows=windows,
        table_name=table_name,
    )

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(table_name)

    deltas: list[ConsumptionDelta] = []
    errors: list[str] = []

    # Extract deltas from records
    for idx, record in enumerate(records):
        if record.get("eventName") != "MODIFY":
            continue

        try:
            delta = extract_delta(record)
            if delta:
                deltas.append(delta)
        except Exception as e:
            error_msg = f"Error processing record: {e}"
            logger.warning(
                error_msg,
                exc_info=True,
                record_index=idx,
            )
            errors.append(error_msg)

    if not deltas:
        processing_time_ms = (time_module.perf_counter() - start_time) * 1000
        logger.info(
            "Batch processing completed",
            processed_count=len(records),
            deltas_extracted=0,
            snapshots_updated=0,
            error_count=len(errors),
            processing_time_ms=round(processing_time_ms, 2),
        )
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
                logger.warning(
                    error_msg,
                    exc_info=True,
                    entity_id=delta.entity_id,
                    resource=delta.resource,
                    limit_name=delta.limit_name,
                    window=window,
                )
                errors.append(error_msg)

    processing_time_ms = (time_module.perf_counter() - start_time) * 1000
    logger.info(
        "Batch processing completed",
        processed_count=len(records),
        deltas_extracted=len(deltas),
        snapshots_updated=snapshots_updated,
        error_count=len(errors),
        processing_time_ms=round(processing_time_ms, 2),
    )

    return ProcessResult(len(records), snapshots_updated, errors)


def extract_delta(record: dict[str, Any]) -> ConsumptionDelta | None:
    """
    Extract consumption delta from a stream record.

    Uses the total_consumed_milli counter (flat top-level attribute) for accurate
    tracking. This counter is unaffected by token bucket refill, so it correctly
    captures consumption even with high rate limits (10M+ TPM). See issue #179.

    Falls back to None if counter not present (old buckets without counter).

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

    # Extract counter values from FLAT top-level attribute (not nested data.M).
    # Counter is stored flat to enable atomic ADD operations. See issue #179.
    new_counter_attr = new_image.get("total_consumed_milli", {})
    old_counter_attr = old_image.get("total_consumed_milli", {})

    # Skip if counter not present in both images (old bucket without counter)
    if "N" not in new_counter_attr or "N" not in old_counter_attr:
        logger.debug(
            "Skipping bucket without consumption counter",
            entity_id=entity_id,
            resource=resource,
            limit_name=limit_name,
        )
        return None

    new_counter = int(new_counter_attr["N"])
    old_counter = int(old_counter_attr["N"])

    new_refill_ms = int(new_image.get("last_refill_ms", {}).get("N", "0"))

    # Calculate delta: new - old = net consumption since last update
    # Positive = consumed, negative = returned (via release/adjust)
    tokens_delta = new_counter - old_counter

    # Skip if no consumption change
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
    the record if it doesn't exist. Uses a FLAT schema (no nested
    data map) to enable atomic upsert with ADD operations in a
    single DynamoDB call.

    Args:
        table: boto3 Table resource
        delta: Consumption delta to record
        window: Window type
        ttl_days: TTL in days
    """
    window_key = get_window_key(delta.timestamp_ms, window)

    # Convert millitokens to tokens for storage
    tokens_delta = delta.tokens_delta // 1000

    # Build update expression using FLATTENED schema (no nested data map).
    #
    # Snapshots use a flat structure unlike other record types (entities, buckets)
    # which use nested data.M maps. This is because snapshots require atomic upsert
    # with ADD counters, and DynamoDB has a limitation: you cannot SET a map path
    # (#data = if_not_exists(...)) AND ADD to paths within it (#data.counter) in
    # the same expression - it fails with "overlapping document paths" error.
    #
    # The flat structure allows a single atomic update_item call that:
    # - Creates the item if it doesn't exist (SET with if_not_exists for metadata)
    # - Atomically increments counters (ADD for limit consumption and event count)
    #
    # See: https://github.com/zeroae/zae-limiter/issues/168
    table.update_item(
        Key={
            "PK": pk_entity(delta.entity_id),
            "SK": sk_usage(delta.resource, window_key),
        },
        UpdateExpression="""
            SET entity_id = :entity_id,
                #resource = if_not_exists(#resource, :resource),
                #window = if_not_exists(#window, :window),
                #window_start = if_not_exists(#window_start, :window_start),
                GSI2PK = :gsi2pk,
                GSI2SK = :gsi2sk,
                #ttl = if_not_exists(#ttl, :ttl)
            ADD #limit_name :delta,
                #total_events :one
        """,
        ExpressionAttributeNames={
            "#resource": "resource",
            "#window": "window",
            "#window_start": "window_start",
            "#limit_name": delta.limit_name,
            "#total_events": "total_events",
            "#ttl": "ttl",
        },
        ExpressionAttributeValues={
            ":entity_id": delta.entity_id,
            ":resource": delta.resource,
            ":window": window,
            ":window_start": window_key,
            ":gsi2pk": gsi2_pk_resource(delta.resource),
            ":gsi2sk": gsi2_sk_usage(window_key, delta.entity_id),
            ":ttl": calculate_snapshot_ttl(ttl_days),
            ":delta": tokens_delta,
            ":one": 1,
        },
    )

    logger.debug(
        "Snapshot updated",
        entity_id=delta.entity_id,
        resource=delta.resource,
        limit_name=delta.limit_name,
        window=window,
        window_key=window_key,
        tokens_delta=tokens_delta,
    )
