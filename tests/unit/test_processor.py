"""Tests for aggregator processor module."""

import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from botocore.exceptions import ClientError

from zae_limiter.schedule import ScheduleEntry, decode, decode_reset
from zae_limiter.schema import BUCKET_SCHED_NONE
from zae_limiter_aggregator.processor import (
    BucketRefillState,
    ConsumptionDelta,
    LimitRefillInfo,
    ProcessResult,
    StructuredLogger,
    _is_quota_limit,
    _parse_bucket_record,
    aggregate_bucket_states,
    calculate_snapshot_ttl,
    extract_deltas,
    get_window_end,
    get_window_key,
    process_stream_records,
    propagate_shard_count,
    try_proactive_shard,
    try_refill_bucket,
    update_snapshot,
)


class TestConsumptionDelta:
    """Tests for ConsumptionDelta dataclass."""

    def test_dataclass_fields(self) -> None:
        """ConsumptionDelta stores all fields."""
        delta = ConsumptionDelta(
            namespace_id="a7x3kq",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=5000,
            timestamp_ms=1704067200000,
        )

        assert delta.namespace_id == "a7x3kq"
        assert delta.entity_id == "entity-1"
        assert delta.resource == "gpt-4"
        assert delta.limit_name == "tpm"
        assert delta.tokens_delta == 5000
        assert delta.timestamp_ms == 1704067200000


class TestProcessResult:
    """Tests for ProcessResult dataclass."""

    def test_dataclass_fields(self) -> None:
        """ProcessResult stores all fields."""
        result = ProcessResult(
            processed_count=10,
            snapshots_updated=5,
            refills_written=3,
            errors=["error1", "error2"],
        )

        assert result.processed_count == 10
        assert result.snapshots_updated == 5
        assert result.refills_written == 3
        assert result.errors == ["error1", "error2"]


class TestExtractDeltas:
    """Tests for extract_deltas function (composite bucket format, ADR-114)."""

    def _make_record(
        self,
        sk: str = "#BUCKET#gpt-4",
        entity_id: str = "entity-1",
        rf: int = 1704067200000,
        limits: dict[str, tuple[int | None, int | None]] | None = None,
    ) -> dict:
        """Helper to create a composite bucket stream record.

        Args:
            sk: Sort key for the composite item.
            entity_id: Entity ID.
            rf: Shared refill timestamp (milliseconds).
            limits: Dict of limit_name -> (old_tc, new_tc) pairs.
                Defaults to {"tpm": (0, 5000000)} (5000 tokens consumed in millitokens).
                Use None values to omit the tc attribute from that image.
        """
        if limits is None:
            limits = {"tpm": (0, 5000000)}

        new_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        old_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
        }

        for name, (old_tc, new_tc) in limits.items():
            if new_tc is not None:
                new_image[f"b_{name}_tc"] = {"N": str(new_tc)}
            if old_tc is not None:
                old_image[f"b_{name}_tc"] = {"N": str(old_tc)}

        return {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": new_image,
                "OldImage": old_image,
            },
        }

    def test_valid_bucket_record_consumption(self) -> None:
        """Extract deltas from valid composite BUCKET record with consumption."""
        record = self._make_record(
            entity_id="test-entity",
            rf=1704067200000,
            limits={"tpm": (0, 5000000)},
        )

        deltas = extract_deltas(record)

        assert len(deltas) == 1
        delta = deltas[0]
        assert delta.namespace_id == "default"
        assert delta.entity_id == "test-entity"
        assert delta.resource == "gpt-4"
        assert delta.limit_name == "tpm"
        assert delta.tokens_delta == 5000000  # counter delta in millitokens
        assert delta.timestamp_ms == 1704067200000

    def test_valid_bucket_record_refund(self) -> None:
        """Extract negative delta when tokens are released (refund)."""
        record = self._make_record(
            limits={"tpm": (10000000, 5000000)},  # counter decreased
        )

        deltas = extract_deltas(record)

        assert len(deltas) == 1
        assert deltas[0].tokens_delta == -5000000  # negative = returned (millitokens)

    def test_multiple_limits_from_single_record(self) -> None:
        """Composite record with multiple limits produces multiple deltas."""
        record = self._make_record(
            entity_id="multi-limit",
            limits={
                "tpm": (0, 5000000),
                "rpm": (0, 1000),
            },
        )

        deltas = extract_deltas(record)

        assert len(deltas) == 2
        names = {d.limit_name for d in deltas}
        assert names == {"tpm", "rpm"}
        deltas_by_name = {d.limit_name: d for d in deltas}
        assert deltas_by_name["tpm"].tokens_delta == 5000000
        assert deltas_by_name["rpm"].tokens_delta == 1000

    def test_non_bucket_record_returns_empty(self) -> None:
        """Non-BUCKET records return empty list."""
        record = self._make_record(sk="#LIMIT#gpt-4#tpm")
        assert extract_deltas(record) == []

        record = self._make_record(sk="#META")
        assert extract_deltas(record) == []

        record = self._make_record(sk="#RESOURCE#gpt-4")
        assert extract_deltas(record) == []

    def test_zero_delta_returns_empty(self) -> None:
        """Zero tc delta (no consumption change) returns empty list."""
        record = self._make_record(
            limits={"tpm": (1000000, 1000000)},  # same counter = no consumption
        )
        assert extract_deltas(record) == []

    def test_empty_resource_returns_empty(self) -> None:
        """Empty resource in SK returns empty list."""
        record = self._make_record(sk="#BUCKET#")
        assert extract_deltas(record) == []

    def test_missing_counter_returns_empty(self) -> None:
        """Missing tc counter (no b_{name}_tc attributes) returns empty list."""
        # No limits at all → no tc attributes to discover
        record = self._make_record(limits={})
        assert extract_deltas(record) == []

    def test_partial_counter_returns_empty(self) -> None:
        """Partial counter (only one image has it) is skipped."""
        # Only new has counter (old doesn't)
        record = self._make_record(limits={"tpm": (None, 1000000)})
        assert extract_deltas(record) == []

        # Only old has counter (new doesn't, so attribute not discovered)
        record = self._make_record(limits={"tpm": (1000000, None)})
        assert extract_deltas(record) == []

    def test_missing_entity_id_returns_empty(self) -> None:
        """Missing entity_id returns empty list."""
        record = self._make_record()
        del record["dynamodb"]["NewImage"]["entity_id"]
        assert extract_deltas(record) == []

    def test_empty_entity_id_returns_empty(self) -> None:
        """Empty entity_id returns empty list."""
        record = self._make_record()
        record["dynamodb"]["NewImage"]["entity_id"]["S"] = ""
        assert extract_deltas(record) == []

    def test_missing_dynamodb_key(self) -> None:
        """Missing dynamodb key returns empty list."""
        record = {"eventName": "MODIFY"}
        assert extract_deltas(record) == []

    def test_missing_new_image(self) -> None:
        """Missing NewImage returns empty list."""
        record = {"eventName": "MODIFY", "dynamodb": {"OldImage": {}}}
        assert extract_deltas(record) == []


class TestGetWindowKey:
    """Tests for get_window_key function."""

    def test_hourly_window(self) -> None:
        """Hourly window truncates to hour."""
        # 2024-01-01 14:35:22 UTC
        ts_ms = int(datetime(2024, 1, 1, 14, 35, 22, tzinfo=UTC).timestamp() * 1000)
        assert get_window_key(ts_ms, "hourly") == "2024-01-01T14:00:00Z"

    def test_daily_window(self) -> None:
        """Daily window truncates to day."""
        ts_ms = int(datetime(2024, 1, 15, 18, 45, 0, tzinfo=UTC).timestamp() * 1000)
        assert get_window_key(ts_ms, "daily") == "2024-01-15T00:00:00Z"

    def test_monthly_window(self) -> None:
        """Monthly window truncates to first of month."""
        ts_ms = int(datetime(2024, 3, 25, 10, 0, 0, tzinfo=UTC).timestamp() * 1000)
        assert get_window_key(ts_ms, "monthly") == "2024-03-01T00:00:00Z"

    def test_unknown_window_raises(self) -> None:
        """Unknown window type raises ValueError."""
        ts_ms = int(datetime(2024, 1, 1, tzinfo=UTC).timestamp() * 1000)
        with pytest.raises(ValueError, match="Unknown window type"):
            get_window_key(ts_ms, "weekly")

    def test_midnight_boundary(self) -> None:
        """Test midnight boundary."""
        ts_ms = int(datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC).timestamp() * 1000)
        assert get_window_key(ts_ms, "hourly") == "2024-01-01T00:00:00Z"
        assert get_window_key(ts_ms, "daily") == "2024-01-01T00:00:00Z"

    def test_end_of_day(self) -> None:
        """Test end of day (23:59:59)."""
        ts_ms = int(datetime(2024, 1, 1, 23, 59, 59, tzinfo=UTC).timestamp() * 1000)
        assert get_window_key(ts_ms, "hourly") == "2024-01-01T23:00:00Z"
        assert get_window_key(ts_ms, "daily") == "2024-01-01T00:00:00Z"

    def test_year_boundary(self) -> None:
        """Test year boundary (Dec 31 -> Jan 1)."""
        ts_ms = int(datetime(2024, 12, 31, 23, 30, 0, tzinfo=UTC).timestamp() * 1000)
        assert get_window_key(ts_ms, "daily") == "2024-12-31T00:00:00Z"
        assert get_window_key(ts_ms, "monthly") == "2024-12-01T00:00:00Z"


class TestGetWindowEnd:
    """Tests for get_window_end function."""

    def test_hourly_window_end(self) -> None:
        """Hourly window ends at :59:59."""
        end = get_window_end("2024-01-01T14:00:00Z", "hourly")
        assert end == "2024-01-01T14:59:59Z"

    def test_daily_window_end(self) -> None:
        """Daily window ends at 23:59:59."""
        end = get_window_end("2024-01-15T00:00:00Z", "daily")
        assert end == "2024-01-15T23:59:59Z"

    def test_monthly_window_end_regular(self) -> None:
        """Monthly window ends at last second of month."""
        # January has 31 days
        end = get_window_end("2024-01-01T00:00:00Z", "monthly")
        assert end == "2024-01-31T23:59:59Z"

        # February 2024 (leap year) has 29 days
        end = get_window_end("2024-02-01T00:00:00Z", "monthly")
        assert end == "2024-02-29T23:59:59Z"

        # April has 30 days
        end = get_window_end("2024-04-01T00:00:00Z", "monthly")
        assert end == "2024-04-30T23:59:59Z"

    def test_monthly_window_december_year_rollover(self) -> None:
        """December monthly window correctly rolls to next year."""
        end = get_window_end("2024-12-01T00:00:00Z", "monthly")
        assert end == "2024-12-31T23:59:59Z"

    def test_unknown_window_returns_same(self) -> None:
        """Unknown window returns the input datetime."""
        end = get_window_end("2024-01-01T14:00:00Z", "unknown")
        assert end == "2024-01-01T14:00:00Z"


class TestCalculateSnapshotTtl:
    """Tests for calculate_snapshot_ttl function."""

    def test_returns_future_timestamp(self) -> None:
        """TTL is in the future."""
        now = int(time.time())
        ttl = calculate_snapshot_ttl(90)
        assert ttl > now
        assert ttl >= now + (90 * 86400) - 1  # Allow 1 second variance

    def test_different_ttl_days(self) -> None:
        """Different TTL days produce different results."""
        ttl_30 = calculate_snapshot_ttl(30)
        ttl_90 = calculate_snapshot_ttl(90)
        ttl_365 = calculate_snapshot_ttl(365)

        assert ttl_30 < ttl_90 < ttl_365
        assert ttl_90 - ttl_30 == 60 * 86400
        assert ttl_365 - ttl_90 == 275 * 86400


class TestUpdateSnapshot:
    """Tests for update_snapshot function using mocks.

    Note: moto doesn't fully support the combined SET + ADD update expression
    used by update_snapshot, so we use mocks to verify the correct parameters
    are passed to DynamoDB.
    """

    def test_calls_update_item_with_correct_keys(self) -> None:
        """Verifies update_item is called with correct PK/SK."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=5000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 30, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "default/ENTITY#entity-1"
        assert call_kwargs["Key"]["SK"] == "#USAGE#gpt-4#2024-01-01T14:00:00Z"

    def test_converts_millitokens_to_tokens(self) -> None:
        """Verifies millitokens are converted to tokens."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=5000000,  # 5000 tokens in millitokens
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":delta"] == 5000

    def test_sets_gsi2_keys(self) -> None:
        """Verifies GSI2 keys are set for resource aggregation."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":gsi2pk"] == "default/RESOURCE#gpt-4"
        assert ":gsi2sk" in call_kwargs["ExpressionAttributeValues"]

    def test_sets_ttl(self) -> None:
        """Verifies TTL is set in the future."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        ttl = call_kwargs["ExpressionAttributeValues"][":ttl"]
        assert ttl > int(time.time())

    def test_update_expression_structure(self) -> None:
        """Verifies update expression has correct structure.

        Snapshots use a FLAT schema (no nested data map) to allow atomic upsert
        with ADD counters in a single DynamoDB call. This avoids the "overlapping
        document paths" error that occurs when trying to SET a map AND ADD to
        paths within it in the same expression.

        See: https://github.com/zeroae/zae-limiter/issues/168
        """
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        expr = call_kwargs["UpdateExpression"]

        # Check SET clause elements - flat top-level attributes with if_not_exists
        assert "entity_id = :entity_id" in expr
        assert "if_not_exists(#resource, :resource)" in expr
        assert "if_not_exists(#window, :window)" in expr
        assert "if_not_exists(#window_start, :window_start)" in expr
        assert "GSI2PK = :gsi2pk" in expr
        assert "GSI2SK = :gsi2sk" in expr

        # Check ADD clause elements - flat top-level counters
        assert "ADD #limit_name :delta" in expr
        assert "#total_events :one" in expr

    def test_expression_attribute_values(self) -> None:
        """Verifies expression attribute values for flat structure."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        values = call_kwargs["ExpressionAttributeValues"]

        assert values[":entity_id"] == "entity-1"
        assert values[":resource"] == "gpt-4"
        assert values[":window"] == "hourly"
        assert values[":window_start"] == "2024-01-01T14:00:00Z"
        assert values[":delta"] == 1000  # 1000000 millitokens / 1000
        assert values[":one"] == 1

    def test_expression_attribute_names(self) -> None:
        """Verifies expression attribute names for flat structure."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="default",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        attr_names = call_kwargs["ExpressionAttributeNames"]

        # Flat structure uses top-level attribute names (no #data prefix)
        assert attr_names["#resource"] == "resource"
        assert attr_names["#window"] == "window"
        assert attr_names["#window_start"] == "window_start"
        assert attr_names["#limit_name"] == "tpm"
        assert attr_names["#total_events"] == "total_events"
        assert attr_names["#ttl"] == "ttl"


