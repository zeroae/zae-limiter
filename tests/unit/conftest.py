"""Unit test fixtures using moto."""

import pytest

from zae_limiter import RateLimiter, Repository, __version__
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version


async def _setup_moto_table(name: str = "test-rate-limits", region: str = "us-east-1") -> None:
    """Create table, default namespace and a deployed stack's version record (#638)."""
    repo = Repository(name=name, region=region, _skip_deprecation_warning=True)
    await repo.create_table()
    await repo._register_namespace("default")
    await repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    await repo.close()


def _setup_moto_table_sync(name: str = "test-rate-limits", region: str = "us-east-1") -> None:
    """Create table and register default namespace in moto (sync test setup)."""
    repo = SyncRepository(name=name, region=region, _skip_deprecation_warning=True)
    repo.create_table()
    repo._register_namespace("default")
    repo.set_version_record(schema_version=get_schema_version(), lambda_version=__version__)
    repo.close()


@pytest.fixture
async def limiter(mock_dynamodb):
    """Create a RateLimiter with mocked DynamoDB."""
    await _setup_moto_table()
    repo = await Repository.open(stack="test-rate-limits")
    limiter = RateLimiter(repository=repo)
    async with limiter:
        yield limiter


@pytest.fixture
def sync_repository(mock_dynamodb):
    """Create a SyncRepository with mocked DynamoDB."""
    _setup_moto_table_sync()
    repo = SyncRepository.open(stack="test-rate-limits")
    yield repo
    repo.close()
