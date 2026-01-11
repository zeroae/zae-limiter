"""Lambda handler for DynamoDB Stream events."""

import os
import time
from typing import Any

from .processor import StructuredLogger, process_stream_records

# Configuration from environment
TABLE_NAME = os.environ.get("TABLE_NAME", "rate_limits")
SNAPSHOT_WINDOWS = os.environ.get("SNAPSHOT_WINDOWS", "hourly,daily").split(",")
SNAPSHOT_TTL_DAYS = int(os.environ.get("SNAPSHOT_TTL_DAYS", "90"))

logger = StructuredLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for DynamoDB Stream events.

    Processes bucket changes and updates usage snapshots.

    Environment variables:
        TABLE_NAME: DynamoDB table name (default: rate_limits)
        SNAPSHOT_WINDOWS: Comma-separated windows (default: hourly,daily)
        SNAPSHOT_TTL_DAYS: TTL for snapshots in days (default: 90)

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
    )

    if not records:
        processing_time_ms = (time.perf_counter() - start_time) * 1000
        logger.info(
            "Lambda invocation completed",
            request_id=request_id,
            processed=0,
            snapshots_updated=0,
            error_count=0,
            processing_time_ms=round(processing_time_ms, 2),
        )
        return {
            "statusCode": 200,
            "body": {"processed": 0, "snapshots_updated": 0, "errors": []},
        }

    result = process_stream_records(
        records=records,
        table_name=TABLE_NAME,
        windows=SNAPSHOT_WINDOWS,
        ttl_days=SNAPSHOT_TTL_DAYS,
    )

    processing_time_ms = (time.perf_counter() - start_time) * 1000
    logger.info(
        "Lambda invocation completed",
        request_id=request_id,
        processed=result.processed_count,
        snapshots_updated=result.snapshots_updated,
        error_count=len(result.errors),
        processing_time_ms=round(processing_time_ms, 2),
    )

    return {
        "statusCode": 200,
        "body": {
            "processed": result.processed_count,
            "snapshots_updated": result.snapshots_updated,
            "errors": result.errors,
        },
    }
