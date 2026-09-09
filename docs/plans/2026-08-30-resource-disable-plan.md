# Resource & Entity Disable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let operators turn a resource off at the resource level and at the entity level, with an entity-level `disabled: false` re-admitting a specific entity to an otherwise-disabled resource, enforced immediately on the default speculative write path.

**Architecture:** `disabled` is stored as a tri-state attribute on existing config items (absent = inherit, `true`/`false` = explicit) and resolved by an independent walk over entity → entity-default → resource. Because `acquire()`'s default fast path issues a conditional `UpdateItem` straight at the bucket item and never reads config, the resolved value is **denormalized onto bucket items** and enforced with `attribute_not_exists(disabled)` in the existing `ConditionExpression` — the same shape as the TTL guard already there. Disabling is **eager**: the disable call writes config, then fans out to every affected bucket via GSI2/GSI3 before returning.

**Tech Stack:** Python 3.11+, aiobotocore (async) / boto3 (Lambda), DynamoDB single-table, Click CLI, pytest + moto (unit) + LocalStack (integration), AST-generated sync mirror (`hatch run generate-sync`).

**Spec:** `docs/adr/125-resource-disable.md` (created in Task 1). The research report that motivated it compares this design against the `capacity: 0` alternative.

## Global Constraints

- **Tri-state, not boolean.** `disabled` has three states everywhere it is stored or passed: `None` (inherit), `True`, `False`. Never collapse `None` into `False` at the storage or resolution layer — only at the final effective-value computation.
- **Resolution levels are entity(resource) → entity(`_default_`) → resource.** System level is explicitly **out of scope**; do not add `disabled` to system config. Rationale in ADR-125.
- **First explicit value wins**, independent of the limits walk. A level with `disabled` set but no limits still decides `disabled`.
- **Bucket items carry `disabled` only when effectively `True`.** When effectively `False`, the attribute is `REMOVE`d, so the guard stays `attribute_not_exists(#disabled)` and costs nothing on the enabled path.
- **Never write `disabled` in `build_composite_create`.** The slow path raises before creating a bucket for a disabled resource, so a newly created bucket is by construction enabled.
- Flat schema only — top-level attributes, no nested `data.M` (ADR-111).
- `disabled` is not a DynamoDB reserved word, but `resource`, `name`, `action`, and `timestamp` are. Keep using `#resource` aliases wherever those appear in the same expression.
- Every change to `repository.py`, `limiter.py`, `lease.py`, `repository_protocol.py`, `config_cache.py` **must** be followed by `hatch run generate-sync`; a pre-commit hook and CI both verify the generated mirror is current.
- Commit messages follow gitmoji conventional commits (`.claude/rules/commits.md`). Scopes for this work: `limiter`, `repository`, `schema`, `cli`, `provisioner`, `exceptions`, `test`, `docs`.
- All work happens on a feature branch; never commit to `main`.

---

## File Structure

**Created:**
- `docs/adr/125-resource-disable.md` — the architectural decision record.
- `tests/unit/test_disable.py` — resolution, tri-state serialization, enforcement, fan-out.
- `tests/integration/test_disable_fanout.py` — eager fan-out against LocalStack.
- `tests/unit/test_provisioner_manifest.py` — manifest parsing of the `disabled` field.
- `src/zae_limiter_provisioner/fanout.py` — sync boto3 fan-out for the Lambda.

**Modified:**
- `src/zae_limiter/schema.py` — attribute-name constants and tri-state codec helpers.
- `src/zae_limiter/exceptions.py` — `ResourceDisabled`.
- `src/zae_limiter/__init__.py` — export `ResourceDisabled`.
- `src/zae_limiter/repository_protocol.py` — `SpeculativeFailureReason.DISABLED`, protocol methods.
- `src/zae_limiter/repository.py` — config read/write of `disabled`, `resolve_disabled()`, speculative guard + classification, fan-out helpers, public disable/enable API.
- `src/zae_limiter/limiter.py` — slow-path gate, fast-path `ResourceDisabled` raise, re-raise passthrough.
- `src/zae_limiter/cli.py` — `resource disable/enable/clear-disabled`, `entity disable/enable/clear-disabled`.
- `src/zae_limiter_provisioner/manifest.py`, `applier.py`, `handler.py` — manifest field + Lambda-side fan-out.
- Generated sync mirrors (via `hatch run generate-sync`, never hand-edited).
- `CLAUDE.md`, `docs/cli.md`, `docs/api/exceptions.md`, `docs/guide/basic-usage.md`.

---

### Task 1: ADR-125 — record the decision

**Files:**
- Create: `docs/adr/125-resource-disable.md`

**Interfaces:**
- Consumes: nothing.
- Produces: the normative decision every later task cites. Task 10 links to it from `CLAUDE.md`.

- [ ] **Step 1: Write the ADR**

Create `docs/adr/125-resource-disable.md`:

