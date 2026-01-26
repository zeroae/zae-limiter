"""Tests for aggregator processor module."""

import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter.aggregator.processor import (
    ConsumptionDelta,
    ProcessResult,
    StructuredLogger,
    calculate_snapshot_ttl,
    extract_delta,
    get_window_end,
    get_window_key,
    process_stream_records,
    update_snapshot,
)


class TestConsumptionDelta:
    """Tests for ConsumptionDelta dataclass."""

    def test_dataclass_fields(self) -> None:
        """ConsumptionDelta stores all fields."""
        delta = ConsumptionDelta(
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=5000,
            timestamp_ms=1704067200000,
        )

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
            errors=["error1", "error2"],
        )

        assert result.processed_count == 10
        assert result.snapshots_updated == 5
        assert result.errors == ["error1", "error2"]


class TestExtractDelta:
    """Tests for extract_delta function."""

    def _make_record(
        self,
        sk: str = "#BUCKET#gpt-4#tpm",
        entity_id: str = "entity-1",
        old_tokens: int = 10000,
        new_tokens: int = 5000,
        last_refill_ms: int = 1704067200000,
        old_counter: int | None = 0,
        new_counter: int | None = 5000000,  # 5000 tokens consumed in millitokens
    ) -> dict:
        """Helper to create a stream record.

        The counter values (old_counter, new_counter) track consumption in millitokens.
        Set both to None to simulate old buckets without counter (issue #179).
        """
        new_image: dict = {
            "PK": {"S": f"ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "tokens_milli": {"N": str(new_tokens)},
            "last_refill_ms": {"N": str(last_refill_ms)},
        }
        old_image: dict = {
            "PK": {"S": f"ENTITY#{entity_id}"},
            "SK": {"S": sk},
            "entity_id": {"S": entity_id},
            "tokens_milli": {"N": str(old_tokens)},
            "last_refill_ms": {"N": str(last_refill_ms - 1000)},
        }

        record: dict = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": new_image,
                "OldImage": old_image,
            },
        }
        # Add counter as FLAT top-level attribute - issue #179
        if new_counter is not None:
            record["dynamodb"]["NewImage"]["total_consumed_milli"] = {"N": str(new_counter)}
        if old_counter is not None:
            record["dynamodb"]["OldImage"]["total_consumed_milli"] = {"N": str(old_counter)}
        return record

    def test_valid_bucket_record_consumption(self) -> None:
        """Extract delta from valid BUCKET record with consumption."""
        # Counter tracks consumption in millitokens: 5000 tokens = 5000000 millitokens
        record = self._make_record(
            sk="#BUCKET#gpt-4#tpm",
            entity_id="test-entity",
            old_tokens=10000,
            new_tokens=5000,
            last_refill_ms=1704067200000,
            old_counter=0,
            new_counter=5000000,  # consumed 5000 tokens (in millitokens)
        )

        delta = extract_delta(record)

        assert delta is not None
        assert delta.entity_id == "test-entity"
        assert delta.resource == "gpt-4"
        assert delta.limit_name == "tpm"
        assert delta.tokens_delta == 5000000  # counter delta in millitokens
        assert delta.timestamp_ms == 1704067200000

    def test_valid_bucket_record_refund(self) -> None:
        """Extract negative delta when tokens are released (refund)."""
        # Counter decreases when tokens are released (net tracking)
        record = self._make_record(
            old_tokens=5000,
            new_tokens=10000,  # tokens increased (release)
            old_counter=10000000,  # was at 10000 tokens consumed
            new_counter=5000000,  # now at 5000 tokens (released 5000)
        )

        delta = extract_delta(record)

        assert delta is not None
        assert delta.tokens_delta == -5000000  # negative = returned (millitokens)

    def test_non_bucket_record_returns_none(self) -> None:
        """Non-BUCKET records return None."""
        record = self._make_record(sk="#LIMIT#gpt-4#tpm")
        assert extract_delta(record) is None

        record = self._make_record(sk="#META")
        assert extract_delta(record) is None

        record = self._make_record(sk="#RESOURCE#gpt-4")
        assert extract_delta(record) is None

    def test_zero_delta_returns_none(self) -> None:
        """Zero delta (no consumption) returns None."""
        record = self._make_record(
            old_tokens=5000,
            new_tokens=5000,
            old_counter=1000000,
            new_counter=1000000,  # same counter = no consumption
        )
        assert extract_delta(record) is None

    def test_malformed_sk_returns_none(self) -> None:
        """Malformed SK returns None."""
        # Only one part after #BUCKET#
        record = self._make_record(sk="#BUCKET#gpt-4")
        assert extract_delta(record) is None

        # Empty parts - counter delta still valid
        record = self._make_record(sk="#BUCKET##", old_counter=0, new_counter=1000000)
        delta = extract_delta(record)
        # This actually extracts "" and "" which is valid format
        assert delta is not None

    def test_missing_counter_returns_none(self) -> None:
        """Missing counter (old bucket) returns None - issue #179."""
        record = self._make_record(old_counter=None, new_counter=None)
        assert extract_delta(record) is None

    def test_partial_counter_returns_none(self) -> None:
        """Partial counter (only one image has it) returns None."""
        record = self._make_record(old_counter=None, new_counter=1000000)
        assert extract_delta(record) is None

        record = self._make_record(old_counter=1000000, new_counter=None)
        assert extract_delta(record) is None

    def test_missing_entity_id_returns_none(self) -> None:
        """Missing entity_id returns None."""
        record = self._make_record()
        del record["dynamodb"]["NewImage"]["entity_id"]
        assert extract_delta(record) is None

    def test_empty_entity_id_returns_none(self) -> None:
        """Empty entity_id returns None."""
        record = self._make_record()
        record["dynamodb"]["NewImage"]["entity_id"]["S"] = ""
        assert extract_delta(record) is None

    def test_missing_data_uses_defaults(self) -> None:
        """Missing data fields default to 0 (but counter still works)."""
        record = self._make_record(old_counter=0, new_counter=1000000)
        del record["dynamodb"]["NewImage"]["tokens_milli"]
        del record["dynamodb"]["OldImage"]["tokens_milli"]

        delta = extract_delta(record)
        assert delta is not None  # counter delta is 1000000
        assert delta.tokens_delta == 1000000

    def test_missing_dynamodb_key(self) -> None:
        """Missing dynamodb key returns None."""
        record = {"eventName": "MODIFY"}
        assert extract_delta(record) is None

    def test_missing_new_image(self) -> None:
        """Missing NewImage returns None."""
        record = {"eventName": "MODIFY", "dynamodb": {"OldImage": {}}}
        assert extract_delta(record) is None


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
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=5000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 30, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["Key"]["PK"] == "ENTITY#entity-1"
        assert call_kwargs["Key"]["SK"] == "#USAGE#gpt-4#2024-01-01T14:00:00Z"

    def test_converts_millitokens_to_tokens(self) -> None:
        """Verifies millitokens are converted to tokens."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
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
            entity_id="entity-1",
            resource="gpt-4",
            limit_name="tpm",
            tokens_delta=1000000,
            timestamp_ms=int(datetime(2024, 1, 1, 14, 0, 0, tzinfo=UTC).timestamp() * 1000),
        )

        update_snapshot(mock_table, delta, "hourly", 90)

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":gsi2pk"] == "RESOURCE#gpt-4"
        assert ":gsi2sk" in call_kwargs["ExpressionAttributeValues"]

    def test_sets_ttl(self) -> None:
        """Verifies TTL is set in the future."""
        mock_table = MagicMock()
        delta = ConsumptionDelta(
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
        sk: str = "#BUCKET#gpt-4#tpm",
        entity_id: str = "entity-1",
        old_tokens: int = 10000,
        new_tokens: int = 5000,
        old_counter: int | None = 0,
        new_counter: int | None = 5000000,  # 5000 tokens consumed in millitokens
    ) -> dict:
        """Helper to create a stream record with flat format (v1.1.0+)."""
        record: dict = {
            "eventName": event_name,
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "tokens_milli": {"N": str(new_tokens)},
                    "last_refill_ms": {"N": "1704067200000"},
                },
                "OldImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "tokens_milli": {"N": str(old_tokens)},
                    "last_refill_ms": {"N": "1704067199000"},
                },
            },
        }
        # Add counter as FLAT top-level attribute (issue #179)
        if new_counter is not None:
            record["dynamodb"]["NewImage"]["total_consumed_milli"] = {"N": str(new_counter)}
        if old_counter is not None:
            record["dynamodb"]["OldImage"]["total_consumed_milli"] = {"N": str(old_counter)}
        return record

    def test_empty_records(self) -> None:
        """Empty records list returns zero counts."""
        with patch("zae_limiter.aggregator.processor.boto3"):
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

        with patch("zae_limiter.aggregator.processor.boto3"):
            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 2
        assert result.snapshots_updated == 0

    def test_processes_valid_modify_events(self) -> None:
        """Valid MODIFY events are processed."""
        records = [
            self._make_record(
                entity_id="e1",
                old_tokens=10000,
                new_tokens=5000,
                old_counter=0,
                new_counter=5000000,
            ),
            self._make_record(
                entity_id="e2",
                old_tokens=8000,
                new_tokens=3000,
                old_counter=0,
                new_counter=5000000,
            ),
        ]

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
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

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly", "daily"])

        assert result.snapshots_updated == 2  # 1 delta * 2 windows
        assert mock_table.update_item.call_count == 2

    def test_handles_extract_delta_exception(self) -> None:
        """Handles exceptions during extract_delta."""
        # Create a record that will cause an exception - counter value is not a number
        bad_record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "SK": {"S": "#BUCKET#res#limit"},
                    "entity_id": {"S": "entity"},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": "1000"},
                            "last_refill_ms": {"N": "1704067200000"},
                        }
                    },
                    "total_consumed_milli": {"N": "not_a_number"},  # invalid counter
                },
                "OldImage": {
                    "data": {"M": {"tokens_milli": {"N": "1000"}}},
                    "total_consumed_milli": {"N": "0"},
                },
            },
        }

        with patch("zae_limiter.aggregator.processor.boto3"):
            result = process_stream_records([bad_record], "test_table", ["hourly"])

        assert result.processed_count == 1
        assert len(result.errors) == 1
        assert "Error processing record" in result.errors[0]

    def test_handles_update_snapshot_exception(self) -> None:
        """Handles exceptions during update_snapshot."""
        records = [self._make_record()]

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_table.update_item.side_effect = Exception("DynamoDB error")
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 1
        assert len(result.errors) == 1
        assert "Error updating snapshot" in result.errors[0]

    def test_skips_zero_delta_records(self) -> None:
        """Records with zero delta are skipped."""
        records = [
            # Zero counter delta = no consumption change
            self._make_record(
                old_tokens=5000,
                new_tokens=5000,
                old_counter=1000000,
                new_counter=1000000,  # same counter = zero delta
            ),
        ]

        with patch("zae_limiter.aggregator.processor.boto3"):
            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 1
        assert result.snapshots_updated == 0

    def test_mixed_valid_and_invalid_records(self) -> None:
        """Processes valid records even when some are invalid."""
        records = [
            # Valid: has counter with positive delta
            self._make_record(
                entity_id="valid",
                old_tokens=10000,
                new_tokens=5000,
                old_counter=0,
                new_counter=5000000,
            ),
            # Invalid: non-bucket SK (will return None)
            self._make_record(sk="#LIMIT#res#name"),
            # Invalid: zero counter delta
            self._make_record(
                old_tokens=5000,
                new_tokens=5000,
                old_counter=1000000,
                new_counter=1000000,
            ),
        ]

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
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
        sk: str = "#BUCKET#gpt-4#tpm",
        entity_id: str = "entity-1",
        old_tokens: int = 10000,
        new_tokens: int = 5000,
        old_counter: int | None = 0,
        new_counter: int | None = 5000000,  # 5000 tokens consumed in millitokens
    ) -> dict:
        """Helper to create a stream record with flat format (v1.1.0+)."""
        record: dict = {
            "eventName": event_name,
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "tokens_milli": {"N": str(new_tokens)},
                    "last_refill_ms": {"N": "1704067200000"},
                },
                "OldImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "tokens_milli": {"N": str(old_tokens)},
                    "last_refill_ms": {"N": "1704067199000"},
                },
            },
        }
        # Add counter as FLAT top-level attribute (issue #179)
        if new_counter is not None:
            record["dynamodb"]["NewImage"]["total_consumed_milli"] = {"N": str(new_counter)}
        if old_counter is not None:
            record["dynamodb"]["OldImage"]["total_consumed_milli"] = {"N": str(old_counter)}
        return record

    def test_batch_processing_logs_start_and_end(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Logs batch start and completion with metrics."""
        records = [self._make_record()]

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
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

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
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

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            process_stream_records(records, "test_table", ["hourly"])

        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().split("\n") if line]
        logs = [json.loads(line) for line in lines]
        debug_logs = [log for log in logs if log["level"] == "DEBUG"]

        assert len(debug_logs) == 1
        debug_log = debug_logs[0]

        assert debug_log["message"] == "Snapshot updated"
        assert debug_log["entity_id"] == "entity-1"
        assert debug_log["resource"] == "gpt-4"
        assert debug_log["limit_name"] == "tpm"
        assert debug_log["window"] == "hourly"
        assert "window_key" in debug_log
        assert "tokens_delta" in debug_log
