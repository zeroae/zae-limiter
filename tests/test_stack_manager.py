"""Tests for CloudFormation stack manager."""

from unittest.mock import AsyncMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter.infra.stack_manager import StackManager


class TestStackManager:
    """Test StackManager functionality."""

    def test_stack_name_generation(self) -> None:
        """Test stack name generation from table name."""
        manager = StackManager(table_name="my_table", region="us-east-1")
        assert manager.get_stack_name() == "zae-limiter-my_table"
        assert manager.get_stack_name("other_table") == "zae-limiter-other_table"

    def test_cloudformation_used_with_endpoint_url(self) -> None:
        """Test that CloudFormation is used even with endpoint_url (LocalStack)."""
        manager = StackManager(
            table_name="test",
            region="us-east-1",
            endpoint_url="http://localhost:4566",
        )
        assert manager._should_use_cloudformation()

    def test_cloudformation_use_without_endpoint(self) -> None:
        """Test that CloudFormation is used when no endpoint_url is set."""
        manager = StackManager(table_name="test", region="us-east-1")
        assert manager._should_use_cloudformation()

    def test_format_parameters_defaults(self) -> None:
        """Test parameter formatting with defaults."""
        manager = StackManager(table_name="rate_limits", region="us-east-1")
        params = manager._format_parameters(None)

        # Should include TableName and SchemaVersion
        assert len(params) == 2
        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert param_dict["TableName"] == "rate_limits"
        assert "SchemaVersion" in param_dict

    def test_format_parameters_with_values(self) -> None:
        """Test parameter formatting with custom values."""
        manager = StackManager(table_name="rate_limits", region="us-east-1")
        params = manager._format_parameters(
            {
                "snapshot_windows": "hourly,daily,monthly",
                "retention_days": "180",
                "enable_aggregator": "false",
            }
        )

        # Should include TableName, SchemaVersion, plus custom params
        assert len(params) == 5

        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert param_dict["TableName"] == "rate_limits"
        assert "SchemaVersion" in param_dict
        assert param_dict["SnapshotWindows"] == "hourly,daily,monthly"
        assert param_dict["SnapshotRetentionDays"] == "180"
        assert param_dict["EnableAggregator"] == "false"

    def test_load_template(self) -> None:
        """Test loading CloudFormation template."""
        manager = StackManager(table_name="test", region="us-east-1")
        template = manager._load_template()

        assert template is not None
        assert "AWSTemplateFormatVersion" in template
        assert "AWS::DynamoDB::Table" in template
        assert "RateLimitsTable" in template

    @pytest.mark.skip(reason="Requires real AWS CloudFormation API - moto doesn't support async")
    @pytest.mark.asyncio
    async def test_stack_exists_false(self) -> None:
        """Test stack_exists returns False for non-existent stack."""
        manager = StackManager(table_name="test", region="us-east-1")

        exists = await manager.stack_exists("non-existent-stack")
        assert not exists

    @pytest.mark.skip(reason="Requires real AWS CloudFormation API - moto doesn't support async")
    @pytest.mark.asyncio
    async def test_get_stack_status_none(self) -> None:
        """Test get_stack_status returns None for non-existent stack."""
        manager = StackManager(table_name="test", region="us-east-1")

        status = await manager.get_stack_status("non-existent-stack")
        assert status is None

    @pytest.mark.skip(reason="Requires real AWS CloudFormation API - moto doesn't support async")
    @pytest.mark.asyncio
    async def test_delete_stack_nonexistent(self) -> None:
        """Test deleting a non-existent stack doesn't raise error."""
        manager = StackManager(table_name="test", region="us-east-1")

        # Should not raise
        await manager.delete_stack("non-existent-stack")

    @pytest.mark.asyncio
    async def test_create_stack_with_parameters(self) -> None:
        """Test create_stack parameter handling with CloudFormation."""
        from unittest.mock import MagicMock

        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            # Use MagicMock for client, with explicit async methods
            mock_client = MagicMock()
            # Simulate stack doesn't exist
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
            )
            mock_client.create_stack = AsyncMock(return_value={"StackId": "test-stack-id"})

            # Mock the waiter correctly - get_waiter is sync, wait is async
            mock_waiter = MagicMock()
            mock_waiter.wait = AsyncMock()
            mock_client.get_waiter.return_value = mock_waiter

            mock_get_client.return_value = mock_client

            manager = StackManager(
                table_name="test",
                region="us-east-1",
                endpoint_url="http://localhost:4566",
            )

            result = await manager.create_stack(
                parameters={
                    "snapshot_windows": "hourly",
                    "retention_days": "30",
                }
            )

            assert result["status"] == "CREATE_COMPLETE"
            assert result["stack_id"] == "test-stack-id"
            mock_client.create_stack.assert_called_once()


class TestStackManagerIntegration:
    """Integration tests for stack manager (require real AWS)."""

    @pytest.mark.skip(reason="Requires real AWS credentials and creates resources")
    @pytest.mark.asyncio
    async def test_create_and_delete_stack_real_aws(self) -> None:
        """Test creating and deleting a real CloudFormation stack."""
        manager = StackManager(table_name="test_rate_limits", region="us-east-1")
        stack_name = manager.get_stack_name()

        try:
            # Create stack
            result = await manager.create_stack(wait=True)
            assert result["status"] == "CREATE_COMPLETE"
            assert result["stack_id"] is not None

            # Verify stack exists
            exists = await manager.stack_exists(stack_name)
            assert exists

            # Get status
            status = await manager.get_stack_status(stack_name)
            assert status == "CREATE_COMPLETE"

        finally:
            # Clean up
            await manager.delete_stack(stack_name, wait=True)

            # Verify deleted
            exists = await manager.stack_exists(stack_name)
            assert not exists