```markdown
# ADR-125: Resource and Entity Disable

**Status:** Proposed
**Date:** 2026-08-30
**Issue:** TBD — link the tracking issue once filed

## Context

Operators need to turn a resource off, at the resource level and per entity, without
deleting its limit configuration. Two designs were considered: a `disabled` flag stored
beside `limits`, and reusing a limit with `capacity: 0`.

`capacity: 0` does not work. `acquire()` defaults to `speculative_writes=True`, whose
admission test is a conditional `UpdateItem` on the bucket item with
`b_{limit}_tk >= :consumed` — tokens, not capacity, and no config read at all.
`_sync_bucket_params` (ADR-120) propagates changed `cp`/`ra`/`rp` to an existing bucket
but never resets `tk`, and it only fires for entity-level `set_limits`/`delete_limits`
— `set_resource_defaults` and `set_system_defaults` have no fan-out. A bucket holding a
balance therefore keeps admitting traffic against a "disabled" resource. `capacity: 0`
would also require loosening `Limit.__post_init__`'s positive-only invariants, destroys
the configured capacity it overwrites, and surfaces a permanently-off resource as a
retryable 429 with a finite `retry_after_seconds` that never comes good.

## Decision

1. Store `disabled` as a **tri-state** attribute on existing config items: absent means
   inherit, `true`/`false` are explicit. Config items already carry non-limit siblings
   (`on_unavailable`, `resource`, `entity_id`), and `_deserialize_composite_limits`
   discovers limits by scanning for `l_*_cp`, so a sibling attribute is invisible to it.

2. Resolve `disabled` by an **independent walk** over entity(resource) →
   entity(`_default_`) → resource. First explicit value wins, regardless of whether that
   level defines limits. This is what makes an entity-level `disabled: false` re-admit a
   specific entity to a disabled resource.

3. **Denormalize** the resolved value onto bucket items and enforce it by adding
   `attribute_not_exists(#disabled)` to the speculative `ConditionExpression`. Bucket
   items already denormalize `cascade`, `parent_id` and `shard_count` for exactly this
   reason, and the same expression already carries a non-token guard for TTL.

4. Enforce **eagerly**: the disable call writes config and then fans out to every
   affected bucket before returning, via GSI2 (`GSI2PK={ns}/RESOURCE#{name}`,
   `GSI2SK begins_with BUCKET#`) for resource scope and GSI3
   (`GSI3PK={ns}/ENTITY#{id}`) for entity scope.

5. Raise a distinct `ResourceDisabled` exception rather than `RateLimitExceeded`.
   Disabled is closer to a 403 than a 429 and nothing should tell a client to retry.

## Scope

System-level `disabled` is **not** implemented. A system-level kill switch would have to
fan out across every bucket in the namespace (GSI4) and its blast radius warrants its own
decision. Resolution stops at the resource level.

## Consequences

**Positive:**
- Takes effect on the default fast path, which is the only path that matters in steady state.
- Limits survive the disable; re-enabling is one attribute.
- Token-bucket invariants in `models.py` and `bucket.py` are untouched.
- Callers can distinguish "intentionally off" from "temporarily saturated".

**Negative:**
- Disable is O(buckets for the resource) writes, not O(1). Bounded by entity count x shards.
- A narrow race exists between the config write and the fan-out query: an `acquire()`
  already in flight can create a bucket the fan-out's GSI query does not see. Mitigated by
  a second fan-out pass; the residual window is one in-flight acquire.
- `set_resource_defaults` / `set_limits` become read-before-write to preserve `disabled`,
  costing 1 extra RCU on an infrequent admin path.
- The provisioner's Lambda-side fan-out does not evaluate per-entity overrides; the
  handler orders resource changes before entity changes so a carve-out re-stamps last.

## Alternatives Considered

### Limit with `capacity: 0`
Rejected — see Context. Does not reach the fast path, and overloads a real bucket parameter.

### Config-only flag with no denormalization
Rejected because the fast path never reads config; the flag would only take effect on the
slow path, which steady-state traffic does not use.

### Stream-driven fan-out via the aggregator
Rejected for the initial implementation because it makes disable asynchronous with no
completion signal. A kill switch should not return before it has taken effect. Worth
revisiting as a repair mechanism for the in-flight-acquire race.

### Reserved synthetic always-failing limit (`wcu`-style)
Rejected because it still rides on `tk` and inherits the same stale-balance problem
unless tokens are explicitly zeroed.
```

- [ ] **Step 2: Commit**

```bash
git add docs/adr/125-resource-disable.md
git commit -m "📝 docs(adr): add ADR-125 for resource and entity disable"
```

---

### Task 2: Schema constants and tri-state codec

**Files:**
- Modify: `src/zae_limiter/schema.py`
- Test: `tests/unit/test_disable.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `schema.CONFIG_FIELD_DISABLED: str` (`"disabled"`)
  - `schema.BUCKET_FIELD_DISABLED: str` (`"disabled"`)
  - `schema.encode_disabled(value: bool | None) -> dict[str, Any] | None`
  - `schema.decode_disabled(item: dict[str, Any]) -> bool | None`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_disable.py`:

```python
"""Tests for resource/entity disable (ADR-125)."""

from zae_limiter import schema


class TestDisabledCodec:
    def test_encode_none_returns_none(self):
        assert schema.encode_disabled(None) is None

    def test_encode_true(self):
        assert schema.encode_disabled(True) == {"BOOL": True}

    def test_encode_false(self):
        assert schema.encode_disabled(False) == {"BOOL": False}

    def test_decode_absent_attribute_is_none(self):
        assert schema.decode_disabled({"PK": {"S": "ns/RESOURCE#gpt-4"}}) is None

    def test_decode_true(self):
        assert schema.decode_disabled({"disabled": {"BOOL": True}}) is True

    def test_decode_false_is_false_not_none(self):
        # Explicit False must be distinguishable from "inherit"
        assert schema.decode_disabled({"disabled": {"BOOL": False}}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_disable.py -v`
Expected: FAIL with `AttributeError: module 'zae_limiter.schema' has no attribute 'encode_disabled'`

- [ ] **Step 3: Add the constants and codec**

In `src/zae_limiter/schema.py`, next to the existing `BUCKET_FIELD_*` block (around lines 63-68), add:

```python
# Disable flag (ADR-125). Tri-state on config items: absent = inherit,
# True/False = explicit. On bucket items the attribute is present only
# when the bucket is effectively disabled, so the speculative guard can
# stay `attribute_not_exists(disabled)`.
CONFIG_FIELD_DISABLED = "disabled"
BUCKET_FIELD_DISABLED = "disabled"
```

Then add the codec functions near the other serialization helpers:

```python
def encode_disabled(value: bool | None) -> dict[str, Any] | None:
    """Encode a tri-state disabled value as a DynamoDB attribute.

    Args:
        value: True, False, or None (meaning "inherit from the level above").

    Returns:
        A DynamoDB BOOL attribute, or None when the value is "inherit"
        (the caller should omit the attribute entirely).
    """
    if value is None:
        return None
    return {"BOOL": value}


def decode_disabled(item: dict[str, Any]) -> bool | None:
    """Decode the tri-state disabled attribute from a DynamoDB item.

    Returns:
        True or False when explicitly set, None when the attribute is
        absent (meaning "inherit from the level above").
    """
    attr = item.get(CONFIG_FIELD_DISABLED)
    if not attr:
        return None
    return bool(attr.get("BOOL", False))
```

`schema.py` already has `from typing import TYPE_CHECKING, Any` at line 3, so no import change
is needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_disable.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/zae_limiter/schema.py tests/unit/test_disable.py
git commit -m "✨ feat(schema): add tri-state disabled attribute codec"
```

---

### Task 3: The `ResourceDisabled` exception

**Files:**
- Modify: `src/zae_limiter/exceptions.py`
- Modify: `src/zae_limiter/__init__.py`
- Modify: `docs/api/exceptions.md`
- Test: `tests/unit/test_exceptions.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ResourceDisabled(entity_id: str, resource: str, level: str)` with attributes
  `entity_id`, `resource`, `level`. Raised by `limiter.acquire()` on both paths.

**Note on placement:** per `.claude/rules/exceptions.md`, `except RateLimitError` must only
catch "you're going too fast" scenarios, so `ResourceDisabled` is a **direct
`ZAELimiterError` subclass**, not a `RateLimitError`. It represents a condition rather than
an error, so it omits the `Error` suffix and carries `# noqa: N818`, like `RateLimitExceeded`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_exceptions.py`:

```python
class TestResourceDisabled:
    def test_attributes(self):
        from zae_limiter.exceptions import ResourceDisabled

        exc = ResourceDisabled(entity_id="user-1", resource="gpt-4", level="resource")
        assert exc.entity_id == "user-1"
        assert exc.resource == "gpt-4"
        assert exc.level == "resource"

    def test_message_names_entity_and_resource(self):
        from zae_limiter.exceptions import ResourceDisabled

        exc = ResourceDisabled(entity_id="user-1", resource="gpt-4", level="resource")
        assert "user-1" in str(exc)
        assert "gpt-4" in str(exc)

    def test_is_zae_limiter_error_but_not_rate_limit_error(self):
        from zae_limiter.exceptions import (
            RateLimitError,
            ResourceDisabled,
            ZAELimiterError,
        )

        exc = ResourceDisabled(entity_id="user-1", resource="gpt-4", level="entity")
        assert isinstance(exc, ZAELimiterError)
        # A disabled resource is not a throttling signal.
        assert not isinstance(exc, RateLimitError)

    def test_exported_from_package_root(self):
        import zae_limiter

        assert hasattr(zae_limiter, "ResourceDisabled")
        assert "ResourceDisabled" in zae_limiter.__all__
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_exceptions.py -k ResourceDisabled -v`
Expected: FAIL with `ImportError: cannot import name 'ResourceDisabled'`

- [ ] **Step 3: Add the exception**

In `src/zae_limiter/exceptions.py`, add a new section after the Rate Limit Exceptions block:

```python
# ---------------------------------------------------------------------------
# Configuration State Exceptions
# ---------------------------------------------------------------------------


class ResourceDisabled(ZAELimiterError):  # noqa: N818
    """
    Raised when a resource is disabled for the requesting entity.

    This is a configuration state, not a throttling signal: it does not
    inherit from RateLimitError and carries no retry hint, because retrying
    will not help. Map it to 403, not 429.

    The resolved value comes from the first level that sets ``disabled``
    explicitly, walking entity -> entity default -> resource (ADR-125).

    Attributes:
        entity_id: Entity that attempted the acquire
        resource: Resource that is disabled
        level: Config level that decided it ("entity", "entity_default",
            "resource", or "bucket" when the decision came from the
            denormalized bucket attribute on the fast path)
    """

    def __init__(self, entity_id: str, resource: str, level: str) -> None:
        self.entity_id = entity_id
        self.resource = resource
        self.level = level
        super().__init__(
            f"Resource '{resource}' is disabled for entity '{entity_id}' "
            f"(disabled at {level} level)"
        )
```

- [ ] **Step 4: Export it**

In `src/zae_limiter/__init__.py`, add `ResourceDisabled` to the `from .exceptions import (...)`
block and to `__all__`, keeping both lists in their existing alphabetical position.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_exceptions.py -k ResourceDisabled -v`
Expected: PASS (4 tests)

- [ ] **Step 6: Document it**

In `docs/api/exceptions.md`, add an autodoc entry:

```markdown
### ResourceDisabled

::: zae_limiter.ResourceDisabled
```

and add it to the hierarchy diagram in that file as a direct child of `ZAELimiterError`.

- [ ] **Step 7: Commit**

```bash
git add src/zae_limiter/exceptions.py src/zae_limiter/__init__.py \
        tests/unit/test_exceptions.py docs/api/exceptions.md
git commit -m "✨ feat(exceptions): add ResourceDisabled for disabled resources"
```

---

### Task 4: Read and preserve `disabled` on config items

**Files:**
- Modify: `src/zae_limiter/repository.py` (`set_limits` ~line 2513, `get_limits` ~2790, `set_resource_defaults` ~2983, `get_resource_defaults` ~3048, `set_system_defaults` ~3146)
- Test: `tests/unit/test_disable.py`

**Interfaces:**
- Consumes: `schema.encode_disabled`, `schema.decode_disabled`, `schema.CONFIG_FIELD_DISABLED` (Task 2).
- Produces:
  - `Repository.get_resource_disabled(resource: str) -> bool | None`
  - `Repository.get_entity_disabled(entity_id: str, resource: str) -> bool | None`
  - `set_resource_defaults(...)` and `set_limits(...)` gain a keyword-only
    `disabled: bool | None = _PRESERVE_DISABLED` parameter.

**Why read-before-write:** `set_resource_defaults` and `set_limits` are full-replace
`PutItem` calls. Without preservation, an operator calling `set_resource_defaults()` to
adjust limits would silently re-enable a disabled resource.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_disable.py`. The unit suite is moto-backed, so define the two
fixtures locally rather than reaching for `tests/fixtures/repositories.py` — the `test_repo`
fixture there is built on `shared_minimal_stack` and requires LocalStack. Deliberately
distinct names, so nothing shadows the integration fixtures:

```python
import pytest

from zae_limiter import RateLimiter
from zae_limiter.models import Limit
from zae_limiter.repository import Repository


@pytest.fixture
async def disable_repo(mock_dynamodb):
    """Moto-backed repository for disable tests."""
    repo = Repository(
        name="test-disable", region="us-east-1", _skip_deprecation_warning=True
    )
    await repo.create_table()
    await repo._register_namespace("default")
    yield repo
    await repo.close()


@pytest.fixture
async def disable_limiter(disable_repo):
    """RateLimiter sharing the disable_repo table."""
    limiter = RateLimiter(repository=disable_repo)
    async with limiter:
        yield limiter


@pytest.mark.asyncio
class TestConfigDisabledPersistence:
    async def test_resource_disabled_defaults_to_none(self, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        assert await disable_repo.get_resource_disabled("gpt-4") is None

    async def test_set_resource_defaults_with_disabled_true(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        assert await disable_repo.get_resource_disabled("gpt-4") is True

    async def test_set_resource_defaults_preserves_existing_disabled(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        # A caller that knows nothing about `disabled` must not re-enable it.
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 200)])
        assert await disable_repo.get_resource_disabled("gpt-4") is True

    async def test_explicit_false_is_preserved_and_distinct_from_none(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=False
        )
        assert await disable_repo.get_resource_disabled("gpt-4") is False

    async def test_entity_disabled_roundtrip(self, disable_repo):
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=False
        )
        assert await disable_repo.get_entity_disabled("user-1", "gpt-4") is False
```

`mock_dynamodb` comes from `tests/fixtures/moto.py` and is already available to
`tests/unit/`; `tests/unit/test_repository.py` builds its own `repo` fixture the same way.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_disable.py -k ConfigDisabledPersistence -v`
Expected: FAIL with `AttributeError: 'Repository' object has no attribute 'get_resource_disabled'`

- [ ] **Step 3: Add a preserve sentinel and the getters**

In `src/zae_limiter/repository.py`, near the module-level constants add:

```python
#: Sentinel meaning "keep whatever `disabled` value is already stored" (ADR-125).
#: Distinct from None, which explicitly means "inherit from the level above".
_PRESERVE_DISABLED: Any = object()
```

Then add the two getters alongside the other config reads:

```python
    async def get_resource_disabled(self, resource: str) -> bool | None:
        """Read the tri-state disabled flag from a resource config item.

        Returns:
            True or False when explicitly set, None when unset (inherit).
        """
        validate_resource(resource)
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
                "SK": {"S": schema.sk_config()},
            },
            ConsistentRead=False,
        )
        item = response.get("Item")
        if not item:
            return None
        return schema.decode_disabled(item)

    async def get_entity_disabled(self, entity_id: str, resource: str) -> bool | None:
        """Read the tri-state disabled flag from an entity config item.

        Returns:
            True or False when explicitly set, None when unset (inherit).
        """
        client = await self._get_client()
        response = await client.get_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
                "SK": {"S": schema.sk_config(resource)},
            },
            ConsistentRead=False,
        )
        item = response.get("Item")
        if not item:
            return None
        return schema.decode_disabled(item)
```

- [ ] **Step 4: Thread `disabled` through the setters**

Change `set_resource_defaults`'s signature to add a keyword-only parameter:

```python
    async def set_resource_defaults(
        self,
        resource: str,
        limits: list[Limit],
        principal: str | None = None,
        *,
        disabled: bool | None = _PRESERVE_DISABLED,
    ) -> None:
```

Immediately after `validate_resource(resource)` and before the item is built, add:

```python
        # Full-replace PutItem would drop `disabled`; preserve it unless the
        # caller passed an explicit value (ADR-125).
        if disabled is _PRESERVE_DISABLED:
            disabled = await self.get_resource_disabled(resource)
```

Then, after the `self._serialize_composite_limits(limits, item)` call, add:

```python
        disabled_attr = schema.encode_disabled(disabled)
        if disabled_attr is not None:
            item[schema.CONFIG_FIELD_DISABLED] = disabled_attr
```

Apply the same three edits to `set_limits`, using `get_entity_disabled(entity_id, resource)`
for the preserve read. Do **not** add the parameter to `set_system_defaults` — system-level
disable is out of scope per ADR-125.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_disable.py -k ConfigDisabledPersistence -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Regenerate sync and verify**

```bash
hatch run generate-sync
uv run pytest tests/unit/ -q
uv run mypy src/zae_limiter
```
Expected: all pass, and `git status` shows the regenerated `sync_*.py` files.

- [ ] **Step 7: Commit**

```bash
git add src/zae_limiter/ tests/unit/test_disable.py
git commit -m "✨ feat(repository): store and preserve tri-state disabled on config items"
```

---

### Task 5: `resolve_disabled()` — the independent three-level walk

**Files:**
- Modify: `src/zae_limiter/repository.py` (Config resolution section, ~line 4189)
- Modify: `src/zae_limiter/repository_protocol.py`
- Test: `tests/unit/test_disable.py`

**Interfaces:**
- Consumes: `schema.decode_disabled` (Task 2).
- Produces: `Repository.resolve_disabled(entity_id: str, resource: str) -> tuple[bool, str | None]`
  returning `(effective_disabled, deciding_level)` where level is `"entity"`,
  `"entity_default"`, `"resource"`, or `None` when nothing set it.

**Why uncached:** this deliberately does **not** go through `ConfigCache`. It is called only
on the slow path (which already costs 3 round trips) and by the fan-out, so a 60-second
stale read would be far more dangerous than the ~1.5 RCU it costs. The fast path never
calls it — it is guarded by the denormalized bucket attribute instead. This is what keeps
the change out of the hot config-cache code.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_disable.py`:

```python
@pytest.mark.asyncio
class TestResolveDisabled:
    async def test_nothing_set_resolves_false(self, disable_repo):
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, None)

    async def test_resource_disabled_applies_to_entity(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

    async def test_entity_false_overrides_resource_true(self, disable_repo):
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=False
        )
        # The whole point of the feature: a carve-out for one entity.
        assert await disable_repo.resolve_disabled("vip-1", "gpt-4") == (False, "entity")
        # Other entities stay disabled.
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")

    async def test_entity_true_overrides_resource_unset(self, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "bad-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=True
        )
        assert await disable_repo.resolve_disabled("bad-1", "gpt-4") == (True, "entity")

    async def test_entity_default_disables_all_resources_for_entity(self, disable_repo):
        await disable_repo.set_limits(
            "banned-1", [Limit.per_minute("rpm", 10)], resource="_default_", disabled=True
        )
        assert await disable_repo.resolve_disabled("banned-1", "gpt-4") == (
            True,
            "entity_default",
        )

    async def test_resource_specific_entity_beats_entity_default(self, disable_repo):
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 10)], resource="_default_", disabled=True
        )
        await disable_repo.set_limits(
            "user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4", disabled=False
        )
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (False, "entity")

    async def test_disabled_resolves_independently_of_limits(self, disable_repo):
        # The resource sets `disabled` but the entity supplies the limits.
        # The limits walk stops at "entity"; the disabled walk must still
        # reach "resource".
        await disable_repo.set_resource_defaults("gpt-4", [], disabled=True)
        await disable_repo.set_limits("user-1", [Limit.per_minute("rpm", 10)], resource="gpt-4")
        assert await disable_repo.resolve_disabled("user-1", "gpt-4") == (True, "resource")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_disable.py -k TestResolveDisabled -v`
Expected: FAIL with `AttributeError: 'Repository' object has no attribute 'resolve_disabled'`

- [ ] **Step 3: Implement the walk**

Add to `src/zae_limiter/repository.py` in the "Config resolution (ADR-122)" section:

```python
    async def resolve_disabled(
        self,
        entity_id: str,
        resource: str,
    ) -> tuple[bool, str | None]:
        """Resolve the effective disabled state for an entity+resource (ADR-125).

        Walks entity(resource) -> entity(_default_) -> resource and returns the
        first level that sets `disabled` explicitly. This walk is independent of
        the limits walk in resolve_limits(): a level that sets `disabled` but
        defines no limits still decides the outcome, which is what lets an
        entity-level `disabled: false` re-admit one entity to a disabled resource.

        Deliberately uncached — see ADR-125. Called only on the slow path and by
        the eager fan-out, never on the speculative fast path.

        Args:
            entity_id: Entity to resolve for
            resource: Resource being accessed

        Returns:
            (effective_disabled, deciding_level) where deciding_level is
            "entity", "entity_default", "resource", or None if nothing set it.
        """
        ns = self._namespace_id
        levels: list[tuple[str, str, str]] = [
            ("entity", schema.pk_entity(ns, entity_id), schema.sk_config(resource)),
        ]
        if resource != schema.DEFAULT_RESOURCE:
            levels.append(
                (
                    "entity_default",
                    schema.pk_entity(ns, entity_id),
                    schema.sk_config(schema.DEFAULT_RESOURCE),
                )
            )
        levels.append(("resource", schema.pk_resource(ns, resource), schema.sk_config()))

        client = await self._get_client()
        response = await client.batch_get_item(
            RequestItems={
                self.table_name: {
                    "Keys": [{"PK": {"S": pk}, "SK": {"S": sk}} for _, pk, sk in levels],
                    "ConsistentRead": False,
                }
            }
        )
        items = response.get("Responses", {}).get(self.table_name, [])
        by_key = {
            (i.get("PK", {}).get("S", ""), i.get("SK", {}).get("S", "")): i for i in items
        }

        for level, pk, sk in levels:
            item = by_key.get((pk, sk))
            if item is None:
                continue
            value = schema.decode_disabled(item)
            if value is not None:
                return value, level

        return False, None
