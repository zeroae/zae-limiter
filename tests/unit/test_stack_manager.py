"""Tests for CloudFormation stack manager."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from zae_limiter import StackOptions
from zae_limiter.exceptions import StackAlreadyExistsError, StackCreationError
from zae_limiter.infra.stack_manager import StackManager


class TestStackManager:
    """Test StackManager functionality."""

    def test_stack_name_normalization(self) -> None:
        """Test stack name validation (no prefix added)."""
        manager = StackManager(stack_name="my-table", region="us-east-1")
        assert manager.stack_name == "my-table"
        assert manager.table_name == "my-table"

    def test_stack_name_already_prefixed(self) -> None:
        """Test stack name when already prefixed (returned as-is)."""
        manager = StackManager(stack_name="ZAEL-my-table", region="us-east-1")
        assert manager.stack_name == "ZAEL-my-table"
        assert manager.table_name == "ZAEL-my-table"

    def test_format_parameters_defaults(self) -> None:
        """Test parameter formatting with defaults."""
        manager = StackManager(stack_name="rate-limits", region="us-east-1")
        params = manager._format_parameters(None)

        # Should include only SchemaVersion (TableName derived from AWS::StackName)
        assert len(params) == 1
        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert "SchemaVersion" in param_dict
        assert "TableName" not in param_dict

    def test_format_parameters_with_values(self) -> None:
        """Test parameter formatting with custom values."""
        manager = StackManager(stack_name="rate-limits", region="us-east-1")
        params = manager._format_parameters(
            {
                "snapshot_windows": "hourly,daily,monthly",
                "retention_days": "180",
                "enable_aggregator": "false",
            }
        )

        # Should include SchemaVersion plus custom params (TableName derived from AWS::StackName)
        assert len(params) == 4

        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert "TableName" not in param_dict
        assert "SchemaVersion" in param_dict
        assert param_dict["SnapshotWindows"] == "hourly,daily,monthly"
        assert param_dict["SnapshotRetentionDays"] == "180"
        assert param_dict["EnableAggregator"] == "false"

    def test_format_parameters_with_pitr_recovery_days(self) -> None:
        """Test parameter formatting with PITR recovery period."""
        manager = StackManager(stack_name="rate-limits", region="us-east-1")
        params = manager._format_parameters(
            {
                "pitr_recovery_days": "7",
            }
        )

        # Should include SchemaVersion plus PITR parameter (TableName derived from AWS::StackName)
        assert len(params) == 2

        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert "TableName" not in param_dict
        assert "SchemaVersion" in param_dict
        assert param_dict["PITRRecoveryPeriodDays"] == "7"

    def test_format_parameters_pitr_edge_cases(self) -> None:
        """Test PITR parameter with edge case values."""
        manager = StackManager(stack_name="rate-limits", region="us-east-1")

        # Test minimum value (1 day)
        params_min = manager._format_parameters({"pitr_recovery_days": "1"})
        param_dict_min = {p["ParameterKey"]: p["ParameterValue"] for p in params_min}
        assert param_dict_min["PITRRecoveryPeriodDays"] == "1"

        # Test maximum value (35 days)
        params_max = manager._format_parameters({"pitr_recovery_days": "35"})
        param_dict_max = {p["ParameterKey"]: p["ParameterValue"] for p in params_max}
        assert param_dict_max["PITRRecoveryPeriodDays"] == "35"

    def test_format_parameters_with_log_retention_days(self) -> None:
        """Test parameter formatting with log retention days."""
        manager = StackManager(stack_name="rate-limits", region="us-east-1")
        params = manager._format_parameters(
            {
                "log_retention_days": "90",
            }
        )

        # SchemaVersion + log retention (TableName derived from AWS::StackName)
        assert len(params) == 2

        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert "TableName" not in param_dict
        assert "SchemaVersion" in param_dict
        assert param_dict["LogRetentionDays"] == "90"

    def test_format_parameters_with_deletion_protection(self) -> None:
        """Test parameter formatting with deletion protection enabled."""
        manager = StackManager(stack_name="rate-limits", region="us-east-1")
        params = manager._format_parameters({"enable_deletion_protection": "true"})

        param_dict = {p["ParameterKey"]: p["ParameterValue"] for p in params}
        assert param_dict["EnableDeletionProtection"] == "true"

    def test_load_template(self) -> None:
        """Test loading CloudFormation template."""
        manager = StackManager(stack_name="test", region="us-east-1")
        template = manager._load_template()

        assert template is not None
        assert "AWSTemplateFormatVersion" in template
        assert "AWS::DynamoDB::Table" in template
        assert "RateLimitsTable" in template

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
                stack_name="test",
                region="us-east-1",
                endpoint_url="http://localhost:4566",
            )

            result = await manager.create_stack(
                stack_options=StackOptions(
                    snapshot_windows="hourly",
                    retention_days=30,
                )
            )

            assert result["status"] == "CREATE_COMPLETE"
            assert result["stack_id"] == "test-stack-id"
            mock_client.create_stack.assert_called_once()


class TestStackExists:
    """Tests for stack_exists method with mocks."""

    @pytest.mark.asyncio
    async def test_stack_exists_returns_true(self) -> None:
        """stack_exists returns True when stack exists."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.stack_exists("test-stack")

            assert result is True

    @pytest.mark.asyncio
    async def test_stack_exists_returns_false_for_delete_complete(self) -> None:
        """stack_exists returns False when stack is DELETE_COMPLETE."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "DELETE_COMPLETE"}]}
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.stack_exists("test-stack")

            assert result is False

    @pytest.mark.asyncio
    async def test_stack_exists_returns_false_on_validation_error(self) -> None:
        """stack_exists returns False when stack doesn't exist."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.stack_exists("non-existent")

            assert result is False

    @pytest.mark.asyncio
    async def test_stack_exists_returns_false_for_empty_stacks(self) -> None:
        """stack_exists returns False when Stacks list is empty."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.stack_exists("test-stack")

            assert result is False


class TestGetStackStatus:
    """Tests for get_stack_status method with mocks."""

    @pytest.mark.asyncio
    async def test_returns_status_when_exists(self) -> None:
        """get_stack_status returns status string when stack exists."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "UPDATE_COMPLETE"}]}
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.get_stack_status("test-stack")

            assert result == "UPDATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_returns_none_on_validation_error(self) -> None:
        """get_stack_status returns None when stack doesn't exist."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.get_stack_status("non-existent")

            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_stacks(self) -> None:
        """get_stack_status returns None when Stacks list is empty."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.get_stack_status("test-stack")

            assert result is None


