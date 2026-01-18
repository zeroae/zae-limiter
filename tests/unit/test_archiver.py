"""Unit tests for the S3 audit archiver."""

import gzip
import json
from unittest.mock import MagicMock, patch

from zae_limiter.aggregator.archiver import (
    _deserialize_map,
    _deserialize_value,
    archive_audit_events,
    create_jsonl_gzip,
    extract_audit_event,
    get_object_key,
    get_partition_key,
)


class TestExtractAuditEvent:
    """Tests for extract_audit_event function."""

    def test_extract_valid_remove_event(self) -> None:
        """Extract audit event from valid REMOVE record."""
        record = self._make_audit_record(
            event_name="REMOVE",
            entity_id="user-123",
            event_id="01HQJK123456789",
            action="entity_created",
            timestamp="2024-01-15T14:30:00Z",
            principal="admin@example.com",
        )

        result = extract_audit_event(record)

        assert result is not None
        assert result["event_id"] == "01HQJK123456789"
        assert result["entity_id"] == "user-123"
        assert result["action"] == "entity_created"
        assert result["timestamp"] == "2024-01-15T14:30:00Z"
        assert result["principal"] == "admin@example.com"

    def test_skip_modify_event(self) -> None:
        """Skip MODIFY events (not TTL deletions)."""
        record = self._make_audit_record(event_name="MODIFY")

        result = extract_audit_event(record)

        assert result is None

    def test_skip_insert_event(self) -> None:
        """Skip INSERT events."""
        record = self._make_audit_record(event_name="INSERT")

        result = extract_audit_event(record)

        assert result is None

    def test_skip_non_audit_record(self) -> None:
        """Skip records that don't have AUDIT# prefix."""
        record = {
            "eventName": "REMOVE",
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": "ENTITY#user-123"},
                    "SK": {"S": "#META"},
                    "data": {"M": {}},
                }
            },
        }

        result = extract_audit_event(record)

        assert result is None

    def test_handle_missing_data_map(self) -> None:
        """Handle records with missing data map."""
        record = {
            "eventName": "REMOVE",
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": "AUDIT#user-123"},
                    "SK": {"S": "#AUDIT#01HQJK123456789"},
                    # Missing data.M
                }
            },
        }

        result = extract_audit_event(record)

        assert result is None

    def test_extract_with_nested_details(self) -> None:
        """Extract event with nested details map."""
        record = self._make_audit_record(
            event_name="REMOVE",
            entity_id="user-123",
            event_id="01HQJK123456789",
            action="limits_set",
            details={
                "M": {
                    "resource": {"S": "gpt-4"},
                    "limits": {
                        "L": [
                            {
                                "M": {
                                    "name": {"S": "rpm"},
                                    "capacity": {"N": "100"},
                                }
                            }
                        ]
                    },
                }
            },
        )

        result = extract_audit_event(record)

        assert result is not None
        assert result["details"]["resource"] == "gpt-4"
        assert len(result["details"]["limits"]) == 1
        assert result["details"]["limits"][0]["name"] == "rpm"
        assert result["details"]["limits"][0]["capacity"] == 100

    def test_extract_with_null_values(self) -> None:
        """Extract event with NULL values."""
        record = self._make_audit_record(
            event_name="REMOVE",
            entity_id="user-123",
            event_id="01HQJK123456789",
            action="entity_created",
            principal=None,
            resource=None,
        )

        result = extract_audit_event(record)

        assert result is not None
        assert result["principal"] is None
        assert result["resource"] is None

    def _make_audit_record(
        self,
        event_name: str = "REMOVE",
        entity_id: str = "user-123",
        event_id: str = "01HQJK123456789",
        action: str = "entity_created",
        timestamp: str = "2024-01-15T14:30:00Z",
        principal: str | None = "admin@example.com",
        resource: str | None = None,
        details: dict | None = None,
    ) -> dict:
        """Helper to create audit stream records."""
        data_map: dict = {
            "event_id": {"S": event_id},
            "entity_id": {"S": entity_id},
            "action": {"S": action},
            "timestamp": {"S": timestamp},
        }

        if principal is not None:
            data_map["principal"] = {"S": principal}
        else:
            data_map["principal"] = {"NULL": True}

        if resource is not None:
            data_map["resource"] = {"S": resource}
        else:
            data_map["resource"] = {"NULL": True}

        if details is not None:
            data_map["details"] = details
        else:
            data_map["details"] = {"M": {}}

        return {
            "eventName": event_name,
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": f"AUDIT#{entity_id}"},
                    "SK": {"S": f"#AUDIT#{event_id}"},
                    "entity_id": {"S": entity_id},
                    "data": {"M": data_map},
                    "ttl": {"N": "1705330200"},
                }
            },
        }