```

- [ ] **Step 4: Add it to the protocol**

In `src/zae_limiter/repository_protocol.py`, inside `RepositoryProtocol`, add:

```python
    async def resolve_disabled(
        self,
        entity_id: str,
        resource: str,
    ) -> tuple[bool, str | None]:
        """Resolve the effective disabled state for an entity+resource (ADR-125)."""
        ...
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/test_disable.py -k TestResolveDisabled -v`
Expected: PASS (7 tests)

- [ ] **Step 6: Regenerate sync and commit**

```bash
hatch run generate-sync
uv run pytest tests/unit/ -q
git add src/zae_limiter/ tests/unit/test_disable.py
git commit -m "✨ feat(repository): resolve disabled via independent three-level walk"
```

---

### Task 6: Eager fan-out and the public disable/enable API

**Files:**
- Modify: `src/zae_limiter/repository.py`
- Modify: `src/zae_limiter/repository_protocol.py`
- Test: `tests/unit/test_disable.py`

**Interfaces:**
- Consumes: `resolve_disabled` (Task 5), `schema.BUCKET_FIELD_DISABLED` (Task 2).
- Produces:
  - `Repository.disable_resource(resource, principal=None) -> int`
  - `Repository.enable_resource(resource, principal=None) -> int`
  - `Repository.clear_resource_disabled(resource, principal=None) -> int`
  - `Repository.disable_entity(entity_id, resource=None, principal=None) -> int`
  - `Repository.enable_entity(entity_id, resource=None, principal=None) -> int`
  - `Repository.clear_entity_disabled(entity_id, resource=None, principal=None) -> int`

  Each returns the number of bucket items stamped. `resource=None` on the entity variants
  targets the entity's `_default_` config, disabling it across every resource.

**Order of operations (required):** write config **first**, then fan out. Any acquire
starting after the config write resolves the new value on the slow path; the fan-out then
catches buckets that already exist. Run the discovery query **twice** to catch buckets
created by an acquire that was already in flight during the first pass.

This task comes before fast-path enforcement so that Task 7's tests have a way to disable
something.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_disable.py`:

