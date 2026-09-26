"""Helpers that drive a bucket through ``wcu``-driven shard doublings (#587, ADR-139).

Shared by the quota shard-creation tests and the duration-window shard-creation
tests, which both need to force a real doubling and measure what a created
shard was granted. Async only: these call the async ``Repository`` directly.
"""

import contextlib
import random

from zae_limiter import RateLimitExceeded, schema

RESOURCE = "gpt-4"


@contextlib.contextmanager
def pinned_shard(shard: int):
    """Force every random shard draw to ``shard`` for the duration."""
    real_randrange, real_choice = random.randrange, random.choice
    random.randrange = lambda a, b=None: shard if b is None else real_randrange(a, b)
    random.choice = lambda seq: shard if shard in seq else real_choice(seq)
    try:
        yield
    finally:
        random.randrange, random.choice = real_randrange, real_choice


async def drain_wcu(repo, entity_id, shard_id, resource=RESOURCE):
    """Spend a shard's whole ``wcu`` allowance, and slow its refill so it sticks.

    No-op on a shard that does not exist: a bare ``UpdateItem`` would *create*
    one carrying nothing but ``wcu``, which is not a state the limiter can
    produce.
    """
    client = await repo._get_client()
    key = {
        "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
        "SK": {"S": schema.sk_state()},
    }
    if (await client.get_item(TableName=repo.table_name, Key=key)).get("Item") is None:
        return
    await client.update_item(
        TableName=repo.table_name,
        Key=key,
        UpdateExpression="SET #rp = :hour, #ra = :one",
        ExpressionAttributeNames={
            "#rp": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RP),
            "#ra": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_RA),
        },
        ExpressionAttributeValues={":hour": {"N": "3600000"}, ":one": {"N": "1"}},
    )
    await client.update_item(
        TableName=repo.table_name,
        Key=key,
        UpdateExpression="SET #tk = :zero",
        ExpressionAttributeNames={
            "#tk": schema.bucket_attr(schema.WCU_LIMIT_NAME, schema.BUCKET_FIELD_TK)
        },
        ExpressionAttributeValues={":zero": {"N": "0"}},
    )


async def shard_balances(repo, entity_id, limit_name, shard_count, resource=RESOURCE):
    """Per-shard millitoken balances; ``None`` where the shard does not exist."""
    client = await repo._get_client()
    attr = schema.bucket_attr(limit_name, schema.BUCKET_FIELD_TK)
    out: list[int | None] = []
    for shard in range(shard_count):
        response = await client.get_item(
            TableName=repo.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard)},
                "SK": {"S": schema.sk_state()},
            },
        )
        item = response.get("Item") or {}
        raw = item.get(attr)
        out.append(None if raw is None else int(raw["N"]))
    return out


async def spendable(repo, entity_id, limit_name, shard_count, resource=RESOURCE):
    """Entity-wide tokens still drawable, in whole tokens.

    Debt on one shard is dead weight for a quota — nothing repays it before the
    reset edge — so it must not offset a positive balance elsewhere. A shard
    that does not exist yet contributes whatever the *creation rule* would give
    it, which is the thing under test, so it is counted as zero here and the
    walk materialises every shard before reading.
    """
    balances = await shard_balances(repo, entity_id, limit_name, shard_count, resource)
    return sum(max(0, b) for b in balances if b is not None) // 1000


async def drain_shard(repo, entity_id, limit_name, shard_id, floor=2, resource=RESOURCE):
    """Spend one shard down to ``floor`` tokens. Returns how many were admitted.

    The spend is written straight to the item rather than looped through
    ``acquire()``: thousands of moto round trips per generation put this test
    past six minutes, and what #587 is about is what the *creation* rule grants,
    not how the balance got low. The doubling and the shard creation either side
    of every measurement still go through the real path.
    """
    client = await repo._get_client()
    key = {
        "PK": {"S": schema.pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
        "SK": {"S": schema.sk_state()},
    }
    response = await client.get_item(TableName=repo.table_name, Key=key)
    item = response.get("Item") or {}
    attr = schema.bucket_attr(limit_name, schema.BUCKET_FIELD_TK)
    raw = item.get(attr)
    if raw is None:
        return 0
    current = int(raw["N"])
    if current <= floor * 1000:
        return 0
    await client.update_item(
        TableName=repo.table_name,
        Key=key,
        UpdateExpression="SET #tk = :floor",
        ExpressionAttributeNames={"#tk": attr},
        ExpressionAttributeValues={":floor": {"N": str(floor * 1000)}},
    )
    return (current - floor * 1000) // 1000


async def materialise(limiter, entity_id, limit_name, shard, resource=RESOURCE):
    """Force ``shard`` into existence the way a random draw eventually would.

    Returns 1 if the acquire that created it was admitted, 0 if it was rejected.
    """
    with pinned_shard(shard):
        try:
            async with limiter.acquire(entity_id, resource, {limit_name: 1}):
                return 1
        except RateLimitExceeded:
            return 0


async def walk_doublings(limiter, entity_id, limit_name, generations=5, resource=RESOURCE):
    """The issue's probe: drain every shard, trip ``wcu``, let the count double.

    Returns ``(admitted, shard_count, spends)`` where ``spends`` is the list of
    ``(spendable_before, spendable_after)`` pairs bracketing each doubling.
    """
    repo = limiter._repository
    admitted = 0
    shard_count = 1
    spends = []

    for _ in range(generations):
        for shard in range(shard_count):
            admitted += await drain_shard(repo, entity_id, limit_name, shard, resource=resource)
        for shard in range(shard_count):
            await drain_wcu(repo, entity_id, shard, resource=resource)

        before = await spendable(repo, entity_id, limit_name, shard_count, resource)

        # The wcu-exhausted acquire doubles shard_count and falls to the slow
        # path, which creates a shard drawn from the newly added range. Draw
        # shard 0 for the attempt itself: it exists and its wcu is spent.
        admitted += await materialise(limiter, entity_id, limit_name, 0, resource)
        shard_count = repo._entity_cache[(repo._namespace_id, entity_id)][2][resource]

        # Force the rest of the new range into existence too.
        balances = await shard_balances(repo, entity_id, limit_name, shard_count, resource)
        for shard, balance in enumerate(balances):
            if balance is None:
                admitted += await materialise(limiter, entity_id, limit_name, shard, resource)

        after = await spendable(repo, entity_id, limit_name, shard_count, resource)
        spends.append((before, after))

    return admitted, shard_count, spends