class TestDeserializeMap:
    """Tests for DynamoDB deserialization functions."""

    def test_deserialize_string(self) -> None:
        """Deserialize string attribute."""
        result = _deserialize_value({"S": "hello"})
        assert result == "hello"

    def test_deserialize_number_int(self) -> None:
        """Deserialize integer number."""
        result = _deserialize_value({"N": "42"})
        assert result == 42
        assert isinstance(result, int)

    def test_deserialize_number_float(self) -> None:
        """Deserialize float number."""
        result = _deserialize_value({"N": "3.14"})
        assert result == 3.14
        assert isinstance(result, float)

    def test_deserialize_bool_true(self) -> None:
        """Deserialize boolean true."""
        result = _deserialize_value({"BOOL": True})
        assert result is True

    def test_deserialize_bool_false(self) -> None:
        """Deserialize boolean false."""
        result = _deserialize_value({"BOOL": False})
        assert result is False

    def test_deserialize_null(self) -> None:
        """Deserialize NULL value."""
        result = _deserialize_value({"NULL": True})
        assert result is None

    def test_deserialize_nested_map(self) -> None:
        """Deserialize nested map."""
        ddb_map = {
            "name": {"S": "test"},
            "count": {"N": "5"},
            "nested": {"M": {"key": {"S": "value"}}},
        }
        result = _deserialize_map(ddb_map)
        assert result == {"name": "test", "count": 5, "nested": {"key": "value"}}

    def test_deserialize_list(self) -> None:
        """Deserialize list attribute."""
        result = _deserialize_value({"L": [{"S": "a"}, {"S": "b"}, {"N": "1"}]})
        assert result == ["a", "b", 1]

    def test_deserialize_unknown_type(self) -> None:
        """Handle unknown DynamoDB type."""
        result = _deserialize_value({"UNKNOWN": "value"})
        assert result == {"UNKNOWN": "value"}


class TestCreateJsonlGzip:
    """Tests for JSONL gzip creation."""

    def test_create_single_event(self) -> None:
        """Create JSONL from single event."""
        events = [{"id": "1", "action": "test"}]

        result = create_jsonl_gzip(events)

        # Decompress and verify
        decompressed = gzip.decompress(result).decode("utf-8")
        lines = decompressed.strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"id": "1", "action": "test"}

    def test_create_multiple_events(self) -> None:
        """Create JSONL from multiple events."""
        events = [
            {"id": "1", "action": "create"},
            {"id": "2", "action": "delete"},
            {"id": "3", "action": "update"},
        ]

        result = create_jsonl_gzip(events)

        decompressed = gzip.decompress(result).decode("utf-8")
        lines = decompressed.strip().split("\n")
        assert len(lines) == 3
        assert json.loads(lines[0])["id"] == "1"
        assert json.loads(lines[1])["id"] == "2"
        assert json.loads(lines[2])["id"] == "3"

    def test_creates_valid_gzip(self) -> None:
        """Verify output is valid gzip."""
        events = [{"test": "data"}]

        result = create_jsonl_gzip(events)

        # Should not raise
        gzip.decompress(result)

    def test_compact_json_format(self) -> None:
        """Verify JSON uses compact format (no extra whitespace)."""
        events = [{"key": "value", "number": 42}]

        result = create_jsonl_gzip(events)

        decompressed = gzip.decompress(result).decode("utf-8")
        # Should not have spaces after : or ,
        assert ": " not in decompressed
        assert ", " not in decompressed