```python
@pytest.mark.asyncio
class TestFanout:
    async def test_disable_resource_stamps_existing_buckets(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        for entity in ("user-1", "user-2", "user-3"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass

        assert await disable_repo.disable_resource("gpt-4") == 3

    async def test_disable_resource_skips_entities_with_false_override(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 100)], resource="gpt-4", disabled=False
        )
        for entity in ("user-1", "vip-1"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass

        # vip-1 is carved out, so only user-1's bucket is stamped.
        assert await disable_repo.disable_resource("gpt-4") == 1

    async def test_disable_is_idempotent(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        assert await disable_repo.disable_resource("gpt-4") == 1
        assert await disable_repo.disable_resource("gpt-4") == 1

    async def test_enable_resource_clears_the_stamp(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")
        assert await disable_repo.enable_resource("gpt-4") == 1
        assert await disable_repo.get_resource_disabled("gpt-4") is False

    async def test_disable_entity_covers_all_resources(self, disable_limiter, disable_repo):
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        for res in ("gpt-4", "claude-3"):
            async with disable_limiter.acquire("user-1", res, {"rpm": 1}):
                pass

        assert await disable_repo.disable_entity("user-1") == 2

    async def test_disable_entity_scoped_to_one_resource(self, disable_limiter, disable_repo):
        await disable_repo.set_system_defaults([Limit.per_minute("rpm", 100)])
        for res in ("gpt-4", "claude-3"):
            async with disable_limiter.acquire("user-1", res, {"rpm": 1}):
                pass

        assert await disable_repo.disable_entity("user-1", resource="gpt-4") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_disable.py -k TestFanout -v`
Expected: FAIL with `AttributeError: 'Repository' object has no attribute 'disable_resource'`

- [ ] **Step 3: Implement the bucket stamp primitive**

Add to `src/zae_limiter/repository.py`:

```python
    async def _stamp_bucket_disabled(self, pk: str, disabled: bool) -> None:
        """Set or remove the `disabled` attribute on one bucket item (ADR-125).

        The attribute is present only when the bucket is effectively disabled,
        which keeps the speculative guard as a cheap attribute_not_exists check.

        Args:
            pk: Full bucket partition key (already namespace- and shard-qualified)
            disabled: True to stamp the bucket, False to clear the stamp
        """
        client = await self._get_client()
        kwargs: dict[str, Any] = {
            "TableName": self.table_name,
            "Key": {"PK": {"S": pk}, "SK": {"S": schema.sk_state()}},
            "ExpressionAttributeNames": {"#disabled": schema.BUCKET_FIELD_DISABLED},
            # Never resurrect a bucket that TTL or a delete removed.
            "ConditionExpression": "attribute_exists(PK)",
        }
        if disabled:
            kwargs["UpdateExpression"] = "SET #disabled = :true"
            kwargs["ExpressionAttributeValues"] = {":true": {"BOOL": True}}
        else:
            kwargs["UpdateExpression"] = "REMOVE #disabled"

        try:
            await client.update_item(**kwargs)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            # Bucket vanished between discovery and stamp — nothing to disable.
```

- [ ] **Step 4: Implement bucket discovery for both scopes**

```python
    async def _discover_resource_bucket_pks(self, resource: str) -> list[tuple[str, str]]:
        """Find every bucket PK for a resource, across entities and shards.

        Uses GSI2 (GSI2PK={ns}/RESOURCE#{name}, GSI2SK begins_with BUCKET#),
        the same access pattern used for resource capacity aggregation.

        Returns:
            List of (bucket_pk, entity_id) tuples.
        """
        client = await self._get_client()
        results: list[tuple[str, str]] = []
        start_key: dict[str, Any] | None = None

        while True:
            params: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": schema.GSI2_NAME,
                "KeyConditionExpression": "GSI2PK = :pk AND begins_with(GSI2SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": schema.gsi2_pk_resource(self._namespace_id, resource)},
                    ":sk": {"S": "BUCKET#"},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = await client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if not pk:
                    continue
                _ns, entity_id, _res, _shard = schema.parse_bucket_pk(pk)
                results.append((pk, entity_id))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break

        return results

    async def _discover_entity_bucket_pks(
        self, entity_id: str, resource: str | None
    ) -> list[str]:
        """Find every bucket PK for an entity, optionally scoped to one resource.

        Uses GSI3 (GSI3PK={ns}/ENTITY#{id}, GSI3SK begins_with BUCKET#{resource}#),
        the KEYS_ONLY discovery index added for GHSA-76rv.

        Returns:
            List of bucket PKs.
        """
        client = await self._get_client()
        pks: list[str] = []
        start_key: dict[str, Any] | None = None
        sk_prefix = f"BUCKET#{resource}#" if resource else "BUCKET#"

        while True:
            params: dict[str, Any] = {
                "TableName": self.table_name,
                "IndexName": schema.GSI3_NAME,
                "KeyConditionExpression": "GSI3PK = :pk AND begins_with(GSI3SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": {"S": schema.gsi3_pk_entity(self._namespace_id, entity_id)},
                    ":sk": {"S": sk_prefix},
                },
            }
            if start_key:
                params["ExclusiveStartKey"] = start_key
            response = await client.query(**params)
            for item in response.get("Items", []):
                pk = item.get("PK", {}).get("S", "")
                if pk:
                    pks.append(pk)
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break

        return pks
```

- [ ] **Step 5: Implement the two fan-out drivers**

```python
    async def _fanout_resource(self, resource: str, disabled: bool) -> int:
        """Stamp every bucket for a resource, honoring per-entity overrides.

        An entity whose own config resolves to a different value than the
        resource-level one is skipped — that is what makes an entity-level
        `disabled: false` a carve-out from a disabled resource (ADR-125).

        Runs the discovery query twice: the second pass catches buckets created
        by an acquire that was already in flight during the first pass.

        Returns:
            Number of bucket items stamped.
        """
        stamped: set[str] = set()
        effective_by_entity: dict[str, bool] = {}

        for _pass in range(2):
            for pk, entity_id in await self._discover_resource_bucket_pks(resource):
                if pk in stamped:
                    continue
                if entity_id not in effective_by_entity:
                    effective, _level = await self.resolve_disabled(entity_id, resource)
                    effective_by_entity[entity_id] = effective
                if effective_by_entity[entity_id] != disabled:
                    # This entity overrides the resource-level value; leave it alone.
                    continue
                await self._stamp_bucket_disabled(pk, disabled)
                stamped.add(pk)

        return len(stamped)

    async def _fanout_entity(
        self, entity_id: str, resource: str | None, disabled: bool
    ) -> int:
        """Stamp every bucket for an entity (optionally scoped to one resource).

        Returns:
            Number of bucket items stamped.
        """
        stamped: set[str] = set()
        for _pass in range(2):
            for pk in await self._discover_entity_bucket_pks(entity_id, resource):
                if pk in stamped:
                    continue
                await self._stamp_bucket_disabled(pk, disabled)
                stamped.add(pk)
        return len(stamped)
```

- [ ] **Step 6: Implement the resource-level public API**

