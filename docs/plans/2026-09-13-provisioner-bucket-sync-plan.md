# Provisioner Bucket-Param Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `zae-limiter limits apply` propagate entity-level limit changes to existing bucket items, so a manifest-applied change actually takes effect.

> **Status: delivered.** This plan shipped as PR #485 (`d22199b5`), closing #481. It is kept as
> the record of how the fix was specified, not as pending work.
>
> **One known gap it left, now tracked as #487.** Both this plan and the code it produced scope
> discovery with `f"BUCKET#{resource}#"`, so an entity change targeting the entity-wide
> `_default_` config queries `BUCKET#_default_#` and matches no real bucket — a silent no-op.
> `Repository._sync_bucket_params` has the identical behaviour, so the mirror was faithful
> rather than wrong, and #487 widens both together. Note the fix is **not** simply passing an
> unscoped `None`: precedence is Entity(resource) > Entity(`_default_`) > Resource > System, so
> an unscoped sync would stamp `_default_` limits onto buckets whose resource has its own
> higher-precedence entity config. The correct shape is `_fanout_entity`'s per-bucket
> re-resolution, and the TTL multiplier has to follow each bucket's resolved level rather than
> being fixed at the call site.

**Architecture:** The provisioner writes config items with a bare `put_item` and never touches buckets, so entity-level limits — which carry no TTL and therefore never expire — keep enforcing whatever numbers they were born with. This mirrors `Repository._sync_bucket_params` into sync boto3 in a new `bucket_sync.py`, reusing the two-pass GSI3 discovery pattern already proven in `fanout.py`, and wires it into the handler after `apply_changes` for both the set and delete paths.

**Tech Stack:** Python 3.11/3.12, sync boto3 (the provisioner runs in Lambda where aiobotocore is unavailable), pytest with `MagicMock` DynamoDB clients, LocalStack for E2E.

**Spec:** `docs/plans/2026-09-13-scheduled-limits-design.md` §5.2 (and §3.4 for the scheduling fields a later plan adds to the same code path).

## Global Constraints