class TestProcessStreamRecords:
    """Tests for process_stream_records function using mocks."""

    def _make_record(
        self,
        event_name: str = "MODIFY",
        sk: str = "#BUCKET#gpt-4",
        entity_id: str = "entity-1",
        limit_name: str = "tpm",
        old_tc: int | None = 0,
        new_tc: int | None = 5000000,  # 5000 tokens consumed in millitokens
        rf: int = 1704067200000,
    ) -> dict:
        """Helper to create a composite bucket stream record (ADR-114)."""
        new_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        old_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
        }

        # Add per-limit tc counter (composite format)
        if new_tc is not None:
            new_image[f"b_{limit_name}_tc"] = {"N": str(new_tc)}
        if old_tc is not None:
            old_image[f"b_{limit_name}_tc"] = {"N": str(old_tc)}

        return {
            "eventName": event_name,
            "dynamodb": {
                "NewImage": new_image,
                "OldImage": old_image,
            },
        }

    def test_empty_records(self) -> None:
        """Empty records list returns zero counts."""
        with patch("zae_limiter_aggregator.processor.boto3"):
            result = process_stream_records([], "test_table", ["hourly"])

        assert result.processed_count == 0
        assert result.snapshots_updated == 0
        assert result.errors == []

    def test_filters_non_modify_events(self) -> None:
        """Non-MODIFY events are ignored."""
        records = [
            self._make_record(event_name="INSERT"),
            self._make_record(event_name="REMOVE"),
        ]

        with patch("zae_limiter_aggregator.processor.boto3"):
            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 2
        assert result.snapshots_updated == 0

    def test_processes_valid_modify_events(self) -> None:
        """Valid MODIFY events are processed."""
        records = [
            self._make_record(
                entity_id="e1",
                old_tc=0,
                new_tc=5000000,
            ),
            self._make_record(
                entity_id="e2",
                old_tc=0,
                new_tc=5000000,
            ),
        ]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 2
        assert result.snapshots_updated == 2  # 2 deltas * 1 window
        assert result.errors == []
        assert mock_table.update_item.call_count == 2

    def test_multiple_windows(self) -> None:
        """Updates multiple window types."""
        records = [self._make_record()]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly", "daily"])

        assert result.snapshots_updated == 2  # 1 delta * 2 windows
        assert mock_table.update_item.call_count == 2

    def test_handles_extract_deltas_exception(self) -> None:
        """Handles exceptions during extract_deltas."""
        # Create a composite record with invalid tc counter value
        bad_record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "default/ENTITY#entity"},
                    "SK": {"S": "#BUCKET#res"},
                    "entity_id": {"S": "entity"},
                    "rf": {"N": "1704067200000"},
                    "b_limit_tc": {"N": "not_a_number"},  # invalid counter
                },
                "OldImage": {
                    "PK": {"S": "default/ENTITY#entity"},
                    "SK": {"S": "#BUCKET#res"},
                    "entity_id": {"S": "entity"},
                    "rf": {"N": "1704067200000"},
                    "b_limit_tc": {"N": "0"},
                },
            },
        }

        with patch("zae_limiter_aggregator.processor.boto3"):
            result = process_stream_records([bad_record], "test_table", ["hourly"])

        assert result.processed_count == 1
        assert len(result.errors) == 1
        assert "Error processing record" in result.errors[0]

    def test_handles_update_snapshot_exception(self) -> None:
        """Handles exceptions during update_snapshot."""
        records = [self._make_record()]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_table.update_item.side_effect = Exception("DynamoDB error")
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 1
        assert len(result.errors) == 1
        assert "Error updating snapshot" in result.errors[0]

    def test_skips_zero_delta_records(self) -> None:
        """Records with zero tc delta are skipped."""
        records = [
            self._make_record(
                old_tc=1000000,
                new_tc=1000000,  # same counter = zero delta
            ),
        ]

        with patch("zae_limiter_aggregator.processor.boto3"):
            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 1
        assert result.snapshots_updated == 0

    def test_mixed_valid_and_invalid_records(self) -> None:
        """Processes valid records even when some are invalid."""
        records = [
            # Valid: has tc counter with positive delta
            self._make_record(
                entity_id="valid",
                old_tc=0,
                new_tc=5000000,
            ),
            # Invalid: non-bucket SK (will return empty list)
            self._make_record(sk="#LIMIT#res#name"),
            # Invalid: zero tc delta
            self._make_record(
                old_tc=1000000,
                new_tc=1000000,
            ),
        ]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 3
        assert result.snapshots_updated == 1  # only one valid delta
        assert result.errors == []


