"""Delete AWS e2e test stacks (``zci-*``) older than a cutoff (#189).

Every AWS test deletes its own stacks, but a killed run, a cancelled job or a
DynamoDB ``DeleteTable`` 500 during teardown leaves one behind. The e2e
workflow's cleanup step runs this with ``--older-than-days 0`` (safe there: runs
are serialized); the daily ``aws-e2e-reaper`` workflow runs it with a cutoff of
at least a day, so it can never delete a stack a live run still owns.

A stack in ``DELETE_FAILED`` is deleted again: the failures seen so far were
transient (a DynamoDB 500), and a second delete clears them. A stack whose
operation is in progress is left alone.

    uv run python scripts/reap_e2e_stacks.py --older-than-days 1 --dry-run
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
from botocore.exceptions import ClientError, WaiterError

DEFAULT_PREFIX = "zci-"
DEFAULT_REGION = "us-east-1"
DELETE_ATTEMPTS = 2
"""A first delete and one retry of a stack that ended in ``DELETE_FAILED``."""

_SETTLED = {
    "CREATE_COMPLETE",
    "CREATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "DELETE_FAILED",
    "UPDATE_COMPLETE",
    "UPDATE_FAILED",
    "UPDATE_ROLLBACK_COMPLETE",
    "UPDATE_ROLLBACK_FAILED",
    "IMPORT_COMPLETE",
    "IMPORT_ROLLBACK_COMPLETE",
    "IMPORT_ROLLBACK_FAILED",
}
"""Statuses a delete can start from. ``*_IN_PROGRESS`` stacks are left alone."""


@dataclass(frozen=True)
class Stack:
    name: str
    stack_id: str
    status: str
    created: datetime


def select(summaries: list[dict[str, Any]], prefix: str, cutoff: datetime) -> list[Stack]:
    """The stacks to delete: named ``prefix*``, created before ``cutoff``, not busy."""
    return [
        Stack(s["StackName"], s["StackId"], s["StackStatus"], s["CreationTime"])
        for s in summaries
        if s["StackName"].startswith(prefix)
        and s["StackStatus"] in _SETTLED
        and s["CreationTime"] < cutoff
    ]


def list_stacks(cfn: Any) -> list[dict[str, Any]]:
    pages = cfn.get_paginator("list_stacks").paginate(StackStatusFilter=sorted(_SETTLED))
    return [summary for page in pages for summary in page["StackSummaries"]]


def delete(cfn: Any, stack: Stack, attempts: int = DELETE_ATTEMPTS) -> bool:
    """Delete ``stack`` and wait for it to go; True once it has."""
    waiter = cfn.get_waiter("stack_delete_complete")
    for _ in range(attempts):
        try:
            cfn.delete_stack(StackName=stack.stack_id)
            waiter.wait(StackName=stack.stack_id, WaiterConfig={"Delay": 15, "MaxAttempts": 80})
            return True
        except (ClientError, WaiterError) as e:
            print(f"  delete of {stack.name} did not complete: {e}", file=sys.stderr)
    return False


def reap(cfn: Any, prefix: str, older_than: timedelta, now: datetime, dry_run: bool) -> int:
    """Delete every matching stack; the number that could not be deleted."""
    stacks = select(list_stacks(cfn), prefix, now - older_than)
    if not stacks:
        print(f"No {prefix}* stacks older than {older_than}.")
        return 0
    failed = 0
    for stack in stacks:
        age = now - stack.created
        print(
            f"{'Would delete' if dry_run else 'Deleting'} {stack.name} ({stack.status}, age {age})"
        )
        if not dry_run and not delete(cfn, stack):
            print(f"::error::Could not delete {stack.name}; delete it by hand")
            failed += 1
    return failed


def main(argv: list[str] | None = None, cfn: Any = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--older-than-days", type=float, required=True)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.older_than_days < 0:
        parser.error("--older-than-days must not be negative")
    if not args.prefix:
        parser.error("--prefix must not be empty")
    if cfn is None:
        cfn = boto3.client("cloudformation", region_name=args.region)
    failed = reap(
        cfn,
        args.prefix,
        timedelta(days=args.older_than_days),
        datetime.now(UTC),
        args.dry_run,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
