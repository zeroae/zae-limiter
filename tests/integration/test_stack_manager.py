"""Integration tests for CloudFormation stack manager (require LocalStack)."""

from unittest.mock import patch

import pytest

from zae_limiter.infra.stack_manager import StackManager

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Stack Existence and Status Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stack_exists_false(localstack_endpoint) -> None:
    """Test stack_exists returns False for non-existent stack."""
    manager = StackManager(
        stack_name="test",
        region="us-east-1",
        endpoint_url=localstack_endpoint,
    )

    exists = await manager.stack_exists("non-existent-stack-123456")
    assert not exists


@pytest.mark.asyncio
async def test_get_stack_status_none(localstack_endpoint) -> None:
    """Test get_stack_status returns None for non-existent stack."""
    manager = StackManager(
        stack_name="test",
        region="us-east-1",
        endpoint_url=localstack_endpoint,
    )

    status = await manager.get_stack_status("non-existent-stack-123456")
    assert status is None


@pytest.mark.asyncio
async def test_delete_stack_nonexistent(localstack_endpoint) -> None:
    """Test deleting a non-existent stack doesn't raise error."""
    manager = StackManager(
        stack_name="test",
        region="us-east-1",
        endpoint_url=localstack_endpoint,
    )

    await manager.delete_stack("non-existent-stack-123456")


@pytest.mark.asyncio
async def test_stack_create_and_delete_minimal(
    localstack_endpoint, minimal_stack_options, unique_name
) -> None:
    """Test creating and deleting a minimal stack (no aggregator, no alarms).

    This test verifies the full stack lifecycle with LocalStack.
    Stack deletion may fail due to a known LocalStack v2 engine bug.
    See: https://github.com/localstack/localstack/issues/13609
    """
    manager = StackManager(
        stack_name=unique_name,
        region="us-east-1",
        endpoint_url=localstack_endpoint,
    )
    stack_name = manager.stack_name

    try:
        result = await manager.create_stack(
            stack_options=minimal_stack_options,
            wait=True,
        )
        assert result["status"] == "CREATE_COMPLETE"

        assert await manager.stack_exists(stack_name)

        try:
            await manager.delete_stack(stack_name, wait=True)
            assert not await manager.stack_exists(stack_name)
        except Exception as e:
            pytest.skip(f"Stack deletion failed (known LocalStack v2 bug): {e}")
    finally:
        try:
            await manager.delete_stack(stack_name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Lambda Code Deployment Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_lambda_code_raises_on_update_failure(localstack_endpoint) -> None:
    """deploy_lambda_code raises on Lambda update failure with invalid function."""
    manager = StackManager(
        stack_name="test",
        region="us-east-1",
        endpoint_url=localstack_endpoint,
    )

    with pytest.raises(Exception):
        with patch.object(
            manager,
            "_get_stack_outputs",
            return_value={"aggregator_function_name": "non-existent-function-123456"},
        ):
            await manager.deploy_lambda_code()
