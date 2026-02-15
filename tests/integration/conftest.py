"""Integration test fixtures for LocalStack."""

import uuid

import pytest
import pytest_asyncio

from tests.fixtures.names import unique_name, unique_name_class  # noqa: F401
from tests.fixtures.repositories import make_test_repo
from tests.fixtures.stacks import (  # noqa: F401
    SharedStack,
    create_shared_stack,
    destroy_shared_stack,
    localstack_endpoint,
)
from zae_limiter import RateLimiter, StackOptions


@pytest.fixture(scope="session")
def minimal_stack_options():
    """Minimal stack - no aggregator, no alarms. Fastest deployment."""
    return StackOptions(enable_aggregator=False, enable_alarms=False)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_minimal_stack(localstack_endpoint):  # noqa: F811
    """Session-scoped shared stack without aggregator or alarms."""
    name = f"int-minimal-{uuid.uuid4().hex[:8]}"
    stack, repo = await create_shared_stack(
        name,
        "us-east-1",
        endpoint_url=localstack_endpoint,
        enable_aggregator=False,
        enable_alarms=False,
    )
    yield stack
    await destroy_shared_stack(repo)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_aggregator_stack(localstack_endpoint):  # noqa: F811
    """Session-scoped shared stack with aggregator Lambda."""
    name = f"int-aggr-{uuid.uuid4().hex[:8]}"
    stack, repo = await create_shared_stack(
        name,
        "us-east-1",
        endpoint_url=localstack_endpoint,
        enable_aggregator=True,
        enable_alarms=False,
    )
    yield stack
    await destroy_shared_stack(repo)


@pytest_asyncio.fixture
async def test_repo(shared_minimal_stack, unique_namespace):
    """Namespace-scoped async Repository on the shared minimal stack."""
    parent, scoped = await make_test_repo(shared_minimal_stack, unique_namespace)
    yield scoped
    await parent.close()


@pytest_asyncio.fixture
async def localstack_limiter(test_repo):
    """RateLimiter wrapping the namespace-scoped test_repo."""
    limiter = RateLimiter(repository=test_repo)
    async with limiter:
        yield limiter


@pytest.fixture
def unique_namespace():
    """Generate unique namespace name for per-test data isolation."""
    return f"ns-{uuid.uuid4().hex[:8]}"