class TestCreateStackErrors:
    """Tests for create_stack error handling."""

    @pytest.mark.asyncio
    async def test_waits_for_in_progress_stack(self) -> None:
        """create_stack waits when stack is CREATE_IN_PROGRESS."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "CREATE_IN_PROGRESS"}]}
            )

            mock_waiter = MagicMock()
            mock_waiter.wait = AsyncMock()
            mock_client.get_waiter.return_value = mock_waiter

            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.create_stack(wait=True)

            assert result["status"] == "already_exists_and_ready"
            mock_waiter.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_existing_stack_status(self) -> None:
        """create_stack returns existing stack info without waiting."""
        with (
            patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch.object(StackManager, "ensure_tags", new_callable=AsyncMock),
        ):
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.create_stack(wait=True)

            assert result["status"] == "CREATE_COMPLETE"
            assert "already exists" in result.get("message", "")

    @pytest.mark.asyncio
    async def test_calls_ensure_tags_when_stack_exists(self) -> None:
        """create_stack calls ensure_tags on existing stacks."""
        with (
            patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch.object(StackManager, "ensure_tags", new_callable=AsyncMock) as mock_ensure_tags,
        ):
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            opts = StackOptions(tags={"env": "prod"})
            await manager.create_stack(stack_options=opts, wait=True)

            mock_ensure_tags.assert_called_once_with({"env": "prod"})

    @pytest.mark.asyncio
    async def test_calls_ensure_tags_without_user_tags(self) -> None:
        """create_stack calls ensure_tags with None when no stack_options."""
        with (
            patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch.object(StackManager, "ensure_tags", new_callable=AsyncMock) as mock_ensure_tags,
        ):
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            await manager.create_stack(wait=True)

            mock_ensure_tags.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_raises_already_exists_on_race(self) -> None:
        """create_stack raises StackAlreadyExistsError on race condition."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            # First call returns no stack (triggering create attempt)
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
            )
            # Create fails with AlreadyExistsException
            mock_client.create_stack = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "AlreadyExistsException", "Message": "Stack exists"}},
                    "CreateStack",
                )
            )

            mock_waiter = MagicMock()
            mock_waiter.wait = AsyncMock()
            mock_client.get_waiter.return_value = mock_waiter

            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")

            with pytest.raises(StackAlreadyExistsError):
                await manager.create_stack()

    @pytest.mark.asyncio
    async def test_raises_creation_error_on_client_error(self) -> None:
        """create_stack raises StackCreationError on other ClientErrors."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
            )
            mock_client.create_stack = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "InsufficientCapabilities", "Message": "Need IAM"}},
                    "CreateStack",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")

            with pytest.raises(StackCreationError) as exc_info:
                await manager.create_stack()

            assert "Need IAM" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_on_waiter_failure(self) -> None:
        """create_stack raises StackCreationError when waiter fails."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                    "DescribeStacks",
                )
            )
            mock_client.create_stack = AsyncMock(return_value={"StackId": "test-id"})

            mock_waiter = MagicMock()
            mock_waiter.wait = AsyncMock(side_effect=Exception("Waiter timeout"))
            mock_client.get_waiter.return_value = mock_waiter

            mock_client.describe_stack_events = AsyncMock(return_value={"StackEvents": []})

            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")

            with pytest.raises(StackCreationError) as exc_info:
                await manager.create_stack(wait=True)

            assert "Waiter timeout" in str(exc_info.value)


