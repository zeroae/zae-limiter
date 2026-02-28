"""Shared stack dataclass and lifecycle helpers.

Provides FileLock-based coordination so that xdist workers share
CloudFormation stacks instead of each creating their own.

Stack creation: First worker acquires FileLock and creates the stack;
other workers read the metadata from a shared JSON file.

Stack cleanup: ``cleanup_shared_stacks()`` runs via ``pytest_sessionfinish``
in the xdist controller (after all workers finish) or in the single
process when xdist is disabled.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from filelock import FileLock

if TYPE_CHECKING:
    from zae_limiter import StackOptions
    from zae_limiter.repository import Repository


@pytest.fixture(scope="session")
def minimal_stack_options() -> StackOptions:
    """Minimal stack - no aggregator, no alarms. Fastest deployment."""
    from zae_limiter import StackOptions

    return StackOptions(enable_aggregator=False, enable_alarms=False)


@pytest.fixture(scope="session")
def aggregator_stack_options() -> StackOptions:
    """Stack with aggregator Lambda but no CloudWatch alarms."""
    from zae_limiter import StackOptions

    return StackOptions(enable_aggregator=True, enable_alarms=False)


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

    builder = Repository.builder().stack(name).region(region)
    if endpoint_url:
        builder = builder.endpoint_url(endpoint_url)
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
    await repo.delete_stack()
    await repo.close()


async def get_or_create_shared_stack(
    tmp_path_factory: pytest.TempPathFactory,
    lock_name: str,
    endpoint_url: str,
    *,
    enable_aggregator: bool = False,
    enable_alarms: bool = False,
    snapshot_windows: str | None = None,
    usage_retention_days: int | None = None,
) -> SharedStack:
    """Get an existing shared stack or create one, coordinated via FileLock.

    With xdist, each worker has its own session scope. This function uses
    a FileLock so only the first worker creates the CloudFormation stack;
    other workers read the stack metadata from a shared JSON file.

    Cleanup is handled by ``cleanup_shared_stacks()`` via
    ``pytest_sessionfinish``, not by individual fixture teardown.
    """
    # tmp_path_factory.getbasetemp().parent is shared across all xdist workers
    root = tmp_path_factory.getbasetemp().parent
    lock_file = root / f"{lock_name}.lock"
    data_file = root / f"{lock_name}.json"

    with FileLock(str(lock_file)):
        if data_file.exists():
            return SharedStack(**json.loads(data_file.read_text()))

        # First worker — create the stack
        stack, repo = await create_shared_stack(
            lock_name,
            "us-east-1",
            endpoint_url=endpoint_url,
            enable_aggregator=enable_aggregator,
            enable_alarms=enable_alarms,
            snapshot_windows=snapshot_windows,
            usage_retention_days=usage_retention_days,
        )
        data_file.write_text(json.dumps(asdict(stack)))
        await repo.close()

    return stack


def cleanup_shared_stacks(tmp_root: Path) -> None:
    """Delete all shared stacks recorded in the tmp directory.

    Called from ``pytest_sessionfinish`` after all workers complete.
    Uses SyncRepository.delete_stack() since the hook is synchronous.
    """
    from zae_limiter.sync_repository import SyncRepository

    for data_file in sorted(tmp_root.glob("shared-*.json")):
        try:
            data = json.loads(data_file.read_text())
            stack = SharedStack(**data)
            repo = SyncRepository(
                name=stack.name,
                region=stack.region,
                endpoint_url=stack.endpoint_url,
            )
            repo.delete_stack()
            repo.close()
        except Exception as e:
            warnings.warn(f"cleanup of {data_file.stem} failed: {e}", ResourceWarning, stacklevel=2)


# Session-scoped shared stack fixtures


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_minimal_stack(localstack_endpoint, tmp_path_factory):
    """Session-scoped shared stack without aggregator or alarms."""
    return await get_or_create_shared_stack(
        tmp_path_factory,
        "shared-minimal",
        localstack_endpoint,
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def shared_aggregator_stack(localstack_endpoint, tmp_path_factory):
    """Session-scoped shared stack with aggregator Lambda."""
    return await get_or_create_shared_stack(
        tmp_path_factory,
        "shared-aggregator",
        localstack_endpoint,
        enable_aggregator=True,
        snapshot_windows="hourly",
        usage_retention_days=7,
    )
