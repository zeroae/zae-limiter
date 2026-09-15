"""Shared stack dataclass and lifecycle helpers.

Provides FileLock-based coordination so that xdist workers share
CloudFormation stacks instead of each creating their own.

Stack creation: First worker acquires FileLock and creates the stack;
other workers read the metadata from a shared JSON file.

Stack cleanup: ``cleanup_shared_stacks()`` runs via ``pytest_sessionfinish``
in the xdist controller (after all workers finish) or in the single
process when xdist is disabled.

Every shared stack is named for the session that created it
(``shared-minimal-<session key>``). The lock and the metadata file coordinate
*within* one pytest session; the name is what keeps two **concurrent pytest
invocations** apart. See issue #577: with a fixed global name, a second
invocation silently adopted the first one's live stack (``ensure_infrastructure``
is idempotent, so creating it again is not an error) and then deleted it from
``pytest_sessionfinish`` while the first was still writing to the table.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
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


_XDIST_WORKER_DIR = re.compile(r"^popen-gw\d+$")
"""Name of the per-worker basetemp xdist creates under the controller's."""

OWNER_FILE = "zae-session-owner.pid"
"""Marker recording the pid of the pytest process that owns a session root."""


def session_root(basetemp: Path) -> Path:
    """Return the controller basetemp for a session, given any process's basetemp.

    xdist gives each worker ``<controller basetemp>/popen-gwN``; the controller
    (and a single-process run) gets ``<pytest-of-user>/pytest-NNNN``. Both map
    to the same directory, so every process in one pytest session agrees on it
    and no two concurrent sessions ever do.

    The previous code took ``getbasetemp().parent`` unconditionally. That is
    right in a worker and wrong everywhere else: without xdist it resolves to
    ``pytest-of-<user>``, which is shared with every other pytest run on the
    machine — and which ``pytest_sessionfinish`` never looked in, so a
    non-xdist run leaked its stack *and* left a metadata file for the next
    non-xdist run to adopt.
    """
    if _XDIST_WORKER_DIR.match(basetemp.name):
        return basetemp.parent
    return basetemp


def session_key(root: Path) -> str:
    """Short, stable identifier for one pytest session.

    Derived from the controller basetemp path, which every worker resolves to
    the same value (see :func:`session_root`) and which pytest guarantees is
    unique per live session. Hashing rather than reusing the ``pytest-NNNN``
    suffix keeps the key fixed-width and legal under ``--basetemp``, where the
    path is arbitrary.
    """
    return hashlib.sha256(str(root).encode()).hexdigest()[:8]


def session_stack_name(base: str, root: Path) -> str:
    """Namespace a shared-stack name to one pytest session.

    Eight hex characters plus a hyphen, so the longest base in use
    (``shared-aggregator``, 17) yields 26 — well inside the 55-character stack
    name limit enforced by ``zae_limiter.naming.validate_name``.
    """
    return f"{base}-{session_key(root)}"


def record_session_owner(root: Path) -> None:
    """Stamp ``root`` with this process's pid so orphans can be recognised."""
    root.mkdir(parents=True, exist_ok=True)
    (root / OWNER_FILE).write_text(str(os.getpid()))


def _owner_alive(root: Path) -> bool:
    """Whether the pytest session that owns ``root`` is still running.

    A missing or unreadable marker counts as dead: only the ownership check in
    :func:`_delete_recorded_stack` decides what may actually be deleted, and a
    pid that has been recycled reads as *alive*, which errs toward leaking a
    stack rather than deleting a live one.
    """
    try:
        pid = int((root / OWNER_FILE).read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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

    The CloudFormation stack is named ``<lock_name>-<session key>`` so a
    concurrent pytest invocation can neither adopt it nor delete it (#577).
    The lock and metadata files keep the plain ``lock_name`` spelling — they
    already live in a directory private to this session.

    Cleanup is handled by ``cleanup_shared_stacks()`` via
    ``pytest_sessionfinish``, not by individual fixture teardown.
    """
    # The controller basetemp is shared by every xdist worker and private to
    # this session; see session_root().
    root = session_root(tmp_path_factory.getbasetemp())
    lock_file = root / f"{lock_name}.lock"
    data_file = root / f"{lock_name}.json"

    with FileLock(str(lock_file)):
        if data_file.exists():
            return SharedStack(**json.loads(data_file.read_text()))

        # First worker — create the stack
        stack, repo = await create_shared_stack(
            session_stack_name(lock_name, root),
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


def _delete_recorded_stack(data_file: Path) -> bool:
    """Delete the stack recorded in ``data_file``; return whether it succeeded.

    Refuses any record whose stack name does not carry the session key of the
    directory holding it. That check is the ownership proof #577 was missing:
    it makes deleting another session's stack unrepresentable, and it leaves
    alone a metadata file written by a pre-#577 revision (an un-suffixed
    ``shared-minimal``), which may well belong to a concurrent run of that
    revision.
    """
    from zae_limiter.sync_repository import SyncRepository

    try:
        stack = SharedStack(**json.loads(data_file.read_text()))
    except Exception:
        return False
    if not stack.name.endswith(f"-{session_key(data_file.parent)}"):
        return False
    try:
        repo = SyncRepository(
            name=stack.name,
            region=stack.region,
            endpoint_url=stack.endpoint_url,
        )
        repo.delete_stack()
        repo.close()
    except Exception as e:
        warnings.warn(f"cleanup of {stack.name} failed: {e}", ResourceWarning, stacklevel=2)
        return False
    return True


def cleanup_shared_stacks(tmp_root: Path) -> None:
    """Delete this session's shared stacks.

    Called from ``pytest_sessionfinish`` after all workers complete.
    Uses SyncRepository.delete_stack() since the hook is synchronous.

    A record that fails to delete is deliberately left on disk so the next
    run's :func:`reap_orphan_stacks` retries it.
    """
    root = session_root(tmp_root)
    for data_file in sorted(root.glob("shared-*.json")):
        if _delete_recorded_stack(data_file):
            data_file.unlink(missing_ok=True)


def reap_orphan_stacks(root: Path) -> None:
    """Delete shared stacks left behind by sessions that never reached cleanup.

    Per-session stack names mean a run killed before ``pytest_sessionfinish``
    (Ctrl-C during a 15-minute integration suite is routine) leaks a stack that
    no later run would otherwise recognise as garbage. Sweeping by *age* would
    reintroduce exactly the #577 failure mode with a longer fuse, so liveness
    is decided by the owner pid instead: a peer session root is reaped only
    once its pytest process is gone, and only for stacks whose names carry that
    peer's own session key.

    Best effort. A killed run's stack survives until the next completed run
    starts, and vanishes for good if pytest garbage-collects the peer's
    basetemp first.
    """
    parent = root.parent
    if parent == root:
        return
    for peer in sorted(parent.glob("pytest-*")):
        if peer == root or not peer.is_dir():
            continue
        data_files = sorted(peer.glob("shared-*.json"))
        if not data_files or _owner_alive(peer):
            continue
        for data_file in data_files:
            if _delete_recorded_stack(data_file):
                data_file.unlink(missing_ok=True)


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
