"""Bucket items in their pre-#821 shape, for tests of the paths that still serve them.

Since #821 every bucket stores its dripping limits as fill instants (ADR-154).
An item written before v0.18 holds them as ``tk``; today's speculative write,
its ``vu`` guard and the ``tk`` slow path still serve one, and these rewrite a
fresh item into that shape so those paths can be tested against a real table.
"""

from typing import Any

from tests.fixtures.balances import balance_milli
from zae_limiter.schema import (
    BUCKET_FIELD_FI,
    BUCKET_FIELD_RF,
    BUCKET_FIELD_TK,
    BUCKET_FIELD_XI,
    bucket_attr,
    parse_bucket_attr,
    pk_bucket,
    sk_state,
)


async def as_legacy_item(
    repo: Any,
    entity_id: str,
    resource: str,
    tokens: dict[str, int] | None = None,
    shard_id: int = 0,
) -> None:
    """Rewrite a bucket item into its pre-#821 shape: every dripping limit as ``tk``.

    REMOVEs every ``b_{name}_fi`` and the item's ``xi``, and SETs ``b_{name}_tk``
    to ``tokens[name]`` (millitokens) or, for a limit not named, to the balance
    its fill instant reads at the item's ``rf``; the reserved ``wcu`` included.
    """
    key = {
        "PK": {"S": pk_bucket(repo._namespace_id, entity_id, resource, shard_id)},
        "SK": {"S": sk_state()},
    }
    client = await repo._get_client()
    response = await client.get_item(TableName=repo.table_name, Key=key, ConsistentRead=True)
    item = response["Item"]
    filled = sorted(
        parsed[0]
        for parsed in map(parse_bucket_attr, item)
        if parsed is not None and parsed[1] == BUCKET_FIELD_FI
    )
    assert set(tokens or {}) <= set(filled), f"{tokens} names a limit not stored as fi"
    rf_ms = int(item[BUCKET_FIELD_RF]["N"])
    names = {"#xi": BUCKET_FIELD_XI}
    values = {}
    sets = []
    removes = ["#xi"]
    for i, name in enumerate(filled):
        tk = (tokens or {}).get(name, balance_milli(repo, item, name, rf_ms))
        names[f"#f{i}"] = bucket_attr(name, BUCKET_FIELD_FI)
        names[f"#t{i}"] = bucket_attr(name, BUCKET_FIELD_TK)
        values[f":t{i}"] = {"N": str(tk)}
        sets.append(f"#t{i} = :t{i}")
        removes.append(f"#f{i}")
    await client.update_item(
        TableName=repo.table_name,
        Key=key,
        UpdateExpression=f"SET {', '.join(sets)} REMOVE {', '.join(removes)}",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    # An image cached while the item stored fill instants would send the next
    # write down the fill-instant fast path, which its pins now refuse.
    repo._fill_images.forget((repo._namespace_id, entity_id, resource, shard_id))
