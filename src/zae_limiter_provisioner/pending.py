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
KINDS = ("disabled", "cascade", "soft", "sync")

# Concurrent single-item writes. Below botocore's default connection pool of
# 10, like `applier._READ_WORKERS`.
_WRITE_WORKERS = 8

#: The longest any Lambda invocation can run (AWS's hard limit, 15 minutes):
#: the lease when an apply cannot tell its own deadline.
MAX_RUN_MS = 900_000
#: Added to every lease, for clock skew between invocations.
LEASE_MARGIN_MS = 60_000


def now_ms() -> int:
    """Wall-clock epoch milliseconds, the clock every lease is read against."""
    return int(time.time() * 1000)


def deadline_for(remaining_ms: int | None) -> int:
    """The instant by which an apply that has ``remaining_ms`` left is certainly gone.

    An apply's pending items are leased to it until then (#739 re-review #2).
    ``remaining_ms`` is the Lambda's own remaining time at the start of the
    invocation, which is its configured timeout: derived from the deployed
    function rather than a constant, so a stack with a longer timeout gets a
    longer lease. Without it (a direct call), the hard limit of any Lambda.
    """
    run = remaining_ms if remaining_ms is not None and remaining_ms > 0 else MAX_RUN_MS
    return now_ms() + run + LEASE_MARGIN_MS


@dataclass(frozen=True)
class Pending:
    """One owed fan-out, and the token of the apply that holds it."""

    kind: str
    level: str
    target: str
    token: str | None = None
    lease_until_ms: int | None = None
    #: Written by a full apply (#739 re-review #3): only such an item, left
    #: by an interrupted run, makes the next full apply resume.
    full: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.level, self.target)

    def claimable(self, at_ms: int) -> bool:
        """Whether its owner is certainly gone, so another apply may take it."""
        return self.lease_until_ms is None or self.lease_until_ms < at_ms


def any_full_claimable(items: Iterable[Pending]) -> bool:
    """Whether an interrupted full apply left work that may be taken over now."""
    at = now_ms()
    return any(item.full and item.claimable(at) for item in items)


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
                        lease_until_ms=(
                            int(item["lease_until_ms"]["N"]) if "lease_until_ms" in item else None
                        ),
                        full=bool(item.get("full", {}).get("BOOL", False)),
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
    lease_until_ms: int | None = None,
    full: bool = False,
) -> set[tuple[str, str, str]]:
    """Record fan-outs this apply is about to owe, under its own token and lease.

    ``full`` marks the items of a full apply, so an interrupted one is
    resumed by the next full apply and no other item is (``Pending.full``).

    ``lease_until_ms`` (``deadline_for``) is when this apply is certainly
    gone; until then no other apply may take the item (#739 re-review #2).
    An item already there is taken over only if its lease has run out: one
    still leased to a live apply stays that apply's, so neither can release
    the other's work, and this apply runs the fan-out without holding it.
    Raises on any other failure, before any config is written. Returns the
    keys this apply now holds.
    """
    keys = list(dict.fromkeys(owed))
    written = now_ms()
    lease = deadline_for(None) if lease_until_ms is None else lease_until_ms

    def _put(key: tuple[str, str, str]) -> bool:
        kind, level, target = key
        try:
            client.put_item(
                TableName=table_name,
                Item={
                    **_key(namespace_id, kind, level, target),
                    "GSI4PK": {"S": namespace_id},
                    # GSI4's sort key: an item without it is not in the index,
                    # and `purge_namespace` would never find it. PK, as the
                    # client's repository writes it (#739 review, finding 3).
                    "GSI4SK": {"S": pk_system(namespace_id)},
                    "kind": {"S": kind},
                    "level": {"S": level},
                    "target": {"S": target},
                    "token": {"S": token},
                    "written_ms": {"N": str(written)},
                    "lease_until_ms": {"N": str(lease)},
                    "full": {"BOOL": full},
                },
                ConditionExpression=(
                    "attribute_not_exists(PK) OR attribute_not_exists(#lease) "
                    "OR #lease < :now OR #tok = :me"
                ),
                ExpressionAttributeNames={"#lease": "lease_until_ms", "#tok": "token"},
                ExpressionAttributeValues={":now": {"N": str(written)}, ":me": {"S": token}},
            )
            return True
        except client.exceptions.ConditionalCheckFailedException:
            logger.debug("Pending %s is leased to a live apply; left to it", key)
            return False

    held = _parallel(_put, keys)
    return {key for key, ok in zip(keys, held, strict=True) if ok}


def claim(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    items: Iterable[Pending],
    lease_until_ms: int | None = None,
) -> list[Pending]:
    """Take over pending items an earlier apply left; return the ones claimed.

    Only an item whose lease has run out: its owner is certainly gone (#739
    re-review #2). Claiming one still leased let a concurrent apply read the
    old config twice, before the owner's config write, release the item, and
    lose the fan-out when the owner then died. Conditioned on the token read
    as well: an item another apply has claimed or removed since is left to it.
    """
    at = now_ms()
    candidates = [item for item in items if item.claimable(at)]
    lease = deadline_for(None) if lease_until_ms is None else lease_until_ms

    def _claim(item: Pending) -> bool:
        condition = "#tok = :old" if item.token is not None else "attribute_not_exists(#tok)"
        values: dict[str, Any] = {
            ":me": {"S": token},
            ":now": {"N": str(at)},
            ":lease": {"N": str(lease)},
        }
        if item.token is not None:
            values[":old"] = {"S": item.token}
        try:
            client.update_item(
                TableName=table_name,
                Key=_key(namespace_id, *item.key),
                UpdateExpression="SET #tok = :me, written_ms = :now, #lease = :lease",
                ConditionExpression=(
                    f"attribute_exists(PK) AND {condition} "
                    "AND (attribute_not_exists(#lease) OR #lease < :now)"
                ),
                ExpressionAttributeNames={"#tok": "token", "#lease": "lease_until_ms"},
                ExpressionAttributeValues=values,
            )
            return True
        except client.exceptions.ConditionalCheckFailedException:
            return False

    claimed = _parallel(_claim, candidates)
    return [item for item, ok in zip(candidates, claimed, strict=True) if ok]


def expire(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    keys: Iterable[tuple[str, str, str]],
) -> None:
    """End this apply's lease on items it still holds as it finishes.

    A fan-out that failed keeps its item for the next apply; the apply that
    held it is done, so the next one need not wait out the lease. Best effort:
    a failure only makes the next apply wait for the lease.
    """
    at = now_ms()

    def _expire(key: tuple[str, str, str]) -> None:
        try:
            client.update_item(
                TableName=table_name,
                Key=_key(namespace_id, *key),
                UpdateExpression="SET #lease = :now",
                ConditionExpression="#tok = :me",
                ExpressionAttributeNames={"#tok": "token", "#lease": "lease_until_ms"},
                ExpressionAttributeValues={":now": {"N": str(at - 1)}, ":me": {"S": token}},
            )
        except client.exceptions.ConditionalCheckFailedException:
            pass
        except Exception as e:
            logger.warning("Could not end the lease on pending %s: %s", key, e)

    _parallel(_expire, list(keys))


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