- **Sync boto3 only.** This package runs inside Lambda; `aioboto3`/`aiobotocore` are not available. Follow `fanout.py`'s conventions exactly.
- **The provisioner Lambda vendors only `zae_limiter/schema.py`, `models.py`, `exceptions.py`** (see `src/zae_limiter/infra/provisioner_builder.py`). Do not import anything else from `zae_limiter`.
- **Units differ between item types and this is the single most dangerous thing in this plan.** Config items store **whole tokens and seconds** (`limit_attr(name, "cp")` = `str(decl["capacity"])`, `applier.py:57-59`). Bucket items store **millitokens and milliseconds** (`bucket_attr(name, BUCKET_FIELD_CP)` = `str(limit.capacity * 1000)`, `repository.py:3113`). Every value crossing from config to bucket must be multiplied by 1000.
- **Entity-level only.** `set_resource_defaults()` and `set_system_defaults()` deliberately never touch buckets: a bucket running on defaults carries a TTL and is recreated with current params when it expires (#271, #296). Only `set_limits` and `reconcile_bucket_to_defaults` call `_sync_bucket_params` — verified, they are the only two callers. Do not widen this.
- **Two discovery passes, always.** The second pass catches a bucket created by an `acquire()` already in flight when the first pass's query ran. Mitigates, does not eliminate, the ADR-125 race.
- **Never remove `rf`.** `BUCKET_FIELD_RF` is shared across all limits in a composite bucket; removing it when an individual limit goes stale would destroy the optimistic lock.
- Commit messages follow `.claude/rules/commits.md` (gitmoji + conventional commits). Scope is `provisioner`.
- All work happens on a feature branch off `main`; direct commits to `main` are prohibited (`.claude/rules/pull-request-workflow.md`).

---

### Task 1: `bucket_sync.py` — build the update expression

**Files:**
- Create: `src/zae_limiter_provisioner/bucket_sync.py`
- Test: `tests/unit/test_provisioner_bucket_sync.py`

**Interfaces:**
- Consumes: `zae_limiter.schema.bucket_attr`, `BUCKET_FIELD_CP/RA/RP/TK/TC`, `calculate_bucket_ttl_seconds`, `calculate_ttl`; `zae_limiter.models.Limit`
- Produces: `build_bucket_param_update(limits: dict[str, dict[str, int]], ttl_multiplier: int | None, stale_limit_names: set[str] | None, now_ms: int) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]` returning `(update_expr, expr_names, expr_values)`

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the provisioner's sync boto3 bucket param sync (issue #468, §5.2).

Mirrors the mocking conventions in test_provisioner_fanout.py: a MagicMock
boto3 DynamoDB client with `client.exceptions.*` populated with real exception
classes so `except client.exceptions.X` matches as it would against real boto3.
"""

from unittest.mock import MagicMock

from zae_limiter.schema import bucket_attr
from zae_limiter_provisioner.bucket_sync import build_bucket_param_update

ConditionalCheckFailedException = type("ConditionalCheckFailedException", (Exception,), {})


def _make_client() -> MagicMock:
    client = MagicMock()
    client.exceptions.ConditionalCheckFailedException = ConditionalCheckFailedException
    return client


LIMITS = {"rpm": {"capacity": 1000, "refill_amount": 1000, "refill_period": 60}}


class TestBuildBucketParamUpdate:
    def test_converts_whole_tokens_to_millitokens(self):
        """Config items store whole tokens; bucket items store millitokens."""
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=0, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        cp_alias = next(k for k, v in names.items() if v == bucket_attr("rpm", "cp"))
        ra_alias = next(k for k, v in names.items() if v == bucket_attr("rpm", "ra"))
        rp_alias = next(k for k, v in names.items() if v == bucket_attr("rpm", "rp"))
        assert values[cp_alias.replace("#", ":")] == {"N": "1000000"}
        assert values[ra_alias.replace("#", ":")] == {"N": "1000000"}
        # refill_period is SECONDS on config, MILLISECONDS on the bucket
        assert values[rp_alias.replace("#", ":")] == {"N": "60000"}
        assert expr.startswith("SET ")

    def test_ttl_multiplier_zero_removes_ttl(self):
        """Entity custom limits mean the bucket must persist: REMOVE ttl."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=0, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert "REMOVE" in expr
        assert names["#ttl"] == "ttl"
        assert expr.split("REMOVE")[1].strip().startswith("#ttl")

    def test_ttl_multiplier_positive_sets_ttl(self):
        """Back on defaults: TTL = now + max_time_to_fill * multiplier."""
        expr, _names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=7, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert ":ttl_val" in values
        # time_to_fill = (1000/1000)*60 = 60s; TTL = 1789000000 + 420
        assert values[":ttl_val"] == {"N": str(1_789_000_000 + 420)}
        assert "REMOVE" not in expr

    def test_ttl_multiplier_none_leaves_ttl_alone(self):
        expr, names, values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names=None, now_ms=1_789_000_000_000
        )
        assert "#ttl" not in names
        assert ":ttl_val" not in values
        assert "REMOVE" not in expr

    def test_stale_limits_removed_but_never_rf(self):
        """Stale limit attrs go; the shared `rf` optimistic lock must not."""
        expr, names, _values = build_bucket_param_update(
            LIMITS, ttl_multiplier=None, stale_limit_names={"tpm"}, now_ms=1_789_000_000_000
        )
        removed = {names[a.strip()] for a in expr.split("REMOVE")[1].split(",")}
        assert removed == {bucket_attr("tpm", f) for f in ("tk", "cp", "ra", "rp", "tc")}
        assert "rf" not in removed

    def test_hyphenated_limit_names_use_indexed_aliases(self):
        """Limit names may contain hyphens, which are illegal in expression names."""
        expr, names, _values = build_bucket_param_update(
            {"req-per-min": {"capacity": 5, "refill_amount": 5, "refill_period": 1}},
            ttl_multiplier=None,
            stale_limit_names=None,
            now_ms=0,
        )
        assert bucket_attr("req-per-min", "cp") in names.values()
        assert "-" not in expr
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'zae_limiter_provisioner.bucket_sync'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Sync bucket static params when manifest-applied limits change (issue #468).

Sync boto3 mirror of ``Repository._sync_bucket_params``, because the
provisioner runs inside Lambda where aiobotocore is unavailable. Key
construction is shared via ``zae_limiter.schema``, already vendored into the
Lambda package.

Scope is deliberately **entity-level only**. ``set_resource_defaults`` and
``set_system_defaults`` never touch buckets: a bucket running on defaults
carries a TTL and is recreated with current params when it expires (#271,
#296). Only ``set_limits`` and ``reconcile_bucket_to_defaults`` fan out on the
async side, and this mirror inherits that rule rather than widening it.

Units: config items store whole tokens and seconds; bucket items store
millitokens and milliseconds. Everything crossing over is multiplied by 1000.
"""

from __future__ import annotations

import logging
from typing import Any

from zae_limiter.models import Limit
from zae_limiter.schema import (
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
    BUCKET_FIELD_TK,
    bucket_attr,
    calculate_bucket_ttl_seconds,
    calculate_ttl,
)

logger = logging.getLogger(__name__)

# Fields removed for a limit that no longer exists in the effective config.
# BUCKET_FIELD_RF is deliberately absent: it is shared across every limit in a
# composite bucket and carries the optimistic lock.
_STALE_FIELDS = (
    BUCKET_FIELD_TK,
    BUCKET_FIELD_CP,
    BUCKET_FIELD_RA,
    BUCKET_FIELD_RP,
    BUCKET_FIELD_TC,
)


def build_bucket_param_update(
    limits: dict[str, dict[str, int]],
    ttl_multiplier: int | None,
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> tuple[str, dict[str, str], dict[str, dict[str, str]]]:
    """Build the SET/REMOVE UpdateExpression for one bucket shard.

    Args:
        limits: Manifest-shaped limits, ``{name: {capacity, refill_amount,
            refill_period}}``, in whole tokens and seconds.
        ttl_multiplier: None leaves ``ttl`` alone; 0 REMOVEs it (entity has
            custom limits, so the bucket must persist); >0 SETs it.
        stale_limit_names: Limit names to strip from the bucket entirely.
        now_ms: Current time, epoch milliseconds.

    Returns:
        ``(update_expr, expr_names, expr_values)``.
    """
    set_parts: list[str] = []
    remove_parts: list[str] = []
    expr_names: dict[str, str] = {}
    expr_values: dict[str, dict[str, str]] = {}

    # Numeric indices for expression names: limit names may contain hyphens,
    # dots, slashes and colons, none of which are legal in an alias.
    for i, (name, decl) in enumerate(limits.items()):
        for alias, field, value in (
            (f"#cp{i}", BUCKET_FIELD_CP, decl["capacity"] * 1000),
            (f"#ra{i}", BUCKET_FIELD_RA, decl["refill_amount"] * 1000),
            (f"#rp{i}", BUCKET_FIELD_RP, decl["refill_period"] * 1000),
        ):
            set_parts.append(f"{alias} = :{alias[1:]}")
            expr_names[alias] = bucket_attr(name, field)
            expr_values[f":{alias[1:]}"] = {"N": str(value)}

    if ttl_multiplier is not None:
        expr_names["#ttl"] = "ttl"
        if ttl_multiplier > 0:
            ttl_seconds = calculate_bucket_ttl_seconds(
                [
                    Limit(
                        name=n,
                        capacity=d["capacity"],
                        refill_amount=d["refill_amount"],
                        refill_period_seconds=d["refill_period"],
                    )
                    for n, d in limits.items()
                ],
                ttl_multiplier,
            )
            if ttl_seconds is not None:
                set_parts.append("#ttl = :ttl_val")
                expr_values[":ttl_val"] = {"N": str(calculate_ttl(now_ms, ttl_seconds))}
        else:
            remove_parts.append("#ttl")

    for stale_name in sorted(stale_limit_names or ()):
        for field in _STALE_FIELDS:
            alias = f"#stale_{abs(hash((stale_name, field))) % 10**8}"
            expr_names[alias] = bucket_attr(stale_name, field)
            remove_parts.append(alias)

    update_expr = f"SET {', '.join(set_parts)}"
    if remove_parts:
        update_expr += f" REMOVE {', '.join(remove_parts)}"
    return update_expr, expr_names, expr_values
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py -v`
Expected: PASS (6 tests)

If `test_stale_limits_removed_but_never_rf` fails on alias collisions, replace the `hash()`-derived alias with a monotonic counter — `hash()` is salted per process and the test asserts on resolved attribute names, not aliases, so either works, but a counter is deterministic and easier to debug.

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/zae_limiter_provisioner/bucket_sync.py tests/unit/test_provisioner_bucket_sync.py
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): build bucket param update expressions

Sync boto3 mirror of the expression-building half of
Repository._sync_bucket_params. Converts config-item whole tokens and
seconds into bucket-item millitokens and milliseconds, and never removes
the shared `rf` optimistic lock when stripping a stale limit.

Refs #468
EOF
)"
```

---

### Task 2: `bucket_sync.py` — two-pass discovery and per-shard write

**Files:**
- Modify: `src/zae_limiter_provisioner/bucket_sync.py`
- Test: `tests/unit/test_provisioner_bucket_sync.py`

**Interfaces:**
- Consumes: `build_bucket_param_update` from Task 1; `zae_limiter.schema.GSI3_NAME`, `gsi3_pk_entity`, `sk_state`
- Produces: `sync_bucket_params(client, table_name, namespace_id, entity_id, resource, limits, ttl_multiplier, stale_limit_names, now_ms) -> int` returning the number of shards written

- [ ] **Step 1: Write the failing test**

```python
from zae_limiter.schema import gsi3_pk_entity, sk_state
from zae_limiter_provisioner.bucket_sync import sync_bucket_params


def _query_pages(*pages):
    """client.query side_effect returning the given pages, then repeating the last.

    _fanout-style discovery runs the query twice, so the side_effect must keep
    answering after the first pass is exhausted.
    """
    responses = list(pages)

    def _query(**kwargs):
        return responses.pop(0) if len(responses) > 1 else responses[0]

    return _query


def _pk(entity_id="user-1", resource="gpt-4", shard=0, ns="ns123"):
    return f"{ns}/BUCKET#{entity_id}#{resource}#{shard}"


class TestSyncBucketParams:
    def test_writes_every_discovered_shard(self):
        client = _make_client()
        client.query.side_effect = _query_pages(
            {"Items": [{"PK": {"S": _pk(shard=0)}}, {"PK": {"S": _pk(shard=1)}}]}
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2
        keys = {c.kwargs["Key"]["PK"]["S"] for c in client.update_item.call_args_list}
        assert keys == {_pk(shard=0), _pk(shard=1)}
        for call in client.update_item.call_args_list:
            assert call.kwargs["Key"]["SK"] == {"S": sk_state()}
            assert call.kwargs["ConditionExpression"] == "attribute_exists(PK)"

    def test_queries_gsi3_scoped_to_the_resource(self):
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": []})
        sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        params = client.query.call_args.kwargs
        assert params["IndexName"] == "GSI3"
        assert params["ExpressionAttributeValues"][":pk"] == {
            "S": gsi3_pk_entity("ns123", "user-1")
        }
        assert params["ExpressionAttributeValues"][":sk"] == {"S": "BUCKET#gpt-4#"}

    def test_runs_two_passes_without_double_writing(self):
        """Second pass catches an in-flight bucket; pass-one PKs are not rewritten."""
        client = _make_client()
        client.query.side_effect = _query_pages(
            {"Items": [{"PK": {"S": _pk(shard=0)}}]},
            {"Items": [{"PK": {"S": _pk(shard=0)}}, {"PK": {"S": _pk(shard=1)}}]},
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2
        assert client.update_item.call_count == 2

    def test_vanished_shard_is_tolerated(self):
        """TTL can expire a shard between discovery and write."""
        client = _make_client()
        client.query.side_effect = _query_pages({"Items": [{"PK": {"S": _pk()}}]})
        client.update_item.side_effect = ConditionalCheckFailedException()
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 0

    def test_no_limits_is_a_noop(self):
        client = _make_client()
        assert (
            sync_bucket_params(
                client,
                "tbl",
                "ns123",
                "user-1",
                "gpt-4",
                {},
                ttl_multiplier=0,
                stale_limit_names=None,
                now_ms=0,
            )
            == 0
        )
        client.query.assert_not_called()

    def test_paginates_discovery(self):
        client = _make_client()
        client.query.side_effect = _query_pages(
            {"Items": [{"PK": {"S": _pk(shard=0)}}], "LastEvaluatedKey": {"PK": {"S": "x"}}},
            {"Items": [{"PK": {"S": _pk(shard=1)}}]},
        )
        written = sync_bucket_params(
            client,
            "tbl",
            "ns123",
            "user-1",
            "gpt-4",
            LIMITS,
            ttl_multiplier=0,
            stale_limit_names=None,
            now_ms=0,
        )
        assert written == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py::TestSyncBucketParams -v`
Expected: FAIL with `ImportError: cannot import name 'sync_bucket_params'`

- [ ] **Step 3: Write minimal implementation**

Append to `bucket_sync.py`:

```python
from zae_limiter.schema import GSI3_NAME, gsi3_pk_entity, sk_state


def _update_one_shard(
    client: Any,
    table_name: str,
    pk: str,
    update_expr: str,
    expr_names: dict[str, str],
    expr_values: dict[str, dict[str, str]],
) -> bool:
    """Apply one shard's param update. Returns False if the shard vanished."""
    try:
        client.update_item(
            TableName=table_name,
            Key={"PK": {"S": pk}, "SK": {"S": sk_state()}},
            UpdateExpression=update_expr,
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
        return True
    except client.exceptions.ConditionalCheckFailedException:
        # Bucket does not exist yet, or no longer does: TTL can expire a shard
        # between discovery and this write. A bucket created later is created
        # with the current params, so there is nothing to reconcile.
        logger.debug("Bucket %s vanished before param sync", pk)
        return False


def sync_bucket_params(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
    limits: dict[str, dict[str, int]],
    ttl_multiplier: int | None,
    stale_limit_names: set[str] | None,
    now_ms: int,
) -> int:
    """Push changed limit params to every shard of one entity+resource bucket.

    Two discovery passes, exactly like the ADR-125 disable fan-out in
    ``fanout.py``: the second catches a bucket created by an ``acquire()``
    already in flight when the first pass's query ran. Mitigates, but does not
    eliminate, that race.

    Returns the number of shards actually written.
    """
    if not limits:
        return 0

    update_expr, expr_names, expr_values = build_bucket_param_update(
        limits, ttl_multiplier, stale_limit_names, now_ms
    )

    synced: set[str] = set()
    written = 0
    for _pass in range(2):
        start_key: dict[str, Any] | None = None
        while True:
            params: dict[str, Any] = {
                "TableName": table_name,
                "IndexName": GSI3_NAME,
                "KeyConditionExpression": "GSI3PK = :pk AND begins_with(GSI3SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": gsi3_pk_entity(namespace_id, entity_id)},
                    ":sk": {"S": f"BUCKET#{resource}#"},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk and pk not in synced:
                    synced.add(pk)
                    if _update_one_shard(
                        client, table_name, pk, update_expr, expr_names, expr_values
                    ):
                        written += 1
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
    return written
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/zae_limiter_provisioner/bucket_sync.py tests/unit/test_provisioner_bucket_sync.py
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): fan out bucket param changes to every shard

Two-pass GSI3 discovery mirroring fanout.py, with a per-shard conditional
update that tolerates a shard expiring between discovery and write.

Refs #468
EOF
)"
```

---

### Task 3: Resolve effective limits after a config deletion

**Files:**
- Modify: `src/zae_limiter_provisioner/bucket_sync.py`
- Test: `tests/unit/test_provisioner_bucket_sync.py`

**Interfaces:**
- Consumes: `zae_limiter.schema.parse_limit_attr`, `pk_entity`, `pk_resource`, `pk_system`, `sk_config`, `DEFAULT_RESOURCE`
- Produces: `resolve_effective_limits(client, table_name, namespace_id, entity_id, resource) -> dict[str, dict[str, int]]` — manifest-shaped limits from the first config level that defines any, walking entity(`_default_`) → resource → system. Returns `{}` if no level defines limits.

**Why this exists:** deleting an entity config must reconcile the bucket to whatever now applies, exactly as `Repository.delete_limits` → `reconcile_bucket_to_defaults` does. Without it, a deleted entity config leaves its buckets enforcing the deleted numbers with no TTL, forever.

- [ ] **Step 1: Write the failing test**

```python
from zae_limiter.schema import limit_attr, pk_entity, pk_resource, pk_system, sk_config
from zae_limiter_provisioner.bucket_sync import resolve_effective_limits


def _limits_item(**limits):
    """A config item carrying composite limit attributes (whole tokens)."""
    item = {}
    for name, (cp, ra, rp) in limits.items():
        item[limit_attr(name, "cp")] = {"N": str(cp)}
        item[limit_attr(name, "ra")] = {"N": str(ra)}
        item[limit_attr(name, "rp")] = {"N": str(rp)}
    return item


def _levels(mapping):
    def _get_item(**kwargs):
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        return {"Item": mapping[key]} if key in mapping else {}

    return _get_item


class TestResolveEffectiveLimits:
    def test_entity_default_wins_over_resource(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {
                (pk_entity("ns123", "user-1"), sk_config("_default_")): _limits_item(
                    rpm=(50, 50, 60)
                ),
                (pk_resource("ns123", "gpt-4"), sk_config()): _limits_item(rpm=(999, 999, 60)),
            }
        )
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {
            "rpm": {"capacity": 50, "refill_amount": 50, "refill_period": 60}
        }

    def test_falls_through_to_resource_then_system(self):
        client = _make_client()
        client.get_item.side_effect = _levels(
            {
                (pk_system("ns123"), sk_config()): _limits_item(rpm=(10, 10, 60)),
            }
        )
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {
            "rpm": {"capacity": 10, "refill_amount": 10, "refill_period": 60}
        }

    def test_no_level_defines_limits(self):
        client = _make_client()
        client.get_item.side_effect = _levels({})
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {}

    def test_skips_entity_default_level_when_resource_is_default(self):
        """Mirrors resolve_disabled: no point reading _default_ twice."""
        client = _make_client()
        client.get_item.side_effect = _levels({})
        resolve_effective_limits(client, "tbl", "ns123", "user-1", "_default_")
        read = [c.kwargs["Key"]["SK"]["S"] for c in client.get_item.call_args_list]
        assert read.count(sk_config("_default_")) == 0

    def test_ignores_non_limit_attributes(self):
        """`disabled`, `config_version` and friends must not become limits."""
        client = _make_client()
        item = _limits_item(rpm=(10, 10, 60))
        item["disabled"] = {"BOOL": True}
        item["config_version"] = {"N": "3"}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        assert set(resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4")) == {"rpm"}

    def test_partial_limit_attributes_are_skipped(self):
        """A limit missing cp/ra/rp is malformed; do not synthesise defaults."""
        client = _make_client()
        item = {limit_attr("rpm", "cp"): {"N": "10"}}
        client.get_item.side_effect = _levels({(pk_resource("ns123", "gpt-4"), sk_config()): item})
        assert resolve_effective_limits(client, "tbl", "ns123", "user-1", "gpt-4") == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py::TestResolveEffectiveLimits -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_effective_limits'`

- [ ] **Step 3: Write minimal implementation**

Append to `bucket_sync.py`:

```python
from zae_limiter.schema import (
    DEFAULT_RESOURCE,
    LIMIT_FIELD_CP,
    LIMIT_FIELD_RA,
    LIMIT_FIELD_RP,
    parse_limit_attr,
    pk_entity,
    pk_resource,
    pk_system,
    sk_config,
)

_MANIFEST_KEY = {
    LIMIT_FIELD_CP: "capacity",
    LIMIT_FIELD_RA: "refill_amount",
    LIMIT_FIELD_RP: "refill_period",
}


def _decode_limits(item: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Decode composite ``l_{name}_{field}`` attributes into manifest shape.

    A limit missing any of cp/ra/rp is malformed and is skipped rather than
    given a synthesised default, which would silently invent a limit.
    """
    partial: dict[str, dict[str, int]] = {}
    for attr, value in item.items():
        parsed = parse_limit_attr(attr)
        if parsed is None:
            continue
        name, field = parsed
        key = _MANIFEST_KEY.get(field)
        if key is None:
            continue
        partial.setdefault(name, {})[key] = int(value["N"])
    return {
        name: decl
        for name, decl in partial.items()
        if decl.keys() == {"capacity", "refill_amount", "refill_period"}
    }


def resolve_effective_limits(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str,
) -> dict[str, dict[str, int]]:
    """Effective limits after an entity's per-resource config is deleted.

    Walks entity(`_default_`) -> resource -> system and returns the first
    level that defines any limits, mirroring the precedence in ADR-100 minus
    the entity(resource) level the caller has just removed. The
    entity(`_default_`) level is skipped when `resource` already IS
    `_default_`, exactly as ``fanout.resolve_disabled`` does.
    """
    levels: list[tuple[str, str]] = []
    if resource != DEFAULT_RESOURCE:
        levels.append((pk_entity(namespace_id, entity_id), sk_config(DEFAULT_RESOURCE)))
    levels.append((pk_resource(namespace_id, resource), sk_config()))
    levels.append((pk_system(namespace_id), sk_config()))

    for pk, sk in levels:
        response = client.get_item(TableName=table_name, Key={"PK": {"S": pk}, "SK": {"S": sk}})
        item = response.get("Item")
        if not item:
            continue
        limits = _decode_limits(item)
        if limits:
            return limits
    return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_provisioner_bucket_sync.py -v`
Expected: PASS (18 tests)

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/zae_limiter_provisioner/bucket_sync.py tests/unit/test_provisioner_bucket_sync.py
git commit -m "$(cat <<'EOF'
✨ feat(provisioner): resolve effective limits after a config delete

Walks entity(_default_) -> resource -> system, mirroring the precedence
Repository.delete_limits relies on so a deleted entity config reconciles
its buckets to whatever now applies instead of enforcing deleted numbers
forever with no TTL.

Refs #468
EOF
)"
```

---

### Task 4: Wire both paths into the handler

**Files:**
- Modify: `src/zae_limiter_provisioner/handler.py` (add alongside `_fanout_disabled_changes`, called at both `apply_changes` sites — currently lines 95-96 and 142-143)
- Test: `tests/unit/test_provisioner_handler.py`

**Interfaces:**
- Consumes: `sync_bucket_params`, `resolve_effective_limits` from Tasks 2-3; `Change` from `differ.py` (`action`, `level`, `target`, `data`)
- Produces: `_sync_bucket_param_changes(table_name: str, namespace_id: str, changes: list[Change]) -> None`

**Key detail:** an entity `Change.target` is `"entity_id/resource"` and must be split with `split("/", 1)`, exactly as `applier._apply_set` does — entity ids may themselves contain `/`, so `split("/")` would be wrong.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import MagicMock, patch

from zae_limiter_provisioner.differ import Change
from zae_limiter_provisioner.handler import _sync_bucket_param_changes


class TestSyncBucketParamChanges:
    def test_entity_set_syncs_with_ttl_removed(self):
        """Entity custom limits mean the bucket must persist: multiplier 0."""
        changes = [
            Change(
                action="update",
                level="entity",
                target="user-1/gpt-4",
                data={"limits": {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}},
            )
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        assert sync.call_count == 1
        kwargs = sync.call_args.kwargs
        assert (kwargs["entity_id"], kwargs["resource"]) == ("user-1", "gpt-4")
        assert kwargs["ttl_multiplier"] == 0
        assert kwargs["stale_limit_names"] is None

    def test_entity_delete_reconciles_to_defaults_with_ttl(self):
        changes = [
            Change(
                action="delete",
                level="entity",
                target="user-1/gpt-4",
                data={"limits": {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}},
            )
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch(
                "zae_limiter_provisioner.handler.resolve_effective_limits",
                return_value={"rpm": {"capacity": 1, "refill_amount": 1, "refill_period": 60}},
            ),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        kwargs = sync.call_args.kwargs
        assert kwargs["ttl_multiplier"] == 7
        assert kwargs["limits"] == {"rpm": {"capacity": 1, "refill_amount": 1, "refill_period": 60}}

    def test_delete_strips_limits_absent_from_the_new_effective_config(self):
        changes = [
            Change(
                action="delete",
                level="entity",
                target="user-1/gpt-4",
                data={
                    "limits": {
                        "rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60},
                        "tpm": {"capacity": 9, "refill_amount": 9, "refill_period": 60},
                    }
                },
            )
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch(
                "zae_limiter_provisioner.handler.resolve_effective_limits",
                return_value={"rpm": {"capacity": 1, "refill_amount": 1, "refill_period": 60}},
            ),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        assert sync.call_args.kwargs["stale_limit_names"] == {"tpm"}

    def test_resource_and_system_levels_are_never_synced(self):
        """Buckets on defaults carry a TTL and are recreated (#271, #296)."""
        changes = [
            Change(action="update", level="resource", target="gpt-4", data={"limits": {}}),
            Change(action="update", level="system", target=None, data={"limits": {}}),
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        sync.assert_not_called()

    def test_entity_id_containing_a_slash_splits_once(self):
        changes = [
            Change(
                action="update",
                level="entity",
                target="org/team/gpt-4",
                data={"limits": {"rpm": {"capacity": 5, "refill_amount": 5, "refill_period": 60}}},
            )
        ]
        with patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync:
            _sync_bucket_param_changes("tbl", "ns123", changes)
        kwargs = sync.call_args.kwargs
        assert (kwargs["entity_id"], kwargs["resource"]) == ("org", "team/gpt-4")

    def test_delete_with_no_effective_limits_is_a_noop(self):
        """Nothing left to reconcile to; leave the bucket for its TTL/recreate."""
        changes = [
            Change(action="delete", level="entity", target="user-1/gpt-4", data={"limits": {}})
        ]
        with (
            patch("zae_limiter_provisioner.handler.sync_bucket_params") as sync,
            patch("zae_limiter_provisioner.handler.resolve_effective_limits", return_value={}),
        ):
            _sync_bucket_param_changes("tbl", "ns123", changes)
        sync.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_provisioner_handler.py::TestSyncBucketParamChanges -v`
Expected: FAIL with `ImportError: cannot import name '_sync_bucket_param_changes'`

- [ ] **Step 3: Write minimal implementation**

Add the import near the existing `from .fanout import ...` line, then the function alongside `_fanout_disabled_changes`:

```python
from .bucket_sync import resolve_effective_limits, sync_bucket_params


def _sync_bucket_param_changes(
    table_name: str,
    namespace_id: str,
    changes: list[Change],
) -> None:
    """Push entity-level limit changes out to existing bucket items (#468).

    Runs AFTER ``apply_changes``, so ``resolve_effective_limits`` sees the
    config this apply has already written — the same ordering contract
    ``_fanout_disabled_changes`` relies on.

    **Entity level only.** Resource and system defaults deliberately never
    touch buckets: a bucket on defaults carries a TTL and is recreated with
    current params when it expires (#271, #296).
    """
    client = boto3.client("dynamodb")
    now_ms = int(time.time() * 1000)

    for change in changes:
        if change.level != "entity" or change.target is None:
            continue
        entity_id, resource = change.target.split("/", 1)
        declared = (change.data or {}).get("limits", {})

        if change.action == "delete":
            # Reconcile to whatever now applies, and strip the limits that the
            # deleted config had but the new effective config does not.
            effective = resolve_effective_limits(
                client, table_name, namespace_id, entity_id, resource
            )
            if not effective:
                continue
            stale = set(declared) - set(effective)
            limits, ttl_multiplier = effective, 7
            stale_limit_names = stale or None
        else:
            if not declared:
                continue
            limits, ttl_multiplier = declared, 0
            stale_limit_names = None

        sync_bucket_params(
            client=client,
            table_name=table_name,
            namespace_id=namespace_id,
            entity_id=entity_id,
            resource=resource,
            limits=limits,
            ttl_multiplier=ttl_multiplier,
            stale_limit_names=stale_limit_names,
            now_ms=now_ms,
        )
```

Then call it at both `apply_changes` sites, after the disable fan-out:

```python
    result = apply_changes(changes, table_name, namespace_id)
    _fanout_disabled_changes(table_name, namespace_id, changes)
    _sync_bucket_param_changes(table_name, namespace_id, changes)
```

Add `import time` at the top if not already present.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_provisioner_handler.py -v`
Expected: PASS, including the pre-existing handler tests

- [ ] **Step 5: Run the full provisioner unit suite**

Run: `uv run pytest tests/unit/ -k "provisioner or applier or differ or manifest" -v`
Expected: PASS

- [ ] **Step 6: Run lint and type check**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run mypy`
Expected: clean

- [ ] **Step 7: Commit**

```bash
git add src/zae_limiter_provisioner/handler.py tests/unit/test_provisioner_handler.py
git commit -m "$(cat <<'EOF'
🐛 fix(provisioner): propagate applied limits to existing buckets

`limits apply` wrote config items and stopped there, so entity-level
limits — which carry no TTL and never expire — kept enforcing whatever
numbers they were born with. Same class as #468 on the Repository path,
still open on the provisioner path.

Entity level only: resource and system defaults rely on bucket TTL and
recreation (#271, #296).

Fixes #468 on the provisioner path
EOF
)"
```

---

### Task 5: E2E — a manifest apply reaches live buckets

**Files:**
- Modify: `tests/e2e/test_localstack.py` (add a class; follow the existing `@pytest.mark.e2e` fixtures in `tests/fixtures/`)

**Interfaces:**
- Consumes: everything from Tasks 1-4
- Produces: nothing consumed downstream — this is the acceptance gate

**Why E2E and not integration:** the bug is the seam between the provisioner Lambda and DynamoDB. A unit test with a mocked client cannot catch a wrong attribute name or a missing 1000x conversion; only a real acquire against a real table can.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.e2e
class TestProvisionerReachesLiveBuckets:
    """A manifest apply must change what an already-created bucket enforces (#468)."""

    async def test_apply_changes_an_existing_bucket(self, e2e_limiter, e2e_repo, tmp_path):
        # 1. Create a bucket by acquiring against a generous entity limit.
        await e2e_repo.set_limits("user-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4")
        async with e2e_limiter.acquire("user-1", "gpt-4", consume={"rpm": 1}):
            pass

        before = await e2e_repo.get_buckets("user-1")
        rpm_before = next(b for b in before if b.limit_name == "rpm")
        assert rpm_before.capacity == 1000

        # 2. Apply a manifest that lowers it.
        manifest = tmp_path / "limits.yaml"
        manifest.write_text(
            "namespace: default\n"
            "entities:\n"
            "  user-1:\n"
            "    resources:\n"
            "      gpt-4:\n"
            "        limits:\n"
            "          rpm:\n"
            "            capacity: 10\n"
        )
        await apply_manifest(e2e_repo, manifest)  # invokes the provisioner Lambda

        # 3. The EXISTING bucket item must now carry the new capacity.
        after = await e2e_repo.get_buckets("user-1")
        rpm_after = next(b for b in after if b.limit_name == "rpm")
        assert rpm_after.capacity == 10, "manifest apply did not reach the live bucket"

        # 4. And it must actually be enforced.
        with pytest.raises(RateLimitExceeded):
            async with e2e_limiter.acquire("user-1", "gpt-4", consume={"rpm": 50}):
                pass
```

- [ ] **Step 2: Start LocalStack and run the test to verify it fails**

```bash
zae-limiter local up
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \
       AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/e2e/test_localstack.py::TestProvisionerReachesLiveBuckets -v
```

Expected before Task 4: FAIL at step 3 with `assert 1000 == 10` — this is the bug, reproduced.
Expected after Task 4: PASS.

Because Tasks 1-4 already landed, confirm the fix by stashing them: `git stash` the handler change, watch it fail, restore it, watch it pass. A test that has never been seen to fail has not been shown to test anything.

- [ ] **Step 3: Resolve the `apply_manifest` helper**

`apply_manifest` above is a placeholder for however the existing E2E suite invokes the provisioner. Before writing the test, grep for the real path:

```bash
rg -n "limits_cli|invoke.*provisioner|limits apply" tests/ src/zae_limiter/limits_cli.py | head -20
```

Use the same entry point the existing `limits plan/apply` tests use. If none exists, invoke the CLI through `CliRunner` the way `tests/e2e/` already drives other commands.

- [ ] **Step 4: Run the full E2E suite for regressions**

Run: `uv run pytest tests/e2e/test_localstack.py -v`
Expected: PASS

- [ ] **Step 5: Run the whole unit suite**

Run: `uv run pytest tests/unit/ -q`
Expected: PASS (~3 min; do **not** pass `-o "addopts="`, which un-skips the gevent tests and deadlocks — see `.claude/rules/testing.md`)

- [ ] **Step 6: Commit and open the PR**

```bash
git add tests/e2e/test_localstack.py
git commit -m "$(cat <<'EOF'
✅ test(provisioner): prove a manifest apply reaches live buckets

E2E acceptance for #468 on the provisioner path: create a bucket, lower
the limit via a manifest apply, assert the existing bucket item carries
and enforces the new capacity.

Refs #468
EOF
)"
git push -u origin HEAD
```

Then open the PR with the `/pr` skill (required by `.claude/rules/issue-skill.md` — do not run `gh pr create` directly).

---

### Task 6: Validate limit values at manifest parse time

**Files:**
- Modify: `src/zae_limiter_provisioner/manifest.py` (`LimitDecl.from_dict`)
- Test: `tests/unit/test_provisioner_manifest.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `LimitDecl.from_dict` raising `ValueError` on non-positive values