```python
    async def disable_resource(self, resource: str, principal: str | None = None) -> int:
        """Disable a resource for all entities without an explicit override (ADR-125).

        Writes config first, then eagerly stamps every existing bucket so the
        change takes effect on the speculative fast path immediately.

        Args:
            resource: Resource to disable
            principal: Caller identity for audit logging

        Returns:
            Number of bucket items stamped.
        """
        return await self._set_resource_disabled(resource, True, principal)

    async def enable_resource(self, resource: str, principal: str | None = None) -> int:
        """Explicitly enable a resource (stores `disabled: false`).

        Returns:
            Number of bucket items unstamped.
        """
        return await self._set_resource_disabled(resource, False, principal)

    async def clear_resource_disabled(
        self, resource: str, principal: str | None = None
    ) -> int:
        """Remove the resource's explicit disabled value, reverting to inherit.

        Returns:
            Number of bucket items unstamped.
        """
        return await self._set_resource_disabled(resource, None, principal)

    async def _set_resource_disabled(
        self, resource: str, value: bool | None, principal: str | None
    ) -> int:
        validate_resource(resource)
        client = await self._get_client()

        # 1. Write config first, so any acquire starting from now resolves the
        #    new value on the slow path.
        key = {
            "PK": {"S": schema.pk_resource(self._namespace_id, resource)},
            "SK": {"S": schema.sk_config()},
        }
        common: dict[str, Any] = {
            "TableName": self.table_name,
            "Key": key,
            "ExpressionAttributeNames": {"#disabled": schema.CONFIG_FIELD_DISABLED},
            "ConditionExpression": "attribute_exists(PK)",
        }
        if value is None:
            await client.update_item(UpdateExpression="REMOVE #disabled", **common)
        else:
            await client.update_item(
                UpdateExpression="SET #disabled = :v",
                ExpressionAttributeValues={":v": {"BOOL": value}},
                **common,
            )

        await self.invalidate_config_cache()

        # 2. Fan out to existing buckets. For a clear, the effective value is
        #    whatever the resource now inherits, which with no system-level
        #    disable is always False.
        count = await self._fanout_resource(resource, disabled=bool(value))

        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=f"$RESOURCE:{resource}",
            principal=principal,
            resource=resource,
            details={"disabled": value, "buckets_stamped": count},
        )
        return count
```

- [ ] **Step 7: Implement the entity-level public API**

```python
    async def disable_entity(
        self,
        entity_id: str,
        resource: str | None = None,
        principal: str | None = None,
    ) -> int:
        """Disable an entity, for one resource or across all of them (ADR-125).

        Args:
            entity_id: Entity to disable
            resource: Resource to scope to. None targets the entity's
                `_default_` config, disabling it for every resource.
            principal: Caller identity for audit logging

        Returns:
            Number of bucket items stamped.
        """
        return await self._set_entity_disabled(entity_id, resource, True, principal)

    async def enable_entity(
        self,
        entity_id: str,
        resource: str | None = None,
        principal: str | None = None,
    ) -> int:
        """Explicitly enable an entity, overriding a disabled resource.

        Returns:
            Number of bucket items unstamped.
        """
        return await self._set_entity_disabled(entity_id, resource, False, principal)

    async def clear_entity_disabled(
        self,
        entity_id: str,
        resource: str | None = None,
        principal: str | None = None,
    ) -> int:
        """Remove the entity's explicit disabled value, reverting to inherit.

        Returns:
            Number of bucket items restamped to match the inherited value.
        """
        return await self._set_entity_disabled(entity_id, resource, None, principal)

    async def _set_entity_disabled(
        self,
        entity_id: str,
        resource: str | None,
        value: bool | None,
        principal: str | None,
    ) -> int:
        target_resource = resource if resource is not None else schema.DEFAULT_RESOURCE
        client = await self._get_client()

        key = {
            "PK": {"S": schema.pk_entity(self._namespace_id, entity_id)},
            "SK": {"S": schema.sk_config(target_resource)},
        }
        if value is None:
            await client.update_item(
                TableName=self.table_name,
                Key=key,
                UpdateExpression="REMOVE #disabled",
                ExpressionAttributeNames={"#disabled": schema.CONFIG_FIELD_DISABLED},
                ConditionExpression="attribute_exists(PK)",
            )
        else:
            # The entity may have no config item yet — create a minimal one so
            # the override is durable even with no entity-level limits.
            await client.update_item(
                TableName=self.table_name,
                Key=key,
                UpdateExpression=(
                    "SET #disabled = :v,"
                    " entity_id = if_not_exists(entity_id, :eid),"
                    " #resource = if_not_exists(#resource, :res),"
                    " GSI4PK = if_not_exists(GSI4PK, :ns)"
                ),
                ExpressionAttributeNames={
                    "#disabled": schema.CONFIG_FIELD_DISABLED,
                    "#resource": "resource",
                },
                ExpressionAttributeValues={
                    ":v": {"BOOL": value},
                    ":eid": {"S": entity_id},
                    ":res": {"S": target_resource},
                    ":ns": {"S": self._namespace_id},
                },
            )

        self._config_cache.evict_entity(entity_id, target_resource)

        # For an explicit value the effective state is that value. For a clear,
        # recompute what the entity now inherits.
        if value is None:
            effective, _level = await self.resolve_disabled(entity_id, target_resource)
        else:
            effective = value

        count = await self._fanout_entity(entity_id, resource, disabled=effective)

        await self._log_audit_event(
            action=AuditAction.LIMITS_SET,
            entity_id=entity_id,
            principal=principal,
            resource=target_resource,
            details={"disabled": value, "buckets_stamped": count},
        )
        return count
```

- [ ] **Step 8: Add the six methods to the protocol**

Add matching `async def ... -> int: ...` stubs for `disable_resource`, `enable_resource`,
`clear_resource_disabled`, `disable_entity`, `enable_entity`, and `clear_entity_disabled`
to `RepositoryProtocol` in `src/zae_limiter/repository_protocol.py`, each with a one-line
docstring matching the implementation.

- [ ] **Step 9: Run tests**

Run: `uv run pytest tests/unit/test_disable.py -k TestFanout -v`
Expected: PASS (6 tests)

- [ ] **Step 10: Regenerate sync and commit**

```bash
hatch run generate-sync
uv run pytest tests/unit/ -q && uv run mypy src/zae_limiter
git add src/zae_limiter/ tests/unit/test_disable.py
git commit -m "✨ feat(repository): add eager disable fan-out and disable/enable API"
```

---

### Task 7: Enforcement on both acquire paths

**Files:**
- Modify: `src/zae_limiter/repository.py` (`speculative_consume` condition build ~lines 2325-2380, failure classification ~2414-2451)
- Modify: `src/zae_limiter/repository_protocol.py` (`SpeculativeFailureReason`)
- Modify: `src/zae_limiter/limiter.py` (`acquire` re-raise ~line 676, `_try_speculative_acquire` ~733, `_do_acquire` ~1213)
- Test: `tests/unit/test_disable.py`

**Interfaces:**
- Consumes: `schema.BUCKET_FIELD_DISABLED` (Task 2), `ResourceDisabled` (Task 3),
  `resolve_disabled` (Task 5), `disable_resource` (Task 6).
- Produces: `SpeculativeFailureReason.DISABLED`; `acquire()` raises `ResourceDisabled` on
  both the fast and slow paths.

**Ordering requirement:** `DISABLED` must be classified and handled **before** the
wcu/shard-doubling and shard-retry branches. Otherwise a disabled bucket triggers pointless
`bump_shard_count` calls and shard retries that all fail the same guard.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_disable.py`:

```python
from zae_limiter.exceptions import RateLimitError, ResourceDisabled


@pytest.mark.asyncio
class TestEnforcement:
    async def test_fast_path_raises_resource_disabled(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        # First acquire creates the bucket while still enabled.
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        with pytest.raises(ResourceDisabled) as exc_info:
            async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass
        assert exc_info.value.resource == "gpt-4"
        assert exc_info.value.entity_id == "user-1"

    async def test_slow_path_raises_before_creating_a_bucket(
        self, disable_limiter, disable_repo
    ):
        # No bucket exists, so the fast-path guard cannot fire.
        await disable_repo.set_resource_defaults(
            "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
        )
        with pytest.raises(ResourceDisabled):
            async with disable_limiter.acquire("brand-new", "gpt-4", {"rpm": 1}):
                pass
        assert await disable_repo.get_buckets("brand-new") == []

    async def test_disabled_is_not_a_rate_limit_error(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        # Callers catching RateLimitError must NOT swallow a disabled resource.
        with pytest.raises(ResourceDisabled):
            try:
                async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                    pass
            except RateLimitError:
                pytest.fail("ResourceDisabled must not be caught as RateLimitError")

    async def test_entity_override_still_admitted_after_resource_disable(
        self, disable_limiter, disable_repo
    ):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        await disable_repo.set_limits(
            "vip-1", [Limit.per_minute("rpm", 100)], resource="gpt-4", disabled=False
        )
        for entity in ("user-1", "vip-1"):
            async with disable_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
                pass
        await disable_repo.disable_resource("gpt-4")

        # The carve-out keeps working...
        async with disable_limiter.acquire("vip-1", "gpt-4", {"rpm": 1}):
            pass
        # ...while everyone else is blocked.
        with pytest.raises(ResourceDisabled):
            async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

    async def test_enable_restores_access(self, disable_limiter, disable_repo):
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")
        with pytest.raises(ResourceDisabled):
            async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

        await disable_repo.enable_resource("gpt-4")
        async with disable_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

    async def test_on_unavailable_allow_does_not_swallow_disabled(self, disable_repo):
        from zae_limiter import RateLimiter

        await disable_repo.set_system_defaults(
            [Limit.per_minute("rpm", 100)], on_unavailable="allow"
        )
        await disable_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 100)])
        limiter = RateLimiter(repository=disable_repo)
        async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
        await disable_repo.disable_resource("gpt-4")

        # `allow` covers infrastructure unavailability, not policy decisions.
        with pytest.raises(ResourceDisabled):
            async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_disable.py -k TestEnforcement -v`
Expected: FAIL — `RateLimitExceeded` (or no exception) is raised instead of `ResourceDisabled`.

- [ ] **Step 3: Add the guard to the speculative condition**

In `speculative_consume` in `src/zae_limiter/repository.py`, immediately after the existing
TTL guard is appended (the
`condition_parts.append("(attribute_not_exists(#ttl) OR #ttl > :now_epoch)")` line, around
line 2378), add:

```python
        # Reject buckets stamped as disabled (ADR-125). The attribute is present
        # only when the bucket is effectively disabled, so this costs nothing on
        # the enabled path.
        attr_names["#disabled"] = schema.BUCKET_FIELD_DISABLED
        condition_parts.append("attribute_not_exists(#disabled)")