class TestDeleteStack:
    """Tests for delete_stack method."""

    @pytest.mark.asyncio
    async def test_deletes_existing_stack(self) -> None:
        """delete_stack successfully deletes an existing stack."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.delete_stack = AsyncMock()

            mock_waiter = MagicMock()
            mock_waiter.wait = AsyncMock()
            mock_client.get_waiter.return_value = mock_waiter

            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")
            await manager.delete_stack("test-stack", wait=True)

            mock_client.delete_stack.assert_called_once_with(StackName="test-stack")
            mock_waiter.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_ignores_nonexistent_stack(self) -> None:
        """delete_stack ignores ValidationError for non-existent stack."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.delete_stack = AsyncMock(
                side_effect=ClientError(
                    {
                        "Error": {
                            "Code": "ValidationError",
                            "Message": "Stack does not exist",
                        }
                    },
                    "DeleteStack",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")

            # Should not raise
            await manager.delete_stack("non-existent")

    @pytest.mark.asyncio
    async def test_raises_on_other_errors(self) -> None:
        """delete_stack raises StackCreationError on other errors."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.delete_stack = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
                    "DeleteStack",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="test", region="us-east-1")

            with pytest.raises(StackCreationError) as exc_info:
                await manager.delete_stack("test-stack")

            assert "Not authorized" in str(exc_info.value)


class TestDeployLambdaCode:
    """Tests for deploy_lambda_code method."""

    @pytest.mark.asyncio
    async def test_successful_deployment(self) -> None:
        """deploy_lambda_code successfully deploys Lambda."""
        with (
            patch(
                "zae_limiter.infra.stack_manager.build_lambda_package",
                return_value=b"fake-zip",
            ),
            patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class,
        ):
            # Setup mock Lambda client
            mock_lambda = MagicMock()
            mock_lambda.update_function_code = AsyncMock(
                return_value={
                    "FunctionArn": "arn:aws:lambda:us-east-1:123:function:test",
                    "CodeSha256": "abc123",
                }
            )
            mock_waiter = MagicMock()
            mock_waiter.wait = AsyncMock()
            mock_lambda.get_waiter.return_value = mock_waiter
            mock_lambda.tag_resource = AsyncMock()

            # Setup context manager for client
            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.deploy_lambda_code()

            assert result["status"] == "deployed"
            assert result["function_arn"] == "arn:aws:lambda:us-east-1:123:function:test"
            mock_lambda.update_function_code.assert_called_once()

    @pytest.mark.asyncio
    async def test_raises_on_build_failure(self) -> None:
        """deploy_lambda_code raises on package build failure."""
        with patch(
            "zae_limiter.infra.stack_manager.build_lambda_package",
            side_effect=Exception("Build failed"),
        ):
            manager = StackManager(stack_name="test", region="us-east-1")

            with pytest.raises(StackCreationError) as exc_info:
                await manager.deploy_lambda_code()

            assert "Build failed" in str(exc_info.value)


class TestContextManager:
    """Tests for async context manager functionality."""

    @pytest.mark.asyncio
    async def test_aenter_returns_self(self) -> None:
        """__aenter__ returns the manager instance."""
        manager = StackManager(stack_name="test", region="us-east-1")

        result = await manager.__aenter__()

        assert result is manager

    @pytest.mark.asyncio
    async def test_aexit_calls_close(self) -> None:
        """__aexit__ calls close."""
        manager = StackManager(stack_name="test", region="us-east-1")

        with patch.object(manager, "close", new_callable=AsyncMock) as mock_close:
            await manager.__aexit__(None, None, None)
            mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_context_manager_usage(self) -> None:
        """Context manager works correctly."""
        with patch.object(StackManager, "close", new_callable=AsyncMock) as mock_close:
            async with StackManager(stack_name="test", region="us-east-1") as manager:
                assert manager is not None

            mock_close.assert_called_once()


class TestClose:
    """Tests for close method."""

    @pytest.mark.asyncio
    async def test_close_cleans_up_client(self) -> None:
        """close cleans up client and session."""
        manager = StackManager(stack_name="test", region="us-east-1")

        # Setup mock client
        mock_client = MagicMock()
        mock_client.__aexit__ = AsyncMock()
        manager._client = mock_client
        manager._session = MagicMock()

        await manager.close()

        assert manager._client is None
        assert manager._session is None
        mock_client.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_handles_none_client(self) -> None:
        """close handles None client gracefully."""
        manager = StackManager(stack_name="test", region="us-east-1")
        manager._client = None
        manager._session = None

        # Should not raise
        await manager.close()

        assert manager._client is None
        assert manager._session is None

    @pytest.mark.asyncio
    async def test_close_handles_client_exit_error(self) -> None:
        """close handles errors from client __aexit__."""
        manager = StackManager(stack_name="test", region="us-east-1")

        mock_client = MagicMock()
        mock_client.__aexit__ = AsyncMock(side_effect=Exception("Cleanup error"))
        manager._client = mock_client
        manager._session = MagicMock()

        # Should not raise - errors are suppressed
        await manager.close()

        assert manager._client is None
        assert manager._session is None


class TestGetStackEvents:
    """Tests for _get_stack_events method."""

    @pytest.mark.asyncio
    async def test_returns_formatted_events(self) -> None:
        """_get_stack_events returns formatted event list."""
        manager = StackManager(stack_name="test", region="us-east-1")

        mock_client = MagicMock()
        mock_client.describe_stack_events = AsyncMock(
            return_value={
                "StackEvents": [
                    {
                        "Timestamp": "2024-01-01T00:00:00Z",
                        "ResourceType": "AWS::DynamoDB::Table",
                        "LogicalResourceId": "Table",
                        "ResourceStatus": "CREATE_COMPLETE",
                        "ResourceStatusReason": None,
                    }
                ]
            }
        )

        events = await manager._get_stack_events(mock_client, "test-stack")

        assert len(events) == 1
        assert events[0]["resource_type"] == "AWS::DynamoDB::Table"
        assert events[0]["status"] == "CREATE_COMPLETE"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self) -> None:
        """_get_stack_events returns empty list on error."""
        manager = StackManager(stack_name="test", region="us-east-1")

        mock_client = MagicMock()
        mock_client.describe_stack_events = AsyncMock(side_effect=Exception("API error"))

        events = await manager._get_stack_events(mock_client, "test-stack")

        assert events == []


class TestGetAllTags:
    """Test _get_all_tags method for building complete tag lists."""

    def test_includes_discovery_tags(self) -> None:
        """_get_all_tags includes ManagedBy and name discovery tags."""
        manager = StackManager(stack_name="my-app", region="us-east-1")
        tags = manager._get_all_tags()

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["ManagedBy"] == "zae-limiter"
        assert tag_dict["zae-limiter:name"] == "my-app"

    def test_includes_version_tags(self) -> None:
        """_get_all_tags includes version, schema, and lambda version tags."""
        manager = StackManager(stack_name="my-app", region="us-east-1")
        tags = manager._get_all_tags()

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert "zae-limiter:version" in tag_dict
        assert "zae-limiter:schema-version" in tag_dict
        assert "zae-limiter:lambda-version" in tag_dict

    def test_includes_user_tags(self) -> None:
        """_get_all_tags includes user-defined tags."""
        manager = StackManager(stack_name="my-app", region="us-east-1")
        user_tags = {"env": "prod", "team": "platform"}
        tags = manager._get_all_tags(user_tags)

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["env"] == "prod"
        assert tag_dict["team"] == "platform"

    def test_managed_tags_override_user_tags(self) -> None:
        """Managed tags take precedence over user tags with same key."""
        manager = StackManager(stack_name="my-app", region="us-east-1")
        # User tries to override ManagedBy — managed tag should win
        user_tags = {"ManagedBy": "someone-else"}
        tags = manager._get_all_tags(user_tags)

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["ManagedBy"] == "zae-limiter"

    def test_no_user_tags(self) -> None:
        """_get_all_tags works without user tags."""
        manager = StackManager(stack_name="my-app", region="us-east-1")
        tags = manager._get_all_tags(None)

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["ManagedBy"] == "zae-limiter"
        assert tag_dict["zae-limiter:name"] == "my-app"

    def test_user_name_from_plain_name(self) -> None:
        """zae-limiter:name tag matches stack name when no prefix."""
        manager = StackManager(stack_name="my-app", region="us-east-1")
        tags = manager._get_all_tags()

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["zae-limiter:name"] == "my-app"

    def test_user_name_extracted_from_legacy_prefixed_name(self) -> None:
        """zae-limiter:name tag strips ZAEL- prefix for legacy stacks."""
        manager = StackManager(stack_name="ZAEL-my-app", region="us-east-1")
        tags = manager._get_all_tags()

        tag_dict = {t["Key"]: t["Value"] for t in tags}
        assert tag_dict["zae-limiter:name"] == "my-app"


class TestEnsureTags:
    """Test ensure_tags method for auto-tagging existing stacks."""

    @pytest.mark.asyncio
    async def test_returns_false_when_tags_already_present(self) -> None:
        """ensure_tags returns False if discovery tags already exist."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-app"},
                            ]
                        }
                    ]
                }
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="my-app", region="us-east-1")
            result = await manager.ensure_tags()

            assert result is False
            mock_client.update_stack.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_true_when_tags_missing(self) -> None:
        """ensure_tags updates stack and returns True if tags are missing."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "Tags": [
                                {"Key": "zae-limiter:version", "Value": "0.6.0"},
                            ]
                        }
                    ]
                }
            )
            mock_client.update_stack = AsyncMock()
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="my-app", region="us-east-1")
            result = await manager.ensure_tags()

            assert result is True
            mock_client.update_stack.assert_called_once()
            call_kwargs = mock_client.update_stack.call_args[1]
            assert call_kwargs["UsePreviousTemplate"] is True
            tag_dict = {t["Key"]: t["Value"] for t in call_kwargs["Tags"]}
            assert tag_dict["ManagedBy"] == "zae-limiter"

    @pytest.mark.asyncio
    async def test_returns_false_on_describe_error(self) -> None:
        """ensure_tags returns False if describe_stacks fails."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "ValidationError", "Message": "not found"}},
                    "DescribeStacks",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="my-app", region="us-east-1")
            result = await manager.ensure_tags()

            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_stacks(self) -> None:
        """ensure_tags returns False if describe_stacks returns empty list."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(return_value={"Stacks": []})
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="my-app", region="us-east-1")
            result = await manager.ensure_tags()

            assert result is False

    @pytest.mark.asyncio
    async def test_strips_legacy_prefix_for_tag_comparison(self) -> None:
        """ensure_tags strips ZAEL- prefix when comparing name tag value."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "Tags": [
                                {"Key": "ManagedBy", "Value": "zae-limiter"},
                                {"Key": "zae-limiter:name", "Value": "my-app"},
                            ]
                        }
                    ]
                }
            )
            mock_get_client.return_value = mock_client

            # Stack with ZAEL- prefix; user_name should be "my-app"
            manager = StackManager(stack_name="ZAEL-my-app", region="us-east-1")
            result = await manager.ensure_tags()

            # Tags already present (ManagedBy + name matches stripped prefix)
            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_no_updates_error(self) -> None:
        """ensure_tags returns False when update_stack says no updates needed."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "Tags": []  # No tags, so update will be attempted
                        }
                    ]
                }
            )
            mock_client.update_stack = AsyncMock(
                side_effect=ClientError(
                    {
                        "Error": {
                            "Code": "ValidationError",
                            "Message": "No updates are to be performed",
                        }
                    },
                    "UpdateStack",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="my-app", region="us-east-1")
            result = await manager.ensure_tags()

            assert result is False

    @pytest.mark.asyncio
    async def test_raises_on_update_error(self) -> None:
        """ensure_tags re-raises ClientError that is not 'No updates'."""
        with patch.object(StackManager, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_client = MagicMock()
            mock_client.describe_stacks = AsyncMock(
                return_value={
                    "Stacks": [
                        {
                            "Tags": []  # No tags, so update will be attempted
                        }
                    ]
                }
            )
            mock_client.update_stack = AsyncMock(
                side_effect=ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": "Not authorized"}},
                    "UpdateStack",
                )
            )
            mock_get_client.return_value = mock_client

            manager = StackManager(stack_name="my-app", region="us-east-1")

            with pytest.raises(ClientError) as exc_info:
                await manager.ensure_tags()

            assert "Not authorized" in str(exc_info.value)


class TestWaitForEsmReady:
    """Tests for wait_for_esm_ready method."""

    @pytest.mark.asyncio
    async def test_returns_true_when_esm_enabled_and_stabilized(self) -> None:
        """wait_for_esm_ready returns True when ESM enabled with OK result."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={
                    "EventSourceMappings": [
                        {
                            "State": "Enabled",
                            "LastProcessingResult": "OK",
                        }
                    ]
                }
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            # Use short stabilization time for test
            result = await manager.wait_for_esm_ready(
                "test-aggregator", max_seconds=10, min_stabilization=0.0
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_returns_true_when_esm_enabled_no_records_processed(self) -> None:
        """wait_for_esm_ready returns True when ESM has polled but found no records."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={
                    "EventSourceMappings": [
                        {
                            "State": "Enabled",
                            "LastProcessingResult": "No records processed",
                        }
                    ]
                }
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready(
                "test-aggregator", max_seconds=10, min_stabilization=0.0
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_returns_false_when_esm_disabled(self) -> None:
        """wait_for_esm_ready returns False when ESM is Disabled."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={
                    "EventSourceMappings": [
                        {
                            "State": "Disabled",
                            "LastProcessingResult": None,
                        }
                    ]
                }
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready("test-aggregator", max_seconds=1)

            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_esm_disabling(self) -> None:
        """wait_for_esm_ready returns False when ESM is Disabling."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={
                    "EventSourceMappings": [
                        {
                            "State": "Disabling",
                            "LastProcessingResult": None,
                        }
                    ]
                }
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready("test-aggregator", max_seconds=1)

            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self) -> None:
        """wait_for_esm_ready returns False when ESM never becomes ready."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            # ESM stays in Creating state
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={
                    "EventSourceMappings": [
                        {
                            "State": "Creating",
                            "LastProcessingResult": None,
                        }
                    ]
                }
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            # Very short timeout to avoid slow test
            result = await manager.wait_for_esm_ready("test-aggregator", max_seconds=1)

            assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_esm_found(self) -> None:
        """wait_for_esm_ready returns False when no ESM mappings exist."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={"EventSourceMappings": []}
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready("test-aggregator", max_seconds=1)

            assert result is False

    @pytest.mark.asyncio
    async def test_handles_api_exception(self) -> None:
        """wait_for_esm_ready handles API exceptions gracefully."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(side_effect=Exception("API error"))

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            # Should not raise, returns False after timeout
            result = await manager.wait_for_esm_ready("test-aggregator", max_seconds=1)

            assert result is False

    @pytest.mark.asyncio
    async def test_waits_when_last_processing_result_is_none(self) -> None:
        """wait_for_esm_ready keeps waiting when LastProcessingResult is None."""
        call_count = 0

        async def mock_list_esm(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # First calls: ESM enabled but no LastProcessingResult yet
                return {
                    "EventSourceMappings": [
                        {
                            "State": "Enabled",
                            "LastProcessingResult": None,
                        }
                    ]
                }
            else:
                # Later: ESM has polled
                return {
                    "EventSourceMappings": [
                        {
                            "State": "Enabled",
                            "LastProcessingResult": "OK",
                        }
                    ]
                }

        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(side_effect=mock_list_esm)

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready(
                "test-aggregator", max_seconds=60, min_stabilization=0.0
            )

            assert result is True
            assert call_count >= 3

    @pytest.mark.asyncio
    async def test_uses_endpoint_url_when_provided(self) -> None:
        """wait_for_esm_ready passes endpoint_url to Lambda client."""
        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(
                return_value={
                    "EventSourceMappings": [{"State": "Enabled", "LastProcessingResult": "OK"}]
                }
            )

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(
                stack_name="test",
                region="us-east-1",
                endpoint_url="http://localhost:4566",
            )
            await manager.wait_for_esm_ready(
                "test-aggregator", max_seconds=10, min_stabilization=0.0
            )

            # Verify endpoint_url was passed to client
            mock_session.client.assert_called_with(
                "lambda",
                region_name="us-east-1",
                endpoint_url="http://localhost:4566",
            )

    @pytest.mark.asyncio
    async def test_waits_for_enabling_state(self) -> None:
        """wait_for_esm_ready keeps waiting when ESM is in Enabling state."""
        call_count = 0

        async def mock_list_esm(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return {
                    "EventSourceMappings": [{"State": "Enabling", "LastProcessingResult": None}]
                }
            else:
                return {"EventSourceMappings": [{"State": "Enabled", "LastProcessingResult": "OK"}]}

        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(side_effect=mock_list_esm)

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready(
                "test-aggregator", max_seconds=60, min_stabilization=0.0
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_waits_on_error_processing_result(self) -> None:
        """wait_for_esm_ready keeps waiting when LastProcessingResult is an error."""
        call_count = 0

        async def mock_list_esm(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                return {
                    "EventSourceMappings": [
                        {"State": "Enabled", "LastProcessingResult": "FunctionError"}
                    ]
                }
            else:
                return {"EventSourceMappings": [{"State": "Enabled", "LastProcessingResult": "OK"}]}

        with patch("zae_limiter.infra.stack_manager.aioboto3.Session") as mock_session_class:
            mock_lambda = MagicMock()
            mock_lambda.list_event_source_mappings = AsyncMock(side_effect=mock_list_esm)

            mock_client_cm = MagicMock()
            mock_client_cm.__aenter__ = AsyncMock(return_value=mock_lambda)
            mock_client_cm.__aexit__ = AsyncMock()

            mock_session = MagicMock()
            mock_session.client.return_value = mock_client_cm
            mock_session_class.return_value = mock_session

            manager = StackManager(stack_name="test", region="us-east-1")
            result = await manager.wait_for_esm_ready(
                "test-aggregator", max_seconds=60, min_stabilization=0.0
            )

            assert result is True
