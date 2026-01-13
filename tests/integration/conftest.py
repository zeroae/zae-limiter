"""Integration test fixtures for LocalStack."""

import os
import time
import uuid

import pytest

from zae_limiter import RateLimiter, StackOptions, SyncRateLimiter


@pytest.fixture(scope="session")
def localstack_endpoint():
    """LocalStack endpoint URL from environment."""
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")
    return endpoint


# StackOptions fixtures for different test scenarios


@pytest.fixture(scope="session")
def minimal_stack_options():
    """Minimal stack - no aggregator, no alarms. Fastest deployment."""
    return StackOptions(enable_aggregator=False, enable_alarms=False)


@pytest.fixture(scope="session")
def aggregator_stack_options():
    """Stack with aggregator Lambda but no CloudWatch alarms."""
    return StackOptions(enable_aggregator=True, enable_alarms=False)


@pytest.fixture(scope="session")
def full_stack_options():
    """Full stack with aggregator and CloudWatch alarms."""
    return StackOptions(enable_aggregator=True, enable_alarms=True)


@pytest.fixture
def unique_name():
    """Generate unique resource name for test isolation.

    Uses hyphens instead of underscores because AWS resource names
    must match pattern [a-zA-Z][-a-zA-Z0-9]*.
    """
    timestamp = int(time.time())
    unique_id = uuid.uuid4().hex[:8]
    return f"integration-test-{timestamp}-{unique_id}"


@pytest.fixture(scope="class")
def unique_name_class():
    """Generate unique resource name for class-level test isolation.

    Uses hyphens instead of underscores because AWS resource names
    must match pattern [a-zA-Z][-a-zA-Z0-9]*.

    This fixture is class-scoped to allow sharing a single resource across
    all tests within a test class.
    """
    timestamp = int(time.time())
    unique_id = uuid.uuid4().hex[:8]
    return f"integration-test-{timestamp}-{unique_id}"


# LocalStack limiter fixtures


@pytest.fixture
async def localstack_limiter(localstack_endpoint, minimal_stack_options, unique_name):
    """RateLimiter with minimal stack for core rate limiting tests."""
    limiter = RateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


@pytest.fixture
async def localstack_limiter_with_aggregator(
    localstack_endpoint, aggregator_stack_options, unique_name
):
    """RateLimiter with Lambda aggregator for stream testing."""
    limiter = RateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=aggregator_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


@pytest.fixture
async def localstack_limiter_full(localstack_endpoint, full_stack_options, unique_name):
    """RateLimiter with full stack including alarms."""
    limiter = RateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=full_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")


@pytest.fixture
def sync_localstack_limiter(localstack_endpoint, minimal_stack_options, unique_name):
    """SyncRateLimiter with minimal stack for sync integration tests."""
    limiter = SyncRateLimiter(
        name=unique_name,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    with limiter:
        yield limiter

    try:
        limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")
