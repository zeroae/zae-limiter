"""Deploy and inspect pairs of stacks, on LocalStack or real AWS (#691).

The multi-stack isolation tests deploy two stacks into one account and region
and assert that an operation on one leaves the other's per-stack state alone:
its version record, its Lambda code, its tags and parameters. These helpers
are plain functions and context managers, not pytest fixtures, so a test
module composes them into class-scoped fixtures of its own.

Stacks are deployed through the CLI, exactly as ``test_upgrade_partial_stacks``
does, so a stack here is the one an operator would have. A :class:`Backend`
says where: LocalStack (an endpoint URL, no extra flags) or real AWS (no
endpoint URL, the PowerUser IAM flags from ``.claude/rules/aws-testing.md``).

Real AWS reads are eventually consistent unless asked otherwise, and GSIs are
always eventually consistent. :func:`set_stamp` therefore waits until a plain
read sees what it wrote, :func:`settled` does the same for a limit-config write
before anything resolves limits from it, and :func:`eventually` polls an
assertion that a GSI or a default read backs. On LocalStack all three succeed
on the first try.
"""

from __future__ import annotations

import asyncio
import time
import warnings
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar
from unittest.mock import patch

import boto3
from click.testing import CliRunner

from zae_limiter import Repository, schema
from zae_limiter.cli import cli
from zae_limiter.sync_repository import SyncRepository
from zae_limiter.version import get_schema_version

REGION = "us-east-1"
PROVISIONER = "limits-provisioner"
"""Suffix of a stack's provisioner function: ``{stack}-limits-provisioner``."""

POWER_USER_FLAGS = (
    "--permission-boundary",
    "arn:aws:iam::aws:policy/PowerUserAccess",
    "--role-name-format",
    "PowerUserPB-{}",
    "--policy-name-format",
    "PowerUserPB-{}",
)
"""What the PowerUser SSO profile needs to create the stack's IAM resources."""

T = TypeVar("T")


@dataclass(frozen=True)
class Backend:
    """Where a pair is deployed: LocalStack (``endpoint_url`` set) or real AWS (``None``)."""

    name: str
    endpoint_url: str | None
    deploy_flags: tuple[str, ...] = ()


def localstack_backend(endpoint_url: str) -> Backend:
    return Backend("localstack", endpoint_url)


def aws_backend() -> Backend:
    return Backend("aws", None, POWER_USER_FLAGS)


@dataclass(frozen=True)
class StackPair:
    """Two stacks deployed side by side in one account and region."""

    a: str
    b: str
    endpoint_url: str | None
    region: str = REGION


def where(stack: str, endpoint_url: str | None) -> list[str]:
    """CLI arguments naming ``stack``; no ``--endpoint-url`` on real AWS."""
    endpoint = [] if endpoint_url is None else ["--endpoint-url", endpoint_url]
    return ["--name", stack, *endpoint, "--region", REGION]


@contextmanager
def deployed_pair(
    base_name: str,
    backend: Backend,
    *,
    version: str,
) -> Iterator[StackPair]:
    """Deploy stacks ``{base_name}-a`` and ``{base_name}-b``; delete both on exit.

    Both are deployed *as* ``version`` (``zae_limiter.__version__`` patched for
    the deploy only), so their tags and version records start at a version the
    caller controls rather than at whatever the checkout happens to carry
    (#655). Only stacks named here are ever deleted: a failed deploy still
    attempts to delete its own half-built stack, and nothing else.

    Every stack is deployed ``--no-aggregator --no-alarms`` plus the backend's
    own flags (the PowerUser IAM flags on real AWS).

    A delete that fails is never ignored: on real AWS it is a leaked stack that
    keeps costing money. When the body completed, teardown fails naming each
    stack and the CLI's output. When the body (or a deploy) is already raising,
    the leak is reported as a warning instead, so the original failure is the
    one pytest shows.
    """
    endpoint_url = backend.endpoint_url
    pair = StackPair(f"{base_name}-a", f"{base_name}-b", endpoint_url)
    runner = CliRunner()
    body_raised = True
    try:
        for stack in (pair.a, pair.b):
            with patch("zae_limiter.__version__", version):
                result = runner.invoke(
                    cli,
                    [
                        "deploy",
                        *where(stack, endpoint_url),
                        "--no-aggregator",
                        "--no-alarms",
                        "--wait",
                        *backend.deploy_flags,
                    ],
                )
            assert result.exit_code == 0, f"Deploy of {stack} failed: {result.output}"
        yield pair
        body_raised = False
    finally:
        leaked = []
        for stack in (pair.a, pair.b):
            result = runner.invoke(cli, ["delete", *where(stack, endpoint_url), "--yes", "--wait"])
            if result.exit_code != 0:
                leaked.append(
                    f"{stack} (exit {result.exit_code}): {result.output.strip()}"
                    + (f" [{result.exception!r}]" if result.exception else "")
                )
        if leaked:
            message = "Stack delete failed; delete by hand:\n" + "\n".join(leaked)
            if body_raised:
                warnings.warn(message, stacklevel=2)
            else:
                raise AssertionError(message)


def sync_repo(stack: str, endpoint_url: str | None) -> SyncRepository:
    """A bare repository for reading and stamping a stack's version record."""
    return SyncRepository(stack, REGION, endpoint_url, _skip_deprecation_warning=True)