class TestStructuredLogger:
    """Tests for StructuredLogger class."""

    def test_info_outputs_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Info logs output valid JSON with required fields."""
        logger = StructuredLogger("test.module")
        logger.info("Test message", key="value", count=42)

        captured = capsys.readouterr()
        log_entry = json.loads(captured.out.strip())

        assert log_entry["level"] == "INFO"
        assert log_entry["logger"] == "test.module"
        assert log_entry["message"] == "Test message"
        assert log_entry["key"] == "value"
        assert log_entry["count"] == 42
        assert "timestamp" in log_entry

    def test_warning_with_exc_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Warning logs include exception traceback when exc_info=True."""
        logger = StructuredLogger("test.module")
        try:
            raise ValueError("Test error")
        except ValueError:
            logger.warning("An error occurred", exc_info=True, entity_id="test-entity")

        captured = capsys.readouterr()
        log_entry = json.loads(captured.out.strip())

        assert log_entry["level"] == "WARNING"
        assert log_entry["message"] == "An error occurred"
        assert log_entry["entity_id"] == "test-entity"
        assert "exception" in log_entry
        assert "ValueError: Test error" in log_entry["exception"]

    def test_debug_outputs_valid_json(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Debug logs output valid JSON."""
        logger = StructuredLogger("test.module")
        logger.debug("Debug message", resource="gpt-4", limit_name="tpm")

        captured = capsys.readouterr()
        log_entry = json.loads(captured.out.strip())

        assert log_entry["level"] == "DEBUG"
        assert log_entry["resource"] == "gpt-4"
        assert log_entry["limit_name"] == "tpm"

    def test_error_with_exc_info(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Error logs include exception traceback when exc_info=True."""
        logger = StructuredLogger("test.module")
        try:
            raise RuntimeError("Critical failure")
        except RuntimeError:
            logger.error("Critical error", exc_info=True)

        captured = capsys.readouterr()
        log_entry = json.loads(captured.out.strip())

        assert log_entry["level"] == "ERROR"
        assert "exception" in log_entry
        assert "RuntimeError: Critical failure" in log_entry["exception"]


class TestStructuredLoggingIntegration:
    """Integration tests for structured logging in processor functions."""

    def _make_record(
        self,
        event_name: str = "MODIFY",
        sk: str = "#BUCKET#gpt-4",
        entity_id: str = "entity-1",
        limit_name: str = "tpm",
        old_tc: int | None = 0,
        new_tc: int | None = 5000000,  # 5000 tokens consumed in millitokens
        rf: int = 1704067200000,
    ) -> dict:
        """Helper to create a composite bucket stream record (ADR-114)."""
        new_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        old_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
        }

        # Add per-limit tc counter (composite format)
        if new_tc is not None:
            new_image[f"b_{limit_name}_tc"] = {"N": str(new_tc)}
        if old_tc is not None:
            old_image[f"b_{limit_name}_tc"] = {"N": str(old_tc)}

        return {
            "eventName": event_name,
            "dynamodb": {
                "NewImage": new_image,
                "OldImage": old_image,
            },
        }

    def test_batch_processing_logs_start_and_end(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Logs batch start and completion with metrics."""
        records = [self._make_record()]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            process_stream_records(records, "test_table", ["hourly"])

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line]

        # Should have at least: batch start, snapshot update debug, batch end
        assert len(lines) >= 2

        # Parse first and last INFO logs
        logs = [json.loads(line) for line in lines]
        info_logs = [log for log in logs if log["level"] == "INFO"]

        assert len(info_logs) >= 2
        start_log = info_logs[0]
        end_log = info_logs[-1]

        assert start_log["message"] == "Batch processing started"
        assert start_log["record_count"] == 1
        assert start_log["table_name"] == "test_table"

        assert end_log["message"] == "Batch processing completed"
        assert end_log["processed_count"] == 1
        assert "processing_time_ms" in end_log

    def test_error_logs_include_context(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Error logs include entity_id, resource, limit_name."""
        records = [self._make_record()]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_table.update_item.side_effect = Exception("DynamoDB error")
            mock_boto.resource.return_value.Table.return_value = mock_table

            process_stream_records(records, "test_table", ["hourly"])

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line]
        logs = [json.loads(line) for line in lines]
        warning_logs = [log for log in logs if log["level"] == "WARNING"]

        assert len(warning_logs) == 1
        warning_log = warning_logs[0]

        assert warning_log["entity_id"] == "entity-1"
        assert warning_log["resource"] == "gpt-4"
        assert warning_log["limit_name"] == "tpm"
        assert warning_log["window"] == "hourly"
        assert "exception" in warning_log

    def test_snapshot_update_logs_debug(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Successful snapshot updates are logged at DEBUG level."""
        records = [self._make_record()]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            process_stream_records(records, "test_table", ["hourly"])

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line]
        logs = [json.loads(line) for line in lines]
        debug_logs = [log for log in logs if log["level"] == "DEBUG"]

        # Find the snapshot debug log (refill skip logs may also appear)
        snapshot_logs = [log for log in debug_logs if log["message"] == "Snapshot updated"]
        assert len(snapshot_logs) == 1
        debug_log = snapshot_logs[0]

        assert debug_log["entity_id"] == "entity-1"
        assert debug_log["resource"] == "gpt-4"
        assert debug_log["limit_name"] == "tpm"
        assert debug_log["window"] == "hourly"
        assert "window_key" in debug_log
        assert "tokens_delta" in debug_log


class TestAggregateBucketStates:
    """Tests for aggregate_bucket_states function."""

    def _make_bucket_record(
        self,
        entity_id: str = "entity-1",
        resource: str = "gpt-4",
        rf: int = 1704067200000,
        limits: dict[str, dict[str, int]] | None = None,
    ) -> dict:
        """Helper to create a composite bucket stream record with full config.

        Args:
            limits: Dict of limit_name -> {old_tc, new_tc, tk, cp, ra, rp}
        """
        if limits is None:
            limits = {
                "tpm": {
                    "old_tc": 0,
                    "new_tc": 5000000,
                    "tk": 95000000,
                    "cp": 100000000,
                    "ra": 100000000,
                    "rp": 60000,
                },
            }

        new_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": f"#BUCKET#{resource}"},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        old_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": f"#BUCKET#{resource}"},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
        }

        for name, fields in limits.items():
            if "new_tc" in fields:
                new_image[f"b_{name}_tc"] = {"N": str(fields["new_tc"])}
            if "old_tc" in fields:
                old_image[f"b_{name}_tc"] = {"N": str(fields["old_tc"])}
            for attr in ("tk", "cp", "ra", "rp"):
                if attr in fields:
                    new_image[f"b_{name}_{attr}"] = {"N": str(fields[attr])}

        return {
            "eventName": "MODIFY",
            "dynamodb": {"NewImage": new_image, "OldImage": old_image},
        }

    def test_single_record_single_limit(self) -> None:
        """Aggregates a single record with one limit."""
        records = [self._make_bucket_record()]
        states = aggregate_bucket_states(records)

        assert len(states) == 1
        key = ("default", "entity-1", "gpt-4", 0)
        assert key in states
        state = states[key]
        assert state.namespace_id == "default"
        assert state.entity_id == "entity-1"
        assert state.resource == "gpt-4"
        assert "tpm" in state.limits
        info = state.limits["tpm"]
        assert info.tc_delta == 5000000
        assert info.tk_milli == 95000000
        assert info.ra_milli == 100000000
        assert info.rp_ms == 60000

    def test_single_record_multiple_limits(self) -> None:
        """Aggregates a composite record with multiple limits."""
        records = [
            self._make_bucket_record(
                limits={
                    "tpm": {
                        "old_tc": 0,
                        "new_tc": 5000000,
                        "tk": 95000000,
                        "cp": 100000000,
                        "ra": 100000000,
                        "rp": 60000,
                    },
                    "rpm": {
                        "old_tc": 0,
                        "new_tc": 1000,
                        "tk": 999000,
                        "cp": 1000000,
                        "ra": 1000000,
                        "rp": 60000,
                    },
                },
            ),
        ]
        states = aggregate_bucket_states(records)

        assert len(states) == 1
        state = states[("default", "entity-1", "gpt-4", 0)]
        assert len(state.limits) == 2
        assert state.limits["tpm"].tc_delta == 5000000
        assert state.limits["rpm"].tc_delta == 1000

    def test_multiple_records_same_bucket_aggregates_deltas(self) -> None:
        """Multiple events for the same bucket accumulate tc deltas."""
        records = [
            self._make_bucket_record(
                rf=1704067200000,
                limits={
                    "tpm": {
                        "old_tc": 0,
                        "new_tc": 2000000,
                        "tk": 98000000,
                        "cp": 100000000,
                        "ra": 100000000,
                        "rp": 60000,
                    }
                },
            ),
            self._make_bucket_record(
                rf=1704067201000,
                limits={
                    "tpm": {
                        "old_tc": 2000000,
                        "new_tc": 5000000,
                        "tk": 95000000,
                        "cp": 100000000,
                        "ra": 100000000,
                        "rp": 60000,
                    }
                },
            ),
        ]
        states = aggregate_bucket_states(records)

        state = states[("default", "entity-1", "gpt-4", 0)]
        assert state.limits["tpm"].tc_delta == 5000000  # 2M + 3M
        # Last event's values
        assert state.limits["tpm"].tk_milli == 95000000
        assert state.rf_ms == 1704067201000

    def test_different_buckets_separate_keys(self) -> None:
        """Different entity+resource pairs get separate entries."""
        records = [
            self._make_bucket_record(entity_id="e1", resource="gpt-4"),
            self._make_bucket_record(entity_id="e2", resource="gpt-4"),
        ]
        states = aggregate_bucket_states(records)
        assert len(states) == 2
        assert ("default", "e1", "gpt-4", 0) in states
        assert ("default", "e2", "gpt-4", 0) in states

    def test_non_modify_events_skipped(self) -> None:
        """INSERT and REMOVE events are ignored."""
        record = self._make_bucket_record()
        record["eventName"] = "INSERT"
        states = aggregate_bucket_states([record])
        assert len(states) == 0

    def test_non_bucket_records_skipped(self) -> None:
        """Non-bucket SK records are ignored."""
        record = self._make_bucket_record()
        record["dynamodb"]["NewImage"]["SK"]["S"] = "#META"
        states = aggregate_bucket_states([record])
        assert len(states) == 0

    def test_missing_tc_counter_skips_limit(self) -> None:
        """Limits without tc counter in both images are skipped."""
        records = [
            self._make_bucket_record(
                limits={
                    "tpm": {
                        "tk": 95000000,
                        "cp": 100000000,
                        "ra": 100000000,
                        "rp": 60000,
                        # no old_tc or new_tc
                    }
                },
            ),
        ]
        states = aggregate_bucket_states(records)
        # Key created but no limits populated
        assert len(states) == 0 or len(states[("default", "entity-1", "gpt-4", 0)].limits) == 0


class TestTryRefillBucket:
    """Tests for try_refill_bucket function."""

    def _make_state(
        self,
        namespace_id: str = "default",
        entity_id: str = "entity-1",
        resource: str = "gpt-4",
        rf_ms: int = 1704067200000,
        limits: dict[str, LimitRefillInfo] | None = None,
    ) -> BucketRefillState:
        """Helper to create a BucketRefillState."""
        if limits is None:
            limits = {
                "tpm": LimitRefillInfo(
                    tc_delta=5000000,
                    tk_milli=50000000,  # 50% of capacity
                    cp_milli=100000000,
                    ra_milli=100000000,  # 100k tokens/min
                    rp_ms=60000,
                ),
            }
        return BucketRefillState(
            namespace_id=namespace_id,
            entity_id=entity_id,
            resource=resource,
            rf_ms=rf_ms,
            limits=limits,
        )

    def test_refill_written_when_projected_tokens_insufficient(self) -> None:
        """Writes refill when projected tokens < consumption estimate."""
        mock_table = MagicMock()
        # Low tokens, high consumption, 5 seconds elapsed for refill
        state = self._make_state(
            rf_ms=1704067195000,  # 5s ago
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=20000000,  # consumed 20k tokens this window
                    tk_milli=5000000,  # only 5k tokens left
                    cp_milli=100000000,
                    ra_milli=100000000,  # 100k/min refill
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is True
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert "rf = :expected_rf" in call_kwargs["ConditionExpression"]
        assert "ADD" in call_kwargs["UpdateExpression"]
        assert "SET rf = :new_rf" in call_kwargs["UpdateExpression"]

    def test_refill_skipped_when_tokens_sufficient(self) -> None:
        """Skips refill when projected tokens >= consumption."""
        mock_table = MagicMock()
        # High tokens, low consumption
        state = self._make_state(
            rf_ms=1704067199000,  # 1s ago
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=1000000,  # consumed 1k tokens
                    tk_milli=90000000,  # 90k tokens remaining
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_refill_skipped_when_no_elapsed_time(self) -> None:
        """Skips refill when no time has elapsed (no tokens to add)."""
        mock_table = MagicMock()
        state = self._make_state(rf_ms=1704067200000)
        now_ms = 1704067200000  # same as rf

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_conditional_check_failure_returns_false(self) -> None:
        """ConditionalCheckFailedException is caught and returns False."""
        mock_table = MagicMock()
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "UpdateItem",
        )
        state = self._make_state(
            rf_ms=1704067195000,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=20000000,
                    tk_milli=5000000,
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is False
        mock_table.update_item.assert_called_once()

    def test_other_client_error_re_raised(self) -> None:
        """Non-conditional ClientErrors are re-raised."""
        mock_table = MagicMock()
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "bad"}},
            "UpdateItem",
        )
        state = self._make_state(
            rf_ms=1704067195000,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=20000000,
                    tk_milli=5000000,
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        with pytest.raises(ClientError):
            try_refill_bucket(mock_table, state, now_ms)

    def test_empty_limits_returns_false(self) -> None:
        """Bucket with no limits returns False."""
        mock_table = MagicMock()
        state = self._make_state(limits={})

        result = try_refill_bucket(mock_table, state, 1704067200000)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_multiple_limits_single_update(self) -> None:
        """Multiple limits needing refill produce a single UpdateItem."""
        mock_table = MagicMock()
        state = self._make_state(
            rf_ms=1704067195000,  # 5s ago
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=20000000,
                    tk_milli=5000000,
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
                "rpm": LimitRefillInfo(
                    tc_delta=200,
                    tk_milli=10,
                    cp_milli=1000,
                    ra_milli=1000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is True
        # Single UpdateItem call for both limits
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        update_expr = call_kwargs["UpdateExpression"]
        # Both limits should have ADD clauses
        assert "b_tpm_tk" in update_expr
        assert "b_rpm_tk" in update_expr

    def test_uses_add_not_set_for_tokens(self) -> None:
        """Verifies ADD is used for token deltas (commutative with speculative writes)."""
        mock_table = MagicMock()
        state = self._make_state(
            rf_ms=1704067195000,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=20000000,
                    tk_milli=5000000,
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        try_refill_bucket(mock_table, state, now_ms)

        call_kwargs = mock_table.update_item.call_args[1]
        update_expr = call_kwargs["UpdateExpression"]
        # Token update must use ADD (not SET) for commutativity
        assert "ADD b_tpm_tk" in update_expr
        # rf uses SET (optimistic lock)
        assert "SET rf = :new_rf" in update_expr
        # Refill delta should be positive
        refill_delta = call_kwargs["ExpressionAttributeValues"][":rd_tpm"]
        assert refill_delta > 0

    def test_negative_tc_delta_skips_refill(self) -> None:
        """Negative tc delta (refund) means no consumption pressure, skip refill."""
        mock_table = MagicMock()
        state = self._make_state(
            rf_ms=1704067195000,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=-5000000,  # tokens were returned
                    tk_milli=50000000,
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is False
        mock_table.update_item.assert_not_called()


class TestNegativeRefillDelta:
    """A bucket over its effective cap must be trimmed, not skipped (#222 §3.3, #469)."""

    def test_writes_a_negative_delta_to_trim_a_surplus(self) -> None:
        """A bucket holding more than its cap must be trimmed, not skipped."""
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=1,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=900_000,
                    cp_milli=500_000,
                    ra_milli=500_000,
                    rp_ms=60_000,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -400_000

    def test_still_skips_when_nothing_to_do(self) -> None:
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=1,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=500_000,
                    cp_milli=500_000,
                    ra_milli=500_000,
                    rp_ms=60_000,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is False
        table.update_item.assert_not_called()

    def test_trims_against_the_per_shard_share(self) -> None:
        """Effective cap is capacity // shard_count; trimming to the undivided
        capacity would leave every shard holding the whole limit."""
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=4,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=400_000,
                    cp_milli=800_000,
                    ra_milli=800_000,
                    rp_ms=60_000,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -200_000  # 400_000 -> 800_000 // 4 == 200_000

    def test_trim_is_not_gated_by_the_consumption_threshold(self) -> None:
        """The positive-delta threshold (projected >= consumption estimate) must
        not suppress a trim: a hot bucket has a large tc_delta, and that is
        exactly where a shrink most needs to land."""
        table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            shard_count=1,
            rf_ms=1000,
            limits={
                "rpm": LimitRefillInfo(
                    tc_delta=10_000_000,  # busy bucket
                    tk_milli=900_000,
                    cp_milli=500_000,
                    ra_milli=500_000,
                    rp_ms=60_000,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=1000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -400_000


class TestProcessStreamRecordsRefill:
    """Tests for refill integration in process_stream_records."""

    def _make_bucket_record(
        self,
        entity_id: str = "entity-1",
        resource: str = "gpt-4",
        rf: int = 1704067200000,
        limits: dict[str, dict[str, int]] | None = None,
    ) -> dict:
        """Helper to create a composite bucket stream record with full config."""
        if limits is None:
            limits = {
                "tpm": {
                    "old_tc": 0,
                    "new_tc": 5000000,
                    "tk": 95000000,
                    "cp": 100000000,
                    "ra": 100000000,
                    "rp": 60000,
                },
            }

        new_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": f"#BUCKET#{resource}"},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        old_image: dict = {
            "PK": {"S": f"default/ENTITY#{entity_id}"},
            "SK": {"S": f"#BUCKET#{resource}"},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
        }

        for name, fields in limits.items():
            if "new_tc" in fields:
                new_image[f"b_{name}_tc"] = {"N": str(fields["new_tc"])}
            if "old_tc" in fields:
                old_image[f"b_{name}_tc"] = {"N": str(fields["old_tc"])}
            for attr in ("tk", "cp", "ra", "rp"):
                if attr in fields:
                    new_image[f"b_{name}_{attr}"] = {"N": str(fields[attr])}

        return {
            "eventName": "MODIFY",
            "dynamodb": {"NewImage": new_image, "OldImage": old_image},
        }

    def test_refills_written_in_result(self) -> None:
        """ProcessResult includes refills_written count."""
        # Low tokens + high consumption => refill should trigger
        records = [
            self._make_bucket_record(
                rf=1704067195000,
                limits={
                    "tpm": {
                        "old_tc": 0,
                        "new_tc": 20000000,
                        "tk": 5000000,
                        "cp": 100000000,
                        "ra": 100000000,
                        "rp": 60000,
                    },
                },
            ),
        ]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            with patch("zae_limiter_aggregator.processor.time_module") as mock_time:
                mock_time.perf_counter.return_value = 0.0
                mock_time.time.return_value = 1704067200.0

                result = process_stream_records(records, "test_table", ["hourly"])

        assert result.refills_written == 1

    def test_refill_error_captured_in_errors(self) -> None:
        """Errors during refill are logged but don't fail the batch."""
        records = [
            self._make_bucket_record(
                rf=1704067195000,
                limits={
                    "tpm": {
                        "old_tc": 0,
                        "new_tc": 20000000,
                        "tk": 5000000,
                        "cp": 100000000,
                        "ra": 100000000,
                        "rp": 60000,
                    },
                },
            ),
        ]

        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            # First call succeeds (snapshot), subsequent calls fail (refill)
            call_count = 0

            def side_effect(**kwargs):
                nonlocal call_count
                call_count += 1
                if "ConditionExpression" in kwargs:
                    raise Exception("DynamoDB error")

            mock_table.update_item.side_effect = side_effect
            mock_boto.resource.return_value.Table.return_value = mock_table

            with patch("zae_limiter_aggregator.processor.time_module") as mock_time:
                mock_time.perf_counter.return_value = 0.0
                mock_time.time.return_value = 1704067200.0

                result = process_stream_records(records, "test_table", ["hourly"])

        assert result.refills_written == 0
        assert any("Error refilling bucket" in e for e in result.errors)


class TestNamespaceExtraction:
    """Tests for namespace ID extraction from stream record PKs (#367)."""

    def _make_record(
        self,
        pk: str = "a7x3kq/ENTITY#entity-1",
        sk: str = "#BUCKET#gpt-4",
        entity_id: str = "entity-1",
        rf: int = 1704067200000,
        limits: dict[str, tuple[int, int]] | None = None,
    ) -> dict:
        """Helper to create a stream record with a specific PK."""
        if limits is None:
            limits = {"tpm": (0, 5000000)}

        new_image: dict = {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
        }
        old_image: dict = {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
        }

        for name, (old_tc, new_tc) in limits.items():
            new_image[f"b_{name}_tc"] = {"N": str(new_tc)}
            old_image[f"b_{name}_tc"] = {"N": str(old_tc)}

        return {
            "eventName": "MODIFY",
            "dynamodb": {"NewImage": new_image, "OldImage": old_image},
        }

    def test_extract_namespace_from_pk(self) -> None:
        """_parse_bucket_record extracts namespace_id from PK."""
        record = self._make_record(pk="a7x3kq/ENTITY#user-123", entity_id="user-123")
        parsed = _parse_bucket_record(record)

        assert parsed is not None
        assert parsed.namespace_id == "a7x3kq"
        assert parsed.entity_id == "user-123"

    def test_pre_migration_record_returns_none(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Unprefixed PK (pre-migration) returns None and logs warning."""
        record = self._make_record(pk="ENTITY#user-123", entity_id="user-123")
        parsed = _parse_bucket_record(record)

        assert parsed is None

        captured = capsys.readouterr()
        logs = [json.loads(line) for line in captured.out.strip().split("\n") if line]
        warning_logs = [log for log in logs if log["level"] == "WARNING"]
        assert len(warning_logs) == 1
        assert "pre-migration" in warning_logs[0]["message"].lower()
        assert warning_logs[0]["pk"] == "ENTITY#user-123"

    def test_extract_deltas_propagates_namespace_id(self) -> None:
        """extract_deltas populates namespace_id on ConsumptionDelta."""
        record = self._make_record(pk="ns42/ENTITY#e1", entity_id="e1")
        deltas = extract_deltas(record)

        assert len(deltas) == 1
        assert deltas[0].namespace_id == "ns42"
        assert deltas[0].entity_id == "e1"

    def test_aggregate_bucket_states_propagates_namespace_id(self) -> None:
        """aggregate_bucket_states populates namespace_id on BucketRefillState."""
        records = [self._make_record(pk="nsabc/ENTITY#e1", entity_id="e1")]
        states = aggregate_bucket_states(records)

        assert len(states) == 1
        key = ("nsabc", "e1", "gpt-4", 0)
        assert key in states
        assert states[key].namespace_id == "nsabc"

    def test_cross_namespace_aggregation_independence(self) -> None:
        """Records from different namespaces aggregate independently."""
        records = [
            self._make_record(
                pk="ns1/ENTITY#user-1",
                entity_id="user-1",
                limits={"tpm": (0, 3000000)},
            ),
            self._make_record(
                pk="ns2/ENTITY#user-1",
                entity_id="user-1",
                limits={"tpm": (0, 7000000)},
            ),
        ]
        states = aggregate_bucket_states(records)

        assert len(states) == 2
        assert ("ns1", "user-1", "gpt-4", 0) in states
        assert ("ns2", "user-1", "gpt-4", 0) in states
        assert states[("ns1", "user-1", "gpt-4", 0)].limits["tpm"].tc_delta == 3000000
        assert states[("ns2", "user-1", "gpt-4", 0)].limits["tpm"].tc_delta == 7000000

    def test_update_snapshot_uses_namespaced_keys(self) -> None:
        """update_snapshot passes namespace_id to pk_entity and gsi2_pk_resource."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
            namespace_id="a7x3kq",
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "a7x3kq/ENTITY#entity-1"
        assert call_kwargs["ExpressionAttributeValues"][":gsi2pk"] == "a7x3kq/RESOURCE#gpt-4"

    def test_try_refill_bucket_uses_namespaced_key(self) -> None:
        """try_refill_bucket uses new bucket PK format."""
        mock_table = MagicMock()
        state = BucketRefillState(
            namespace_id="a7x3kq",
            entity_id="entity-1",
            resource="gpt-4",
            rf_ms=1704067195000,
            limits={
                "tpm": LimitRefillInfo(
                    tc_delta=20000000,
                    tk_milli=5000000,
                    cp_milli=100000000,
                    ra_milli=100000000,
                    rp_ms=60000,
                ),
            },
        )
        now_ms = 1704067200000

        try_refill_bucket(mock_table, state, now_ms)

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "a7x3kq/BUCKET#entity-1#gpt-4#0"
        assert call_kwargs["Key"]["SK"] == "#STATE"

    def test_pre_migration_records_skipped_in_extract_deltas(self) -> None:
        """extract_deltas returns empty list for pre-migration records."""
        record = self._make_record(pk="ENTITY#user-123", entity_id="user-123")
        deltas = extract_deltas(record)
        assert deltas == []

    def test_pre_migration_records_skipped_in_aggregate(self) -> None:
        """aggregate_bucket_states skips pre-migration records."""
        records = [self._make_record(pk="ENTITY#user-123", entity_id="user-123")]
        states = aggregate_bucket_states(records)
        assert len(states) == 0


class TestNewBucketPKParsing:
    """Tests for _parse_bucket_record with new BUCKET PK scheme."""

    def _make_new_pk_record(
        self,
        pk: str = "ns1/BUCKET#user-1#gpt-4#0",
        sk: str = "#STATE",
        entity_id: str = "user-1",
        resource: str = "gpt-4",
        rf: int = 1704067200000,
        shard_count: int = 1,
        limits: dict[str, tuple[int, int]] | None = None,
    ) -> dict:
        """Helper to create a stream record with new BUCKET PK."""
        if limits is None:
            limits = {"rpm": (0, 1000)}

        new_image: dict = {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf)},
            "shard_count": {"N": str(shard_count)},
        }
        old_image: dict = {
            "PK": {"S": pk},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "rf": {"N": str(rf - 1000)},
            "shard_count": {"N": str(shard_count)},
        }

        for name, (old_tc, new_tc) in limits.items():
            new_image[f"b_{name}_tc"] = {"N": str(new_tc)}
            old_image[f"b_{name}_tc"] = {"N": str(old_tc)}
            # Add bucket config attrs
            new_image[f"b_{name}_tk"] = {"N": "100000000"}
            new_image[f"b_{name}_cp"] = {"N": "100000000"}
            new_image[f"b_{name}_ra"] = {"N": "100000000"}
            new_image[f"b_{name}_rp"] = {"N": "60000"}

        return {
            "eventName": "MODIFY",
            "dynamodb": {"NewImage": new_image, "OldImage": old_image},
        }

    def test_parse_bucket_record_new_pk(self) -> None:
        """Parser handles new BUCKET PK scheme."""
        record = self._make_new_pk_record()
        result = _parse_bucket_record(record)

        assert result is not None
        assert result.namespace_id == "ns1"
        assert result.entity_id == "user-1"
        assert result.resource == "gpt-4"
        assert result.shard_id == 0
        assert result.shard_count == 1

    def test_parse_bucket_record_new_pk_with_shards(self) -> None:
        """Parser extracts shard_id and shard_count from new PK."""
        record = self._make_new_pk_record(
            pk="ns1/BUCKET#user-1#gpt-4#2",
            shard_count=4,
        )
        result = _parse_bucket_record(record)

        assert result is not None
        assert result.shard_id == 2
        assert result.shard_count == 4

    def test_parse_bucket_record_old_pk_still_works(self) -> None:
        """Old ENTITY PK with #BUCKET# SK still parses (backwards compat)."""
        record = self._make_new_pk_record(
            pk="ns1/ENTITY#user-1",
            sk="#BUCKET#gpt-4",
        )
        result = _parse_bucket_record(record)

        assert result is not None
        assert result.namespace_id == "ns1"
        assert result.entity_id == "user-1"
        assert result.resource == "gpt-4"
        assert result.shard_id == 0
        assert result.shard_count == 1

    def test_aggregate_bucket_states_keys_by_shard(self) -> None:
        """Different shards for same (entity, resource) are aggregated separately."""
        records = [
            self._make_new_pk_record(
                pk="ns1/BUCKET#user-1#gpt-4#0",
                shard_count=2,
                limits={"rpm": (0, 1000)},
            ),
            self._make_new_pk_record(
                pk="ns1/BUCKET#user-1#gpt-4#1",
                shard_count=2,
                limits={"rpm": (0, 2000)},
            ),
        ]
        states = aggregate_bucket_states(records)
        assert ("ns1", "user-1", "gpt-4", 0) in states
        assert ("ns1", "user-1", "gpt-4", 1) in states
        assert len(states) == 2
        assert states[("ns1", "user-1", "gpt-4", 0)].shard_id == 0
        assert states[("ns1", "user-1", "gpt-4", 1)].shard_id == 1
        assert states[("ns1", "user-1", "gpt-4", 0)].shard_count == 2

    def test_try_refill_bucket_new_pk_and_effective_limits(self) -> None:
        """Refill uses new PK and divides capacity/refill_amount by shard_count."""
        mock_table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns1",
            entity_id="user-1",
            resource="gpt-4",
            shard_id=0,
            shard_count=2,
            rf_ms=1704067195000,  # 5s ago
            limits={
                "rpm": LimitRefillInfo(
                    tc_delta=5000_000,
                    tk_milli=0,  # empty bucket
                    cp_milli=10000_000,  # original capacity 10000
                    ra_milli=10000_000,  # original refill_amount 10000
                    rp_ms=60_000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is True
        call_kwargs = mock_table.update_item.call_args[1]
        # Verify new PK format
        assert call_kwargs["Key"]["PK"] == "ns1/BUCKET#user-1#gpt-4#0"
        assert call_kwargs["Key"]["SK"] == "#STATE"

    def test_extract_deltas_filters_wcu(self) -> None:
        """wcu limit deltas are excluded from usage snapshots."""
        record = self._make_new_pk_record(
            limits={"rpm": (0, 1000), "wcu": (0, 500)},
        )
        deltas = extract_deltas(record)
        limit_names = [d.limit_name for d in deltas]
        assert "rpm" in limit_names
        assert "wcu" not in limit_names

    def test_try_refill_bucket_old_pk_shard_0(self) -> None:
        """Refill for shard_count=1 (old format) uses new PK format."""
        mock_table = MagicMock()
        state = BucketRefillState(
            namespace_id="ns1",
            entity_id="user-1",
            resource="gpt-4",
            shard_id=0,
            shard_count=1,
            rf_ms=1704067195000,
            limits={
                "rpm": LimitRefillInfo(
                    tc_delta=5000_000,
                    tk_milli=0,
                    cp_milli=10000_000,
                    ra_milli=10000_000,
                    rp_ms=60_000,
                ),
            },
        )
        now_ms = 1704067200000

        result = try_refill_bucket(mock_table, state, now_ms)

        assert result is True
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "ns1/BUCKET#user-1#gpt-4#0"
        assert call_kwargs["Key"]["SK"] == "#STATE"


class TestTryProactiveShard:
    """Tests for try_proactive_shard function.

    Uses wcu token level (remaining / capacity) as the sharding signal.
    Triggers when tokens < 20% of capacity (WCU_PROACTIVE_THRESHOLD_LOW).
    """

    def _make_state(
        self,
        shard_id: int = 0,
        shard_count: int = 1,
    ) -> BucketRefillState:
        return BucketRefillState(
            namespace_id="ns1",
            entity_id="user-1",
            resource="gpt-4",
            shard_id=shard_id,
            shard_count=shard_count,
            rf_ms=1704067200000,
        )

    def test_triggers_at_low_token_level(self) -> None:
        """Proactive sharding triggers when wcu tokens < 20% of capacity."""
        mock_table = MagicMock()
        state = self._make_state()
        wcu_tk_milli = 150_000  # 15% remaining < 20% threshold
        wcu_capacity_milli = 1_000_000

        result = try_proactive_shard(mock_table, state, wcu_tk_milli, wcu_capacity_milli)

        assert result is True
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "ns1/BUCKET#user-1#gpt-4#0"
        assert call_kwargs["ExpressionAttributeValues"][":new"] == 2
        assert call_kwargs["ConditionExpression"] == "shard_count = :old"

    def test_skips_above_threshold(self) -> None:
        """No sharding when wcu tokens >= 20% of capacity."""
        mock_table = MagicMock()
        state = self._make_state()
        wcu_tk_milli = 250_000  # 25% remaining >= 20% threshold
        wcu_capacity_milli = 1_000_000

        result = try_proactive_shard(mock_table, state, wcu_tk_milli, wcu_capacity_milli)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_skips_non_shard_0(self) -> None:
        """Only shard 0 can be bumped."""
        mock_table = MagicMock()
        state = self._make_state(shard_id=1, shard_count=2)

        result = try_proactive_shard(mock_table, state, 100_000, 1_000_000)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_conditional_check_failure_returns_false(self) -> None:
        """If another writer already bumped, silently skip."""
        mock_table = MagicMock()
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "UpdateItem",
        )
        state = self._make_state()

        result = try_proactive_shard(mock_table, state, 100_000, 1_000_000)

        assert result is False

    def test_zero_capacity_skips(self) -> None:
        """Zero wcu capacity does not divide by zero."""
        mock_table = MagicMock()
        state = self._make_state()

        result = try_proactive_shard(mock_table, state, 100_000, 0)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_triggers_at_zero_tokens(self) -> None:
        """Proactive sharding triggers when wcu tokens are completely exhausted."""
        mock_table = MagicMock()
        state = self._make_state()

        result = try_proactive_shard(mock_table, state, 0, 1_000_000)

        assert result is True

    def test_boundary_at_exactly_20_percent(self) -> None:
        """At exactly 20% remaining, no sharding (threshold is strictly less than)."""
        mock_table = MagicMock()
        state = self._make_state()

        result = try_proactive_shard(mock_table, state, 200_000, 1_000_000)

        assert result is False
        mock_table.update_item.assert_not_called()

    def test_negative_tokens_triggers(self) -> None:
        """Negative tokens (overdrawn wcu) trigger sharding."""
        mock_table = MagicMock()
        state = self._make_state()

        result = try_proactive_shard(mock_table, state, -50_000, 1_000_000)

        assert result is True

    def test_one_millitoken_below_threshold(self) -> None:
        """Off-by-one: 19.9999% < 20% triggers sharding."""
        mock_table = MagicMock()
        state = self._make_state()

        result = try_proactive_shard(mock_table, state, 199_999, 1_000_000)

        assert result is True

    def test_doubles_shard_count_from_2(self) -> None:
        """Verify shard_count * 2 with existing count=2."""
        mock_table = MagicMock()
        state = self._make_state(shard_count=2)

        result = try_proactive_shard(mock_table, state, 100_000, 1_000_000)

        assert result is True
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":new"] == 4

    def test_warns_above_threshold(self, capsys) -> None:
        """Proactive sharding logs warning when new count exceeds threshold."""
        mock_table = MagicMock()
        state = self._make_state(shard_id=0, shard_count=32)

        result = try_proactive_shard(mock_table, state, 100_000, 1_000_000)

        assert result is True
        captured = capsys.readouterr().out
        assert '"level": "WARNING"' in captured
        assert "High shard count" in captured
        assert '"shard_count": 64' in captured

    def test_batch_size_independent(self) -> None:
        """Token level is invariant to BatchSize (unlike tc_delta)."""
        mock_table = MagicMock()
        state = self._make_state()

        # With old tc_delta metric, BatchSize=100 could only reach 10%.
        # With token level, we check remaining tokens regardless of batch size.
        result = try_proactive_shard(mock_table, state, 100_000, 1_000_000)

        assert result is True  # 10% remaining < 20% threshold


class TestPropagateShardsCount:
    """Tests for propagate_shard_count function."""

    def _make_shard_change_record(
        self,
        pk: str = "ns1/BUCKET#user-1#gpt-4#0",
        sk: str = "#STATE",
        old_shard_count: int = 2,
        new_shard_count: int = 4,
    ) -> dict:
        """Helper to create a stream record with shard_count change."""
        return {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": pk},
                    "SK": {"S": sk},
                    "shard_count": {"N": str(new_shard_count)},
                },
                "OldImage": {
                    "PK": {"S": pk},
                    "SK": {"S": sk},
                    "shard_count": {"N": str(old_shard_count)},
                },
            },
        }

    def test_propagate_on_change(self) -> None:
        """When shard_count changes, propagate to other shards.

        Existing shards (1) get UpdateItem, new shards (2, 3) get PutItem.
        """
        mock_table = MagicMock()
        record = self._make_shard_change_record(
            old_shard_count=2,
            new_shard_count=4,
        )

        propagated = propagate_shard_count(mock_table, record)

        assert propagated == 3  # 1 updated + 2 created (not shard 0)
        # Existing shard 1 updated via UpdateItem
        update_pks = {c[1]["Key"]["PK"] for c in mock_table.update_item.call_args_list}
        assert "ns1/BUCKET#user-1#gpt-4#1" in update_pks
        assert "ns1/BUCKET#user-1#gpt-4#0" not in update_pks
        # New shards 2, 3 created via PutItem
        put_pks = {c.kwargs["Item"]["PK"] for c in mock_table.put_item.call_args_list}
        assert "ns1/BUCKET#user-1#gpt-4#2" in put_pks
        assert "ns1/BUCKET#user-1#gpt-4#3" in put_pks

    def test_no_change_no_propagation(self) -> None:
        """No propagation when shard_count unchanged."""
        mock_table = MagicMock()
        record = self._make_shard_change_record(
            old_shard_count=2,
            new_shard_count=2,
        )

        propagated = propagate_shard_count(mock_table, record)

        assert propagated == 0
        mock_table.update_item.assert_not_called()

    def test_conditional_prevents_downgrade(self) -> None:
        """Conditional write prevents overwriting a higher shard_count."""
        conditional_error = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "UpdateItem",
        )
        mock_table = MagicMock()
        mock_table.update_item.side_effect = conditional_error
        mock_table.put_item.side_effect = conditional_error
        record = self._make_shard_change_record(
            old_shard_count=2,
            new_shard_count=4,
        )

        propagated = propagate_shard_count(mock_table, record)

        assert propagated == 0  # All skipped

    def test_skips_non_shard_0(self) -> None:
        """Only propagates from shard 0."""
        mock_table = MagicMock()
        record = self._make_shard_change_record(
            pk="ns1/BUCKET#user-1#gpt-4#1",
            old_shard_count=2,
            new_shard_count=4,
        )

        propagated = propagate_shard_count(mock_table, record)

        assert propagated == 0
        mock_table.update_item.assert_not_called()

    def test_skips_non_bucket_pk(self) -> None:
        """Non-bucket PK returns 0."""
        mock_table = MagicMock()
        record = self._make_shard_change_record(
            pk="ns1/ENTITY#user-1",
        )

        propagated = propagate_shard_count(mock_table, record)

        assert propagated == 0
        mock_table.update_item.assert_not_called()

    def test_creates_full_items_for_new_shards(self) -> None:
        """New shards get full items cloned from shard 0's NewImage."""
        record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "4"},
                    "entity_id": {"S": "user-1"},
                    "GSI2PK": {"S": "ns1/RESOURCE#gpt-4"},
                    "GSI2SK": {"S": "BUCKET#user-1#0"},
                    "GSI3PK": {"S": "ns1/ENTITY#user-1"},
                    "GSI3SK": {"S": "BUCKET#gpt-4#0"},
                    "GSI4PK": {"S": "ns1"},
                    "GSI4SK": {"S": "BUCKET#user-1#gpt-4#0"},
                    "b_rpm_tk": {"N": "50000000"},
                    "b_rpm_cp": {"N": "100000000"},
                    "b_rpm_ra": {"N": "100000000"},
                    "b_rpm_rp": {"N": "60000"},
                    "b_rpm_tc": {"N": "0"},
                    "b_wcu_tk": {"N": "1000000"},
                    "b_wcu_cp": {"N": "1000000"},
                    "b_wcu_ra": {"N": "1000000"},
                    "b_wcu_rp": {"N": "1000"},
                    "b_wcu_tc": {"N": "500"},
                    "cascade": {"BOOL": False},
                    "rf": {"N": "1000"},
                },
                "OldImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "2"},
                },
            },
        }
        mock_table = MagicMock()
        propagated = propagate_shard_count(mock_table, record)

        # Shard 1 is EXISTING (update only), shards 2 and 3 are NEW (put)
        update_calls = mock_table.update_item.call_args_list
        put_calls = mock_table.put_item.call_args_list

        assert len(update_calls) == 1  # shard 1
        assert "shard_count < :new" in update_calls[0].kwargs["ConditionExpression"]

        assert len(put_calls) == 2  # shards 2 and 3
        for put_call in put_calls:
            item = put_call.kwargs["Item"]
            assert "GSI3PK" in item
            assert "GSI2PK" in item
            assert "b_rpm_tk" in item
            assert "b_wcu_tk" in item

        new_pks = {c.kwargs["Item"]["PK"] for c in put_calls}
        assert "ns1/BUCKET#user-1#gpt-4#2" in new_pks
        assert "ns1/BUCKET#user-1#gpt-4#3" in new_pks

        assert propagated == 3  # 1 updated + 2 created

    def test_existing_shards_not_put(self) -> None:
        """Existing shards get UpdateItem, not PutItem."""
        record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "4"},
                    "entity_id": {"S": "user-1"},
                    "b_rpm_tk": {"N": "50000000"},
                    "b_rpm_cp": {"N": "100000000"},
                    "b_rpm_ra": {"N": "100000000"},
                    "b_rpm_rp": {"N": "60000"},
                    "b_rpm_tc": {"N": "0"},
                    "cascade": {"BOOL": False},
                    "rf": {"N": "1000"},
                },
                "OldImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "2"},
                },
            },
        }
        mock_table = MagicMock()
        propagate_shard_count(mock_table, record)

        # Shard 1 is existing — UpdateItem, not PutItem
        update_pks = {c.kwargs["Key"]["PK"] for c in mock_table.update_item.call_args_list}
        assert "ns1/BUCKET#user-1#gpt-4#1" in update_pks

        put_pks = {c.kwargs["Item"]["PK"] for c in mock_table.put_item.call_args_list}
        assert "ns1/BUCKET#user-1#gpt-4#1" not in put_pks

    def test_new_shard_skips_if_client_created(self) -> None:
        """PutItem with attribute_not_exists(PK) skips client-created shards."""
        mock_table = MagicMock()
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": ""}},
            "PutItem",
        )
        record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "2"},
                    "entity_id": {"S": "user-1"},
                    "b_rpm_tk": {"N": "50000000"},
                    "b_rpm_cp": {"N": "100000000"},
                    "b_rpm_ra": {"N": "100000000"},
                    "b_rpm_rp": {"N": "60000"},
                    "b_rpm_tc": {"N": "0"},
                    "cascade": {"BOOL": False},
                    "rf": {"N": "1000"},
                },
                "OldImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "1"},
                },
            },
        }

        propagated = propagate_shard_count(mock_table, record)
        assert propagated == 0  # Client already created it

    def test_new_shard_tokens_set_to_effective_capacity(self) -> None:
        """New shard tokens are set to effective per-shard capacity."""
        record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "2"},
                    "entity_id": {"S": "user-1"},
                    "b_rpm_tk": {"N": "50000000"},
                    "b_rpm_cp": {"N": "100000000"},
                    "b_rpm_ra": {"N": "100000000"},
                    "b_rpm_rp": {"N": "60000"},
                    "b_rpm_tc": {"N": "5000"},
                    "b_wcu_tk": {"N": "500000"},
                    "b_wcu_cp": {"N": "1000000"},
                    "b_wcu_ra": {"N": "1000000"},
                    "b_wcu_rp": {"N": "1000"},
                    "b_wcu_tc": {"N": "200"},
                    "cascade": {"BOOL": False},
                    "rf": {"N": "1000"},
                },
                "OldImage": {
                    "PK": {"S": "ns1/BUCKET#user-1#gpt-4#0"},
                    "SK": {"S": "#STATE"},
                    "shard_count": {"N": "1"},
                },
            },
        }
        mock_table = MagicMock()
        propagate_shard_count(mock_table, record)

        put_calls = mock_table.put_item.call_args_list
        assert len(put_calls) == 1  # shard 1 is new
        item = put_calls[0].kwargs["Item"]

        # rpm tokens = effective capacity = 100000000 // 2 = 50000000
        assert item["b_rpm_tk"] == 50000000
        # rpm tc reset to 0
        assert item["b_rpm_tc"] == 0
        # wcu tokens = full capacity (not divided) = 1000000
        assert item["b_wcu_tk"] == 1000000
        # wcu tc reset to 0
        assert item["b_wcu_tc"] == 0


