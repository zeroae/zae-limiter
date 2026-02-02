"""Integration tests for update_snapshot function against real DynamoDB.

These tests verify that the UpdateExpression fix for issue #168 works correctly
with actual DynamoDB (via LocalStack), not just with mocks.
"""

import os
import uuid
from datetime import UTC, datetime

import boto3
import pytest

from zae_limiter.schema import get_table_definition, pk_entity, sk_usage
from zae_limiter_aggregator.processor import ConsumptionDelta, update_snapshot

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def dynamodb_table():
    """Create a DynamoDB table for testing."""
    endpoint_url = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint_url:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")

    table_name = f"test-snapshot-{uuid.uuid4().hex[:8]}"

    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=endpoint_url,
        region_name="us-east-1",
    )

    table_def = get_table_definition(table_name)
    table = dynamodb.create_table(**table_def)
    table.wait_until_exists()

    yield table

    table.delete()


# ---------------------------------------------------------------------------
# Snapshot Tests
# ---------------------------------------------------------------------------


def test_first_snapshot_creates_item(dynamodb_table) -> None:
    """First snapshot update creates the item with correct flat structure.

    This verifies that the if_not_exists() calls work correctly when
    the item doesn't exist yet.
    """
    delta = ConsumptionDelta(
        entity_id="test-entity-1",
        resource="gpt-4",
        limit_name="tpm",
        tokens_delta=5000000,
        timestamp_ms=int(datetime(2024, 1, 1, 14, 30, 0, tzinfo=UTC).timestamp() * 1000),
    )

    update_snapshot(dynamodb_table, delta, "hourly", 90)

    response = dynamodb_table.get_item(
        Key={
            "PK": pk_entity("test-entity-1"),
            "SK": sk_usage("gpt-4", "2024-01-01T14:00:00Z"),
        }
    )

    assert "Item" in response
    item = response["Item"]

    assert item["entity_id"] == "test-entity-1"
    assert item["resource"] == "gpt-4"
    assert item["window"] == "hourly"
    assert item["window_start"] == "2024-01-01T14:00:00Z"
    assert item["tpm"] == 5000
    assert item["total_events"] == 1
    assert item["GSI2PK"] == "RESOURCE#gpt-4"
    assert "ttl" in item


def test_second_snapshot_updates_counters(dynamodb_table) -> None:
    """Second snapshot update increments existing counters.

    This verifies that ADD operations work correctly on existing items.
    """
    delta = ConsumptionDelta(
        entity_id="test-entity-2",
        resource="gpt-4",
        limit_name="rpm",
        tokens_delta=1000000,
        timestamp_ms=int(datetime(2024, 1, 1, 15, 30, 0, tzinfo=UTC).timestamp() * 1000),
    )

    update_snapshot(dynamodb_table, delta, "hourly", 90)

    delta2 = ConsumptionDelta(
        entity_id="test-entity-2",
        resource="gpt-4",
        limit_name="rpm",
        tokens_delta=2000000,
        timestamp_ms=int(datetime(2024, 1, 1, 15, 45, 0, tzinfo=UTC).timestamp() * 1000),
    )
    update_snapshot(dynamodb_table, delta2, "hourly", 90)

    response = dynamodb_table.get_item(
        Key={
            "PK": pk_entity("test-entity-2"),
            "SK": sk_usage("gpt-4", "2024-01-01T15:00:00Z"),
        }
    )

    item = response["Item"]

    assert item["rpm"] == 3000
    assert item["total_events"] == 2


def test_multiple_limit_types_same_snapshot(dynamodb_table) -> None:
    """Multiple limit types can be tracked in the same snapshot.

    This verifies that different limit names (tpm, rpm) create
    separate counters at the top level.
    """
    base_ts = int(datetime(2024, 1, 1, 16, 30, 0, tzinfo=UTC).timestamp() * 1000)

    delta_tpm = ConsumptionDelta(
        entity_id="test-entity-3",
        resource="claude-3",
        limit_name="tpm",
        tokens_delta=10000000,
        timestamp_ms=base_ts,
    )
    update_snapshot(dynamodb_table, delta_tpm, "hourly", 90)

    delta_rpm = ConsumptionDelta(
        entity_id="test-entity-3",
        resource="claude-3",
        limit_name="rpm",
        tokens_delta=1000000,
        timestamp_ms=base_ts + 1000,
    )
    update_snapshot(dynamodb_table, delta_rpm, "hourly", 90)

    response = dynamodb_table.get_item(
        Key={
            "PK": pk_entity("test-entity-3"),
            "SK": sk_usage("claude-3", "2024-01-01T16:00:00Z"),
        }
    )

    item = response["Item"]

    assert item["tpm"] == 10000
    assert item["rpm"] == 1000
    assert item["total_events"] == 2


def test_negative_delta_decrements_counter(dynamodb_table) -> None:
    """Negative delta (refund) decrements the counter.

    This verifies that ADD works correctly with negative values.
    """
    base_ts = int(datetime(2024, 1, 1, 17, 30, 0, tzinfo=UTC).timestamp() * 1000)

    delta1 = ConsumptionDelta(
        entity_id="test-entity-4",
        resource="api",
        limit_name="rph",
        tokens_delta=5000000,
        timestamp_ms=base_ts,
    )
    update_snapshot(dynamodb_table, delta1, "hourly", 90)

    delta2 = ConsumptionDelta(
        entity_id="test-entity-4",
        resource="api",
        limit_name="rph",
        tokens_delta=-2000000,
        timestamp_ms=base_ts + 1000,
    )
    update_snapshot(dynamodb_table, delta2, "hourly", 90)

    response = dynamodb_table.get_item(
        Key={
            "PK": pk_entity("test-entity-4"),
            "SK": sk_usage("api", "2024-01-01T17:00:00Z"),
        }
    )

    item = response["Item"]

    assert item["rph"] == 3000
    assert item["total_events"] == 2


def test_daily_window(dynamodb_table) -> None:
    """Daily window creates correct snapshot keys."""
    delta = ConsumptionDelta(
        entity_id="test-entity-5",
        resource="service",
        limit_name="daily_limit",
        tokens_delta=1000000,
        timestamp_ms=int(datetime(2024, 1, 15, 14, 30, 0, tzinfo=UTC).timestamp() * 1000),
    )

    update_snapshot(dynamodb_table, delta, "daily", 90)

    response = dynamodb_table.get_item(
        Key={
            "PK": pk_entity("test-entity-5"),
            "SK": sk_usage("service", "2024-01-15T00:00:00Z"),
        }
    )

    assert "Item" in response
    item = response["Item"]
    assert item["window"] == "daily"
    assert item["window_start"] == "2024-01-15T00:00:00Z"
