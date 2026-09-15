"""Shared pytest configuration and fixtures.

Set GEVENT=1 to enable gevent monkey-patching before plugins import ssl/boto3.
Must be combined with -n 0 or -o 'addopts=' (xdist incompatible with patching).

Example:
    GEVENT=1 pytest tests/benchmark/test_aws.py --run-aws -o "addopts=" -v
"""

import os
from pathlib import Path

if os.environ.get("GEVENT"):
    import gevent.monkey

    gevent.monkey.patch_all()

import pytest  # noqa: E402

from tests.fixtures.stacks import (  # noqa: E402
    cleanup_shared_stacks,
    reap_orphan_stacks,
    record_session_owner,
    session_root,
)

pytest_plugins = [
    "tests.fixtures.aws_clients",
    "tests.fixtures.capacity",
    "tests.fixtures.moto",
    "tests.fixtures.names",
    "tests.fixtures.repositories",
    "tests.fixtures.stacks",
]


def pytest_addoption(parser):
    """Add --run-aws pytest option."""
    parser.addoption(
        "--run-aws",
        action="store_true",
        default=False,
        help="Run tests against real AWS (requires valid credentials)",
    )


def pytest_ignore_collect(collection_path, config):
    """Skip gevent-marked test files when xdist is active.

    Gevent monkey-patching at import time is incompatible with xdist workers.
    This hook prevents collection (and therefore import) of these files.
    Run with -n 0 to include them.
    """
    if not collection_path.suffix == ".py":
        return None
    # Controller has numprocesses > 0; workers have workerinput attribute.
    # Both must skip gevent files to prevent import during collection.
    try:
        numprocesses = config.getoption("numprocesses")
    except (ValueError, AttributeError):
        numprocesses = None
    is_xdist_worker = hasattr(config, "workerinput")
    if not numprocesses and not is_xdist_worker:
        return None
    try:
        source = collection_path.read_text()
    except (OSError, UnicodeDecodeError):
        return None
    if "pytestmark = pytest.mark.gevent" in source:
        return True
    return None


def pytest_collection_modifyitems(config, items):
    """Skip AWS tests unless --run-aws flag is provided."""
    if not config.getoption("--run-aws"):
        skip_aws = pytest.mark.skip(reason="Need --run-aws option to run")
        for item in items:
            if "aws" in item.keywords:
                item.add_marker(skip_aws)


def _session_root(config) -> Path:
    """This session's controller basetemp (see ``tests.fixtures.stacks``)."""
    return session_root(Path(config._tmp_path_factory.getbasetemp()))


def pytest_sessionstart(session):
    """Claim this session's tmp root and reclaim stacks from dead sessions.

    The pid marker is what lets a later run tell a killed session's leftover
    stack from a concurrently running session's live one (#577). Reaping is
    gated on ``AWS_ENDPOINT_URL`` because shared stacks only ever exist on
    LocalStack — the ``localstack_endpoint`` fixture skips otherwise — so a
    plain unit run must not try to reach CloudFormation.
    """
    if hasattr(session.config, "workerinput"):
        return

    root = _session_root(session.config)
    record_session_owner(root)
    if os.getenv("AWS_ENDPOINT_URL"):
        reap_orphan_stacks(root)


def pytest_sessionfinish(session, exitstatus):
    """Clean up shared CloudFormation stacks after all xdist workers finish.

    Runs in the xdist controller (after all workers complete) or in the
    single process when xdist is disabled. Workers skip cleanup since
    they don't have the controller's tmp directory.
    """
    # Only run in the controller or single-process mode (not in workers)
    if hasattr(session.config, "workerinput"):
        return

    cleanup_shared_stacks(_session_root(session.config))
