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

``upgrade`` must also bring the stack's version tags up to date (a stack
deployed by an older release keeps its creation-time tags otherwise) without
dropping a user tag or resetting any stack parameter to its template default,
and ``open()`` must leave the tags alone. LocalStack cannot show the tags
propagating to the table and functions; the real-AWS run covers that.

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

Pinned rather than read from ``__version__`` for a version this test controls
directly: CI now fetches tags (#655), and ``version.parse_version`` accepts
the tagless fallback form (``0.1.dev1+g...`` reads as ``0.1.0-dev``) either
way, so the ambient ``__version__`` is a real, comparable version regardless
— just not one this test can predict or bump on demand. The test needs to
push each stamp (``OLD`` below) forward by a known, controlled amount to
exercise the update paths, which pinning ``CLIENT`` gives it independent of
whatever tag or dev-distance the checkout happens to carry. Its major must
match the schema's."""

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


USER_TAG = ("team", "platform")
"""A user tag set at deploy time, which the upgrade's tag refresh must keep."""


def _stack(stack: str, endpoint: str) -> dict:
    client = boto3.client("cloudformation", region_name="us-east-1", endpoint_url=endpoint)
    return client.describe_stacks(StackName=stack)["Stacks"][0]


def _tags(stack: dict) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in stack.get("Tags", [])}


def _parameters(stack: dict) -> dict[str, str]:
    return {p["ParameterKey"]: p.get("ParameterValue", "") for p in stack.get("Parameters", [])}


def _upgrade_both_ways(endpoint: str, stack: str, flags: list[str], present: set[str]) -> None:
    runner = CliRunner()
    where = ["--name", stack, "--endpoint-url", endpoint, "--region", "us-east-1"]
    try:
        # Deployed by an "older release": the stack's version tags carry OLD.
        tag = ["--tag", "=".join(USER_TAG)]
        with patch("zae_limiter.__version__", OLD):
            result = runner.invoke(cli, ["deploy", *where, "--no-alarms", "--wait", *tag, *flags])
        assert result.exit_code == 0, f"Deploy failed: {result.output}"
        assert _functions(stack, endpoint) == present
        deployed = _stack(stack, endpoint)
        assert _tags(deployed)["zae-limiter:version"] == OLD
        parameters = _parameters(deployed)

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

        # ... and refreshed the stack's version tags, keeping everything else.
        assert "Stack tags updated" in result.output, result.output
        assert "Tag update failed" not in result.output, result.output
        upgraded = _stack(stack, endpoint)
        assert upgraded["StackStatus"] == "UPDATE_COMPLETE"
        tags = _tags(upgraded)
        assert tags["zae-limiter:version"] == __version__
        assert tags["zae-limiter:schema-version"] == get_schema_version()
        # Every function was pushed or proven absent, so the claim is earned.
        assert tags["zae-limiter:lambda-version"] == __version__
        assert tags[USER_TAG[0]] == USER_TAG[1]
        assert tags["ManagedBy"] == "zae-limiter"
        assert _parameters(upgraded) == parameters

        # Repository.open(auto_update=True) after a faked version bump
        _set_stamp(stack, endpoint, OLD)
        with patch("zae_limiter.__version__", CLIENT):
            repo = SyncRepository.open(stack=stack, region="us-east-1", endpoint_url=endpoint)
            repo.close()
        assert _stamp(stack, endpoint) == CLIENT
        # Application code never updates the stack: its tags are as upgrade left them.
        assert _tags(_stack(stack, endpoint)) == tags
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