**Why:** `manifest.py` validates only `namespace` — nothing checks limit values. Today
`capacity: 0` is accepted and written to the config item as a zero capacity, because
`applier._build_limit_item` stringifies the raw dict without constructing a `Limit`. Task 1
constructs real `Limit` objects for the TTL calculation, and `Limit.__post_init__` rejects
non-positive values — so without this task, `capacity: 0` turns from a silently-wrong config
into a `ValueError` raised inside the Lambda, where the operator sees a stack trace instead of
a message. Validating at parse time means `zae-limiter limits plan` catches it before anything
is written.

- [ ] **Step 1: Write the failing test**

```python
import pytest

from zae_limiter_provisioner.manifest import LimitDecl


class TestLimitDeclValidation:
    @pytest.mark.parametrize("field", ["capacity", "refill_amount", "refill_period"])
    @pytest.mark.parametrize("bad", [0, -1])
    def test_rejects_non_positive(self, field, bad):
        decl = {"capacity": 100, "refill_amount": 100, "refill_period": 60, field: bad}
        with pytest.raises(ValueError, match=field):
            LimitDecl.from_dict(decl)

    def test_accepts_positive(self):
        decl = LimitDecl.from_dict({"capacity": 100})
        assert (decl.capacity, decl.refill_amount, decl.refill_period) == (100, 100, 60)

    def test_burst_backcompat_still_validated(self):
        with pytest.raises(ValueError, match="capacity"):
            LimitDecl.from_dict({"capacity": 100, "burst": 0})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_provisioner_manifest.py::TestLimitDeclValidation -v`
