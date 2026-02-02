"""Integration test fixtures for LocalStack."""

import os
import time
import uuid

import pytest
import pytest_asyncio

from zae_limiter import RateLimiter, StackOptions, SyncRateLimiter
from zae_limiter.repository import Repository


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


@pytest.fixture(scope="module")
def unique_name_module():
    """Generate unique resource name for module-level test isolation.

    Uses hyphens instead of underscores because AWS resource names
    must match pattern [a-zA-Z][-a-zA-Z0-9]*.

    This fixture is module-scoped to allow sharing a single resource across
    all tests within a test module (file).
    """
    timestamp = int(time.time())
    unique_id = uuid.uuid4().hex[:8]
    return f"int-mod-{timestamp}-{unique_id}"


@pytest.fixture
def unique_entity_prefix():
    """Generate unique entity ID prefix for data isolation within shared infrastructure.

    Returns an 8-character hex prefix that tests prepend to entity names.
    This allows multiple tests to share the same DynamoDB table while
    maintaining complete data isolation.

    Example usage:
        entity_id = f"{unique_entity_prefix}-user-1"
    """
    return uuid.uuid4().hex[:8]


# Module-scoped fixtures for shared infrastructure (issue #253)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def localstack_repo_module(localstack_endpoint, unique_name_module):
    """Repository connected to LocalStack (module-scoped, shared across all tests).

    Creates DynamoDB table directly (no CloudFormation) once per test module.
    Tests MUST use unique_entity_prefix for entity ID isolation.

    This fixture reduces test time by avoiding repeated table creation/deletion.
    See issue #253 for details.
    """
    repo = Repository(
        name=unique_name_module,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
    )
    await repo.create_table()
    yield repo
    try:
        await repo.delete_table()
    except Exception:
        pass  # Table might not exist
    await repo.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def localstack_limiter_module(localstack_endpoint, minimal_stack_options, unique_name_module):
    """RateLimiter with minimal stack (module-scoped, shared across all tests).

    Creates CloudFormation stack once per test module. Tests MUST use
    unique_entity_prefix for entity ID isolation.

    This fixture reduces test time by avoiding repeated stack creation/deletion.
    See issue #253 for details.
    """
    limiter = RateLimiter(
        name=unique_name_module,
        endpoint_url=localstack_endpoint,
        region="us-east-1",
        stack_options=minimal_stack_options,
    )

    async with limiter:
        yield limiter

    try:
        await limiter.delete_stack()
    except Exception as e:
        print(f"Warning: Module-scoped stack cleanup failed: {e}")


# LocalStack limiter fixtures (function-scoped)


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
