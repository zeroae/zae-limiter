"""E2E test fixtures."""

import pytest
import pytest_asyncio

from tests.fixtures.stacks import get_or_create_shared_stack
from zae_limiter import StackOptions

pytest_plugins = [
    "tests.fixtures.aws_clients",
    "tests.fixtures.repositories",
]

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
async def shared_full_stack(localstack_endpoint, tmp_path_factory):
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