Expected: FAIL — `DID NOT RAISE <class 'ValueError'>`

- [ ] **Step 3: Write minimal implementation**

In `LimitDecl.from_dict`, after the existing field resolution and before `return cls(...)`:

```python
        refill_amount = d.get("refill_amount", capacity)
        refill_period = d.get("refill_period", 60)
        for field_name, value in (
            ("capacity", capacity),
            ("refill_amount", refill_amount),
            ("refill_period", refill_period),
        ):
            if value <= 0:
                raise ValueError(
                    f"{field_name} must be positive, got {value}. "
                    "Limits are rejected at parse time so `limits plan` surfaces the "
                    "problem before anything is written."
                )
        return cls(
            capacity=capacity,
            refill_amount=refill_amount,
            refill_period=refill_period,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_provisioner_manifest.py -v`
Expected: PASS, including the pre-existing manifest tests

- [ ] **Step 5: Run lint and type check**

Run: `uv run ruff check --fix . && uv run ruff format . && uv run mypy`
Expected: clean

- [ ] **Step 6: Commit**

```bash
git add src/zae_limiter_provisioner/manifest.py tests/unit/test_provisioner_manifest.py
git commit -m "$(cat <<'EOF'
â¨ feat(provisioner): reject non-positive limit values at parse time

Nothing validated limit values, so `capacity: 0` was accepted and written
as a zero-capacity config. Rejecting at parse time means `limits plan`
surfaces it before anything is written, rather than Limit.__post_init__
raising inside the Lambda once bucket sync constructs real Limit objects.

Refs #468
EOF
)"
```