# ---------------------------------------------------------------------------
# Scheduled limits (#222 §3.3, §5) — the aggregator evaluates the bucket item's
# own schedule, never config, and re-stamps `vu` on the same rf-locked write.
# ---------------------------------------------------------------------------

NY = ZoneInfo("America/New_York")
TUE_1400 = int(datetime(2026, 9, 15, 14, 0, tzinfo=NY).timestamp() * 1000)
# Business hours at half rate. The window closes at 18:00 local.
BUSINESS = (ScheduleEntry(cron="* 9-17 * * MON-FRI", tz="America/New_York", scale=0.5),)
BUSINESS_COMPACT = "1h9-17w1-5s500"
# The 14:00 hour at a quarter rate. Its window closes at 15:00 local — three
# hours *before* BUSINESS does, which is what makes it useful as an override.
HOUR_14 = (ScheduleEntry(cron="* 14 * * *", tz="America/New_York", scale=0.25),)
HOUR_14_COMPACT = "1h14s250"
# What comes back off the wire. `decode` normalises weekday names to numbers
# (Task 5), so `MON-FRI` round-trips as `1-5` — the same schedule, spelled
# canonically. Comparing against the literal above would be asserting that
# normalisation does not happen.
BUSINESS_DECODED = decode(BUSINESS_COMPACT, "America/New_York")
HOUR_14_DECODED = decode(HOUR_14_COMPACT, "America/New_York")


def _sched_state(**kwargs) -> BucketRefillState:
    """A one-limit bucket, one minute stale, that has consumed 600 tokens."""
    base = dict(
        namespace_id="ns123",
        entity_id="user-1",
        resource="gpt-4",
        rf_ms=TUE_1400 - 60_000,
        limits={
            "rpm": LimitRefillInfo(
                tc_delta=600_000,
                tk_milli=0,
                cp_milli=1_000_000,
                ra_milli=1_000_000,
                rp_ms=60_000,
            )
        },
    )
    limit_sched = kwargs.pop("limit_sched", None)
    limit_reset_sched = kwargs.pop("limit_reset_sched", None)
    base.update(kwargs)
    state = BucketRefillState(**base)
    # `_parse_bucket_record` resolves every limit against the item-level
    # default, so a real state never carries one only at the item level. Since
    # #541 `try_refill_bucket` reads `info.sched` alone — an empty tuple there
    # means the limit is *explicitly* unscheduled — so the helper has to seed
    # the per-limit copies the way the parser would.
    for name, info in state.limits.items():
        info.sched = (limit_sched or {}).get(name, state.sched)
        info.reset_sched = (limit_reset_sched or {}).get(name, state.reset_sched)
    return state


