"""Unit test fixtures using moto."""

import pytest

from tests.fixtures.moto import _patch_aiobotocore_response
from zae_limiter import RateLimiter
from zae_limiter.sync_repository import SyncRepository

pytest_plugins = [
    "tests.fixtures.moto",
]


@pytest.fixture
async def limiter(mock_dynamodb):
    """Create a RateLimiter with mocked DynamoDB."""
    with _patch_aiobotocore_response():
        limiter = RateLimiter(
            name="test-rate-limits",
            region="us-east-1",
        )
        await limiter._repository.create_table()
        async with limiter:
            yield limiter


@pytest.fixture
def sync_repository(mock_dynamodb):
    """Create a SyncRepository with mocked DynamoDB."""
    repo = SyncRepository(
        name="test-rate-limits",
        region="us-east-1",
    )
    repo.create_table()
    yield repo
    repo.close()
