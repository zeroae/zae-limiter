"""Integration tests for CloudFormation stack manager (require LocalStack)."""

import pytest

from zae_limiter.infra.stack_manager import StackManager


class TestStackManagerLocalStack:
    """Integration tests for StackManager with LocalStack."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_stack_exists_false(self, localstack_endpoint) -> None:
        """Test stack_exists returns False for non-existent stack."""
        manager = StackManager(
            table_name="test",
            region="us-east-1",
            endpoint_url=localstack_endpoint,
        )

        exists = await manager.stack_exists("non-existent-stack-123456")
        assert not exists

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_stack_status_none(self, localstack_endpoint) -> None:
        """Test get_stack_status returns None for non-existent stack."""
        manager = StackManager(
            table_name="test",
            region="us-east-1",
            endpoint_url=localstack_endpoint,
        )

        status = await manager.get_stack_status("non-existent-stack-123456")
        assert status is None

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_delete_stack_nonexistent(self, localstack_endpoint) -> None:
        """Test deleting a non-existent stack doesn't raise error."""
        manager = StackManager(
            table_name="test",
            region="us-east-1",
            endpoint_url=localstack_endpoint,
        )

        # Should not raise
        await manager.delete_stack("non-existent-stack-123456")

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_stack_create_and_delete_minimal(
        self, localstack_endpoint, minimal_stack_options
    ) -> None:
        """Test creating and deleting a minimal stack (no aggregator, no alarms).

        This test verifies the full stack lifecycle with LocalStack.
        Stack deletion may fail due to a known LocalStack v2 engine bug.
        See: https://github.com/localstack/localstack/issues/13609
        """
        manager = StackManager(
            table_name="test_minimal_stack",
            region="us-east-1",
            endpoint_url=localstack_endpoint,
        )
        stack_name = manager.get_stack_name()

        try:
            # Create minimal stack
            result = await manager.create_stack(
                stack_options=minimal_stack_options,
                wait=True,
            )
            assert result["status"] == "CREATE_COMPLETE"

            # Verify stack exists
            assert await manager.stack_exists(stack_name)

            # Try to delete - may fail due to LocalStack v2 bug
            try:
                await manager.delete_stack(stack_name, wait=True)
                assert not await manager.stack_exists(stack_name)
            except Exception as e:
                # Known issue: LocalStack v2 engine has deletion bugs
                # See: https://github.com/localstack/localstack/issues/13609
                pytest.skip(f"Stack deletion failed (known LocalStack v2 bug): {e}")
        finally:
            # Best-effort cleanup
            try:
                await manager.delete_stack(stack_name)
            except Exception:
                pass


class TestDeployLambdaCodeLocalStack:
    """Integration tests for deploy_lambda_code with LocalStack."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_raises_on_update_failure(self, localstack_endpoint) -> None:
        """deploy_lambda_code raises on Lambda update failure with invalid function."""
        from unittest.mock import patch

        manager = StackManager(
            table_name="test",
            region="us-east-1",
            endpoint_url=localstack_endpoint,
        )

        # Attempting to update a non-existent Lambda function should raise
        with pytest.raises(Exception):  # Will be ClientError or similar
            # Mock the get_stack_outputs to return a fake function name
            with patch.object(
                manager,
                "_get_stack_outputs",
                return_value={"aggregator_function_name": "non-existent-function-123456"},
            ):
                await manager.deploy_lambda_code()
