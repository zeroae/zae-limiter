"""Tests for aggregator processor module."""

import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter_aggregator.processor import (
    BucketRefillState,
    ConsumptionDelta,
    LimitRefillInfo,
    ProcessResult,
    StructuredLogger,
    _parse_bucket_record,
    aggregate_bucket_states,
    calculate_snapshot_ttl,
    extract_deltas,
    get_window_end,
    get_window_key,
    process_stream_records,
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
