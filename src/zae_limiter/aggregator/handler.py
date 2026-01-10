"""Lambda handler for DynamoDB Stream events."""

import os
from typing import Any

from .processor import process_stream_records

# Configuration from environment
TABLE_NAME = os.environ.get("TABLE_NAME", "rate_limits")
SNAPSHOT_WINDOWS = os.environ.get("SNAPSHOT_WINDOWS", "hourly,daily").split(",")
SNAPSHOT_TTL_DAYS = int(os.environ.get("SNAPSHOT_TTL_DAYS", "90"))


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
    records = event.get("Records", [])

    if not records:
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

    return {
        "statusCode": 200,
        "body": {
            "processed": result.processed_count,
            "snapshots_updated": result.snapshots_updated,
            "errors": result.errors,
        },
    }
