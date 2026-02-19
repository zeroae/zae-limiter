"""Repository and limiter factory helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

if TYPE_CHECKING:
    from zae_limiter import RateLimiter
    from zae_limiter.repository import Repository
    from zae_limiter.sync_repository import SyncRepository

    from .stacks import SharedStack


async def make_test_repo(stack: SharedStack, namespace: str) -> tuple[Repository, Repository]:
    """Create a namespace-scoped async Repository for a shared stack.

    Creates a new Repository on the caller's event loop, registers the
    namespace, and returns the scoped repo. The parent repo must be closed
    by the caller on teardown.

    Returns:
        (parent_repo, scoped_repo) — close parent_repo to release the client.
    """
    from zae_limiter.repository import Repository

    parent = await Repository.connect(
        name=stack.name,
        region=stack.region,
        endpoint_url=stack.endpoint_url,
    )
    await parent.register_namespace(namespace)
    scoped = await parent.namespace(namespace)
    return parent, scoped


async def make_test_limiter(stack: SharedStack, namespace: str) -> tuple[Repository, RateLimiter]:
    """Create a namespace-scoped async RateLimiter for a shared stack.

    Returns:
        (parent_repo, limiter) — close parent_repo to release the client.
    """
    from zae_limiter import RateLimiter

    parent, scoped = await make_test_repo(stack, namespace)
    limiter = RateLimiter(repository=scoped)
    return parent, limiter


def make_sync_test_repo(
    stack: SharedStack, namespace: str
) -> tuple[SyncRepository, SyncRepository]:
    """Create a namespace-scoped sync Repository for a shared stack.

    Returns:
        (parent_repo, scoped_repo) — close parent_repo to release the client.
    """
    from zae_limiter.sync_repository import SyncRepository

    parent = SyncRepository.connect(
        name=stack.name,
        region=stack.region,
        endpoint_url=stack.endpoint_url,
    )
    parent.register_namespace(namespace)
    scoped = parent.namespace(namespace)
    return parent, scoped


# Async fixtures


@pytest_asyncio.fixture
async def test_repo(shared_minimal_stack, unique_namespace):
    """Namespace-scoped async Repository on the shared minimal stack."""
    parent, scoped = await make_test_repo(shared_minimal_stack, unique_namespace)
    yield scoped
    await parent.close()


@pytest_asyncio.fixture
async def localstack_limiter(test_repo):
    """RateLimiter wrapping the namespace-scoped test_repo."""
    from zae_limiter import RateLimiter

    limiter = RateLimiter(repository=test_repo)
    async with limiter:
        yield limiter
