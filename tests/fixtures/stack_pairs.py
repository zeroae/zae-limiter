"""Deploy and inspect pairs of stacks on LocalStack (#691).

The multi-stack isolation tests deploy two stacks into one account and region
and assert that an operation on one leaves the other's per-stack state alone:
its version record, its Lambda code, its tags and parameters. These helpers
are plain functions and context managers, not pytest fixtures, so a test
module composes them into class-scoped fixtures of its own.

Stacks are deployed through the CLI, exactly as ``test_upgrade_partial_stacks``
does, so a stack here is the one an operator would have.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import patch

import boto3
from click.testing import CliRunner

from zae_limiter import schema
from zae_limiter.cli import cli
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version

REGION = "us-east-1"
PROVISIONER = "limits-provisioner"
"""Suffix of a stack's provisioner function: ``{stack}-limits-provisioner``."""


@dataclass(frozen=True)
class StackPair:
    """Two stacks deployed side by side in one account and region."""

    a: str
    b: str
    endpoint_url: str
    region: str = REGION


def _where(stack: str, endpoint_url: str) -> list[str]:
    return ["--name", stack, "--endpoint-url", endpoint_url, "--region", REGION]


@contextmanager
def deployed_pair(
    base_name: str,
    endpoint_url: str,
    *,
    version: str,
    flags: tuple[str, ...] = ("--no-aggregator",),
) -> Iterator[StackPair]:
    """Deploy stacks ``{base_name}-a`` and ``{base_name}-b``; delete both on exit.

    Both are deployed *as* ``version`` (``zae_limiter.__version__`` patched for
    the deploy only), so their tags and version records start at a version the
    caller controls rather than at whatever the checkout happens to carry
    (#655). Only stacks named here are ever deleted: a failed deploy still
    attempts to delete its own half-built stack, and nothing else.
    """
    pair = StackPair(f"{base_name}-a", f"{base_name}-b", endpoint_url)
    runner = CliRunner()
    try:
        for stack in (pair.a, pair.b):
            with patch("zae_limiter.__version__", version):
                result = runner.invoke(
                    cli,
                    ["deploy", *_where(stack, endpoint_url), "--no-alarms", "--wait", *flags],
                )
            assert result.exit_code == 0, f"Deploy of {stack} failed: {result.output}"
        yield pair
    finally:
        for stack in (pair.a, pair.b):
            runner.invoke(cli, ["delete", *_where(stack, endpoint_url), "--yes", "--wait"])


def sync_repo(stack: str, endpoint_url: str) -> SyncRepository:
    """A bare repository for reading and stamping a stack's version record."""
    return SyncRepository(stack, REGION, endpoint_url, _skip_deprecation_warning=True)


def set_stamp(stack: str, endpoint_url: str, lambda_version: str | None) -> None:
    """Overwrite ``lambda_version`` on the stack's version record (``None`` = unknown)."""
    repo = sync_repo(stack, endpoint_url)
    try:
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=lambda_version)
    finally:
        repo.close()


def version_item(stack: str, endpoint_url: str) -> dict[str, Any]:
    """The stack's whole ``#VERSION`` item, raw, read strongly consistently.

    Compared whole so that *any* attribute drifting counts (``lambda_version``,
    ``client_min_version``, ``schema_version``, and ``updated_at`` /
    ``updated_by``, which a stray write would also move).
    """
    client = boto3.client("dynamodb", region_name=REGION, endpoint_url=endpoint_url)
    response = client.get_item(
        TableName=stack,
        Key={
            "PK": {"S": schema.pk_system(schema.RESERVED_NAMESPACE)},
            "SK": {"S": schema.sk_version()},
        },
        ConsistentRead=True,
    )
    item = response.get("Item")
    assert item, f"{stack} has no version record"
    return item


def function_config(stack: str, endpoint_url: str, suffix: str = PROVISIONER) -> dict[str, str]:
    """``CodeSha256`` and ``LastModified`` of ``{stack}-{suffix}``."""
    client = boto3.client("lambda", region_name=REGION, endpoint_url=endpoint_url)
    config = client.get_function_configuration(FunctionName=f"{stack}-{suffix}")
    return {"CodeSha256": config["CodeSha256"], "LastModified": config["LastModified"]}


def functions(stack: str, endpoint_url: str) -> set[str]:
    """Suffixes of every Lambda function named ``{stack}-*``."""
    client = boto3.client("lambda", region_name=REGION, endpoint_url=endpoint_url)
    names: set[str] = set()
    for page in client.get_paginator("list_functions").paginate():
        for function in page["Functions"]:
            if function["FunctionName"].startswith(f"{stack}-"):
                names.add(function["FunctionName"].removeprefix(f"{stack}-"))
    return names


def stack_shape(stack: str, endpoint_url: str) -> dict[str, Any]:
    """Status, tags and parameters of the CloudFormation stack."""
    client = boto3.client("cloudformation", region_name=REGION, endpoint_url=endpoint_url)
    described = client.describe_stacks(StackName=stack)["Stacks"][0]
    return {
        "status": described["StackStatus"],
        "tags": {tag["Key"]: tag["Value"] for tag in described.get("Tags", [])},
        "parameters": {
            p["ParameterKey"]: p.get("ParameterValue", "") for p in described.get("Parameters", [])
        },
    }


def snapshot(stack: str, endpoint_url: str) -> dict[str, Any]:
    """Everything per-stack that another stack's upgrade must not touch."""
    return {
        "version_item": version_item(stack, endpoint_url),
        "provisioner": function_config(stack, endpoint_url),
        "functions": functions(stack, endpoint_url),
        "stack": stack_shape(stack, endpoint_url),
    }
