"""Unit test fixtures using moto."""

import pytest

from zae_limiter import RateLimiter, Repository
from zae_limiter.sync_repository import SyncRepository


async def _setup_moto_table(name: str = "test-rate-limits", region: str = "us-east-1") -> None:
    """Create table and register default namespace in moto (test setup)."""
    repo = Repository(name=name, region=region, _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    await repo.close()


def _setup_moto_table_sync(name: str = "test-rate-limits", region: str = "us-east-1") -> None:
    """Create table and register default namespace in moto (sync test setup)."""
    repo = SyncRepository(name=name, region=region, _skip_deprecation_warning=True)
    repo.create_table()
    repo._register_namespace("default")
    repo.close()


@pytest.fixture
async def limiter(mock_dynamodb):
    """Create a RateLimiter with mocked DynamoDB."""
    await _setup_moto_table()
    repo = await Repository.connect("test-rate-limits", "us-east-1")
    limiter = RateLimiter(repository=repo)
    async with limiter:
        yield limiter


@pytest.fixture
def sync_repository(mock_dynamodb):
    """Create a SyncRepository with mocked DynamoDB."""
    _setup_moto_table_sync()
    repo = SyncRepository.connect("test-rate-limits", "us-east-1")
    yield repo
    repo.close()
