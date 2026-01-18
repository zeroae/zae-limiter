"""S3 archiver for expired audit events."""

import gzip
import json
import time as time_module
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..schema import AUDIT_PREFIX
from .processor import StructuredLogger

logger = StructuredLogger(__name__)


@dataclass
class ArchiveResult:
    """Result of archiving audit events to S3."""

    processed_count: int
    events_archived: int
    s3_objects_created: int
    errors: list[str]


def archive_audit_events(
    records: list[dict[str, Any]],
    bucket_name: str,
    s3_client: Any,
    request_id: str = "unknown",
) -> ArchiveResult:
    """
    Archive TTL-deleted audit events to S3.

    Filters REMOVE events with AUDIT# prefix, extracts audit data from OldImage,
    groups all events into a single gzip-compressed JSONL file, and writes to S3
    with date partitioning.

    Args:
        records: DynamoDB stream records from Lambda event
        bucket_name: S3 bucket name for archive storage
        s3_client: boto3 S3 client
        request_id: Lambda request ID for object naming

    Returns:
        ArchiveResult with counts and any errors encountered
    """
    start_time = time_module.perf_counter()
    errors: list[str] = []
    events: list[dict[str, Any]] = []

    logger.info(
        "Archive processing started",
        record_count=len(records),
        bucket_name=bucket_name,
    )

    # Extract audit events from REMOVE records
    for idx, record in enumerate(records):
        try:
            audit_event = extract_audit_event(record)
            if audit_event:
                events.append(audit_event)
        except Exception as e:
            error_msg = f"Error extracting audit event from record {idx}: {e}"
            logger.warning(error_msg, exc_info=True, record_index=idx)
            errors.append(error_msg)

    if not events:
        processing_time_ms = (time_module.perf_counter() - start_time) * 1000
        logger.info(
            "Archive processing completed",
            processed_count=len(records),
            events_archived=0,
            s3_objects_created=0,
            error_count=len(errors),
            processing_time_ms=round(processing_time_ms, 2),
        )
        return ArchiveResult(
            processed_count=len(records),
            events_archived=0,
            s3_objects_created=0,
            errors=errors,
        )

    # Create JSONL with gzip compression
    try:
        jsonl_gz = create_jsonl_gzip(events)
    except Exception as e:
        error_msg = f"Error creating JSONL: {e}"
        logger.error(error_msg, exc_info=True)
        errors.append(error_msg)
        return ArchiveResult(
            processed_count=len(records),
            events_archived=0,
            s3_objects_created=0,
            errors=errors,
        )

    # Determine partition from earliest event timestamp
    earliest_timestamp = min(e.get("timestamp", "") for e in events)
    partition = get_partition_key(earliest_timestamp)
    object_key = get_object_key(partition, request_id, earliest_timestamp)

    # Write to S3
    s3_objects_created = 0
    try:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=object_key,
            Body=jsonl_gz,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
        )
        s3_objects_created = 1
        logger.info(
            "S3 object created",
            bucket=bucket_name,
            key=object_key,
            events_count=len(events),
            size_bytes=len(jsonl_gz),
        )
    except Exception as e:
        error_msg = f"Error writing to S3: {e}"
        logger.error(error_msg, exc_info=True, bucket=bucket_name, key=object_key)
        errors.append(error_msg)

    processing_time_ms = (time_module.perf_counter() - start_time) * 1000
    logger.info(
        "Archive processing completed",
        processed_count=len(records),
        events_archived=len(events),
        s3_objects_created=s3_objects_created,
        error_count=len(errors),
        processing_time_ms=round(processing_time_ms, 2),
    )

    return ArchiveResult(
        processed_count=len(records),
        events_archived=len(events),
        s3_objects_created=s3_objects_created,
        errors=errors,
    )


