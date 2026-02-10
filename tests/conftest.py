"""Shared pytest configuration and fixtures.

Set GEVENT=1 to enable gevent monkey-patching before plugins import ssl/boto3.
Must be combined with -n 0 or -o 'addopts=' (xdist incompatible with patching).

Example:
    GEVENT=1 pytest tests/benchmark/test_aws.py --run-aws -o "addopts=" -v
"""

import os

if os.environ.get("GEVENT"):
    import gevent.monkey

    gevent.monkey.patch_all()

import pytest  # noqa: E402


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
