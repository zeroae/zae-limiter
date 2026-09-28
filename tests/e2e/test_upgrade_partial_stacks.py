"""Upgrading a stack that does not deploy every Lambda, on LocalStack (#644).

A stack deployed with ``--no-aggregator`` has no ``{stack}-aggregator``
function; ``--no-provisioner`` has no ``{stack}-limits-provisioner``; and
``--no-iam`` has no provisioner, and no aggregator unless
``--aggregator-role-arn`` is given. Each shape is deployed for real and both
upgrade paths must push to the functions that exist, skip the rest, and stamp
the client version:

- ``zae-limiter upgrade``, from an **unknown** stamp: ``open()`` never updates
  on an unknown stamp, so the CLI's own push steps are what run, and no
  ``--force`` is needed.
- ``Repository.open()`` with its default ``auto_update=True``, from a stamp
  lowered to fake a library upgrade (the sync twin here, since the CLI runs its
  own event loop).

One class per shape: under ``--dist loadscope`` each class is a scheduling
unit, so the three stacks deploy on separate workers instead of in series.

To run locally::

    zae-limiter local up
    export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \\
           AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
    uv run pytest tests/e2e/test_upgrade_partial_stacks.py -v
"""

from unittest.mock import patch

import boto3
import pytest
from click.testing import CliRunner

from zae_limiter import __version__
from zae_limiter.cli import cli
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

CLIENT = "0.99.0"
"""The client version the ``open()`` leg runs as.

Pinned rather than read from ``__version__``: CI checks out without tags, so
hatch-vcs falls back to ``0.1.dev1+g...``, which ``version.parse_version``
rejects. ``check_compatibility`` then reports the client invalid, no Lambda
update is ever requested, and ``open()`` pushes nothing whatever the stamp.
Its major must match the schema's."""

OLD = "0.1.0"
"""A stamp below ``CLIENT``: a faked version bump."""


def _functions(stack: str, endpoint: str) -> set[str]:
    client = boto3.client("lambda", region_name="us-east-1", endpoint_url=endpoint)
    names: set[str] = set()
    for page in client.get_paginator("list_functions").paginate():
        for function in page["Functions"]:
            if function["FunctionName"].startswith(f"{stack}-"):
                names.add(function["FunctionName"].removeprefix(f"{stack}-"))
    return names


def _repo(stack: str, endpoint: str) -> SyncRepository:
    return SyncRepository(stack, "us-east-1", endpoint, _skip_deprecation_warning=True)


def _set_stamp(stack: str, endpoint: str, lambda_version: str | None) -> None:
    repo = _repo(stack, endpoint)
    try:
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=lambda_version)
    finally:
        repo.close()


def _stamp(stack: str, endpoint: str) -> str | None:
    repo = _repo(stack, endpoint)
    try:
        record = repo.get_version_record()
    finally:
        repo.close()
    assert record is not None
    return record.get("lambda_version")


def _upgrade_both_ways(endpoint: str, stack: str, flags: list[str], present: set[str]) -> None:
    runner = CliRunner()
    where = ["--name", stack, "--endpoint-url", endpoint, "--region", "us-east-1"]
    try:
        result = runner.invoke(cli, ["deploy", *where, "--no-alarms", "--wait", *flags])
        assert result.exit_code == 0, f"Deploy failed: {result.output}"
        assert _functions(stack, endpoint) == present

        # zae-limiter upgrade from an unknown stamp: open() leaves it alone,
        # so the CLI's own skip/push steps do the work.
        _set_stamp(stack, endpoint, None)
        result = runner.invoke(cli, ["upgrade", *where])
        assert result.exit_code == 0, f"Upgrade failed: {result.output}"
        assert "Upgrade complete" in result.output
        assert ("No aggregator Lambda on this stack, skipped" in result.output) is (
            "aggregator" not in present
        )
        assert ("No provisioner Lambda on this stack, skipped" in result.output) is (
            "limits-provisioner" not in present
        )
        assert _stamp(stack, endpoint) == __version__

        # Repository.open(auto_update=True) after a faked version bump
        _set_stamp(stack, endpoint, OLD)
        with patch("zae_limiter.__version__", CLIENT):
            repo = SyncRepository.open(stack=stack, region="us-east-1", endpoint_url=endpoint)
            repo.close()
        assert _stamp(stack, endpoint) == CLIENT
    finally:
        runner.invoke(cli, ["delete", *where, "--yes", "--wait"])


class TestNoAggregator:
    def test_upgrades_after_a_version_bump(self, localstack_endpoint, unique_name):
        _upgrade_both_ways(
            localstack_endpoint, unique_name, ["--no-aggregator"], {"limits-provisioner"}
        )


class TestNoProvisioner:
    def test_upgrades_after_a_version_bump(self, localstack_endpoint, unique_name):
        _upgrade_both_ways(localstack_endpoint, unique_name, ["--no-provisioner"], {"aggregator"})


class TestNoIam:
    def test_upgrades_after_a_version_bump(self, localstack_endpoint, unique_name):
        _upgrade_both_ways(localstack_endpoint, unique_name, ["--no-aggregator", "--no-iam"], set())