```

- [ ] **Step 4: Add the failure reason**

In `src/zae_limiter/repository_protocol.py`, add to `SpeculativeFailureReason`:

```python
    DISABLED = "disabled"
```

and extend the enum docstring with: `DISABLED means the bucket is stamped disabled
(ADR-125); the limiter must raise ResourceDisabled rather than retry or reshard.`

- [ ] **Step 5: Classify it first**

In `speculative_consume`'s `ConditionalCheckFailedException` handler, inside the
`if old_item:` branch, make the disabled check the **first** classification — before
`wcu_exhausted` / `app_exhausted` are computed:

```python
                if old_item:
                    old_buckets = self._deserialize_composite_bucket(old_item)
                    old_shard_count = int(old_item.get("shard_count", {}).get("N", "1"))

                    # Disabled wins over every other classification: retrying on
                    # another shard or doubling shards cannot help (ADR-125).
                    if old_item.get(schema.BUCKET_FIELD_DISABLED, {}).get("BOOL", False):
                        return SpeculativeResult(
                            success=False,
                            old_buckets=old_buckets,
                            shard_id=shard_id,
                            shard_count=old_shard_count,
                            failure_reason=SpeculativeFailureReason.DISABLED,
                        )

                    # Classify failure reason (GHSA-76rv)
                    wcu_exhausted = any(
```

- [ ] **Step 6: Raise from the fast path, before shard handling**

In `src/zae_limiter/limiter.py`, in `_try_speculative_acquire`, insert this block
immediately after the parent-compensation block and **before** the wcu shard-doubling
branch (between the current lines 737 and 739):

```python
            # Disabled: no shard retry or doubling can help (ADR-125).
            if result.failure_reason == SpeculativeFailureReason.DISABLED:
                raise ResourceDisabled(
                    entity_id=entity_id, resource=resource, level="bucket"
                )
```

Add `ResourceDisabled` to the existing `from .exceptions import (...)` block in `limiter.py`.

- [ ] **Step 7: Handle the disabled parent in cascade**

In `_handle_nested_parent_failure`, after the existing child-compensation call (so the
child's tokens are returned before the exception propagates), add:

```python
        if (
            result.parent_result is not None
            and result.parent_result.failure_reason == SpeculativeFailureReason.DISABLED
        ):
            assert result.parent_id is not None
            raise ResourceDisabled(
                entity_id=result.parent_id, resource=resource, level="bucket"
            )
```

- [ ] **Step 8: Add the slow-path gate**

In `_do_acquire`, immediately after `child_limits` are resolved (around line 1213) and
**before** any bucket is fetched or created, add:

```python
        # Slow path gate (ADR-125). Covers first acquire — no bucket exists yet,
        # so the fast-path guard cannot fire — and every fallback path.
        disabled, level = await self._repository.resolve_disabled(entity_id, resource)
        if disabled:
            raise ResourceDisabled(
                entity_id=entity_id, resource=resource, level=level or "resource"
            )
```

Apply the same check for `parent_id` in `_acquire_parent_slow_path` before consuming from
the parent, raising with `entity_id=parent_id`.

- [ ] **Step 9: Stop `on_unavailable=allow` from swallowing it**

In `acquire()`, extend the re-raise tuple (currently line 676) so a policy decision is never
converted into a permissive no-op lease:

```python
        except (RateLimitExceeded, ValidationError, ResourceDisabled):
            raise
```

- [ ] **Step 10: Run tests**

Run: `uv run pytest tests/unit/test_disable.py tests/unit/test_limiter.py tests/unit/test_sync_limiter.py -v`
Expected: PASS

- [ ] **Step 11: Regenerate sync and commit**

```bash
hatch run generate-sync
uv run pytest tests/unit/ -q && uv run mypy src/zae_limiter
git add src/zae_limiter/ tests/unit/test_disable.py
git commit -m "✨ feat(limiter): enforce disabled on fast and slow acquire paths"
```

---

### Task 8: Integration coverage against LocalStack

**Files:**
- Create: `tests/integration/test_disable_fanout.py`

**Interfaces:**
- Consumes: everything from Tasks 5-7.
- Produces: no new source interfaces; proves shard and first-acquire behavior against a real
  DynamoDB implementation with real GSI propagation.

**Why this is its own task:** moto does not model GSI eventual consistency, and the fan-out
depends on GSI2/GSI3 queries returning freshly written buckets. This is the only place that
risk is exercised.

- [ ] **Step 1: Write the test**

Create `tests/integration/test_disable_fanout.py`:

```python
"""LocalStack coverage for eager disable fan-out (ADR-125)."""

import pytest

from zae_limiter.exceptions import ResourceDisabled
from zae_limiter.models import Limit

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_fanout_covers_every_shard(localstack_limiter, test_repo):
    """A multi-shard bucket must be disabled on all of its shards."""
    await test_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 10_000)])
    async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
        pass
    # Force a second shard, then populate it.
    await test_repo.bump_shard_count("user-1", "gpt-4", 1)
    async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
        pass

    assert await test_repo.disable_entity("user-1", resource="gpt-4") >= 2

    with pytest.raises(ResourceDisabled):
        async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass


async def test_first_acquire_on_disabled_resource_creates_no_bucket(
    localstack_limiter, test_repo
):
    """The slow-path gate must fire before a bucket is created."""
    await test_repo.set_resource_defaults(
        "gpt-4", [Limit.per_minute("rpm", 100)], disabled=True
    )
    with pytest.raises(ResourceDisabled):
        async with localstack_limiter.acquire("brand-new", "gpt-4", {"rpm": 1}):
            pass

    assert await test_repo.get_buckets("brand-new") == []


async def test_carve_out_survives_resource_disable_across_shards(
    localstack_limiter, test_repo
):
    """An entity-level enable must not be stamped by a resource-level disable."""
    await test_repo.set_resource_defaults("gpt-4", [Limit.per_minute("rpm", 1000)])
    await test_repo.set_limits(
        "vip-1", [Limit.per_minute("rpm", 1000)], resource="gpt-4", disabled=False
    )
    for entity in ("user-1", "vip-1"):
        async with localstack_limiter.acquire(entity, "gpt-4", {"rpm": 1}):
            pass

    await test_repo.disable_resource("gpt-4")

    async with localstack_limiter.acquire("vip-1", "gpt-4", {"rpm": 1}):
        pass
    with pytest.raises(ResourceDisabled):
        async with localstack_limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass
```

- [ ] **Step 2: Run it**

```bash
zae-limiter local up
export AWS_ENDPOINT_URL=http://localhost:4566 AWS_ACCESS_KEY_ID=test \
       AWS_SECRET_ACCESS_KEY=test AWS_DEFAULT_REGION=us-east-1
uv run pytest tests/integration/test_disable_fanout.py -v
```
Expected: PASS (3 tests)

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_disable_fanout.py
git commit -m "✅ test(repository): add LocalStack coverage for disable fan-out"
```

---

### Task 9: CLI commands

**Files:**
- Modify: `src/zae_limiter/cli.py` (`resource` group ~line 2177, `entity` group ~line 2796)
- Test: `tests/unit/test_cli.py`

**Interfaces:**
- Consumes: the six Repository methods from Task 6.
- Produces: `zae-limiter resource disable|enable|clear-disabled RESOURCE_NAME` and
  `zae-limiter entity disable|enable|clear-disabled ENTITY_ID [--resource R]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_cli.py`. That suite tests commands through the `runner: CliRunner`
fixture with `--help` output and argument validation, patching `zae_limiter.repository.Repository`
when a command needs a backend — follow that convention rather than inventing fixtures.
Behavioral coverage for these commands lives in Task 8's integration test.

```python
class TestDisableCommands:
    """Disable/enable commands (ADR-125)."""

    def test_resource_disable_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["resource", "disable", "--help"])
        assert result.exit_code == 0
        assert "Disable a resource" in result.output
        assert "--namespace" in result.output

    def test_resource_enable_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["resource", "enable", "--help"])
        assert result.exit_code == 0

    def test_resource_clear_disabled_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["resource", "clear-disabled", "--help"])
        assert result.exit_code == 0

    def test_resource_disable_requires_resource_name(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["resource", "disable"])
        assert result.exit_code != 0
        assert "Missing argument" in result.output

    def test_entity_disable_help_has_resource_option(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["entity", "disable", "--help"])
        assert result.exit_code == 0
        assert "--resource" in result.output

    def test_entity_enable_help_describes_the_override(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["entity", "enable", "--help"])
        assert result.exit_code == 0
        assert "override" in result.output.lower()

    def test_entity_clear_disabled_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["entity", "clear-disabled", "--help"])
        assert result.exit_code == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_cli.py -k TestDisableCommands -v`
Expected: FAIL — `Error: No such command 'disable'`

- [ ] **Step 3: Add the resource commands**

In `src/zae_limiter/cli.py`, after `resource_delete_defaults`, add:

