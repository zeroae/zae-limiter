"""Lambda handler for DynamoDB Stream events."""

import os
import time
from typing import Any

import boto3

from .archiver import archive_audit_events
from .processor import StructuredLogger, process_stream_records

# Configuration from environment
TABLE_NAME = os.environ.get("TABLE_NAME", "ZAEL-rate-limits")
SNAPSHOT_WINDOWS = os.environ.get("SNAPSHOT_WINDOWS", "hourly,daily").split(",")
SNAPSHOT_TTL_DAYS = int(os.environ.get("SNAPSHOT_TTL_DAYS", "90"))

# Archival configuration
ENABLE_ARCHIVAL = os.environ.get("ENABLE_ARCHIVAL", "false").lower() == "true"
ARCHIVE_BUCKET_NAME = os.environ.get("ARCHIVE_BUCKET_NAME", "")

logger = StructuredLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for DynamoDB Stream events.

    Processes bucket changes and updates usage snapshots.
    Optionally archives TTL-deleted audit events to S3.

    Environment variables:
        TABLE_NAME: DynamoDB table name (default: ZAEL-rate-limits)
        SNAPSHOT_WINDOWS: Comma-separated windows (default: hourly,daily)
        SNAPSHOT_TTL_DAYS: TTL for snapshots in days (default: 90)
        ENABLE_ARCHIVAL: Enable audit archival to S3 (default: false)
        ARCHIVE_BUCKET_NAME: S3 bucket for audit archives (required if archival enabled)

    Args:
        event: DynamoDB Stream event
        context: Lambda context

    Returns:
        Processing result summary
    """
    start_time = time.perf_counter()
    request_id = getattr(context, "aws_request_id", "unknown")
    records = event.get("Records", [])

    logger.info(
        "Lambda invocation started",
        request_id=request_id,
        function_name=getattr(context, "function_name", "unknown"),
        record_count=len(records),
        table_name=TABLE_NAME,
        snapshot_windows=SNAPSHOT_WINDOWS,
        enable_archival=ENABLE_ARCHIVAL,
        archive_bucket=ARCHIVE_BUCKET_NAME if ENABLE_ARCHIVAL else None,
    )

    if not records:
        processing_time_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "Lambda invocation completed",
            request_id=request_id,
            processed=0,
            snapshots_updated=0,
            events_archived=0,
            error_count=0,
            processing_time_ms=round(processing_time_ms, 2),
        )
        return {
            "statusCode": 200,
            "body": {
                "processed": 0,
                "snapshots_updated": 0,
                "events_archived": 0,
                "errors": [],
            },
        }

    # Process usage snapshots
    result = process_stream_records(
        records=records,
        table_name=TABLE_NAME,
        windows=SNAPSHOT_WINDOWS,
        ttl_days=SNAPSHOT_TTL_DAYS,
    )

    # Aggregate errors from all operations
    all_errors = list(result.errors)
    events_archived = 0
    s3_objects_created = 0

    # Archive audit events if enabled
    if ENABLE_ARCHIVAL and ARCHIVE_BUCKET_NAME:
        s3_client = boto3.client("s3")
        archive_result = archive_audit_events(
            records=records,
            bucket_name=ARCHIVE_BUCKET_NAME,
            s3_client=s3_client,
            request_id=request_id,
        )
        events_archived = archive_result.events_archived
        s3_objects_created = archive_result.s3_objects_created
        all_errors.extend(archive_result.errors)

    processing_time_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "Lambda invocation completed",
        request_id=request_id,
        processed=result.processed_count,
        snapshots_updated=result.snapshots_updated,
        events_archived=events_archived,
        s3_objects_created=s3_objects_created,
        error_count=len(all_errors),
        processing_time_ms=round(processing_time_ms, 2),
    )

    return {
        "statusCode": 200,
        "body": {
            "processed": result.processed_count,
            "snapshots_updated": result.snapshots_updated,
            "events_archived": events_archived,
            "errors": all_errors,
        },
    }