def _sched_record(
    *,
    limits: dict[str, dict[str, int]],
    rf_ms: int = TUE_1400 - 60_000,
    sched: str | None = None,
    sched_tz: str = "America/New_York",
    limit_sched: dict[str, str] | None = None,
    rsched: str | None = None,
    limit_rsched: dict[str, str] | None = None,
    vu_ms: int | None = None,
    shard_count: int = 1,
    entity_id: str = "user-1",
    resource: str = "gpt-4",
    namespace_id: str = "ns123",
) -> dict:
    """A MODIFY stream record for a composite bucket item."""
    pk = f"{namespace_id}/BUCKET#{entity_id}#{resource}#0"
    new_image: dict = {
        "PK": {"S": pk},
        "SK": {"S": "#STATE"},
        "entity_id": {"S": entity_id},
        "resource": {"S": resource},
        "rf": {"N": str(rf_ms)},
        "shard_count": {"N": str(shard_count)},
    }
    old_image: dict = {"PK": {"S": pk}, "SK": {"S": "#STATE"}}
    for name, fields in limits.items():
        for field, value in fields.items():
            if field == "old_tc":
                continue
            new_image[f"b_{name}_{field}"] = {"N": str(value)}
        old_image[f"b_{name}_tc"] = {"N": str(fields.get("old_tc", 0))}
    if sched is not None:
        new_image["sched"] = {"S": sched}
        new_image["sched_tz"] = {"S": sched_tz}
    for name, compact in (limit_sched or {}).items():
        new_image[f"b_{name}_sched"] = {"S": compact}
    if rsched is not None:
        new_image["rsched"] = {"S": rsched}
        new_image["sched_tz"] = {"S": sched_tz}
    for name, compact in (limit_rsched or {}).items():
        new_image[f"b_{name}_rsched"] = {"S": compact}
    if vu_ms is not None:
        new_image["vu"] = {"N": str(vu_ms)}
    return {"eventName": "MODIFY", "dynamodb": {"NewImage": new_image, "OldImage": old_image}}