def extract_audit_event(record: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract audit event from a DynamoDB stream REMOVE record.

    Only processes REMOVE events with PK starting with AUDIT#.
    Deserializes the nested data.M map to a plain dictionary.

    Args:
        record: DynamoDB stream record

    Returns:
        Deserialized audit event dict, or None if not an audit REMOVE event
    """
    # Only process REMOVE events (TTL deletions)
    if record.get("eventName") != "REMOVE":
        return None

    dynamodb_data = record.get("dynamodb", {})
    old_image = dynamodb_data.get("OldImage", {})

    # Check if this is an audit record
    pk = old_image.get("PK", {}).get("S", "")
    if not pk.startswith(AUDIT_PREFIX):
        return None

    # Extract nested data map
    data_map = old_image.get("data", {}).get("M", {})
    if not data_map:
        logger.warning("Audit record missing data map", pk=pk)
        return None

    # Deserialize to plain dict
    return _deserialize_map(data_map)


def _deserialize_map(ddb_map: dict[str, Any]) -> dict[str, Any]:
    """
    Deserialize DynamoDB map format to plain Python dict.

    Handles S (string), N (number), BOOL, NULL, M (nested map), and L (list).

    Args:
        ddb_map: DynamoDB map in wire format

    Returns:
        Plain Python dictionary
    """
    result: dict[str, Any] = {}
    for key, value in ddb_map.items():
        result[key] = _deserialize_value(value)
    return result


def _deserialize_value(ddb_value: dict[str, Any]) -> Any:
    """
    Deserialize a single DynamoDB attribute value.

    Args:
        ddb_value: DynamoDB attribute in wire format

    Returns:
        Plain Python value
    """
    if "S" in ddb_value:
        return ddb_value["S"]
    elif "N" in ddb_value:
        # Try int first, fall back to float
        num_str = ddb_value["N"]
        try:
            return int(num_str)
        except ValueError:
            return float(num_str)
    elif "BOOL" in ddb_value:
        return ddb_value["BOOL"]
    elif "NULL" in ddb_value:
        return None
    elif "M" in ddb_value:
        return _deserialize_map(ddb_value["M"])
    elif "L" in ddb_value:
        return [_deserialize_value(item) for item in ddb_value["L"]]
    else:
        # Unknown type, return as-is
        logger.warning("Unknown DynamoDB type", value=ddb_value)
        return ddb_value


def create_jsonl_gzip(events: list[dict[str, Any]]) -> bytes:
    """
    Create gzip-compressed JSONL from a list of events.

    Args:
        events: List of event dictionaries

    Returns:
        Gzip-compressed bytes containing JSONL content
    """
    lines = [json.dumps(event, separators=(",", ":"), default=str) for event in events]
    jsonl = "\n".join(lines) + "\n"
    return gzip.compress(jsonl.encode("utf-8"))


def get_partition_key(timestamp: str) -> str:
    """
    Get S3 partition key from ISO timestamp.

    Uses Hive-style partitioning for Athena compatibility:
    audit/year=YYYY/month=MM/day=DD

    Args:
        timestamp: ISO format timestamp string

    Returns:
        S3 partition prefix
    """
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # Fallback to current time if timestamp is invalid
        dt = datetime.now(UTC)
        logger.warning("Invalid timestamp, using current time", timestamp=timestamp)

    return f"audit/year={dt.year}/month={dt.month:02d}/day={dt.day:02d}"


def get_object_key(partition: str, request_id: str, timestamp: str) -> str:
    """
    Build complete S3 object key.

    Format: {partition}/audit-{request_id}-{timestamp}.jsonl.gz

    Args:
        partition: S3 partition prefix from get_partition_key
        request_id: Lambda request ID
        timestamp: ISO timestamp for uniqueness

    Returns:
        Complete S3 object key
    """
    # Sanitize timestamp for use in filename
    safe_timestamp = timestamp.replace(":", "-").replace("+", "")
    return f"{partition}/audit-{request_id}-{safe_timestamp}.jsonl.gz"
