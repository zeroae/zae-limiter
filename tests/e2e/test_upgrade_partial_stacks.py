"""Upgrading a stack that does not deploy every Lambda, on LocalStack (#644).

A stack deployed with ``--no-aggregator`` has no ``{stack}-aggregator``
function; ``--no-provisioner`` has no ``{stack}-limits-provisioner``; and
``--no-iam`` has neither (both need a role). Each is deployed for real, its
``lambda_version`` stamp is lowered to fake a library upgrade, and then both
upgrade paths must push to the functions that exist, skip the rest, and stamp
the client version:

- ``zae-limiter upgrade``
- ``Repository.open()`` with its default ``auto_update=True`` (the sync twin
  here, since the CLI runs its own event loop)

To run locally::

    zae-limiter local up
    export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \\
           AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
    uv run pytest tests/e2e/test_upgrade_partial_stacks.py -v
"""

import boto3
import pytest
from click.testing import CliRunner

from zae_limiter import __version__
from zae_limiter.cli import cli
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

OLD = "0.1.0"
"""A stamp below every client this test runs with: a faked version bump."""


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


def _set_stamp(stack: str, endpoint: str, lambda_version: str) -> None:
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


@pytest.mark.parametrize(
    ("flags", "present"),
    [
        pytest.param(["--no-aggregator"], {"limits-provisioner"}, id="no-aggregator"),
        pytest.param(["--no-provisioner"], {"aggregator"}, id="no-provisioner"),
        pytest.param(["--no-aggregator", "--no-iam"], set(), id="no-iam"),
    ],
)
def test_a_partial_stack_upgrades_after_a_version_bump(
    localstack_endpoint, unique_name, flags, present
):
    runner = CliRunner()
    where = ["--name", unique_name, "--endpoint-url", localstack_endpoint, "--region", "us-east-1"]
    try:
        result = runner.invoke(cli, ["deploy", *where, "--no-alarms", "--wait", *flags])
        assert result.exit_code == 0, f"Deploy failed: {result.output}"
        assert _functions(unique_name, localstack_endpoint) == present

        # zae-limiter upgrade. It connects through open(), which performs the
        # update itself; --force makes the CLI's own push steps run as well.
        _set_stamp(unique_name, localstack_endpoint, OLD)
        result = runner.invoke(cli, ["upgrade", *where, "--force"])
        assert result.exit_code == 0, f"Upgrade failed: {result.output}"
        assert "Upgrade complete" in result.output
        assert ("No aggregator Lambda on this stack, skipped" in result.output) is (
            "aggregator" not in present
        )
        assert ("No provisioner Lambda on this stack, skipped" in result.output) is (
            "limits-provisioner" not in present
        )
        assert _stamp(unique_name, localstack_endpoint) == __version__

        # Repository.open(auto_update=True)
        _set_stamp(unique_name, localstack_endpoint, OLD)
        repo = SyncRepository.open(
            stack=unique_name, region="us-east-1", endpoint_url=localstack_endpoint
        )
        repo.close()
        assert _stamp(unique_name, localstack_endpoint) == __version__
    finally:
        runner.invoke(cli, ["delete", *where, "--yes", "--wait"])