````python
@resource.command(
    "disable",
    epilog="""\b
Examples:
    \b
    # Turn off a resource for everyone without an entity-level override
    zae-limiter resource disable gpt-4
""",
)
@click.argument("resource_name")
@click.option(
    "--name",
    "-n",
    default=DEFAULT_STACK_NAME,
    show_default=True,
    help="Stack identifier used as the CloudFormation stack name.",
)
@click.option("--region", help="AWS region (default: use boto3 defaults)")
@click.option(
    "--endpoint-url",
    help="AWS endpoint URL (e.g., http://localhost:4566 for LocalStack)",
)
@namespace_option
def resource_disable(
    resource_name: str,
    name: str,
    region: str | None,
    endpoint_url: str | None,
    namespace: str,
) -> None:
    """Disable a resource.

    RESOURCE_NAME is the resource to disable (e.g., 'gpt-4').

    Existing buckets are stamped immediately, so the change takes effect on the
    next request. Entities with an explicit enable override keep their access.

    \f

    **Examples:**
        ```bash
        zae-limiter resource disable gpt-4
        ```
    """

    async def _run() -> None:
        repo = await _connect(name, region, endpoint_url, namespace)
        try:
            count = await repo.disable_resource(resource_name)
            click.echo(f"Disabled resource '{resource_name}' ({count} buckets stamped)")
        except Exception as e:
            click.echo(f"Error: Failed to disable resource: {e}", err=True)
            sys.exit(1)
        finally:
            await repo.close()

    asyncio.run(_run())
````

Add `resource_enable` and `resource_clear_disabled` with the same shape, calling
`repo.enable_resource(...)` and `repo.clear_resource_disabled(...)` and echoing
`f"Enabled resource '{resource_name}' ({count} buckets cleared)"` and
`f"Cleared disabled flag for resource '{resource_name}' ({count} buckets updated)"`.

- [ ] **Step 4: Add the entity commands**

After `entity_delete_limits`, add `entity_disable`, `entity_enable`, and
`entity_clear_disabled` following the same shape, each taking `ENTITY_ID` as an argument
plus:

```python
@click.option(
    "--resource",
    default=None,
    help="Resource to scope to. Omit to apply across all resources for this entity.",
)
```

`entity_enable`'s docstring must state that it acts as an **override**, re-admitting the
entity to a resource that is disabled at the resource level — the CLI test asserts on the
word "override".

- [ ] **Step 5: Surface the state in the existing read commands**

In `resource_get_defaults`, after the limits are printed, add:

```python
            disabled = await repo.get_resource_disabled(resource_name)
            if disabled is True:
                click.echo("Status: DISABLED")
            elif disabled is False:
                click.echo("Status: enabled (explicit override)")
```

Do the same in `entity_get_limits` using `await repo.get_entity_disabled(entity_id, resource)`.

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/test_cli.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/zae_limiter/cli.py tests/unit/test_cli.py
git commit -m "✨ feat(cli): add resource/entity disable, enable, and clear-disabled"
```

---

### Task 10: Declarative manifest support

**Files:**
- Modify: `src/zae_limiter_provisioner/manifest.py`
- Modify: `src/zae_limiter_provisioner/applier.py`
- Modify: `src/zae_limiter_provisioner/handler.py`
- Create: `src/zae_limiter_provisioner/fanout.py`
- Create: `tests/unit/test_provisioner_manifest.py` (the provisioner suite currently has
  `test_provisioner_builder.py` and `test_provisioner_handler.py`; manifest parsing gets its own module)

**Interfaces:**
- Consumes: `schema.CONFIG_FIELD_DISABLED`, `schema.BUCKET_FIELD_DISABLED` (Task 2).
- Produces: `ResourceDecl.disabled: bool | None`, `EntityResourceDecl.disabled: bool | None`,
  both flowing through `to_dict()` → `Change.data` → `_apply_set`; plus
  `fanout_resource(...) -> int` and `fanout_entity(...) -> int` in the new module.

**Note:** `differ.py` needs **no changes** — it passes `to_dict()` straight through as
`Change.data`, so a new key propagates automatically.

- [ ] **Step 1: Write the failing test**

```python
from zae_limiter_provisioner.manifest import LimitsManifest


class TestManifestDisabled:
    def test_resource_disabled_parsed(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "resources": {
                    "gpt-4": {"disabled": True, "limits": {"rpm": {"capacity": 10}}}
                },
            }
        )
        assert m.resources["gpt-4"].disabled is True

    def test_resource_disabled_defaults_to_none(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}},
            }
        )
        assert m.resources["gpt-4"].disabled is None

    def test_entity_disabled_false_is_preserved(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "entities": {
                    "vip-1": {
                        "resources": {
                            "gpt-4": {
                                "disabled": False,
                                "limits": {"rpm": {"capacity": 10}},
                            }
                        }
                    }
                },
            }
        )
        decl = m.entities["vip-1"].resources["gpt-4"]
        assert decl.disabled is False
        assert decl.to_dict()["disabled"] is False

    def test_to_dict_omits_disabled_when_unset(self):
        m = LimitsManifest.from_dict(
            {
                "namespace": "default",
                "resources": {"gpt-4": {"limits": {"rpm": {"capacity": 10}}}},
            }
        )
        assert "disabled" not in m.resources["gpt-4"].to_dict()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_provisioner_manifest.py -k Disabled -v`
Expected: FAIL with `AttributeError: 'ResourceDecl' object has no attribute 'disabled'`

- [ ] **Step 3: Add the field to the decls**

In `src/zae_limiter_provisioner/manifest.py`, update `ResourceDecl`:

```python
@dataclass(frozen=True)
class ResourceDecl:
    """Resource-level limit declaration."""

    limits: dict[str, LimitDecl]
    disabled: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResourceDecl:
        limits = {name: LimitDecl.from_dict(val) for name, val in d.get("limits", {}).items()}
        return cls(limits=limits, disabled=d.get("disabled"))

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "limits": {name: lim.to_dict() for name, lim in self.limits.items()}
        }
        if self.disabled is not None:
            result["disabled"] = self.disabled
        return result
```

Apply the identical change to `EntityResourceDecl`. Do **not** add it to `SystemDecl` —
system-level disable is out of scope per ADR-125.

- [ ] **Step 4: Write it in the applier**

In `src/zae_limiter_provisioner/applier.py`, in `_apply_set`, extend the resource and entity
branches:

```python
    elif change.level == "resource":
        assert change.target is not None
        resource = change.target
        pk = pk_resource(namespace_id, resource)
        sk = sk_config()
        extra = {"resource": {"S": resource}}
        disabled = data.get("disabled")
        if disabled is not None:
            extra["disabled"] = {"BOOL": bool(disabled)}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)

    elif change.level == "entity":
        assert change.target is not None
        entity_id, resource = change.target.split("/", 1)
        pk = pk_entity(namespace_id, entity_id)
        sk = sk_config(resource)
        extra = {"entity_id": {"S": entity_id}, "resource": {"S": resource}}
        disabled = data.get("disabled")
        if disabled is not None:
            extra["disabled"] = {"BOOL": bool(disabled)}
        item = _build_limit_item(pk, sk, namespace_id, limits, extra)
```

- [ ] **Step 5: Add the Lambda-side fan-out**

The provisioner writes config with boto3 directly and never constructs a `Repository`, so it
needs its own fan-out. Create `src/zae_limiter_provisioner/fanout.py`:

```python
"""Eager bucket stamping for disabled resources/entities (ADR-125).

Mirrors Repository._fanout_* using sync boto3, because the provisioner runs
inside Lambda where aiobotocore is unavailable. Key construction is shared via
zae_limiter.schema, which is already vendored into the Lambda package.

Limitation: unlike Repository._fanout_resource, this does not evaluate
per-entity overrides. The handler compensates by applying resource-level
changes before entity-level ones, so an entity carve-out re-stamps its own
buckets last.
"""

from __future__ import annotations

import logging
from typing import Any

from zae_limiter.schema import (
    BUCKET_FIELD_DISABLED,
    GSI2_NAME,
    GSI3_NAME,
    gsi2_pk_resource,
    gsi3_pk_entity,
    sk_state,
)

logger = logging.getLogger(__name__)


def stamp_bucket(client: Any, table_name: str, pk: str, disabled: bool) -> None:
    """Set or remove the disabled attribute on one bucket item."""
    kwargs: dict[str, Any] = {
        "TableName": table_name,
        "Key": {"PK": {"S": pk}, "SK": {"S": sk_state()}},
        "ExpressionAttributeNames": {"#disabled": BUCKET_FIELD_DISABLED},
        "ConditionExpression": "attribute_exists(PK)",
    }
    if disabled:
        kwargs["UpdateExpression"] = "SET #disabled = :true"
        kwargs["ExpressionAttributeValues"] = {":true": {"BOOL": True}}
    else:
        kwargs["UpdateExpression"] = "REMOVE #disabled"

    try:
        client.update_item(**kwargs)
    except client.exceptions.ConditionalCheckFailedException:
        logger.debug("Bucket %s vanished before stamping", pk)


def fanout_resource(
    client: Any, table_name: str, namespace_id: str, resource: str, disabled: bool
) -> int:
    """Stamp every bucket for a resource. Returns the number stamped."""
    return _fanout(
        client,
        table_name,
        index=GSI2_NAME,
        pk_name="GSI2PK",
        sk_name="GSI2SK",
        pk_value=gsi2_pk_resource(namespace_id, resource),
        sk_prefix="BUCKET#",
        disabled=disabled,
    )


