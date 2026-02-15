"""Shared stack dataclass and lifecycle helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from zae_limiter.repository import Repository


@pytest.fixture(scope="session")
def localstack_endpoint():
    """LocalStack endpoint URL from environment."""
    endpoint = os.getenv("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set - LocalStack not available")
    return endpoint


@dataclass(frozen=True)
class SharedStack:
    """Metadata for a session-scoped shared CloudFormation stack.

    Contains only connection metadata — no active Repository or client.
    Each consumer creates its own Repository on its own event loop.
    """

    name: str
    region: str
    endpoint_url: str | None


async def create_shared_stack(
    name: str,
    region: str,
    endpoint_url: str | None = None,
    *,
    enable_aggregator: bool = False,
    enable_alarms: bool = False,
    snapshot_windows: str | None = None,
    usage_retention_days: int | None = None,
) -> tuple[SharedStack, Repository]:
    """Create a shared CloudFormation stack using the async RepositoryBuilder.

    Returns both the SharedStack metadata and the live Repository instance.
    The caller (session fixture) uses the Repository for teardown.
    """
    from zae_limiter.repository import Repository

    builder = Repository.builder(name, region, endpoint_url=endpoint_url)
    builder = builder.enable_aggregator(enable_aggregator).enable_alarms(enable_alarms)

    if snapshot_windows is not None:
        builder = builder.snapshot_windows(snapshot_windows)
    if usage_retention_days is not None:
        builder = builder.usage_retention_days(usage_retention_days)

    repo = await builder.namespace("default").build()

    stack = SharedStack(name=name, region=region, endpoint_url=endpoint_url)
    return stack, repo


async def destroy_shared_stack(repo: Repository) -> None:
    """Delete the CloudFormation stack and close the Repository."""
    try:
        await repo.delete_stack()
    except Exception as e:
        print(f"Warning: Stack cleanup failed: {e}")
    await repo.close()