class TestGetPartitionKey:
    """Tests for S3 partition key generation."""

    def test_partition_key_from_timestamp(self) -> None:
        """Generate partition key from ISO timestamp."""
        result = get_partition_key("2024-01-15T14:30:00Z")
        assert result == "audit/year=2024/month=01/day=15"

    def test_partition_key_with_timezone(self) -> None:
        """Handle timestamp with timezone offset."""
        result = get_partition_key("2024-12-25T23:59:59+00:00")
        assert result == "audit/year=2024/month=12/day=25"

    def test_partition_key_single_digit_month(self) -> None:
        """Pad single-digit month with zero."""
        result = get_partition_key("2024-03-05T10:00:00Z")
        assert result == "audit/year=2024/month=03/day=05"

    def test_partition_key_invalid_timestamp_fallback(self) -> None:
        """Fall back to current time for invalid timestamp."""
        result = get_partition_key("not-a-timestamp")
        # Should still return valid format
        assert result.startswith("audit/year=")
        assert "/month=" in result
        assert "/day=" in result

    def test_partition_key_empty_timestamp(self) -> None:
        """Handle empty timestamp."""
        result = get_partition_key("")
        assert result.startswith("audit/year=")


class TestGetObjectKey:
    """Tests for S3 object key generation."""

    def test_object_key_format(self) -> None:
        """Generate complete object key."""
        result = get_object_key(
            partition="audit/year=2024/month=01/day=15",
            request_id="abc123",
            timestamp="2024-01-15T14:30:00Z",
        )
        expected = "audit/year=2024/month=01/day=15/audit-abc123-2024-01-15T14-30-00Z.jsonl.gz"
        assert result == expected

    def test_object_key_sanitizes_colons(self) -> None:
        """Sanitize colons in timestamp for filename."""
        result = get_object_key(
            partition="audit/year=2024/month=01/day=15",
            request_id="req",
            timestamp="2024-01-15T14:30:45Z",
        )
        assert ":" not in result

    def test_object_key_sanitizes_plus(self) -> None:
        """Sanitize plus sign in timestamp for filename."""
        result = get_object_key(
            partition="audit/year=2024/month=01/day=15",
            request_id="req",
            timestamp="2024-01-15T14:30:45+00:00",
        )
        assert "+" not in result


