"""Unit test fixtures using moto."""

import pytest

from tests.fixtures.moto import (  # noqa: F401
    _patch_aiobotocore_response,
    aws_credentials,
    mock_dynamodb,
)
from zae_limiter import RateLimiter, SyncRateLimiter
from zae_limiter.sync_repository import SyncRepository


@pytest.fixture
async def limiter(mock_dynamodb):  # noqa: F811
    """Create a RateLimiter with mocked DynamoDB."""
    with _patch_aiobotocore_response():
        # Create limiter without auto-creation
        limiter = RateLimiter(
            name="test-rate-limits",
            region="us-east-1",
        )
        # Manually create table using direct API (not CloudFormation)
        await limiter._repository.create_table()
        async with limiter:
            yield limiter


@pytest.fixture
def sync_limiter(mock_dynamodb):  # noqa: F811
    """Create a SyncRateLimiter with mocked DynamoDB and native sync."""
    # Create native sync repository
    repo = SyncRepository(
        name="test-rate-limits",
        region="us-east-1",
    )
    # Create table directly
    repo.create_table()

    # Create limiter with native sync repository
    limiter = SyncRateLimiter(repository=repo)

    with limiter:
        yield limiter


@pytest.fixture
def sync_repository(mock_dynamodb):  # noqa: F811
    """Create a SyncRepository with mocked DynamoDB."""
    repo = SyncRepository(
        name="test-rate-limits",
        region="us-east-1",
    )
    repo.create_table()
    yield repo
    repo.close()
