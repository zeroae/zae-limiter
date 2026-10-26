"""Bucket fan-outs an apply owes, one DynamoDB item per (kind, level, target).

The provisioner applies config first and fans it out to live bucket items
afterwards, and an apply is change-only: a level whose stored config already
matches the manifest is skipped. A fan-out that never ran (a Lambda killed or
timed out after the config writes) or that failed would therefore never run
again, because the next apply finds the config in place. Each such fan-out is
recorded here **before** the config write that makes it necessary, and removed
only once it has landed.

One item per owed fan-out, beside the ``#PROVISIONER`` record::

    PK = {ns}/SYSTEM#
    SK = #PROVISIONER#PENDING#{kind}#{level}#{target}

``kind`` is ``disabled`` (ADR-125), ``cascade`` (ADR-146) or ``sync`` (the
#481 bucket param sync). Each item carries the ``token`` of the apply that
owns it. Every apply that takes a fan-out on writes its own token (a write
ahead, or a claim of an item an earlier apply left), and removes the item
with a delete conditioned on that token. So:

- an apply never removes work another apply has taken on since: a later
  writer's token fails the earlier one's delete, and the item stays for
  whoever owns it now (#739 review: one record holding every apply's pending
  list was overwritten by a concurrent apply's final write);
- progress is per level: an interrupted apply leaves exactly the fan-outs it
  had not finished, and the next apply resumes from there instead of
  starting again (#739 review: a full apply too long for the Lambda timeout
  never converged).

Items carry ``GSI4PK`` and ``GSI4SK`` (= PK) so ``purge_namespace`` removes them
with the namespace.
Every operation here is a single-item ``PutItem`` / ``UpdateItem`` /
``DeleteItem`` or a ``Query`` on the table, all of which the provisioner role
already grants on the table. Nothing on the acquire path reads these items.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from zae_limiter.schema import pk_system, sk_provisioner

logger = logging.getLogger(__name__)

PENDING_SK_PREFIX = f"{sk_provisioner()}#PENDING#"

#: The fan-outs a level can owe, in the order the handler runs them.
KINDS = ("disabled", "cascade", "sync")

# Concurrent single-item writes. Below botocore's default connection pool of
# 10, like `applier._READ_WORKERS`.
_WRITE_WORKERS = 8


@dataclass(frozen=True)
class Pending:
    """One owed fan-out, and the token of the apply that holds it."""

    kind: str
    level: str
    target: str
    token: str | None = None

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.level, self.target)


def new_token() -> str:
    """A token identifying one apply."""
    return uuid.uuid4().hex


def sk_pending(kind: str, level: str, target: str) -> str:
    """The sort key of one pending item."""
    return f"{PENDING_SK_PREFIX}{kind}#{level}#{target}"


def _key(namespace_id: str, kind: str, level: str, target: str) -> dict[str, Any]:
    return {
        "PK": {"S": pk_system(namespace_id)},
        "SK": {"S": sk_pending(kind, level, target)},
    }


def read_pending(client: Any, table_name: str, namespace_id: str) -> list[Pending]:
    """Every pending item in the namespace (strongly consistent, paginated)."""
    items: list[Pending] = []
    start_key: dict[str, Any] | None = None
    while True:
        params: dict[str, Any] = {
            "TableName": table_name,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
            "ExpressionAttributeValues": {
                ":pk": {"S": pk_system(namespace_id)},
                ":sk": {"S": PENDING_SK_PREFIX},
            },
            "ConsistentRead": True,
        }
        if start_key:
            params["ExclusiveStartKey"] = start_key
        response = client.query(**params)
        for item in response.get("Items", []):
            try:
                items.append(
                    Pending(
                        kind=item["kind"]["S"],
                        level=item["level"]["S"],
                        target=item["target"]["S"],
                        token=item.get("token", {}).get("S"),
                    )
                )
            except KeyError:
                logger.warning("Ignoring malformed pending item %s", item.get("SK"))
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            return items


def _parallel(fn: Any, args: list[Any]) -> list[Any]:
    if not args:
        return []
    with ThreadPoolExecutor(max_workers=min(_WRITE_WORKERS, len(args))) as pool:
        return list(pool.map(fn, args))


def write_ahead(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    owed: Iterable[tuple[str, str, str]],
) -> set[tuple[str, str, str]]:
    """Record fan-outs this apply is about to owe, under its own token.

    Unconditional: an item already there (another apply's, or one left by an
    interrupted one) changes owner, so whichever apply wrote last is the one
    whose delete removes it. Raises on failure, before any config is written.
    Returns the keys written.
    """
    keys = list(dict.fromkeys(owed))
    now_ms = int(time.time() * 1000)

    def _put(key: tuple[str, str, str]) -> None:
        kind, level, target = key
        client.put_item(
            TableName=table_name,
            Item={
                **_key(namespace_id, kind, level, target),
                "GSI4PK": {"S": namespace_id},
                # GSI4's sort key: an item without it is not in the index, and
                # `purge_namespace` would never find it. PK, as the client's
                # repository writes it (#739 review, finding 3).
                "GSI4SK": {"S": pk_system(namespace_id)},
                "kind": {"S": kind},
                "level": {"S": level},
                "target": {"S": target},
                "token": {"S": token},
                "written_ms": {"N": str(now_ms)},
            },
        )

    _parallel(_put, keys)
    return set(keys)


def claim(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    items: Iterable[Pending],
) -> list[Pending]:
    """Take over pending items an earlier apply left; return the ones claimed.

    Conditioned on the token read: an item another apply has claimed or
    removed since is left to it, and not retried here.
    """
    candidates = list(items)
    now_ms = int(time.time() * 1000)

    def _claim(item: Pending) -> bool:
        condition = "#tok = :old" if item.token is not None else "attribute_not_exists(#tok)"
        values: dict[str, Any] = {":me": {"S": token}, ":now": {"N": str(now_ms)}}
        if item.token is not None:
            values[":old"] = {"S": item.token}
        try:
            client.update_item(
                TableName=table_name,
                Key=_key(namespace_id, *item.key),
                UpdateExpression="SET #tok = :me, written_ms = :now",
                ConditionExpression=f"attribute_exists(PK) AND {condition}",
                ExpressionAttributeNames={"#tok": "token"},
                ExpressionAttributeValues=values,
            )
            return True
        except client.exceptions.ConditionalCheckFailedException:
            return False

    claimed = _parallel(_claim, candidates)
    return [item for item, ok in zip(candidates, claimed, strict=True) if ok]


def release(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    key: tuple[str, str, str],
) -> None:
    """Remove one pending item this apply holds, once its fan-out has landed.

    Conditioned on ``token``: if another apply has taken the item on since, it
    stays for that apply. A failure is logged, never raised: the fan-out
    landed, and an item left behind only costs the next apply a retry.
    """
    try:
        client.delete_item(
            TableName=table_name,
            Key=_key(namespace_id, *key),
            ConditionExpression="#tok = :me",
            ExpressionAttributeNames={"#tok": "token"},
            ExpressionAttributeValues={":me": {"S": token}},
        )
    except client.exceptions.ConditionalCheckFailedException:
        logger.debug("Pending %s was taken over by another apply; left to it", key)
    except Exception as e:
        logger.warning("Could not remove pending %s (retried next apply): %s", key, e)
