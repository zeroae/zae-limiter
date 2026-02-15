"""E2E test fixtures."""

import pytest
import pytest_asyncio

from tests.fixtures.aws_clients import (  # noqa: F401
    cloudwatch_client,
    dynamodb_client,
    lambda_client,
    s3_client,
    sqs_client,
)
from tests.fixtures.names import unique_name, unique_name_class, unique_namespace  # noqa: F401
from tests.fixtures.repositories import localstack_limiter, test_repo  # noqa: F401
from tests.fixtures.stacks import (  # noqa: F401
    aggregator_stack_options,
    get_or_create_shared_stack,
    localstack_endpoint,
    minimal_stack_options,
    shared_aggregator_stack,
    shared_minimal_stack,
)
from zae_limiter import StackOptions

# E2E-specific StackOptions fixtures


@pytest.fixture(scope="session")
def full_stack_options():
    """Full stack with aggregator and CloudWatch alarms."""
    return StackOptions(enable_aggregator=True, enable_alarms=True)


@pytest.fixture(scope="session")
def e2e_stack_options():
    """Full stack options for E2E tests."""
    return StackOptions(
        enable_aggregator=True,
        enable_alarms=True,
        snapshot_windows="hourly",
        usage_retention_days=7,
    )


# E2E-specific shared stack


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_full_stack(localstack_endpoint, tmp_path_factory):  # noqa: F811
    """Session-scoped shared stack with aggregator and alarms."""
    return await get_or_create_shared_stack(
        tmp_path_factory,
        "shared-full",
        localstack_endpoint,
        enable_aggregator=True,
        enable_alarms=True,
        snapshot_windows="hourly",
        usage_retention_days=7,
    )
