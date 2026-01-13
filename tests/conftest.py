"""Shared pytest configuration and fixtures."""

import pytest


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