class TestAggregatorRespectsSchedules:
    """Refill targets the scheduled ceiling at the scheduled rate."""

    def test_refills_at_the_scheduled_rate_not_the_base(self) -> None:
        """During a 0.5x window one minute yields 500_000 millitokens, which
        does not cover the 600_000 consumption estimate, so the top-up runs."""
        table = MagicMock()
        assert try_refill_bucket(table, _sched_state(sched=BUSINESS), now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 500_000

    def test_unscheduled_same_bucket_is_skipped_entirely(self) -> None:
        """Discriminates the test above: at the base rate the same minute
        yields 1_000_000, which already covers 600_000, so nothing is written."""
        table = MagicMock()
        assert try_refill_bucket(table, _sched_state(), now_ms=TUE_1400) is False
        table.update_item.assert_not_called()

    def test_outside_the_window_the_base_rate_applies(self) -> None:
        """The schedule is a window, not a permanent scale: an instant no entry
        matches falls back to the base, and is then skipped like the control."""
        table = MagicMock()
        sunday = int(datetime(2026, 9, 13, 14, 0, tzinfo=NY).timestamp() * 1000)
        state = _sched_state(sched=BUSINESS, rf_ms=sunday - 60_000)
        assert try_refill_bucket(table, state, now_ms=sunday) is False

    def test_scheduled_ceiling_is_per_shard(self) -> None:
        """Scale first, then divide by shard_count."""
        table = MagicMock()
        state = _sched_state(sched=BUSINESS, shard_count=2)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 250_000  # (1_000_000 * 0.5) // 2

    def test_a_scale_down_trims_a_surplus(self) -> None:
        """Entering a 0.5x window with a full bucket must clamp, not sit on
        twice the scheduled ceiling (§3.3 — this is what replaces #469)."""
        table = MagicMock()
        state = _sched_state(sched=BUSINESS, rf_ms=TUE_1400)
        state.limits["rpm"].tk_milli = 1_000_000
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == -500_000

    def test_absolute_entries_override_capacity_and_rate(self) -> None:
        """A `capacity`/`refill_amount` entry replaces the base outright."""
        table = MagicMock()
        sched = (
            ScheduleEntry(
                cron="* 9-17 * * MON-FRI",
                tz="America/New_York",
                capacity=200,
                refill_amount=200,
            ),
        )
        assert try_refill_bucket(table, _sched_state(sched=sched), now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 200_000


class TestPerLimitScheduleOverride:
    """`b_{name}_sched` overrides the item-level default for that limit only."""

    LIMITS = {
        "rpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 600_000},
        "tpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 600_000},
    }

    def _state(self) -> BucketRefillState:
        record = _sched_record(
            limits=self.LIMITS,
            sched=BUSINESS_COMPACT,
            limit_sched={"rpm": HOUR_14_COMPACT},
        )
        states = aggregate_bucket_states([record])
        return next(iter(states.values()))

    def test_overridden_limit_uses_its_own_schedule(self) -> None:
        """rpm is on the 0.25x override, tpm on the 0.5x item default.

        Refilling rpm at the item default would hand it twice the tokens its
        own schedule allows — over-refill is the unsafe direction.
        """
        table = MagicMock()
        assert try_refill_bucket(table, self._state(), now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 250_000
        assert values[":rd_tpm"] == 500_000

    def test_vu_is_the_earliest_boundary_on_the_item(self) -> None:
        """`vu` is one item-level attribute, so the earliest change anywhere on
        the item has to force the pass. The override's window closes at 15:00,
        three hours before the item default's 18:00."""
        table = MagicMock()
        state = self._state()
        state.vu_ms = TUE_1400 - 1
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        expected = int(datetime(2026, 9, 15, 15, 0, tzinfo=NY).timestamp() * 1000)
        assert values[":new_vu"] == expected


class TestWcuIsNeverScheduledOrSharded:
    """`wcu` is the per-partition write ceiling, not a user limit."""

    def _wcu_state(self, **kwargs) -> BucketRefillState:
        return BucketRefillState(
            namespace_id="ns123",
            entity_id="user-1",
            resource="gpt-4",
            rf_ms=TUE_1400 - 60_000,
            limits={
                "wcu": LimitRefillInfo(
                    tc_delta=2_000_000,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=1_000,
                )
            },
            **kwargs,
        )

    def test_wcu_is_refilled_undivided_and_unscaled(self) -> None:
        """A user's 0.5x schedule must not halve the partition write ceiling,
        and every shard is its own partition so the ceiling is not divided."""
        table = MagicMock()
        state = self._wcu_state(shard_count=4, sched=BUSINESS)
        state.limits["wcu"].sched = BUSINESS
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_wcu"] == 1_000_000

    def test_user_limits_on_the_same_item_are_still_divided(self) -> None:
        """Discriminates the test above: the exemption is `wcu`-specific, not
        a blanket "stop dividing"."""
        table = MagicMock()
        state = self._wcu_state(shard_count=4, sched=BUSINESS)
        state.limits["wcu"].sched = BUSINESS
        state.limits["rpm"] = LimitRefillInfo(
            tc_delta=600_000,
            tk_milli=0,
            cp_milli=1_000_000,
            ra_milli=1_000_000,
            rp_ms=60_000,
            sched=BUSINESS,
        )
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_wcu"] == 1_000_000
        assert values[":rd_rpm"] == 125_000  # (1_000_000 * 0.5) // 4


class TestAggregatorRestampsVu:
    """An expired `vu` is replaced in the same rf-locked write."""

    def test_expired_vu_is_replaced_with_the_next_boundary(self) -> None:
        table = MagicMock()
        state = _sched_state(sched=BUSINESS, vu_ms=TUE_1400 - 1)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        expr = table.update_item.call_args.kwargs["UpdateExpression"]
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        names = table.update_item.call_args.kwargs["ExpressionAttributeNames"]
        assert "#vu = :new_vu" in expr
        assert names["#vu"] == "vu"
        assert values[":new_vu"] == int(datetime(2026, 9, 15, 18, 0, tzinfo=NY).timestamp() * 1000)

    def test_future_vu_is_left_alone(self) -> None:
        table = MagicMock()
        state = _sched_state(sched=BUSINESS, vu_ms=TUE_1400 + 3_600_000)
        try_refill_bucket(table, state, now_ms=TUE_1400)
        assert ":new_vu" not in table.update_item.call_args.kwargs["ExpressionAttributeValues"]

    def test_unscheduled_bucket_never_stamps_vu(self) -> None:
        """`vu = 0` on an unscheduled bucket (the #468 fan-out writes one on
        every call) has no boundary to advance to, so this pass must not
        invent one. Task 13 removes it instead."""
        table = MagicMock()
        state = _sched_state(vu_ms=0)
        state.limits["rpm"].tc_delta = 10_000_000  # force a write
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        assert ":new_vu" not in table.update_item.call_args.kwargs["ExpressionAttributeValues"]

    def test_restamp_is_conditioned_on_the_schedule_it_read(self) -> None:
        """The stream image can predate a fan-out that rewrote `sched` and set
        `vu = 0` *without touching rf*, so the rf lock alone does not catch it.
        Without this guard a stale image pushes `vu` back into the future and
        cancels the materialising pass `vu = 0` exists to force."""
        table = MagicMock()
        state = _sched_state(sched=BUSINESS, sched_compact=BUSINESS_COMPACT, vu_ms=TUE_1400 - 1)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        kwargs = table.update_item.call_args.kwargs
        assert kwargs["ConditionExpression"] == (
            "rf = :expected_rf AND #vu = :expected_vu AND #sched = :expected_sched"
        )
        assert kwargs["ExpressionAttributeValues"][":expected_sched"] == BUSINESS_COMPACT
        assert kwargs["ExpressionAttributeNames"]["#sched"] == "sched"

    def test_a_plain_refill_keeps_only_the_rf_and_vu_guards(self) -> None:
        """Discriminates the test above: the `#sched` guard rides with the `vu`
        re-stamp only, so it cannot cost skipped refills on every other write.

        The `#vu` pin does ride on every write — that is #508, and it is the
        one thing that makes a pre-fan-out image detectable at all, since the
        fan-out moves `cp`/`ra`/`rp`/`sched` and `vu` but never `rf`."""
        table = MagicMock()
        state = _sched_state(sched=BUSINESS)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        kwargs = table.update_item.call_args.kwargs
        assert kwargs["ConditionExpression"] == ("rf = :expected_rf AND attribute_not_exists(#vu)")
        assert "#sched" not in kwargs["ExpressionAttributeNames"]

    def test_the_vu_pin_matches_the_stamp_the_image_carried(self) -> None:
        """A `vu` present on the image is pinned by value, not by existence:
        the fan-out writes `vu = 0` over a *scheduled* bucket's live boundary
        too, and that must be just as detectable."""
        table = MagicMock()
        state = _sched_state(sched=BUSINESS, vu_ms=TUE_1400 + 3_600_000)
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        kwargs = table.update_item.call_args.kwargs
        assert "#vu = :expected_vu" in kwargs["ConditionExpression"]
        assert kwargs["ExpressionAttributeValues"][":expected_vu"] == TUE_1400 + 3_600_000


class TestScheduleIsCarriedFromTheStreamImage:
    """`sched`, `sched_tz`, `b_{name}_sched` and `vu` survive parsing."""

    LIMITS = {"rpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 1}}

    def test_parse_decodes_item_and_limit_schedules(self) -> None:
        parsed = _parse_bucket_record(
            _sched_record(
                limits=self.LIMITS,
                sched=BUSINESS_COMPACT,
                limit_sched={"rpm": HOUR_14_COMPACT},
                vu_ms=TUE_1400 + 5,
            )
        )
        assert parsed is not None
        assert parsed.sched == BUSINESS_DECODED
        assert parsed.sched_compact == BUSINESS_COMPACT
        assert parsed.limits["rpm"].sched == HOUR_14_DECODED
        assert parsed.vu_ms == TUE_1400 + 5
        assert parsed.sched_error is None

    def test_limits_without_an_override_inherit_the_item_schedule(self) -> None:
        parsed = _parse_bucket_record(_sched_record(limits=self.LIMITS, sched=BUSINESS_COMPACT))
        assert parsed is not None
        assert parsed.limits["rpm"].sched == BUSINESS_DECODED

    def test_unscheduled_item_parses_to_empty(self) -> None:
        parsed = _parse_bucket_record(_sched_record(limits=self.LIMITS))
        assert parsed is not None
        assert parsed.sched == ()
        assert parsed.sched_compact is None
        assert parsed.vu_ms is None

    def test_aggregate_carries_the_schedule_through(self) -> None:
        states = aggregate_bucket_states(
            [
                _sched_record(
                    limits=self.LIMITS,
                    sched=BUSINESS_COMPACT,
                    limit_sched={"rpm": HOUR_14_COMPACT},
                    vu_ms=TUE_1400 - 1,
                )
            ]
        )
        state = next(iter(states.values()))
        assert state.sched == BUSINESS_DECODED
        assert state.sched_compact == BUSINESS_COMPACT
        assert state.vu_ms == TUE_1400 - 1
        assert state.limits["rpm"].sched == HOUR_14_DECODED


class TestUndecodableSchedule:
    """§6 — an unreadable schedule must not be silently treated as "no schedule"."""

    # A hot bucket: at the *base* rate one minute yields 1_000_000, far short of
    # the 10_000_000 consumption estimate, so an aggregator that ignored the
    # unreadable schedule would definitely write. That is what makes the skip
    # below observable rather than indistinguishable from the usual threshold.
    LIMITS = {"rpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 10_000_000}}

    def test_parse_reports_rather_than_raises(self) -> None:
        """Raising here would abort the whole stream batch, snapshots included,
        and the record would retry until the stream stalled."""
        parsed = _parse_bucket_record(
            _sched_record(limits=self.LIMITS, sched="this-is-not-a-schedule")
        )
        assert parsed is not None
        assert parsed.sched_error is not None
        assert parsed.limits["rpm"].tc_delta == 10_000_000  # usage data still usable

    def test_usage_deltas_are_still_extracted(self) -> None:
        deltas = extract_deltas(_sched_record(limits=self.LIMITS, sched="1h99-nope"))
        assert [d.tokens_delta for d in deltas] == [10_000_000]

    def test_refill_is_skipped_entirely(self) -> None:
        """Refilling at the *base* rate would silently undo a scale-down."""
        table = MagicMock()
        states = aggregate_bucket_states(
            [_sched_record(limits=self.LIMITS, sched="this-is-not-a-schedule")]
        )
        state = next(iter(states.values()))
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is False
        table.update_item.assert_not_called()

    def test_an_undecodable_per_limit_override_also_skips(self) -> None:
        """The override is decoded separately from the item default, so it has
        its own way of being unreadable — and the same consequence."""
        table = MagicMock()
        states = aggregate_bucket_states(
            [
                _sched_record(
                    limits=self.LIMITS,
                    sched=BUSINESS_COMPACT,
                    limit_sched={"rpm": "not-a-schedule"},
                )
            ]
        )
        state = next(iter(states.values()))
        assert state.sched_error is not None
        assert "rpm schedule" in state.sched_error
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is False
        table.update_item.assert_not_called()

    def test_a_readable_schedule_on_the_same_shape_does_refill(self) -> None:
        """Discriminates the test above."""
        table = MagicMock()
        states = aggregate_bucket_states(
            [_sched_record(limits=self.LIMITS, sched=BUSINESS_COMPACT)]
        )
        assert try_refill_bucket(table, next(iter(states.values())), now_ms=TUE_1400) is True


class TestShardCloneRespectsSchedule:
    """Path 2 creates a shard *full*, so it must fill to the scheduled ceiling."""

    def _record(self, *, sched: str | None, limit_sched: dict | None = None) -> dict:
        record = _sched_record(
            limits={
                "rpm": {"tk": 500_000, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 0},
                "wcu": {"tk": 900_000, "cp": 1_000_000, "ra": 1_000_000, "rp": 1_000, "tc": 0},
            },
            sched=sched,
            limit_sched=limit_sched,
            shard_count=2,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "1"}
        return record

    def test_new_shard_starts_at_the_scheduled_share(self) -> None:
        """Cloning the base capacity during a 0.5x window would hand the new
        shard twice what the schedule allows — and the clone carries shard 0's
        `vu`, which is in the future, so the fast path would spend it."""
        table = MagicMock()
        assert propagate_shard_count(table, self._record(sched=BUSINESS_COMPACT), TUE_1400) == 1
        item = table.put_item.call_args.kwargs["Item"]
        assert item["b_rpm_tk"] == 250_000  # (1_000_000 * 0.5) // 2
        assert item["b_wcu_tk"] == 1_000_000  # per-partition, never divided or scaled

    def test_unscheduled_clone_starts_at_the_base_share(self) -> None:
        """Discriminates the test above."""
        table = MagicMock()
        assert propagate_shard_count(table, self._record(sched=None), TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpm_tk"] == 500_000

    def test_per_limit_override_is_honoured(self) -> None:
        table = MagicMock()
        record = self._record(sched=BUSINESS_COMPACT, limit_sched={"rpm": HOUR_14_COMPACT})
        assert propagate_shard_count(table, record, TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpm_tk"] == 125_000

    def test_undecodable_schedule_creates_no_shard(self) -> None:
        """A shard created at the wrong ceiling is worse than no shard: the
        client creates it on its slow path, where §6 applies."""
        table = MagicMock()
        assert propagate_shard_count(table, self._record(sched="not-a-schedule"), TUE_1400) == 0
        table.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Calendar resets (#222 §3.6) — the aggregator is the second materialising
# writer and must agree with the client about when an edge has been crossed.
# ---------------------------------------------------------------------------

DAILY_RESET = (ScheduleEntry.reset(cron="0 0 * * *", tz="America/New_York"),)
DAILY_RESET_COMPACT = "1m0h0"

WED_0030 = int(datetime(2026, 9, 16, 0, 30, tzinfo=NY).timestamp() * 1000)
TUE_2300 = int(datetime(2026, 9, 15, 23, 0, tzinfo=NY).timestamp() * 1000)


def _quota_state(**kwargs) -> BucketRefillState:
    """A 10,000/day quota bucket, 2,000 tokens left, last refilled at 23:00.

    ``ra_milli=0`` is the quota shape ADR-137 mandates: a limit drips or
    resets, never both, so the stored rate is zero and ``rp_ms`` is the inert
    ``_QUOTA_REFILL_PERIOD_SECONDS``. That is what makes the reset the *only*
    thing that can write to this bucket — an unreset one yields no refill delta
    at all, whatever the consumption threshold does.
    """
    base = dict(
        namespace_id="ns123",
        entity_id="user-1",
        resource="gpt-4",
        rf_ms=TUE_2300,
        limits={
            "rpd": LimitRefillInfo(
                tc_delta=0,
                tk_milli=2_000_000,
                cp_milli=10_000_000,
                ra_milli=0,
                rp_ms=1_000,
            )
        },
    )
    limit_reset = kwargs.pop("limit_reset", None)
    limit_sched = kwargs.pop("limit_sched", None)
    base.update(kwargs)
    state = BucketRefillState(**base)
    # Seeded per limit for the same reason `_sched_state` is: since #541
    # `try_refill_bucket` reads `info.reset_sched` alone, because an empty
    # tuple there means "explicitly no reset" rather than "not populated".
    for name, info in state.limits.items():
        info.sched = (limit_sched or {}).get(name, state.sched)
        info.reset_sched = (limit_reset or {}).get(name, state.reset_sched)
    return state


class TestAggregatorAppliesResets:
    """The aggregator expresses a reset as the same delta the client writes."""

    def test_expresses_the_reset_as_an_add_to_the_effective_capacity(self) -> None:
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == 10_000_000 - 2_000_000

    def test_the_same_bucket_without_a_reset_writes_nothing(self) -> None:
        """Discriminates the test above. A quota's stored rate is 0 (ADR-137),
        so `refill_bucket` yields no delta and there is nothing to write —
        the reset is the whole of this bucket's recovery."""
        table = MagicMock()
        assert try_refill_bucket(table, _quota_state(), now_ms=WED_0030) is False
        table.update_item.assert_not_called()

    def test_the_reset_bypasses_the_consumption_threshold(self) -> None:
        """Explicitly pinned, because the threshold is a `continue` on the
        positive branch and a reset is usually positive. A hot bucket has the
        largest tc_delta and is exactly where the aggregator, not the client,
        is the refiller — gating the reset behind the threshold would turn it
        off on the buckets it matters most for, the same defect §3.3 records
        for the negative clamp."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET)
        state.limits["rpd"].tc_delta = 9_000_000  # far above anything refill yields
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True

    def test_no_edge_since_rf_writes_nothing(self) -> None:
        """`rf` is already past midnight, so the edge is not new — the same
        `> rf` comparison, against the same stored `rf`, that the client makes."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, rf_ms=WED_0030 - 60_000)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_an_edge_exactly_at_rf_does_not_re_fire(self) -> None:
        """`>` not `>=`, matching `RateLimiter._apply_reset_edge`. A client
        that applied the reset stamps `rf` *at* the edge when its clock reading
        was the edge itself; re-firing here would refund what it then spent."""
        table = MagicMock()
        midnight = int(datetime(2026, 9, 16, 0, 0, tzinfo=NY).timestamp() * 1000)
        state = _quota_state(reset_sched=DAILY_RESET, rf_ms=midnight)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_the_reset_is_per_shard(self) -> None:
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, shard_count=4)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == (10_000_000 // 4) - 2_000_000

    def test_the_reset_respects_a_concurrent_param_schedule(self) -> None:
        """Compute effective params first, then set the balance to the result."""
        table = MagicMock()
        night = (ScheduleEntry(cron="* 0-6 * * *", tz="America/New_York", scale=0.5),)
        state = _quota_state(reset_sched=DAILY_RESET, sched=night)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == 5_000_000 - 2_000_000

    def test_the_reset_never_writes_tc(self) -> None:
        """`try_refill_bucket` writes only `tk` deltas and `rf`/`vu`. Pinned
        because a reset is the one refill big enough to tempt an implementer
        into 'fixing up' the counter."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        expr = table.update_item.call_args.kwargs["UpdateExpression"]
        assert "_tc" not in expr

    def test_a_balance_already_at_the_effective_capacity_writes_nothing(self) -> None:
        """A zero delta is not a write. The edge is real and new, but the
        balance is already where the reset would put it."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET)
        state.limits["rpd"].tk_milli = 10_000_000
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_a_reset_trims_a_balance_above_the_effective_capacity(self) -> None:
        """A reset *sets* the balance, so a surplus left by a shrink is removed
        by the same expression that tops a deficit up — the negative-ADD shape
        the unconditional clamp already uses."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, shard_count=4)
        state.limits["rpd"].tk_milli = 10_000_000
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == (10_000_000 // 4) - 10_000_000

    def test_wcu_is_exempt_from_the_item_level_reset(self) -> None:
        """`rsched` is item-level and applies to every limit by default, but
        `wcu` is the per-partition write ceiling, not a user limit — the same
        exemption `effective_params` already has.

        Discriminating: at the base rate this bucket's positive delta is
        suppressed by the threshold, so anything written here came from the
        reset.
        """
        table = MagicMock()
        state = _quota_state(
            reset_sched=DAILY_RESET,
            limits={
                "wcu": LimitRefillInfo(
                    tc_delta=0,
                    tk_milli=0,
                    cp_milli=1_000_000,
                    ra_milli=1_000_000,
                    rp_ms=60_000,
                )
            },
        )
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_a_per_limit_reset_override_beats_the_item_default(self) -> None:
        """`b_{name}_rsched` mirrors `b_{name}_sched`. The item-level default
        is Sunday-only and did not fire since `rf`; the per-limit override is
        daily and did."""
        table = MagicMock()
        state = _quota_state(
            reset_sched=(ScheduleEntry.reset(cron="0 0 * * SUN", tz="America/New_York"),),
            limit_reset={"rpd": DAILY_RESET},
        )
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True

    def test_the_item_level_default_alone_does_not_fire_here(self) -> None:
        """Discriminates the override test: without it, the Sunday-only item
        default has no edge between Tuesday 23:00 and Wednesday 00:30."""
        table = MagicMock()
        state = _quota_state(
            reset_sched=(ScheduleEntry.reset(cron="0 0 * * SUN", tz="America/New_York"),),
        )
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_only_the_limit_carrying_the_reset_is_restored(self) -> None:
        """One item, one `rf`, two limits — the reset is decided per limit.

        `rpm` drips, carries no reset of its own and the item has no default,
        so it must recover only what the elapsed time earns it.
        """
        table = MagicMock()
        state = _quota_state(
            limit_reset={"rpd": DAILY_RESET},
            limits={
                "rpd": LimitRefillInfo(
                    tc_delta=0, tk_milli=0, cp_milli=10_000_000, ra_milli=0, rp_ms=1_000
                ),
                # Same ceiling, but a slow drip: 1,000,000 milli an hour, so
                # the 90 minutes since `rf` earn 1,500,000 and not the ceiling.
                "rph": LimitRefillInfo(
                    tc_delta=5_000_000,
                    tk_milli=0,
                    cp_milli=10_000_000,
                    ra_milli=1_000_000,
                    rp_ms=3_600_000,
                ),
            },
        )
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == 10_000_000
        assert values[":rd_rph"] == 1_500_000  # 90 minutes of drip, not a reset

    def test_an_undecodable_schedule_still_skips_the_whole_bucket(self) -> None:
        """`sched_error` short-circuits before any reset logic runs. Refilling
        — or resetting — at the base would silently undo a scale-down (§6)."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, sched_error="rsched 'zzz': bad")
        assert try_refill_bucket(table, state, now_ms=WED_0030) is False

    def test_a_reset_only_item_gets_a_vu(self) -> None:
        """An expired `vu` is re-stamped from both tuples. With the parameter
        tuple alone a quota produces no boundary at all, `vu` is never
        refreshed, and the fast path stays demoted forever."""
        table = MagicMock()
        state = _quota_state(reset_sched=DAILY_RESET, vu_ms=WED_0030 - 1)
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":new_vu"] == int(datetime(2026, 9, 17, 0, 0, tzinfo=NY).timestamp() * 1000)


class TestResetSchedIsCarriedFromTheStreamImage:
    """`rsched` / `b_{name}_rsched` reach the refill state."""

    QUOTA = {"rpd": {"tc": 0, "tk": 2_000_000, "cp": 10_000_000, "ra": 0, "rp": 1_000}}

    def test_item_level_rsched_is_decoded(self) -> None:
        record = _sched_record(
            limits=self.QUOTA,
            rf_ms=TUE_2300,
            rsched=DAILY_RESET_COMPACT,
            sched_tz="America/New_York",
        )
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.reset_sched == decode_reset(DAILY_RESET_COMPACT, "America/New_York")
        assert parsed.limits["rpd"].reset_sched == parsed.reset_sched

    def test_a_per_limit_rsched_overrides_the_item_default(self) -> None:
        record = _sched_record(
            limits=self.QUOTA,
            rf_ms=TUE_2300,
            rsched="1m0h0w0",  # Sunday only
            limit_rsched={"rpd": DAILY_RESET_COMPACT},
        )
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.limits["rpd"].reset_sched == decode_reset(
            DAILY_RESET_COMPACT, "America/New_York"
        )
        assert parsed.reset_sched == decode_reset("1m0h0w0", "America/New_York")

    def test_no_rsched_leaves_both_tuples_empty(self) -> None:
        """Discriminates the two above: the overwhelming majority of items."""
        parsed = _parse_bucket_record(_sched_record(limits=self.QUOTA, rf_ms=TUE_2300))
        assert parsed is not None
        assert parsed.reset_sched == ()
        assert parsed.limits["rpd"].reset_sched == ()

    def test_an_undecodable_rsched_is_reported_not_raised(self) -> None:
        """Same rule as `sched`: raising inside the stream handler aborts the
        whole batch, snapshots included, and the record retries until the
        stream stalls."""
        record = _sched_record(limits=self.QUOTA, rsched="not-a-schedule")
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.sched_error is not None
        assert parsed.reset_sched == ()

    def test_an_undecodable_per_limit_rsched_is_reported_not_raised(self) -> None:
        record = _sched_record(limits=self.QUOTA, limit_rsched={"rpd": "not-a-schedule"})
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.sched_error is not None
        assert parsed.limits["rpd"].reset_sched == ()

    def test_the_reset_survives_aggregation_across_records(self) -> None:
        """`aggregate_bucket_states` keeps the last NewImage per key; the reset
        tuples ride along with `sched` rather than being dropped."""
        record = _sched_record(limits=self.QUOTA, rf_ms=TUE_2300, rsched=DAILY_RESET_COMPACT)
        states = aggregate_bucket_states([record, record])
        state = next(iter(states.values()))
        assert state.reset_sched == decode_reset(DAILY_RESET_COMPACT, "America/New_York")
        assert state.limits["rpd"].reset_sched == state.reset_sched


class TestAnUnscheduledLimitOnAScheduledItem:
    """#541: `BUCKET_SCHED_NONE` is an override meaning "this limit has none".

    Before it, "unscheduled" and "same as the item default" were both spelled
    as *absence* of `b_{name}_sched`, and the aggregator read every absence as
    inheritance. On a mixed item that runs both ways at once: a plain rate
    limit is refilled toward `0.5 x capacity` at `0.5 x` its rate, and it takes
    the quota's midnight reset — a hard SET of its balance on a calendar it
    never declared.
    """

    # 1_000_000 milli capacity refilling in one minute. One stale minute at the
    # base rate yields 1_000_000, which covers the 600_000 consumption estimate
    # and is therefore skipped; at 0.5x it yields 500_000 and is written. The
    # two are distinguishable by whether `update_item` is called at all.
    MIXED = {
        "rpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 600_000},
        "tpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 600_000},
    }

    def _record(self, **kwargs):
        return _sched_record(
            limits=self.MIXED,
            sched=BUSINESS_COMPACT,
            limit_sched={"tpm": BUCKET_SCHED_NONE},
            **kwargs,
        )

    def test_parse_reads_the_marker_as_no_schedule(self) -> None:
        parsed = _parse_bucket_record(self._record())
        assert parsed is not None
        assert parsed.sched == BUSINESS_DECODED  # the item default is unchanged
        assert parsed.limits["rpm"].sched == BUSINESS_DECODED  # inherits, as before
        assert parsed.limits["tpm"].sched == ()
        assert parsed.sched_error is None  # the marker is not a decode failure

    def test_the_marked_limit_refills_at_its_base_rate(self) -> None:
        """The scheduled sibling is written and the marked one is not: at the
        base rate a stale minute already covers the consumption estimate."""
        table = MagicMock()
        state = next(iter(aggregate_bucket_states([self._record()]).values()))
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpm"] == 500_000
        assert ":rd_tpm" not in values

    def test_the_marked_limit_takes_no_reset_edge(self) -> None:
        """The direction the issue understates. `rsched` is one item-level
        attribute, so a quota sharing an item with a rate limit handed the rate
        limit its midnight reset — a hard SET of the balance, and the drip
        skipped on that pass."""
        table = MagicMock()
        record = _sched_record(
            limits={
                "rpd": {"tk": 2_000_000, "cp": 10_000_000, "ra": 0, "rp": 1_000, "tc": 0},
                "rpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 0},
            },
            rf_ms=TUE_2300,
            rsched=DAILY_RESET_COMPACT,
            limit_rsched={"rpm": BUCKET_SCHED_NONE},
        )
        state = next(iter(aggregate_bucket_states([record]).values()))
        assert try_refill_bucket(table, state, now_ms=WED_0030) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":rd_rpd"] == 10_000_000 - 2_000_000  # the quota does reset
        assert ":rd_rpm" not in values  # ...and the rate limit is already full

    def test_a_cloned_shard_seeds_the_marked_limit_unscaled(self) -> None:
        """Path 2 creates a shard full, so an inherited 0.5x would halve an
        unscheduled limit's share for the life of the window."""
        table = MagicMock()
        record = _sched_record(
            limits={
                "rpm": {"tk": 500_000, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 0},
                "tpm": {"tk": 500_000, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 0},
            },
            sched=BUSINESS_COMPACT,
            limit_sched={"tpm": BUCKET_SCHED_NONE},
            shard_count=2,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "1"}
        assert propagate_shard_count(table, record, TUE_1400) == 1
        item = table.put_item.call_args.kwargs["Item"]
        assert item["b_rpm_tk"] == 250_000  # (1_000_000 * 0.5) // 2
        assert item["b_tpm_tk"] == 500_000  # 1_000_000 // 2, unscaled

    def test_the_marker_still_yields_the_items_vu(self) -> None:
        """`vu` is one item-level attribute, so the earliest boundary anywhere
        on the item still has to force a materialising pass — a marked limit
        contributes none of its own but must not suppress its siblings'."""
        table = MagicMock()
        state = next(iter(aggregate_bucket_states([self._record(vu_ms=TUE_1400 - 1)]).values()))
        assert try_refill_bucket(table, state, now_ms=TUE_1400) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert int(values[":new_vu"]) > TUE_1400


class TestABatchWithNoConsumptionStillReachesTheBucket:
    """A batch carrying no `tc` delta must still refill, clamp and re-stamp `vu`.

    `process_stream_records` used to return the moment `extract_deltas` came
    back empty. Usage aggregation was its only job when that short-circuit was
    written; proactive refill (#317), the negative clamp and the reset edge
    (#222), the `vu` re-stamp and proactive sharding all landed *below* it
    afterwards, and none of them reads a consumption delta.

    The batch that exposes it is the one the whole `vu = 0` design depends on:
    a `_sync_bucket_params` fan-out rewrites `cp`/`ra`/`sched` and stamps
    `vu = 0` **without touching `tc`**, so the record it puts on the stream
    carries an unchanged counter. The aggregator returned immediately, leaving
    the bucket above its new ceiling with `vu` expired and pinned to the client
    slow path until some client happened to acquire against it.
    """

    # Unchanged counter: `old_tc == new_tc`, exactly as a fan-out leaves it.
    # `tk` sits an order of magnitude above the freshly lowered `cp`.
    SHRUNK = {
        "rpm": {
            "old_tc": 600_000,
            "tc": 600_000,
            "tk": 1_000_000,
            "cp": 100_000,
            "ra": 100_000,
            "rp": 60_000,
        }
    }

    @staticmethod
    def _run(records, now_ms):
        with patch("zae_limiter_aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table
            with patch("zae_limiter_aggregator.processor.time_module") as mock_time:
                mock_time.perf_counter.return_value = 0.0
                mock_time.time.return_value = now_ms / 1000
                result = process_stream_records(records, "test_table", ["hourly"])
        return result, mock_table

    def test_the_clamp_still_lands(self) -> None:
        record = _sched_record(limits=self.SHRUNK, rf_ms=TUE_1400 - 60_000)
        result, table = self._run([record], TUE_1400)

        assert result.refills_written == 1
        (call,) = table.update_item.call_args_list
        assert call.kwargs["ExpressionAttributeValues"][":rd_rpm"] == 100_000 - 1_000_000

    def test_vu_is_still_restamped(self) -> None:
        """The half that pins the bucket to the slow path when it is skipped."""
        record = _sched_record(
            limits=self.SHRUNK,
            rf_ms=TUE_1400 - 60_000,
            sched=BUSINESS_COMPACT,
            vu_ms=0,
        )
        result, table = self._run([record], TUE_1400)

        assert result.refills_written == 1
        (call,) = table.update_item.call_args_list
        assert "#vu = :new_vu" in call.kwargs["UpdateExpression"]
        # BUSINESS closes at 18:00 local, which is the next parameter change.
        assert call.kwargs["ExpressionAttributeValues"][":new_vu"] > TUE_1400

    def test_an_empty_batch_is_still_a_no_op(self) -> None:
        """The short-circuit's legitimate case: nothing to read, nothing to write."""
        result, table = self._run([], TUE_1400)
        assert (result.processed_count, result.refills_written) == (0, 0)
        table.update_item.assert_not_called()

    def test_one_malformed_record_does_not_poison_the_batch(self) -> None:
        """Reaching the bucket image on every batch means reaching malformed ones.

        `_parse_bucket_record` raises on an attribute it cannot read, and the
        short-circuit used to hide that from `aggregate_bucket_states` whenever
        the same record also broke `extract_deltas`. Out of this loop it would
        fail the invocation and the event source would redrive the same batch
        until it aged out.
        """
        good = _sched_record(limits=self.SHRUNK, rf_ms=TUE_1400 - 60_000)
        bad = _sched_record(limits=self.SHRUNK, rf_ms=TUE_1400 - 60_000, entity_id="user-2")
        bad["dynamodb"]["NewImage"]["b_rpm_tc"] = {"N": "not-a-number"}

        result, table = self._run([bad, good], TUE_1400)

        assert result.refills_written == 1
        (call,) = table.update_item.call_args_list
        assert "user-1" in call.kwargs["Key"]["PK"]


class TestQuotaShardCloneIsATransfer:
    """Path 2 must not mint a quota's new shards (#587).

    A quota never drips (ADR-137), so a clone created at
    ``capacity // shard_count`` is allowance nothing reclaims before the next
    reset edge. The clones are filled by transfer instead: the shards being
    split from are clamped to their new ceiling, and what that takes is what the
    clones get.
    """

    CAPACITY = 10_000_000

    def _record(self, *, tk: int, old_count: int = 1, new_count: int = 2, **kwargs) -> dict:
        record = _sched_record(
            limits={
                "rpd": {"tk": tk, "cp": self.CAPACITY, "ra": 0, "rp": 1_000, "tc": 0},
                "wcu": {"tk": 900_000, "cp": 1_000_000, "ra": 1_000_000, "rp": 1_000, "tc": 0},
            },
            rsched=DAILY_RESET_COMPACT,
            shard_count=new_count,
            **kwargs,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": str(old_count)}
        return record

    @staticmethod
    def _table(*, reclaimed: dict[str, int] | None = None) -> MagicMock:
        """A table whose conditional clamp reports ``reclaimed`` as the old value."""
        table = MagicMock()
        attributes = {f"b_{name}_tk": value for name, value in (reclaimed or {}).items()}
        table.update_item.return_value = {"Attributes": attributes}
        return table

    def test_a_full_quota_clone_is_paid_for_by_the_clamp(self) -> None:
        """Unchanged from before #587 — the case the bug hid behind."""
        table = self._table(reclaimed={"rpd": self.CAPACITY})
        assert propagate_shard_count(table, self._record(tk=self.CAPACITY), TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpd_tk"] == 5_000_000
        clamp = table.update_item.call_args.kwargs
        assert clamp["ExpressionAttributeValues"][":share"] == 5_000_000
        assert clamp["ConditionExpression"] == "attribute_exists(PK) AND #tk > :share"

    def test_a_spent_quota_clone_gets_nothing(self) -> None:
        """#587 itself. The clamp reclaims nothing from a shard already below
        its new ceiling, so there is nothing to hand the clone."""
        table = self._table()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no surplus"}},
            "UpdateItem",
        )
        assert propagate_shard_count(table, self._record(tk=2_000), TUE_1400) == 1
        item = table.put_item.call_args.kwargs["Item"]
        assert item["b_rpd_tk"] == 0
        assert item["b_wcu_tk"] == 1_000_000  # per-partition, never divided

    def test_the_pool_is_handed_out_greedily_across_the_new_shards(self) -> None:
        """One doubling adds several shards; the first usable one gets the
        transfer rather than every clone getting a slice too small to admit."""
        table = self._table(reclaimed={"rpd": 4_000_000})
        record = self._record(tk=4_000_000, old_count=2, new_count=4)
        assert propagate_shard_count(table, record, TUE_1400) == 3  # 1 updated + 2 created
        granted = [c.kwargs["Item"]["b_rpd_tk"] for c in table.put_item.call_args_list]
        # Two shards clamped from 4_000_000 to 2_500_000 => 3_000_000 reclaimed.
        assert granted == [2_500_000, 500_000]

    def test_a_clamp_that_reports_no_attributes_contributes_nothing(self) -> None:
        table = self._table()
        table.update_item.return_value = {}
        assert propagate_shard_count(table, self._record(tk=self.CAPACITY), TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpd_tk"] == 0

    def test_a_non_conditional_clamp_failure_propagates(self) -> None:
        table = self._table()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "no"}},
            "UpdateItem",
        )
        with pytest.raises(ClientError):
            propagate_shard_count(table, self._record(tk=self.CAPACITY), TUE_1400)

    def test_a_dripping_limit_on_the_same_item_still_gets_a_full_share(self) -> None:
        """The regression pin: only the quota changes shape."""
        table = self._table(reclaimed={"rpd": self.CAPACITY})
        record = _sched_record(
            limits={
                "rpd": {"tk": 2_000, "cp": self.CAPACITY, "ra": 0, "rp": 1_000, "tc": 0},
                "rpm": {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 0},
            },
            rsched=DAILY_RESET_COMPACT,
            limit_rsched={"rpm": BUCKET_SCHED_NONE},
            shard_count=2,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "1"}
        assert propagate_shard_count(table, record, TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpm_tk"] == 500_000

    def test_a_zero_rate_without_a_reset_is_not_treated_as_a_quota(self) -> None:
        """A corrupt item (ADR-137 pairs the two), and starving it would be
        wrong for the one shape that is not a quota but reads like one."""
        table = self._table()
        record = _sched_record(
            limits={"rpd": {"tk": 2_000, "cp": self.CAPACITY, "ra": 0, "rp": 1_000, "tc": 0}},
            shard_count=2,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "1"}
        assert propagate_shard_count(table, record, TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpd_tk"] == 5_000_000
        table.update_item.assert_not_called()

    def test_the_unscheduled_marker_blocks_inheriting_the_items_reset(self) -> None:
        """#541's marker means "this limit declares none", so it is not a quota
        however the item-level default reads."""
        table = self._table()
        record = self._record(tk=2_000, limit_rsched={"rpd": BUCKET_SCHED_NONE})
        assert propagate_shard_count(table, record, TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpd_tk"] == 5_000_000

    def test_a_per_limit_reset_makes_it_a_quota_without_an_item_default(self) -> None:
        table = self._table(reclaimed={"rpd": self.CAPACITY})
        record = _sched_record(
            limits={
                "rpd": {"tk": self.CAPACITY, "cp": self.CAPACITY, "ra": 0, "rp": 1_000, "tc": 0}
            },
            limit_rsched={"rpd": DAILY_RESET_COMPACT},
            shard_count=2,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "1"}
        assert propagate_shard_count(table, record, TUE_1400) == 1
        assert table.put_item.call_args.kwargs["Item"]["b_rpd_tk"] == 5_000_000


# ---------------------------------------------------------------------------
# Duration windows (ADR-139, #627) — the aggregator applies a window a client
# anchored and fanned out, reads it off the stream image, and never anchors or
# fans out one of its own.
# ---------------------------------------------------------------------------

# A 5-hour session window that opened at WS. Epoch values are arbitrary: an
# unscheduled item evaluates no cron, so nothing here depends on a calendar.
WS = 1_800_000_000_000
RSA = 18_000  # seconds
WINDOW_END = WS + RSA * 1000
SESSION_CP = 10_000_000


def _session_limit(**overrides: int) -> dict[str, int]:
    fields = {"tk": 0, "cp": SESSION_CP, "ra": 0, "rp": 1_000, "tc": 0, "ws": WS, "rsa": RSA}
    fields.update(overrides)
    return fields


def _rpm_limit(**overrides: int) -> dict[str, int]:
    """A dripping limit whose batch consumption outruns one period of refill."""
    fields = {"tk": 0, "cp": 1_000_000, "ra": 1_000_000, "rp": 60_000, "tc": 2_000_000}
    fields.update(overrides)
    return fields


def _window_state(record: dict) -> BucketRefillState:
    """Aggregate one real stream record, exactly as `process_stream_records` does."""
    states = aggregate_bucket_states([record])
    assert len(states) == 1
    return next(iter(states.values()))


def _session_record(*, rf_ms: int, **kwargs) -> dict:
    limits = kwargs.pop("limits", None) or {"session": _session_limit()}
    return _sched_record(limits=limits, rf_ms=rf_ms, **kwargs)


class TestWindowIsCarriedFromTheStreamImage:
    def test_parse_reads_the_window_off_the_image(self) -> None:
        parsed = _parse_bucket_record(_session_record(rf_ms=WS - 1))
        assert parsed is not None
        assert parsed.limits["session"].window_start_ms == WS
        assert parsed.limits["session"].reset_after_seconds == RSA

    def test_a_limit_without_a_window_parses_to_none(self) -> None:
        parsed = _parse_bucket_record(_session_record(rf_ms=0, limits={"rpm": _rpm_limit()}))
        assert parsed is not None
        assert parsed.limits["rpm"].window_start_ms is None
        assert parsed.limits["rpm"].reset_after_seconds is None

    def test_wcu_never_carries_a_window(self) -> None:
        """`rsched` is item-level and needed an explicit carve-out, or a user's
        midnight reset would hand `wcu` its per-partition write ceiling back at
        every edge. `ws` is per-limit, so the exemption is STRUCTURAL: no writer
        stamps one on `wcu`, and a session window beside it leaves it bare."""
        record = _session_record(
            rf_ms=WS - 1,
            limits={
                "session": _session_limit(),
                "wcu": {"tk": 1_000_000, "cp": 1_000_000, "ra": 1_000_000, "rp": 1_000, "tc": 0},
            },
        )
        parsed = _parse_bucket_record(record)
        assert parsed is not None
        assert parsed.limits["wcu"].window_start_ms is None
        assert parsed.limits["wcu"].reset_after_seconds is None

    def test_the_last_image_in_a_batch_wins(self) -> None:
        first = _session_record(rf_ms=WS - 1, limits={"session": _session_limit(ws=WS - 5)})
        second = _session_record(rf_ms=WS - 1)
        state = aggregate_bucket_states([first, second])[("ns123", "user-1", "gpt-4", 0)]
        assert state.limits["session"].window_start_ms == WS
        assert state.limits["session"].reset_after_seconds == RSA


class TestIsQuotaLimitRecognisesADurationWindow:
    """Reads the STREAM IMAGE, not a `Limit`, so it tests stored attributes. A
    duration quota misread as a dripping limit here would be minted a fresh
    share at shard-create time — #587 again, for this feature."""

    @staticmethod
    def _image(record: dict) -> dict:
        return record["dynamodb"]["NewImage"]

    def test_a_zero_rate_beside_an_rsa_is_a_quota(self) -> None:
        image = self._image(_session_record(rf_ms=WS))
        assert _is_quota_limit("session", image) is True

    def test_an_rsa_without_a_ws_is_still_a_quota(self) -> None:
        """The param sync stamps `rsa` before any window is anchored."""
        limit = _session_limit()
        del limit["ws"]
        image = self._image(_session_record(rf_ms=WS, limits={"session": limit}))
        assert _is_quota_limit("session", image) is True

    def test_an_rsa_beside_a_positive_rate_is_not(self) -> None:
        """The mirror corruption `Limit.__post_init__` rejects (ADR-137)."""
        image = self._image(_session_record(rf_ms=WS, limits={"session": _session_limit(ra=5)}))
        assert _is_quota_limit("session", image) is False

    def test_the_unscheduled_marker_does_not_hide_a_window(self) -> None:
        """A duration quota sharing an item with a calendar quota: the item's
        `rsched` default belongs to the calendar quota, so the duration quota
        is stamped with the #541 marker to block inheriting it. The marker
        speaks for its calendar reset only — it declares no window either way."""
        record = _session_record(
            rf_ms=WS,
            limits={
                "session": _session_limit(),
                "rpd": {"tk": 0, "cp": SESSION_CP, "ra": 0, "rp": 1_000, "tc": 0},
            },
            rsched=DAILY_RESET_COMPACT,
            limit_rsched={"session": BUCKET_SCHED_NONE},
        )
        image = self._image(record)
        assert _is_quota_limit("session", image) is True
        assert _is_quota_limit("rpd", image) is True

    def test_a_shard_clone_fills_the_duration_quota_by_transfer(self) -> None:
        """The #587 guard end to end, on the shared item above. The existing
        shard is spent, so the clamp reclaims nothing and the clone gets
        nothing — not the 5_000_000 fresh share a dripping limit would get."""
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no surplus"}},
            "UpdateItem",
        )
        record = _session_record(
            rf_ms=TUE_1400,
            limits={
                "session": _session_limit(tk=2_000),
                "rpd": {"tk": 2_000, "cp": SESSION_CP, "ra": 0, "rp": 1_000, "tc": 0},
            },
            rsched=DAILY_RESET_COMPACT,
            limit_rsched={"session": BUCKET_SCHED_NONE},
            shard_count=2,
        )
        record["dynamodb"]["OldImage"]["shard_count"] = {"N": "1"}
        assert propagate_shard_count(table, record, TUE_1400) == 1
        item = table.put_item.call_args.kwargs["Item"]
        assert item["b_session_tk"] == 0
        assert item["b_rpd_tk"] == 0
        clamped = {
            c.kwargs["ExpressionAttributeNames"]["#tk"] for c in table.update_item.call_args_list
        }
        assert clamped == {"b_session_tk", "b_rpd_tk"}


class TestAggregatorRollsAWindow:
    """The aggregator applies a window it sees, as the client's `ws > rf` does."""

    @staticmethod
    def _write(table: MagicMock) -> dict:
        return table.update_item.call_args.kwargs

    def test_aggregator_rolls_an_elapsed_window(self) -> None:
        """`rf` before `ws` -> this shard has not applied the window. tk = 0,
        effective cp = 10_000_000 -> ADD +10_000_000. Evaluated BEFORE the
        accrual-rate guard: a duration quota's stored rate is 0 (ADR-137) and
        that guard would skip exactly the limits the feature exists for."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 60_000, vu_ms=0))
        assert try_refill_bucket(table, state, now_ms=WS + 30_000) is True
        write = self._write(table)
        assert write["UpdateExpression"].endswith("ADD #wtk0 :wd0")
        assert write["ExpressionAttributeNames"]["#wtk0"] == "b_session_tk"
        assert write["ExpressionAttributeValues"][":wd0"] == SESSION_CP
        assert write["ExpressionAttributeValues"][":new_rf"] == WS + 30_000

    def test_the_roll_bypasses_the_consumption_threshold(self) -> None:
        """A hot bucket has the largest tc_delta and is where the aggregator,
        not the client, is the refiller."""
        table = MagicMock()
        state = _window_state(
            _session_record(rf_ms=WS - 1, limits={"session": _session_limit(tc=9_000_000)})
        )
        assert try_refill_bucket(table, state, now_ms=WS + 1) is True

    def test_the_roll_is_to_the_per_shard_share(self) -> None:
        table = MagicMock()
        state = _window_state(
            _session_record(
                rf_ms=WS - 1, limits={"session": _session_limit(tk=1_000)}, shard_count=2
            )
        )
        assert try_refill_bucket(table, state, now_ms=WS + 1) is True
        assert self._write(table)["ExpressionAttributeValues"][":wd0"] == SESSION_CP // 2 - 1_000

    def test_a_roll_trims_a_balance_above_the_share(self) -> None:
        table = MagicMock()
        state = _window_state(
            _session_record(
                rf_ms=WS - 1, limits={"session": _session_limit(tk=SESSION_CP)}, shard_count=2
            )
        )
        assert try_refill_bucket(table, state, now_ms=WS + 1) is True
        assert self._write(table)["ExpressionAttributeValues"][":wd0"] == -(SESSION_CP // 2)

    def test_an_applied_window_writes_nothing(self) -> None:
        """`rf` already past `ws`: the window is in the balance."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS + 1))
        assert try_refill_bucket(table, state, now_ms=WS + 60_000) is False
        table.update_item.assert_not_called()

    def test_a_ws_exactly_at_rf_does_not_re_fire(self) -> None:
        """Strictly `>`, as `BucketState.window_rolled`: the pass that applies
        a roll stamps `rf` at `ws`, so `>=` would refund everything spent since."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS))
        assert try_refill_bucket(table, state, now_ms=WS + 60_000) is False
        table.update_item.assert_not_called()

    def test_a_balance_already_at_the_share_writes_nothing(self) -> None:
        table = MagicMock()
        state = _window_state(
            _session_record(rf_ms=WS - 1, limits={"session": _session_limit(tk=SESSION_CP)})
        )
        assert try_refill_bucket(table, state, now_ms=WS + 1) is False
        table.update_item.assert_not_called()

    def test_an_rsa_without_a_ws_is_not_rolled(self) -> None:
        """No window has been anchored yet; opening one is the client's job."""
        limit = _session_limit()
        del limit["ws"]
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 1, limits={"session": limit}))
        assert try_refill_bucket(table, state, now_ms=WS + 1) is False
        table.update_item.assert_not_called()

    def test_a_stale_ws_without_an_rsa_is_not_rolled(self) -> None:
        """The param sync REMOVEs `rsa` (never `ws`) when a limit loses its
        window; the start left behind is not a window in force."""
        limit = _session_limit()
        del limit["rsa"]
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 1, limits={"session": limit}))
        assert try_refill_bucket(table, state, now_ms=WS + 1) is False
        table.update_item.assert_not_called()

    def test_a_limit_carrying_both_resets_keeps_the_calendar_branch(self) -> None:
        """Corrupt (`Limit` makes the two spellings exclusive): no edge since
        `rf`, so the calendar branch writes nothing, and the window is not
        allowed to reset the same balance under a second rule."""
        table = MagicMock()
        state = _window_state(
            _session_record(
                rf_ms=TUE_2300,
                limits={"session": _session_limit(ws=TUE_2300 + 1)},
                limit_rsched={"session": DAILY_RESET_COMPACT},
            )
        )
        assert try_refill_bucket(table, state, now_ms=TUE_2300 + 60_000) is False
        table.update_item.assert_not_called()

    def test_wcu_carrying_a_window_is_never_rolled(self) -> None:
        """No writer stamps one; a corrupt item must still not hand `wcu` its
        per-partition ceiling back."""
        table = MagicMock()
        wcu = {
            "tk": 0,
            "cp": 1_000_000,
            "ra": 1_000_000,
            "rp": 1_000,
            "tc": 0,
            "ws": WS,
            "rsa": RSA,
        }
        state = _window_state(_session_record(rf_ms=WS - 1, limits={"wcu": wcu}))
        # One millisecond of drip is 1_000 milli: the rate refill, not a roll.
        assert try_refill_bucket(table, state, now_ms=WS) is False
        table.update_item.assert_not_called()

    def test_a_limit_name_never_reaches_an_expression_token(self) -> None:
        """`NAME_PATTERN` allows `-` and `.`, neither legal in a token (#634
        tracks the pre-existing `:rd_{name}` spelling; the roll adds none)."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 1, limits={"s.q-1": _session_limit()}))
        assert try_refill_bucket(table, state, now_ms=WS + 1) is True
        write = self._write(table)
        assert "s.q-1" not in write["UpdateExpression"]
        assert not any("s.q-1" in token for token in write["ExpressionAttributeValues"])
        assert write["ExpressionAttributeNames"]["#wtk0"] == "b_s.q-1_tk"

    def test_the_aggregator_does_not_fan_out(self) -> None:
        """It processes one bucket shard per stream record and would fan out
        once per shard per batch — S² writes rather than S. The client's
        fan-out plus the `ws > rf` rule already converges every shard; this is
        an optimisation on top. So one write, to this shard, and no `ws`."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 1, shard_count=4, vu_ms=0))
        assert try_refill_bucket(table, state, now_ms=WS + 1) is True
        assert table.update_item.call_count == 1
        table.put_item.assert_not_called()
        write = self._write(table)
        assert write["Key"]["PK"] == "ns123/BUCKET#user-1#gpt-4#0"
        assert "_ws" not in write["UpdateExpression"]
        # `ws` is named only by the condition's pin, never by the update.
        written = {
            v
            for k, v in write["ExpressionAttributeNames"].items()
            if k in write["UpdateExpression"]
        }
        assert not any(v.endswith("_ws") for v in written)


class TestTheRollIsPinnedToTheWindowItRestores:
    """The `rf` and `vu` pins cannot tell two window fan-outs apart.

    A fan-out of the *next* window writes `SET ws, rsa, vu = 0`: `rf` is
    untouched and `vu` is rewritten to the same 0 the image carried. An
    aggregator whose clock runs behind would restore the dead window under an
    `rf` still below the new `ws`. Pinning `ws` refuses that write.
    """

    def test_a_roll_pins_the_ws_it_read(self) -> None:
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 60_000, vu_ms=0))
        assert try_refill_bucket(table, state, now_ms=WS + 30_000) is True
        write = table.update_item.call_args.kwargs
        assert " AND #wws0 = :ews0" in write["ConditionExpression"]
        assert write["ExpressionAttributeNames"]["#wws0"] == "b_session_ws"
        assert write["ExpressionAttributeValues"][":ews0"] == WS

    def test_each_rolled_limit_carries_its_own_pin(self) -> None:
        table = MagicMock()
        record = _session_record(
            rf_ms=WS - 60_000,
            limits={"session": _session_limit(), "s.q-1": _session_limit(ws=WS + 5)},
        )
        assert try_refill_bucket(table, _window_state(record), now_ms=WS + 30_000) is True
        write = table.update_item.call_args.kwargs
        names = write["ExpressionAttributeNames"]
        values = write["ExpressionAttributeValues"]
        pins = {names[f"#wws{i}"]: values[f":ews{i}"] for i in range(2)}
        assert pins == {"b_session_ws": WS, "b_s.q-1_ws": WS + 5}
        assert "s.q-1" not in write["ConditionExpression"]

    def test_no_roll_carries_no_pin(self) -> None:
        """An applied window beside a drip top-up: nothing restored, nothing pinned."""
        table = MagicMock()
        record = _session_record(
            rf_ms=WS + 1_000, limits={"session": _session_limit(), "rpm": _rpm_limit()}
        )
        assert try_refill_bucket(table, _window_state(record), now_ms=WS + 60_000) is True
        write = table.update_item.call_args.kwargs
        assert "#wws" not in write["ConditionExpression"]
        assert not any(k.startswith("#wws") for k in write["ExpressionAttributeNames"])

    def test_a_refused_pin_is_skipped_like_any_lost_lock(self) -> None:
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException", "Message": "ws moved"}},
            "UpdateItem",
        )
        state = _window_state(_session_record(rf_ms=WS - 60_000, vu_ms=0))
        assert try_refill_bucket(table, state, now_ms=WS + 30_000) is False


class TestAggregatorRfNeverUnappliesAWindow:
    """`rf` is stamped as the client stamps it (`lease._monotonic_rf`)."""

    def test_a_drip_limit_beside_an_unapplied_window_applies_the_roll_too(self) -> None:
        """The Task 7 carry: the client's `_propagate_window_start` wrote
        `SET ws, rsa, vu = 0` on this sibling, and a drip limit on the same
        item needs a top-up. The one write that advances `rf` past `ws` must
        be the write that applies the roll, or the window is recorded as
        applied without its reset — the entity held to the dead window's
        leftovers for a whole window."""
        table = MagicMock()
        record = _session_record(
            rf_ms=WS - 60_000,
            limits={"session": _session_limit(), "rpm": _rpm_limit()},
            vu_ms=0,
        )
        now = WS + 30_000
        assert try_refill_bucket(table, _window_state(record), now_ms=now) is True
        write = table.update_item.call_args.kwargs
        values = write["ExpressionAttributeValues"]
        assert values[":wd0"] == SESSION_CP
        assert values[":rd_rpm"] == 1_000_000
        assert values[":new_rf"] >= WS
        # The #508 pin still matches the fan-out's `vu = 0` ...
        assert values[":expected_vu"] == 0
        # ... and the re-stamp is the window's end, so the fast path resumes
        # inside the window and closes again when it does.
        assert values[":new_vu"] == WINDOW_END

    def test_an_aggregator_clock_behind_ws_stamps_rf_at_ws(self) -> None:
        """A client with a faster clock anchored the window. Stamping `rf =
        now` would leave `ws > rf`, and the next pass would reset again —
        refunding everything spent in between."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 60_000))
        assert try_refill_bucket(table, state, now_ms=WS - 5_000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":wd0"] == SESSION_CP
        assert values[":new_rf"] == WS

    def test_an_aggregator_clock_behind_rf_never_moves_it_backward(self) -> None:
        """An applied window (`ws <= rf`): moving `rf` below `ws` would un-apply
        it. The drip limit's surplus is still trimmed."""
        table = MagicMock()
        record = _session_record(
            rf_ms=WS + 60_000,
            limits={"session": _session_limit(tk=5), "rpm": _rpm_limit(tk=2_000_000, tc=0)},
        )
        assert try_refill_bucket(table, _window_state(record), now_ms=WS - 1_000) is True
        values = table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":new_rf"] == WS + 60_000
        assert values[":rd_rpm"] == -1_000_000
        assert ":wd0" not in values

    def test_an_unwindowed_item_is_never_moved_backward_either(self) -> None:
        table = MagicMock()
        record = _session_record(rf_ms=WS, limits={"rpm": _rpm_limit(tk=2_000_000, tc=0)})
        assert try_refill_bucket(table, _window_state(record), now_ms=WS - 1_000) is True
        assert table.update_item.call_args.kwargs["ExpressionAttributeValues"][":new_rf"] == WS


