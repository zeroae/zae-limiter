"""Unit test fixtures using moto."""

import asyncio
from collections.abc import Awaitable
from unittest.mock import patch

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
    # Unset AWS_ENDPOINT_URL to ensure moto intercepts requests
    # (LocalStack tests use localstack_endpoint fixture which reads from env)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)


@pytest.fixture
def mock_dynamodb(aws_credentials):
    """Mock DynamoDB for tests."""
    with mock_aws():
        yield


def _patch_aiobotocore_response():
    """
    Patch aiobotocore to work with moto's sync responses.

    Moto returns botocore.awsrequest.AWSResponse which has sync content,
    but aiobotocore expects async content. This patch wraps the response
    handling to convert sync content to async.

    See: https://github.com/aio-libs/aiobotocore/discussions/1300
    """
    from aiobotocore import endpoint

    original_convert = endpoint.convert_to_response_dict

    async def patched_convert(http_response, operation_model):
        # If content is not awaitable (moto's sync response), wrap it
        if hasattr(http_response, "_content") and not isinstance(http_response._content, Awaitable):
            # Create a future that returns the content
            fut: asyncio.Future[bytes] = asyncio.Future()
            fut.set_result(http_response.content)
            http_response._content = fut
        return await original_convert(http_response, operation_model)

    return patch.object(endpoint, "convert_to_response_dict", patched_convert)


@pytest.fixture
async def limiter(mock_dynamodb):
    """Create a RateLimiter with mocked DynamoDB."""
    with _patch_aiobotocore_response():
        # Create limiter without auto-creation
        limiter = RateLimiter(
            table_name="test_rate_limits",
            region="us-east-1",
        )
        # Manually create table using direct API (not CloudFormation)
        await limiter._repository.create_table()
        async with limiter:
            yield limiter


@pytest.fixture
def sync_limiter(mock_dynamodb):
    """Create a SyncRateLimiter with mocked DynamoDB."""
    with _patch_aiobotocore_response():
        # Create limiter without auto-creation
        limiter = SyncRateLimiter(
            table_name="test_rate_limits",
            region="us-east-1",
        )
        # Manually create table using direct API (not CloudFormation)
        limiter._run(limiter._limiter._repository.create_table())
        with limiter:
            yield limiter
