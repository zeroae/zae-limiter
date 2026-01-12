"""Pytest fixtures for zae-limiter tests."""

import asyncio
import os
import time
import uuid
from collections.abc import Awaitable
from unittest.mock import patch

import pytest
from moto import mock_aws

from zae_limiter import RateLimiter, StackOptions, SyncRateLimiter

# Pytest hooks for --run-aws flag


def pytest_addoption(parser):
    """Add --run-aws pytest option."""
    parser.addoption(
        "--run-aws",
        action="store_true",
        default=False,
        help="Run tests against real AWS (requires valid credentials)",
    )


def pytest_collection_modifyitems(config, items):
    """Skip AWS tests unless --run-aws flag is provided."""
    if not config.getoption("--run-aws"):
        skip_aws = pytest.mark.skip(reason="Need --run-aws option to run")
        for item in items:
            if "aws" in item.keywords:
                item.add_marker(skip_aws)


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


# LocalStack fixtures for integration testing


@pytest.fixture
def localstack_endpoint():
    """LocalStack endpoint URL from environment."""
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")
    return endpoint


# StackOptions fixtures for different test scenarios


@pytest.fixture
def minimal_stack_options():
    """Minimal stack - no aggregator, no alarms. Fastest deployment."""
    return StackOptions(enable_aggregator=False, enable_alarms=False)


@pytest.fixture
def aggregator_stack_options():
    """Stack with aggregator Lambda but no CloudWatch alarms."""
    return StackOptions(enable_aggregator=True, enable_alarms=False)


@pytest.fixture
def full_stack_options():
    """Full stack with aggregator and CloudWatch alarms."""
    return StackOptions(enable_aggregator=True, enable_alarms=True)


# LocalStack limiter fixtures


@pytest.fixture
async def localstack_limiter(localstack_endpoint, minimal_stack_options):
    """RateLimiter with minimal stack for core rate limiting tests."""
    limiter = RateLimiter(
        table_name="integration_test_rate_limits",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    async with limiter:
        yield limiter


@pytest.fixture
async def localstack_limiter_with_aggregator(localstack_endpoint, aggregator_stack_options):
    """RateLimiter with Lambda aggregator for stream testing."""
    limiter = RateLimiter(
        table_name="integration_test_with_aggregator",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=aggregator_stack_options,
    )

    async with limiter:
        yield limiter


@pytest.fixture
async def localstack_limiter_full(localstack_endpoint, full_stack_options):
    """RateLimiter with full stack including alarms."""
    limiter = RateLimiter(
        table_name="integration_test_full",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=full_stack_options,
    )

    async with limiter:
        yield limiter


@pytest.fixture
def sync_localstack_limiter(localstack_endpoint, minimal_stack_options):
    """SyncRateLimiter with minimal stack for sync integration tests."""
    limiter = SyncRateLimiter(
        table_name="integration_test_rate_limits_sync",
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    with limiter:
        yield limiter


# E2E test fixtures


@pytest.fixture
def unique_table_name():
    """Generate unique table name for test isolation.

    Uses hyphens instead of underscores because CloudFormation stack names
    must match pattern [a-zA-Z][-a-zA-Z0-9]*.
    """
    timestamp = int(time.time())
    unique_id = uuid.uuid4().hex[:8]
    return f"e2e-test-{timestamp}-{unique_id}"


@pytest.fixture
def e2e_stack_options():
    """Full stack options for E2E tests."""
    return StackOptions(
        enable_aggregator=True,
        enable_alarms=True,
        snapshot_windows="hourly",
        retention_days=7,
    )


@pytest.fixture
def cloudwatch_client(localstack_endpoint):
    """CloudWatch client for alarm verification."""
    import boto3

    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("cloudwatch", **kwargs)


@pytest.fixture
def sqs_client(localstack_endpoint):
    """SQS client for DLQ verification."""
    import boto3

    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("sqs", **kwargs)


@pytest.fixture
def lambda_client(localstack_endpoint):
    """Lambda client for function inspection."""
    import boto3

    kwargs = {"region_name": "us-east-1"}
    if localstack_endpoint:
        kwargs["endpoint_url"] = localstack_endpoint
    return boto3.client("lambda", **kwargs)
