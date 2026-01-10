"""Pytest fixtures for zae-limiter tests."""

import pytest
from moto import mock_aws

from zae_limiter import RateLimiter, SyncRateLimiter


@pytest.fixture
def aws_credentials(monkeypatch):
    """Mock AWS credentials for moto."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture
def mock_dynamodb(aws_credentials):
    """Mock DynamoDB for tests."""
    with mock_aws():
        yield


@pytest.fixture
async def limiter(mock_dynamodb):
    """Create a RateLimiter with mocked DynamoDB."""
    limiter = RateLimiter(
        table_name="test_rate_limits",
        region="us-east-1",
        create_table=True,
    )
    async with limiter:
        yield limiter


@pytest.fixture
def sync_limiter(mock_dynamodb):
    """Create a SyncRateLimiter with mocked DynamoDB."""
    limiter = SyncRateLimiter(
        table_name="test_rate_limits",
        region="us-east-1",
        create_table=True,
    )
    with limiter:
        yield limiter