def set_stamp(stack: str, endpoint_url: str | None, lambda_version: str | None) -> None:
    """Overwrite ``lambda_version`` on the stack's version record (``None`` = unknown).

    Returns once a plain (eventually consistent) read sees the new value, since
    that is how ``open()`` and ``connect()`` read the record.
    """
    repo = sync_repo(stack, endpoint_url)
    try:
        repo.set_version_record(schema_version=get_schema_version(), lambda_version=lambda_version)
    finally:
        repo.close()
    wait_until_visible(
        stack, endpoint_url, lambda item: _string(item, "lambda_version") == lambda_version
    )


def _string(item: dict[str, Any], attribute: str) -> str | None:
    """A string attribute of a raw item; absent or ``NULL`` (an unknown stamp) reads as None."""
    value = item.get(attribute)
    return None if value is None else value.get("S")


def wait_until_visible(
    stack: str,
    endpoint_url: str | None,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    reads: int = 3,
    timeout: float = 30.0,
) -> None:
    """Wait until ``reads`` consecutive plain reads of ``#VERSION`` satisfy ``predicate``.

    Several in a row, because a single read only proves one replica caught up.
    """
    deadline = time.monotonic() + timeout
    seen = 0
    while True:
        if predicate(version_item(stack, endpoint_url, consistent=False)):
            seen += 1
            if seen >= reads:
                return
            continue
        seen = 0
        assert time.monotonic() < deadline, f"{stack}'s version record never settled"
        time.sleep(0.2)


def version_item(
    stack: str, endpoint_url: str | None, *, consistent: bool = True
) -> dict[str, Any]:
    """The stack's whole ``#VERSION`` item, raw, strongly consistent unless ``consistent=False``.

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
        ConsistentRead=consistent,
    )
    item = response.get("Item")
    assert item, f"{stack} has no version record"
    return item


def function_config(
    stack: str, endpoint_url: str | None, suffix: str = PROVISIONER
) -> dict[str, str]:
    """``CodeSha256`` and ``LastModified`` of ``{stack}-{suffix}``."""
    client = boto3.client("lambda", region_name=REGION, endpoint_url=endpoint_url)
    config = client.get_function_configuration(FunctionName=f"{stack}-{suffix}")
    return {"CodeSha256": config["CodeSha256"], "LastModified": config["LastModified"]}


def functions(stack: str, endpoint_url: str | None) -> set[str]:
    """Suffixes of every Lambda function named ``{stack}-*``."""
    client = boto3.client("lambda", region_name=REGION, endpoint_url=endpoint_url)
    names: set[str] = set()
    for page in client.get_paginator("list_functions").paginate():
        for function in page["Functions"]:
            if function["FunctionName"].startswith(f"{stack}-"):
                names.add(function["FunctionName"].removeprefix(f"{stack}-"))
    return names


def stack_shape(stack: str, endpoint_url: str | None) -> dict[str, Any]:
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


def snapshot(stack: str, endpoint_url: str | None) -> dict[str, Any]:
    """Everything per-stack that another stack's upgrade must not touch."""
    return {
        "version_item": version_item(stack, endpoint_url),
        "provisioner": function_config(stack, endpoint_url),
        "functions": functions(stack, endpoint_url),
        "stack": stack_shape(stack, endpoint_url),
    }


async def eventually(
    probe: Callable[[], Awaitable[T]],
    predicate: Callable[[T], bool],
    *,
    timeout: float = 30.0,
) -> T:
    """Poll ``probe`` until ``predicate`` holds and return that value.

    For reads real AWS serves eventually consistently: a GSI query
    (``get_buckets``, ``check_availability``), or a default ``GetItem`` /
    ``Query``. Asserting presence through one of them straight after a write is
    a flake on AWS; asserting absence needs no poll.

    This *is* the assertion: on timeout it raises ``AssertionError`` carrying
    the last value read, so a caller does not re-assert the predicate.
    """
    deadline = time.monotonic() + timeout
    while True:
        value = await probe()
        if predicate(value):
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"condition never held within {timeout}s; last value {value!r}")
        await asyncio.sleep(0.5)


async def settled(
    repo: Repository,
    read: Callable[[], Awaitable[T]],
    predicate: Callable[[T], bool],
    *,
    reads: int = 3,
    timeout: float = 30.0,
) -> T:
    """Wait until a limit-config write is visible, then drop ``repo``'s config cache.

    The limiter resolves limits with an eventually consistent ``BatchGetItem``
    (ADR-105). Straight after ``set_resource_defaults`` / ``set_limits`` on
    real AWS, that read can miss the item: the acquire then fails with
    ``ValidationError("No limits configured…")``, and the miss is cached for the
    repository's ``config_cache_ttl``. ``read`` is the matching plain getter
    (``get_resource_defaults``, ``get_limits``, ``get_system_defaults``); once
    ``reads`` consecutive calls satisfy ``predicate``, the write has reached the
    replicas, and the cache is invalidated so no miss taken meanwhile survives.

    Several reads in a row, because a single read only proves one replica caught
    up. Returns the last value read.
    """
    deadline = time.monotonic() + timeout
    seen = 0
    while True:
        value = await read()
        if predicate(value):
            seen += 1
            if seen >= reads:
                await repo.invalidate_config_cache()
                return value
            continue
        seen = 0
        assert time.monotonic() < deadline, f"config write never settled; last read {value!r}"
        await asyncio.sleep(0.2)