def fanout_entity(
    client: Any,
    table_name: str,
    namespace_id: str,
    entity_id: str,
    resource: str | None,
    disabled: bool,
) -> int:
    """Stamp every bucket for an entity. Returns the number stamped."""
    return _fanout(
        client,
        table_name,
        index=GSI3_NAME,
        pk_name="GSI3PK",
        sk_name="GSI3SK",
        pk_value=gsi3_pk_entity(namespace_id, entity_id),
        sk_prefix=f"BUCKET#{resource}#" if resource else "BUCKET#",
        disabled=disabled,
    )


def _fanout(
    client: Any,
    table_name: str,
    index: str,
    pk_name: str,
    sk_name: str,
    pk_value: str,
    sk_prefix: str,
    disabled: bool,
) -> int:
    stamped: set[str] = set()
    start_key: dict[str, Any] | None = None

    while True:
        params: dict[str, Any] = {
            "TableName": table_name,
            "IndexName": index,
            "KeyConditionExpression": f"{pk_name} = :pk AND begins_with({sk_name}, :sk)",
            "ExpressionAttributeValues": {
                ":pk": {"S": pk_value},
                ":sk": {"S": sk_prefix},
            },
        }
        if start_key:
            params["ExclusiveStartKey"] = start_key
        response = client.query(**params)
        for item in response.get("Items", []):
            pk = item.get("PK", {}).get("S", "")
            if pk and pk not in stamped:
                stamp_bucket(client, table_name, pk, disabled)
                stamped.add(pk)
        start_key = response.get("LastEvaluatedKey")
        if not start_key:
            break

    return len(stamped)
```

- [ ] **Step 6: Call it from the handler**

In `src/zae_limiter_provisioner/handler.py`, after `apply_changes(...)` returns, iterate the
applied changes and fan out for any that carry a `disabled` key, resources first:

```python
    from .fanout import fanout_entity, fanout_resource

    for change in sorted(changes, key=lambda c: 0 if c.level == "resource" else 1):
        data = change.data or {}
        if "disabled" not in data:
            continue
        disabled = bool(data["disabled"])
        if change.level == "resource" and change.target:
            fanout_resource(client, table_name, namespace_id, change.target, disabled)
        elif change.level == "entity" and change.target:
            entity_id, resource = change.target.split("/", 1)
            fanout_entity(client, table_name, namespace_id, entity_id, resource, disabled)
```

- [ ] **Step 7: Add the Lambda packaging entry**

`fanout.py` imports `zae_limiter.schema`, which the Lambda stub already vendors, so no new
stub modules are needed. Confirm the provisioner package is picked up whole by
`src/zae_limiter/infra/lambda_builder.py`; if it enumerates modules explicitly, add
`fanout.py` to that list.

- [ ] **Step 8: Run tests and commit**

Run: `uv run pytest tests/unit/ -k provisioner -v`
Expected: PASS

```bash
git add src/zae_limiter_provisioner/ tests/
git commit -m "✨ feat(provisioner): support disabled in limits manifests"
```

---

### Task 11: Documentation and final verification

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/cli.md`
- Modify: `docs/guide/basic-usage.md`
- Modify: `docs/adr/125-resource-disable.md` (status → Accepted)

- [ ] **Step 1: Update CLAUDE.md**

Add `ResourceDisabled` to the `exceptions.py` line of the project-structure block, and
`fanout.py` to the `src/zae_limiter_provisioner/` tree. Then add a new subsection under
"Centralized Configuration":

````markdown
### Disabling Resources and Entities (ADR-125)

`disabled` is a tri-state flag stored beside `limits` on resource and entity config items:
absent means inherit, `true`/`false` are explicit. It resolves by an **independent** walk —
entity (resource-specific) → entity (`_default_`) → resource — where the first level with an
explicit value wins, regardless of which level supplies the limits. An entity-level
`disabled: false` therefore re-admits one entity to a resource that is disabled globally.

Disabling is **eager**: the call writes config, then stamps every existing bucket item with
a `disabled` attribute. The speculative fast path enforces it via
`attribute_not_exists(disabled)` in its `ConditionExpression`, alongside the existing TTL
guard. `acquire()` raises `ResourceDisabled` (a direct `ZAELimiterError`, **not** a
`RateLimitError`) with no retry hint — map it to 403, not 429.

System-level disable is not supported.

| Level | Disable / Enable | Clear |
|-------|------------------|-------|
| Resource | `disable_resource(r)` / `enable_resource(r)` | `clear_resource_disabled(r)` |
| Entity | `disable_entity(e, resource=...)` / `enable_entity(e, resource=...)` | `clear_entity_disabled(e, resource=...)` |

```bash
zae-limiter resource disable gpt-4
zae-limiter entity enable vip-1 --resource gpt-4   # carve-out from a disabled resource
```
````

Also add `disabled` to the YAML manifest example in "Declarative Limits Management", and add
a row to the "DynamoDB writer table" for the disable stamp:

| Writer | UpdateExpression | Condition | Touches `rf`? |
|--------|-----------------|-----------|---------------|
| Disable stamp | `SET disabled = :true` / `REMOVE disabled` | `attribute_exists(PK)` | No |

and note the new clause in the speculative-write pattern section:
`attribute_not_exists(disabled)`.

- [ ] **Step 2: Update docs/cli.md**

Document the six new commands using the same examples as their `epilog` blocks.

- [ ] **Step 3: Update docs/guide/basic-usage.md**

Add a "Turning a resource off" section showing the handler split:

```python
from zae_limiter.exceptions import RateLimitExceeded, ResourceDisabled

try:
    async with limiter.acquire("user-1", "gpt-4", {"rpm": 1}):
        ...
except ResourceDisabled:
    # Not retryable — the resource is intentionally off for this caller.
    return http_403()
except RateLimitExceeded as e:
    return http_429(retry_after=e.retry_after_seconds)
```

- [ ] **Step 4: Run the docs-updater agent**

Per `.claude/rules/docs-parity.md`, invoke the `docs-updater` agent to catch remaining drift
across `docs/api/`, `docs/cli.md`, and the guide. Review its changes before committing.

- [ ] **Step 5: Mark the ADR accepted**

Change ADR-125's `**Status:**` from `Proposed` to `Accepted` and fill in the issue link.

- [ ] **Step 6: Full verification**

```bash
hatch run generate-sync
git diff --exit-code            # generated sync must already be current
uv run ruff check --fix . && uv run ruff format .
uv run mypy src/zae_limiter
uv run pytest tests/unit/ -v
uv run pytest tests/unit/ -m gevent -n 0 -v
pre-commit run --all-files
```
All must pass before opening the PR.

- [ ] **Step 7: Commit and open a draft PR**

```bash
git add -A
git commit -m "📝 docs(limiter): document resource and entity disable"
```

Then use the `/pr` skill (per `.claude/rules/issue-skill.md` — do not run `gh pr create`
directly) to open a draft PR linked to the tracking issue.

---

## Self-Review

**Spec coverage.** Every ADR-125 decision maps to a task: tri-state storage (Tasks 2, 4);
independent three-level walk with the entity-`false` override (Task 5); eager fan-out
(Task 6); bucket denormalization and the fast-path guard (Task 7); distinct exception
(Task 3); CLI (Task 9); manifest (Task 10); docs (Task 11). The two requirements the user
stated explicitly are covered and directly tested: **eager enforcement** by
`_set_resource_disabled` writing config then fanning out before returning (Task 6, Step 6)
with `test_fast_path_raises_resource_disabled` proving it takes effect without waiting;
**entity `disabled: false` overriding resource `disabled: true`** by
`test_entity_false_overrides_resource_true` (Task 5),
`test_disable_resource_skips_entities_with_false_override` (Task 6), and
`test_entity_override_still_admitted_after_resource_disable` (Task 7).

**Type consistency.** `resolve_disabled` returns `tuple[bool, str | None]` at its definition
(Task 5), its protocol stub (Task 5), and all three call sites (Task 6 `_fanout_resource`
and `_set_entity_disabled`, Task 7 slow-path gate). The six public methods return `int`
consistently across implementation, protocol stubs, and CLI callers.
`schema.encode_disabled` / `decode_disabled` signatures match between Task 2 and their
callers in Tasks 4 and 5. `ResourceDisabled(entity_id=..., resource=..., level=...)` is
constructed with all three keyword arguments at every raise site (Task 7, Steps 6-8).
`schema.BUCKET_FIELD_DISABLED` is used for bucket items and `schema.CONFIG_FIELD_DISABLED`
for config items, consistently and never interchanged.

**Ordering.** Task 6 (fan-out) precedes Task 7 (enforcement) because Task 7's tests need a
working `disable_resource()` to disable anything. Task 8 (LocalStack) follows Task 7 because
it exercises the full enforced path.

**Known gaps, deliberately left open:**
1. The provisioner's Lambda-side fan-out does not evaluate per-entity overrides. Task 10
   mitigates by ordering resource changes before entity changes and documents the
   limitation in the module docstring and ADR-125.
2. The config-write-then-fan-out race is narrowed by a two-pass query but not eliminated.
   File a follow-up issue for a `zae-limiter resource verify-disabled` repair command.
3. `clear_entity_disabled` recomputes the inherited value and restamps, which is
   O(buckets) for that entity. Acceptable on an admin path.
4. Disabling is O(buckets for the resource) writes. For a resource with very high entity
   fan-out this is a large burst of WCU; consider batching or throttling if it becomes a
   problem in practice.