class TestAnEndedUnappliedWindowIsLeftToTheClient:
    """`ws > rf` but the window has already closed at `now` (ruling 5).

    The client's next pass opens a *new* window and resets unconditionally.
    Restoring the dead window here would put an allowance on the item that the
    consumption-only retry (which stamps no `ws`) can spend before that pass,
    on top of the new window's full allowance. So the aggregator writes
    nothing to the item at all: advancing `rf` past `ws` would record the dead
    window as applied, and holding `rf` below it would double-credit the drip.
    """

    def test_the_whole_item_is_skipped(self) -> None:
        table = MagicMock()
        record = _session_record(
            rf_ms=WS - 60_000,
            limits={"session": _session_limit(), "rpm": _rpm_limit()},
            vu_ms=0,
        )
        assert try_refill_bucket(table, _window_state(record), now_ms=WINDOW_END) is False
        table.update_item.assert_not_called()

    def test_one_millisecond_before_the_end_it_still_rolls(self) -> None:
        """Half-open `[ws, ws + rsa)`: the end instant belongs to the next window."""
        table = MagicMock()
        state = _window_state(_session_record(rf_ms=WS - 60_000))
        assert try_refill_bucket(table, state, now_ms=WINDOW_END - 1) is True


class TestVuHonoursTheWindowEnd:
    """`_item_next_boundary` carries each window's end (ruling 3)."""

    def test_the_window_end_is_the_restamp_when_it_is_earliest(self) -> None:
        table = MagicMock()
        record = _session_record(
            rf_ms=WS + 1_000,
            limits={"session": _session_limit(), "rpm": _rpm_limit()},
            vu_ms=WS,
        )
        assert try_refill_bucket(table, _window_state(record), now_ms=WS + 60_000) is True
        assert (
            table.update_item.call_args.kwargs["ExpressionAttributeValues"][":new_vu"] == WINDOW_END
        )

    def test_a_param_boundary_before_the_window_end_wins(self) -> None:
        """The `min` rule: HOUR_14's window closes at 15:00, well inside a
        window opened at 14:00 that runs five hours."""
        table = MagicMock()
        ws = TUE_1400
        record = _session_record(
            rf_ms=ws + 1_000,
            limits={"session": _session_limit(ws=ws), "rpm": _rpm_limit()},
            limit_sched={"rpm": HOUR_14_COMPACT},
            vu_ms=ws,
        )
        # `_sched_record` stamps the zone only beside an item-level schedule.
        record["dynamodb"]["NewImage"]["sched_tz"] = {"S": "America/New_York"}
        assert try_refill_bucket(table, _window_state(record), now_ms=ws + 60_000) is True
        new_vu = table.update_item.call_args.kwargs["ExpressionAttributeValues"][":new_vu"]
        assert new_vu == ws + 3_600_000 < ws + RSA * 1000

    def test_an_ended_applied_window_keeps_the_fast_path_closed(self) -> None:
        """The window is in the balance but over: the next use must take the
        slow path and anchor. The drip limit is still topped up, but `vu` is
        left where it was — a re-stamp past the end would let the fast path
        spend the dead window's balance."""
        table = MagicMock()
        ws = TUE_1400 - RSA * 1000 - 60_000  # the window closed at 13:59
        record = _session_record(
            rf_ms=ws + 1_000,
            limits={"session": _session_limit(ws=ws), "rpm": _rpm_limit()},
            # A scheduled neighbour whose own boundary (15:00) is in the
            # future: without the window's vote, that is what `vu` would get.
            limit_sched={"rpm": HOUR_14_COMPACT},
            vu_ms=ws + RSA * 1000,
        )
        record["dynamodb"]["NewImage"]["sched_tz"] = {"S": "America/New_York"}
        assert try_refill_bucket(table, _window_state(record), now_ms=TUE_1400) is True
        write = table.update_item.call_args.kwargs
        assert ":new_vu" not in write["ExpressionAttributeValues"]
        assert "#vu = :new_vu" not in write["UpdateExpression"]
        assert ":wd0" not in write["ExpressionAttributeValues"]

    def test_an_rsa_without_a_ws_keeps_the_fast_path_closed(self) -> None:
        """A window due to open on the next client pass, beside a scheduled
        limit whose own boundary is hours away."""
        limit = _session_limit()
        del limit["ws"]
        table = MagicMock()
        record = _session_record(
            rf_ms=TUE_1400 - 60_000,
            limits={"session": limit, "rpm": _rpm_limit()},
            limit_sched={"rpm": HOUR_14_COMPACT},
            vu_ms=0,
        )
        record["dynamodb"]["NewImage"]["sched_tz"] = {"S": "America/New_York"}
        assert try_refill_bucket(table, _window_state(record), now_ms=TUE_1400) is True
        assert ":new_vu" not in table.update_item.call_args.kwargs["ExpressionAttributeValues"]
