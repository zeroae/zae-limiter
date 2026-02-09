"""Unit tests for the Lambda handler."""

import sys
from unittest.mock import MagicMock, patch

import pytest

# Import the module explicitly, not via __init__.py which exports the function
from zae_limiter_aggregator.handler import handler

# Get the actual module reference for patching
handler_module = sys.modules["zae_limiter_aggregator.handler"]


class TestHandler:
    """Tests for the Lambda handler function."""

    @pytest.fixture
    def mock_context(self) -> MagicMock:
        """Create a mock Lambda context."""
        context = MagicMock()
        context.aws_request_id = "test-request-123"
        context.function_name = "test-aggregator-function"
        return context

    def _make_bucket_record(
        self,
        entity_id: str = "user-1",
        resource: str = "gpt-4",
        limit_name: str = "tpm",
        old_counter: int = 0,
        new_counter: int = 1000000,
    ) -> dict:
        """Create a mock DynamoDB stream MODIFY record for a composite bucket.

        Uses flat schema (v0.6.0+, ADR-111) with b_{limit}_{field} attributes.
        """
        return {
            "eventName": "MODIFY",
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": f"#BUCKET#{resource}"},
                    "entity_id": {"S": entity_id},
                    "rf": {"N": "1705329000000"},
                    f"b_{limit_name}_tc": {"N": str(old_counter)},
                    f"b_{limit_name}_tk": {"N": "10000000"},
                    f"b_{limit_name}_cp": {"N": "10000000"},
                    f"b_{limit_name}_bx": {"N": "10000000"},
                    f"b_{limit_name}_ra": {"N": "10000000"},
                    f"b_{limit_name}_rp": {"N": "60000"},
                },
                "NewImage": {
                    "PK": {"S": f"ENTITY#{entity_id}"},
                    "SK": {"S": f"#BUCKET#{resource}"},
                    "entity_id": {"S": entity_id},
                    "rf": {"N": "1705329000000"},
                    f"b_{limit_name}_tc": {"N": str(new_counter)},
                    f"b_{limit_name}_tk": {"N": "9000000"},
                    f"b_{limit_name}_cp": {"N": "10000000"},
                    f"b_{limit_name}_bx": {"N": "10000000"},
                    f"b_{limit_name}_ra": {"N": "10000000"},
                    f"b_{limit_name}_rp": {"N": "60000"},
                },
            },
        }

    def _make_audit_record(
        self,
        entity_id: str = "user-1",
        event_id: str = "01HPQRS",
        action: str = "entity_created",
        timestamp: str = "2024-01-15T14:30:00Z",
    ) -> dict:
        """Create a mock DynamoDB stream REMOVE record for audit.

        Uses flat schema (v0.6.0+, ADR-111).
        """
        return {
            "eventName": "REMOVE",
            "dynamodb": {
                "OldImage": {
                    "PK": {"S": f"AUDIT#{entity_id}"},
                    "SK": {"S": f"#AUDIT#{event_id}"},
                    "entity_id": {"S": entity_id},
                    "event_id": {"S": event_id},
                    "action": {"S": action},
                    "timestamp": {"S": timestamp},
                    "principal": {"S": "arn:aws:iam::123456789012:user/test"},
                    "details": {"M": {}},
                },
            },
        }

    @patch("zae_limiter_aggregator.handler.process_stream_records")
    def test_handler_processes_records(
        self,
        mock_process: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Handler processes stream records and returns result."""
        mock_process.return_value = MagicMock(
            processed_count=1,
            snapshots_updated=1,
            refills_written=0,
            errors=[],
        )

        event = {"Records": [self._make_bucket_record()]}
        result = handler(event, mock_context)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 1
        assert result["body"]["snapshots_updated"] == 1
        assert result["body"]["refills_written"] == 0
        assert result["body"]["events_archived"] == 0
        assert result["body"]["errors"] == []

        mock_process.assert_called_once()

    @patch.object(handler_module, "ENABLE_ARCHIVAL", True)
    @patch.object(handler_module, "ARCHIVE_BUCKET_NAME", "test-archive-bucket")
    @patch("zae_limiter_aggregator.handler.boto3")
    @patch("zae_limiter_aggregator.handler.archive_audit_events")
    @patch("zae_limiter_aggregator.handler.process_stream_records")
    def test_handler_with_archival_enabled(
        self,
        mock_process: MagicMock,
        mock_archive: MagicMock,
        mock_boto3: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Handler archives audit events when enabled."""
        mock_process.return_value = MagicMock(
            processed_count=2,
            snapshots_updated=1,
            refills_written=0,
            errors=[],
        )
        mock_archive.return_value = MagicMock(
            events_archived=1,
            s3_objects_created=1,
            errors=[],
        )
        mock_s3_client = MagicMock()
        mock_boto3.client.return_value = mock_s3_client

        records = [
            self._make_bucket_record(),
            self._make_audit_record(),
        ]
        event = {"Records": records}
        result = handler(event, mock_context)

        assert result["statusCode"] == 200
        assert result["body"]["processed"] == 2
        assert result["body"]["snapshots_updated"] == 1
        assert result["body"]["events_archived"] == 1
        assert result["body"]["errors"] == []

        mock_boto3.client.assert_called_once_with("s3")
        mock_archive.assert_called_once_with(
            records=records,
            bucket_name="test-archive-bucket",
            s3_client=mock_s3_client,
            request_id="test-request-123",
        )

    @patch.object(handler_module, "ENABLE_ARCHIVAL", True)
    @patch.object(handler_module, "ARCHIVE_BUCKET_NAME", "test-bucket")
    @patch("zae_limiter_aggregator.handler.boto3")
    @patch("zae_limiter_aggregator.handler.archive_audit_events")
    @patch("zae_limiter_aggregator.handler.process_stream_records")
    def test_handler_aggregates_errors(
        self,
        mock_process: MagicMock,
        mock_archive: MagicMock,
        mock_boto3: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Handler aggregates errors from both processors."""
        mock_process.return_value = MagicMock(
            processed_count=2,
            snapshots_updated=0,
            refills_written=0,
            errors=["snapshot error 1"],
        )
        mock_archive.return_value = MagicMock(
            events_archived=0,
            s3_objects_created=0,
            errors=["archive error 1"],
        )
        _ = mock_boto3  # Needed by the patch but not directly used

        event = {"Records": [self._make_bucket_record()]}
        result = handler(event, mock_context)

        assert result["statusCode"] == 200
        assert result["body"]["errors"] == ["snapshot error 1", "archive error 1"]

    @patch.object(handler_module, "ENABLE_ARCHIVAL", False)
    @patch.object(handler_module, "ARCHIVE_BUCKET_NAME", "")
    @patch("zae_limiter_aggregator.handler.archive_audit_events")
    @patch("zae_limiter_aggregator.handler.process_stream_records")
    def test_handler_skips_archival_when_disabled(
        self,
        mock_process: MagicMock,
        mock_archive: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Handler skips archival when ENABLE_ARCHIVAL is false."""
        mock_process.return_value = MagicMock(
            processed_count=1,
            snapshots_updated=1,
            refills_written=0,
            errors=[],
        )

        event = {"Records": [self._make_bucket_record()]}
        handler(event, mock_context)

        mock_archive.assert_not_called()

    @patch.object(handler_module, "ENABLE_ARCHIVAL", True)
    @patch.object(handler_module, "ARCHIVE_BUCKET_NAME", "")  # Empty bucket name
    @patch("zae_limiter_aggregator.handler.archive_audit_events")
    @patch("zae_limiter_aggregator.handler.process_stream_records")
    def test_handler_skips_archival_when_no_bucket(
        self,
        mock_process: MagicMock,
        mock_archive: MagicMock,
        mock_context: MagicMock,
    ) -> None:
        """Handler skips archival when bucket name is empty."""
        mock_process.return_value = MagicMock(
            processed_count=1,
            snapshots_updated=1,
            refills_written=0,
            errors=[],
        )

        event = {"Records": [self._make_bucket_record()]}
        handler(event, mock_context)

        mock_archive.assert_not_called()