---

## Self-Review

**Spec coverage.** §5.2 requires: sync boto3 mirror of `_sync_bucket_params` (Tasks 1-2), GSI3 KEYS_ONLY discovery with two-pass race mitigation (Task 2), `cp`/`ra`/`rp` plus TTL handling and stale-attribute removal (Tasks 1, 3, 4), entity-level scope only (Task 4, asserted by `test_resource_and_system_levels_are_never_synced`), and both the set and delete paths (Task 4). The `sched`/`sched_tz`/`rsched`/`vu = 0` stamp is explicitly deferred to the surface plan, which extends `build_bucket_param_update` — that is the seam this plan leaves for it.

**Placeholders.** One deliberate and flagged: `apply_manifest` in Task 5, which Step 3 resolves against the real test suite rather than guessing at an entry point I have not read. Everything else is complete code.

**Type consistency.** `build_bucket_param_update` returns `(str, dict[str, str], dict[str, dict[str, str]])` and is consumed with exactly that shape in Task 2. `sync_bucket_params` is called with keyword arguments in Task 4 matching its Task 2 signature exactly. `resolve_effective_limits` returns manifest-shaped `dict[str, dict[str, int]]`, the same shape `build_bucket_param_update` consumes as `limits` and the same shape `Change.data["limits"]` carries.

**Resolved during review.** `calculate_bucket_ttl_seconds` is typed as `list[Limit]`, so Task 1's real `Limit` construction subjects manifest values to `Limit.__post_init__`. `manifest.py` was checked and validates only `namespace` — nothing constrains limit values, so `capacity: 0` is accepted today and would newly crash inside the Lambda. Task 6 closes that by validating at parse time, which also improves `limits plan`. Task 6 has no dependency on Tasks 1-5 and may be done first.

**Ordering.** Tasks 1-3 are pure additions with no behaviour change and can be reviewed independently. Task 4 is the commit that actually fixes the bug. Task 5 is the acceptance gate and must be seen to fail before Task 4 is restored. Task 6 is independent.