class TestArchiveAuditEvents:
    """Tests for the main archive_audit_events function."""

    def test_archive_single_event(self) -> None:
        """Archive a single audit event."""
        records = [
            self._make_audit_record(
                entity_id="user-1",
                event_id="evt-1",
                action="entity_created",
            )
        ]
        s3_client = MagicMock()

        result = archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 1
        assert result.events_archived == 1
        assert result.s3_objects_created == 1
        assert len(result.errors) == 0
        s3_client.put_object.assert_called_once()

    def test_archive_multiple_events(self) -> None:
        """Archive multiple audit events in one batch."""
        records = [
            self._make_audit_record(entity_id="user-1", event_id="evt-1"),
            self._make_audit_record(entity_id="user-2", event_id="evt-2"),
            self._make_audit_record(entity_id="user-3", event_id="evt-3"),
        ]
        s3_client = MagicMock()

        result = archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 3
        assert result.events_archived == 3
        assert result.s3_objects_created == 1  # Single batch file
        assert len(result.errors) == 0

    def test_skip_non_audit_records(self) -> None:
        """Skip records that aren't audit events."""
        records = [
            self._make_audit_record(entity_id="user-1", event_id="evt-1"),
            {
                "eventName": "MODIFY",
                "dynamodb": {
                    "NewImage": {"PK": {"S": "ENTITY#user-1"}},
                },
            },
            {
                "eventName": "REMOVE",
                "dynamodb": {
                    "OldImage": {"PK": {"S": "ENTITY#user-2"}},
                },
            },
        ]
        s3_client = MagicMock()

        result = archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 3
        assert result.events_archived == 1  # Only the audit event
        assert result.s3_objects_created == 1

    def test_no_events_to_archive(self) -> None:
        """Handle batch with no audit events."""
        records = [
            {
                "eventName": "MODIFY",
                "dynamodb": {"NewImage": {"PK": {"S": "ENTITY#user-1"}}},
            }
        ]
        s3_client = MagicMock()

        result = archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 1
        assert result.events_archived == 0
        assert result.s3_objects_created == 0
        assert len(result.errors) == 0
        s3_client.put_object.assert_not_called()

    def test_empty_records_list(self) -> None:
        """Handle empty records list."""
        s3_client = MagicMock()

        result = archive_audit_events(
            records=[],
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 0
        assert result.events_archived == 0
        assert result.s3_objects_created == 0
        s3_client.put_object.assert_not_called()

    def test_s3_write_failure(self) -> None:
        """Handle S3 write failure gracefully."""
        records = [self._make_audit_record()]
        s3_client = MagicMock()
        s3_client.put_object.side_effect = Exception("S3 error")

        result = archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 1
        assert result.events_archived == 1  # Extracted but failed to write
        assert result.s3_objects_created == 0
        assert len(result.errors) == 1
        assert "S3 error" in result.errors[0]

    def test_extraction_failure_continues(self) -> None:
        """Continue processing after extraction failure."""
        records = [
            self._make_audit_record(entity_id="user-1", event_id="evt-1"),
            {"eventName": "REMOVE", "dynamodb": "invalid"},  # Will cause exception
            self._make_audit_record(entity_id="user-2", event_id="evt-2"),
        ]
        s3_client = MagicMock()

        result = archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        assert result.processed_count == 3
        assert result.events_archived == 2  # Two valid events
        assert result.s3_objects_created == 1
        assert len(result.errors) == 1  # One extraction error

    def test_s3_object_content(self) -> None:
        """Verify S3 object content is correct JSONL."""
        records = [
            self._make_audit_record(
                entity_id="user-1",
                event_id="evt-1",
                action="entity_created",
                timestamp="2024-01-15T14:30:00Z",
            )
        ]
        s3_client = MagicMock()
        captured_body = None

        def capture_put(*, Body, **kwargs):  # noqa: N803
            nonlocal captured_body
            captured_body = Body

        s3_client.put_object.side_effect = capture_put

        archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        # Decompress and verify content
        assert captured_body is not None
        decompressed = gzip.decompress(captured_body).decode("utf-8")
        event = json.loads(decompressed.strip())
        assert event["event_id"] == "evt-1"
        assert event["entity_id"] == "user-1"
        assert event["action"] == "entity_created"

    def test_s3_object_key_uses_partition(self) -> None:
        """Verify S3 object uses date partition."""
        records = [self._make_audit_record(timestamp="2024-03-20T10:00:00Z")]
        s3_client = MagicMock()

        archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        call_kwargs = s3_client.put_object.call_args.kwargs
        assert "year=2024" in call_kwargs["Key"]
        assert "month=03" in call_kwargs["Key"]
        assert "day=20" in call_kwargs["Key"]

    def test_s3_content_type_and_encoding(self) -> None:
        """Verify correct content type and encoding headers."""
        records = [self._make_audit_record()]
        s3_client = MagicMock()

        archive_audit_events(
            records=records,
            bucket_name="test-bucket",
            s3_client=s3_client,
            request_id="req-123",
        )

        call_kwargs = s3_client.put_object.call_args.kwargs
        assert call_kwargs["ContentType"] == "application/x-ndjson"
        assert call_kwargs["ContentEncoding"] == "gzip"

    def test_archive_handles_jsonl_creation_error(self) -> None:
        """Archive returns error when JSONL creation fails."""
        records = [self._make_audit_record()]
        s3_client = MagicMock()

        # Mock create_jsonl_gzip to raise an exception
        with patch(
            "zae_limiter.aggregator.archiver.create_jsonl_gzip",
            side_effect=ValueError("Serialization failed"),
        ):
            result = archive_audit_events(
                records=records,
                bucket_name="test-bucket",
                s3_client=s3_client,
                request_id="req-123",
            )

        # Should return error result, not raise
        assert result.events_archived == 0
        assert result.s3_objects_created == 0
        assert len(result.errors) == 1
        assert "Error creating JSONL" in result.errors[0]
        assert "Serialization failed" in result.errors[0]

        # S3 should not have been called
        s3_client.put_object.assert_not_called()

    def _make_audit_record(
        self,
        entity_id: str = "user-123",
        event_id: str = "01HQJK123456789",
        action: str = "entity_created",
        timestamp: str = "2024-01-15T14:30:00Z",
        principal: str = "admin@example.com",
    ) -> dict:
        """Helper to create audit stream records."""
        return {
            "eventName": "REMOVE",
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": f"AUDIT#{entity_id}"},
                    "SK": {"S": f"#AUDIT#{event_id}"},
                    "entity_id": {"S": entity_id},
                    "data": {
                        "M": {
                            "event_id": {"S": event_id},
                            "entity_id": {"S": entity_id},
                            "action": {"S": action},
                            "timestamp": {"S": timestamp},
                            "principal": {"S": principal},
                            "resource": {"NULL": True},
                            "details": {"M": {}},
                        }
                    },
                    "ttl": {"N": "1705330200"},
                }
            },
        }
