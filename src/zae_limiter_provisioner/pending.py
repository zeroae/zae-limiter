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

``kind`` is ``disabled`` (ADR-125), ``cascade`` (ADR-146), ``soft`` (#467) or
``sync`` (the #481 bucket param sync).

**An item has many owners** (#739 re-review 2 #1). ``owners`` maps the token of
every apply that took the fan-out on to its lease (``lease``: the instant by
which that apply is certainly gone) and whether it was a full apply
(``full``). Two applies changing the same level each owe its fan-out, and the
obligation lives until **every** one has finished its own:

- ``write_ahead`` adds this apply's entry; another owner never refuses it;
- ``release`` removes only this apply's entry, and deletes the item only once
  no entry is left;
- ``claim`` takes over only entries whose lease has run out (their apply is
  certainly gone), replacing them with this apply's;
- ``expire`` ends this apply's lease as it finishes with fan-outs it could not
  complete, so the next apply claims them at once.

With one owner per item, an apply refused another's live item fanned out
holding nothing: when the other released, the obligation was gone, and a
config this apply wrote and then died with was never fanned out.

An item written by an earlier build of this branch carries a single ``token``
/ ``lease_until_ms`` / ``full`` instead of ``owners``; it is read as that one
owner and cleaned up by the same operations.

Items carry ``GSI4PK`` and ``GSI4SK`` (= PK) so ``purge_namespace`` removes
them with the namespace. Every operation here is a single-item ``UpdateItem``
/ ``DeleteItem`` or a ``Query`` on the table, all of which the provisioner role
already grants on the table. Nothing on the acquire path reads these items.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
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

_OWNERS = "owners"
_LEASE = "lease"
_FULL = "full"
# The single-owner shape an earlier build of this branch wrote.
_LEGACY_TOKEN = "token"
_LEGACY_LEASE = "lease_until_ms"
_LEGACY_FULL = "full"


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
class Owner:
    """One apply's share of a pending item."""

    #: None for a lease never recorded (read as already run out).
    lease_until_ms: int | None = None
    #: Taken on by a full apply that may still resume it (#739 re-review #3).
    full: bool = False
    #: Stored in the single-owner shape of an earlier build.
    legacy: bool = False

    def expired(self, at_ms: int) -> bool:
        return self.lease_until_ms is None or self.lease_until_ms < at_ms


@dataclass(frozen=True)
class Pending:
    """One owed fan-out, and every apply that holds a share of it."""

    kind: str
    level: str
    target: str
    owners: dict[str, Owner] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.kind, self.level, self.target)

    def expired_tokens(self, at_ms: int) -> list[str]:
        """The owners whose lease has run out: their applies are certainly gone."""
        return sorted(token for token, owner in self.owners.items() if owner.expired(at_ms))

    def claimable(self, at_ms: int) -> bool:
        """Whether another apply may take a share now: an owner is gone, or none is left."""
        return not self.owners or bool(self.expired_tokens(at_ms))


def any_full_claimable(items: Iterable[Pending]) -> bool:
    """Whether an interrupted full apply left work that may be taken over now."""
    at = now_ms()
    return any(owner.full and owner.expired(at) for item in items for owner in item.owners.values())


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


def _owners_of(item: dict[str, Any]) -> dict[str, Owner]:
    """Every owner an item records, the single-owner shape included."""
    owners: dict[str, Owner] = {}
    for token, entry in item.get(_OWNERS, {}).get("M", {}).items():
        fields = entry.get("M", {})
        owners[token] = Owner(
            lease_until_ms=int(fields[_LEASE]["N"]) if _LEASE in fields else None,
            full=bool(fields.get(_FULL, {}).get("BOOL", False)),
        )
    legacy = item.get(_LEGACY_TOKEN, {}).get("S")
    if legacy is not None:
        owners[legacy] = Owner(
            lease_until_ms=(int(item[_LEGACY_LEASE]["N"]) if _LEGACY_LEASE in item else None),
            full=bool(item.get(_LEGACY_FULL, {}).get("BOOL", False)),
            legacy=True,
        )
    return owners


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
                        owners=_owners_of(item),
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


def _entry(lease_until_ms: int, full: bool) -> dict[str, Any]:
    return {"M": {_LEASE: {"N": str(lease_until_ms)}, _FULL: {"BOOL": full}}}


def write_ahead(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    owed: Iterable[tuple[str, str, str]],
    lease_until_ms: int | None = None,
    full: bool = False,
) -> set[tuple[str, str, str]]:
    """Take on fan-outs this apply is about to owe: add its share to each item.

    ``lease_until_ms`` (``deadline_for``) is when this apply is certainly
    gone. ``full`` marks a full apply's share, so an interrupted one is resumed
    by the next full apply and no other item is. Another owner never refuses
    the write (#739 re-review 2 #1): the item then has both, and stays until
    both have finished. Raises on failure, before any config is written.
    Returns the keys this apply now holds.
    """
    keys = list(dict.fromkeys(owed))
    written = now_ms()
    lease = deadline_for(None) if lease_until_ms is None else lease_until_ms
    entry = _entry(lease, full)

    def _put(key: tuple[str, str, str]) -> None:
        kind, level, target = key
        meta = {
            ":ns": {"S": namespace_id},
            # GSI4's sort key: an item without it is not in the index, and
            # `purge_namespace` would never find it. PK, as the client's
            # repository writes it (#739 review, finding 3).
            ":g4sk": {"S": pk_system(namespace_id)},
            ":kind": {"S": kind},
            ":level": {"S": level},
            ":target": {"S": target},
            ":written": {"N": str(written)},
        }
        sets = (
            "GSI4PK = :ns, GSI4SK = :g4sk, kind = :kind, #lvl = :level, "
            "#tgt = :target, written_ms = :written"
        )
        names = {"#o": _OWNERS, "#lvl": "level", "#tgt": "target"}
        # Create the map with this apply's entry, or add the entry to the map
        # another owner made. The two cannot be one expression (a path and its
        # parent overlap), and each is conditioned on the map's existence, so
        # whichever races loses only to the other and is re-sent. Creating
        # first: an item nobody else holds, the usual case, costs one write.
        for _attempt in range(2):
            try:
                client.update_item(
                    TableName=table_name,
                    Key=_key(namespace_id, kind, level, target),
                    UpdateExpression=f"SET #o = :m, {sets}",
                    ConditionExpression="attribute_not_exists(#o)",
                    ExpressionAttributeNames=names,
                    ExpressionAttributeValues={**meta, ":m": {"M": {token: entry}}},
                )
                return
            except client.exceptions.ConditionalCheckFailedException:
                pass
            try:
                client.update_item(
                    TableName=table_name,
                    Key=_key(namespace_id, kind, level, target),
                    UpdateExpression=f"SET #o.#me = :e, {sets}",
                    ConditionExpression="attribute_exists(#o)",
                    ExpressionAttributeNames={**names, "#me": token},
                    ExpressionAttributeValues={**meta, ":e": entry},
                )
                return
            except client.exceptions.ConditionalCheckFailedException:
                continue
        raise RuntimeError(f"could not record pending fan-out {key}")

    _parallel(_put, keys)
    return set(keys)


def claim(
    client: Any,
    table_name: str,
    namespace_id: str,
    token: str,
    items: Iterable[Pending],
    lease_until_ms: int | None = None,
    full: bool = False,
) -> list[Pending]:
    """Take over the shares of applies that are certainly gone; return the items claimed.

    Only owners whose lease has run out (#739 re-review #2): claiming a share
    still leased let a concurrent apply read the old config twice, before the
    owner's config write, and release the work. In one conditional update the
    expired owners are replaced by this apply, conditioned on each still being
    there and expired, so two claimers cannot both take one share. An item with
    no owner left at all is claimable too. Live owners keep their share.
    """
    at = now_ms()
    candidates = [item for item in items if item.claimable(at)]
    lease = deadline_for(None) if lease_until_ms is None else lease_until_ms
    entry = _entry(lease, full)

    def _claim(item: Pending) -> bool:
        expired = item.expired_tokens(at)
        names: dict[str, str] = {"#o": _OWNERS}
        values: dict[str, Any] = {":now": {"N": str(at)}}
        conditions = ["attribute_exists(PK)"]
        removes: list[str] = []
        has_map = any(not owner.legacy for owner in item.owners.values())
        for i, tok in enumerate(expired):
            owner = item.owners[tok]
            if owner.legacy:
                names.update({"#ltok": _LEGACY_TOKEN, "#llease": _LEGACY_LEASE})
                values[":ltok"] = {"S": tok}
                conditions.append("#ltok = :ltok")
                conditions.append("(attribute_not_exists(#llease) OR #llease < :now)")
                removes += ["#ltok", "#llease", f"#{_LEGACY_FULL}x"]
                names[f"#{_LEGACY_FULL}x"] = _LEGACY_FULL
                continue
            names[f"#t{i}"] = tok
            names["#l"] = _LEASE
            conditions.append(
                f"attribute_exists(#o.#t{i}) "
                f"AND (attribute_not_exists(#o.#t{i}.#l) OR #o.#t{i}.#l < :now)"
            )
            removes.append(f"#o.#t{i}")
        if has_map or (not item.owners and not expired):
            names["#me"] = token
            sets = "#o.#me = :e"
            values[":e"] = entry
            conditions.append("attribute_exists(#o)")
            if not item.owners:
                values[":z"] = {"N": "0"}
                conditions.append("size(#o) = :z")
        else:
            sets = "#o = :m"
            values[":m"] = {"M": {token: entry}}
            conditions.append("attribute_not_exists(#o)")
        if not expired:
            del values[":now"]  # an item with no owner left tests no lease
        expression = f"SET {sets}" + (f" REMOVE {', '.join(removes)}" if removes else "")
        try:
            client.update_item(
                TableName=table_name,
                Key=_key(namespace_id, *item.key),
                UpdateExpression=expression,
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
            return True
        except client.exceptions.ConditionalCheckFailedException as e:
            # A re-send of a claim whose first send landed fails on the token
            # it has just replaced (#739 re-review 2 #3): the item already
            # names this apply, so it is claimed. Otherwise it was taken or
            # removed since it was read, and is left alone.
            current = e.response.get("Item") if isinstance(e.response, dict) else None
            return token in _owners_of(current or {})

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

    A fan-out that failed keeps its share for the next apply; the apply that
    held it is done, so the next one need not wait out the lease. The share's
    ``full`` mark is cleared too (#739 re-review 2 #2): a full apply that
    finished, failures and all, is not an interrupted one, and a resumable
    share would make every later full apply change-only. Only an apply killed
    mid-run, which never gets here, leaves a share a full apply resumes. Best
    effort: a failure only makes the next apply wait for the lease.
    """
    at = now_ms()

    def _expire(key: tuple[str, str, str]) -> None:
        past = {":past": {"N": str(at - 1)}, ":no": {"BOOL": False}}
        try:
            client.update_item(
                TableName=table_name,
                Key=_key(namespace_id, *key),
                UpdateExpression="SET #o.#me.#l = :past, #o.#me.#f = :no",
                ConditionExpression="attribute_exists(#o.#me)",
                ExpressionAttributeNames={"#o": _OWNERS, "#me": token, "#l": _LEASE, "#f": _FULL},
                ExpressionAttributeValues=past,
            )
            return
        except client.exceptions.ConditionalCheckFailedException:
            pass
        except Exception as e:
            logger.warning("Could not end the lease on pending %s: %s", key, e)
            return
        try:
            client.update_item(
                TableName=table_name,
                Key=_key(namespace_id, *key),
                UpdateExpression="SET #llease = :past, #lfull = :no",
                ConditionExpression="#ltok = :me",
                ExpressionAttributeNames={
                    "#ltok": _LEGACY_TOKEN,
                    "#llease": _LEGACY_LEASE,
                    "#lfull": _LEGACY_FULL,
                },
                ExpressionAttributeValues={**past, ":me": {"S": token}},
            )
        except client.exceptions.ConditionalCheckFailedException:
            logger.debug("Pending %s is no longer leased to this apply; left alone", key)
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
    """Remove this apply's share once its fan-out has landed; the item once none is left.

    Another apply's share keeps the item (#739 re-review 2 #1): its fan-out is
    still owed. The delete is conditioned on no share being left, so an owner
    added in between keeps it too. A failure is logged, never raised: the
    fan-out landed, and a share left behind only costs a later apply a retry.
    """
    item_key = _key(namespace_id, *key)
    try:
        try:
            response = client.update_item(
                TableName=table_name,
                Key=item_key,
                UpdateExpression="REMOVE #o.#me",
                ConditionExpression="attribute_exists(#o.#me)",
                ExpressionAttributeNames={"#o": _OWNERS, "#me": token},
                ReturnValues="ALL_NEW",
            )
        except client.exceptions.ConditionalCheckFailedException:
            # Not in the map: an item of the single-owner shape, or no share.
            response = client.update_item(
                TableName=table_name,
                Key=item_key,
                UpdateExpression="REMOVE #ltok, #llease, #lfull",
                ConditionExpression="#ltok = :me",
                ExpressionAttributeNames={
                    "#ltok": _LEGACY_TOKEN,
                    "#llease": _LEGACY_LEASE,
                    "#lfull": _LEGACY_FULL,
                },
                ExpressionAttributeValues={":me": {"S": token}},
                ReturnValues="ALL_NEW",
            )
        if _owners_of(response.get("Attributes", {})):
            return
        client.delete_item(
            TableName=table_name,
            Key=item_key,
            ConditionExpression=(
                "(attribute_not_exists(#o) OR size(#o) = :z) AND attribute_not_exists(#ltok)"
            ),
            ExpressionAttributeNames={"#o": _OWNERS, "#ltok": _LEGACY_TOKEN},
            ExpressionAttributeValues={":z": {"N": "0"}},
        )
    except client.exceptions.ConditionalCheckFailedException:
        logger.debug("Pending %s is still owed by another apply; left to it", key)
    except Exception as e:
        logger.warning("Could not remove pending %s (retried next apply): %s", key, e)
