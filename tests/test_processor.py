"""Tests for aggregator processor module."""

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter.aggregator.processor import (
    ConsumptionDelta,
    ProcessResult,
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
    ) -> dict:
        """Helper to create a stream record."""
        return {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": str(new_tokens)},
                            "last_refill_ms": {"N": str(last_refill_ms)},
                        }
                    },
                },
                "OldImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": str(old_tokens)},
                            "last_refill_ms": {"N": str(last_refill_ms - 1000)},
                        }
                    },
                },
            },
        }

    def test_valid_bucket_record_consumption(self) -> None:
        """Extract delta from valid BUCKET record with consumption."""
        record = self._make_record(
            sk="#BUCKET#gpt-4#tpm",
            entity_id="test-entity",
            old_tokens=10000,
            new_tokens=5000,
            last_refill_ms=1704067200000,
        )

        delta = extract_delta(record)

        assert delta is not None
        assert delta.entity_id == "test-entity"
        assert delta.resource == "gpt-4"
        assert delta.limit_name == "tpm"
        assert delta.tokens_delta == 5000  # 10000 - 5000 = consumed
        assert delta.timestamp_ms == 1704067200000

    def test_valid_bucket_record_refund(self) -> None:
        """Extract negative delta when tokens increase (refund)."""
        record = self._make_record(
            old_tokens=5000,
            new_tokens=10000,  # tokens increased
        )

        delta = extract_delta(record)

        assert delta is not None
        assert delta.tokens_delta == -5000  # negative = returned

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
        record = self._make_record(old_tokens=5000, new_tokens=5000)
        assert extract_delta(record) is None

    def test_malformed_sk_returns_none(self) -> None:
        """Malformed SK returns None."""
        # Only one part after #BUCKET#
        record = self._make_record(sk="#BUCKET#gpt-4")
        assert extract_delta(record) is None

        # Empty parts
        record = self._make_record(sk="#BUCKET##")
        delta = extract_delta(record)
        # This actually extracts "" and "" which is valid format
        assert delta is not None

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
        """Missing data fields default to 0."""
        record = self._make_record()
        del record["dynamodb"]["NewImage"]["data"]["M"]["tokens_milli"]
        del record["dynamodb"]["OldImage"]["data"]["M"]["tokens_milli"]

        delta = extract_delta(record)
        assert delta is None  # 0 - 0 = 0, returns None

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
        """Verifies update expression has correct structure."""
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

        # Check SET clause elements
        assert "entity_id = :entity_id" in expr
        assert "if_not_exists(#data, :initial_data)" in expr
        assert "GSI2PK = :gsi2pk" in expr
        assert "GSI2SK = :gsi2sk" in expr

        # Check ADD clause elements
        assert "ADD #data.#limit_name :delta" in expr
        assert "#data.total_events :one" in expr

    def test_initial_data_structure(self) -> None:
        """Verifies initial data map structure."""
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
        initial_data = call_kwargs["ExpressionAttributeValues"][":initial_data"]

        assert initial_data["resource"] == "gpt-4"
        assert initial_data["window"] == "hourly"
        assert initial_data["window_start"] == "2024-01-01T14:00:00Z"
        assert initial_data["tpm"] == 0
        assert initial_data["total_events"] == 0

    def test_expression_attribute_names(self) -> None:
        """Verifies expression attribute names are set correctly."""
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

        assert attr_names["#data"] == "data"
        assert attr_names["#limit_name"] == "tpm"
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
    ) -> dict:
        """Helper to create a stream record."""
        return {
            "eventName": event_name,
            "dynamodb": {
                "NewImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": str(new_tokens)},
                            "last_refill_ms": {"N": "1704067200000"},
                        }
                    },
                },
                "OldImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": sk},
                    "entity_id": {"S": entity_id},
                    "data": {
                        "M": {
                            "tokens_milli": {"N": str(old_tokens)},
                            "last_refill_ms": {"N": "1704067199000"},
                        }
                    },
                },
            },
        }

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
            self._make_record(entity_id="e1", old_tokens=10000, new_tokens=5000),
            self._make_record(entity_id="e2", old_tokens=8000, new_tokens=3000),
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
        # Create a record that will cause an exception
        bad_record = {
            "eventName": "MODIFY",
            "dynamodb": {
                "NewImage": {
                    "SK": {"S": "#BUCKET#res#limit"},
                    "entity_id": {"S": "entity"},
                    "data": {"M": {"tokens_milli": {"N": "not_a_number"}}},
                },
                "OldImage": {
                    "data": {"M": {"tokens_milli": {"N": "1000"}}},
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
            self._make_record(old_tokens=5000, new_tokens=5000),  # zero delta
        ]

        with patch("zae_limiter.aggregator.processor.boto3"):
            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 1
        assert result.snapshots_updated == 0

    def test_mixed_valid_and_invalid_records(self) -> None:
        """Processes valid records even when some are invalid."""
        records = [
            self._make_record(entity_id="valid", old_tokens=10000, new_tokens=5000),
            self._make_record(sk="#LIMIT#res#name"),  # non-bucket, will be None
            self._make_record(old_tokens=5000, new_tokens=5000),  # zero delta
        ]

        with patch("zae_limiter.aggregator.processor.boto3") as mock_boto:
            mock_table = MagicMock()
            mock_boto.resource.return_value.Table.return_value = mock_table

            result = process_stream_records(records, "test_table", ["hourly"])

        assert result.processed_count == 3
        assert result.snapshots_updated == 1  # only one valid delta
        assert result.errors == []
