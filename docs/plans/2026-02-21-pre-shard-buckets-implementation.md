# Pre-Shard Buckets Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move bucket items to per-(entity, resource, shard) partition keys with auto-injected `wcu` infrastructure limit to mitigate DynamoDB hot partition throttling (GHSA-76rv-2r9v-c5m6).

**Architecture:** Buckets move from `PK={ns}/ENTITY#{id}, SK=#BUCKET#{resource}` to `PK={ns}/BUCKET#{id}#{resource}#{shard}, SK=#STATE`. A reserved `wcu:1000` limit auto-injected on every bucket tracks per-partition write pressure. When exhausted, the client doubles shard count. The aggregator proactively shards before exhaustion and propagates shard_count changes. GSI3 (KEYS_ONLY) enables bucket discovery without impacting the $0.625/M hot path cost.

**Tech Stack:** Python 3.11+, DynamoDB (boto3/aioboto3), pytest, moto

**Design doc:** `docs/plans/2026-02-21-pre-shard-buckets-design.md`

---

## Phase 1: Schema Foundation

### Task 1: Add bucket PK/SK builders and constants

**Files:**
- Modify: `src/zae_limiter/schema.py:19-30` (add constants), `:108-135` (add new builders)
- Test: `tests/unit/test_schema.py`

**Step 1: Write failing tests for new key builders**

```python
def test_pk_bucket():
    assert schema.pk_bucket("ns1", "user-1", "gpt-4", 0) == "ns1/BUCKET#user-1#gpt-4#0"
    assert schema.pk_bucket("ns1", "user-1", "gpt-4", 3) == "ns1/BUCKET#user-1#gpt-4#3"

def test_sk_state():
    assert schema.sk_state() == "#STATE"

def test_parse_bucket_pk():
    ns, entity, resource, shard = schema.parse_bucket_pk("ns1/BUCKET#user-1#gpt-4#0")
    assert ns == "ns1"
    assert entity == "user-1"
    assert resource == "gpt-4"
    assert shard == 0

def test_parse_bucket_pk_invalid():
    with pytest.raises(ValueError):
        schema.parse_bucket_pk("ns1/ENTITY#user-1")

def test_gsi3_pk_entity():
    assert schema.gsi3_pk_entity("ns1", "user-1") == "ns1/ENTITY#user-1"

def test_gsi3_sk_bucket():
    assert schema.gsi3_sk_bucket("gpt-4", 0) == "BUCKET#gpt-4#0"
    assert schema.gsi3_sk_bucket("gpt-4", 3) == "BUCKET#gpt-4#3"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_schema.py -k "test_pk_bucket or test_sk_state or test_parse_bucket_pk or test_gsi3_pk_entity or test_gsi3_sk_bucket" -v`
Expected: FAIL with `AttributeError: module 'zae_limiter.schema' has no attribute 'pk_bucket'`

**Step 3: Implement new builders in schema.py**

Add constants after line 26:

```python
BUCKET_PREFIX = "BUCKET#"
SK_STATE = "#STATE"
```

Add builders after `sk_bucket()` (line 135):

```python
def pk_bucket(namespace_id: str, entity_id: str, resource: str, shard_id: int) -> str:
    """Build partition key for a bucket shard."""
    return f"{namespace_id}/{BUCKET_PREFIX}{entity_id}#{resource}#{shard_id}"


def sk_state() -> str:
    """Build sort key for bucket state (fixed)."""
    return SK_STATE


def parse_bucket_pk(pk: str) -> tuple[str, str, str, int]:
    """Parse namespace, entity_id, resource, shard_id from a bucket PK.

    Args:
        pk: A bucket PK like 'ns1/BUCKET#user-1#gpt-4#0'

    Returns:
        Tuple of (namespace_id, entity_id, resource, shard_id)

    Raises:
        ValueError: If PK is not a valid bucket PK
    """
    namespace_id, remainder = parse_namespace(pk)
    if not remainder.startswith(BUCKET_PREFIX):
        raise ValueError(f"Not a bucket PK: {pk}")
    rest = remainder[len(BUCKET_PREFIX):]
    # Split from the right: last # is shard_id
    parts = rest.rsplit("#", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid bucket PK format: {pk}")
    entity_resource, shard_str = parts
    shard_id = int(shard_str)
    # Split entity_id and resource: first # separates them
    er_parts = entity_resource.split("#", 1)
    if len(er_parts) != 2:
        raise ValueError(f"Invalid bucket PK format: {pk}")
    entity_id, resource = er_parts
    return namespace_id, entity_id, resource, shard_id


def gsi3_pk_entity(namespace_id: str, entity_id: str) -> str:
    """Build GSI3 partition key for entity bucket discovery."""
    return f"{namespace_id}/{ENTITY_PREFIX}{entity_id}"


def gsi3_sk_bucket(resource: str, shard_id: int) -> str:
    """Build GSI3 sort key for bucket entry."""
    return f"{BUCKET_PREFIX}{resource}#{shard_id}"
```

Also update `gsi2_sk_bucket()` to include shard_id:

```python
def gsi2_sk_bucket(entity_id: str, shard_id: int = 0) -> str:
    """Build GSI2 sort key for composite bucket entry."""
    return f"BUCKET#{entity_id}#{shard_id}"
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_schema.py -v`
Expected: PASS (new tests + existing tests)

**Step 5: Commit**

```bash
git add src/zae_limiter/schema.py tests/unit/test_schema.py
git commit -m "feat(schema): add bucket PK builders and GSI3 entity keys"
```

---

### Task 2: Add reserved limit name validation

**Files:**
- Modify: `src/zae_limiter/models.py:24` (add reserved set), `:72-100` (update validate_name)
- Test: `tests/unit/test_models.py`

**Step 1: Write failing test for reserved limit name**

```python
def test_validate_name_rejects_reserved_wcu():
    with pytest.raises(InvalidNameError, match="reserved"):
        validate_name("wcu", "limit_name")

def test_validate_name_allows_normal_names():
    validate_name("rpm", "limit_name")  # should not raise
    validate_name("tpm", "limit_name")  # should not raise
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_models.py -k "test_validate_name_rejects_reserved_wcu" -v`
Expected: FAIL (no exception raised)

**Step 3: Add reserved names set and validation**

In `models.py` after line 31:

```python
# Reserved limit names (internal infrastructure limits, not user-configurable)
RESERVED_LIMIT_NAMES = frozenset({"wcu"})
```

In `validate_name()` after the `FORBIDDEN_CHAR` check (line 92):

```python
    if value in RESERVED_LIMIT_NAMES:
        raise InvalidNameError(
            field_name, value, f"'{value}' is a reserved limit name"
        )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/test_models.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/models.py tests/unit/test_models.py
git commit -m "feat(models): add reserved limit name validation for wcu"
```

---

### Task 3: Add WCU infrastructure limit constant

**Files:**
- Modify: `src/zae_limiter/schema.py` (add WCU constants)
- Test: `tests/unit/test_schema.py`

**Step 1: Write failing test**

```python
def test_wcu_limit_constants():
    assert schema.WCU_LIMIT_NAME == "wcu"
    assert schema.WCU_LIMIT_CAPACITY == 1000
    assert schema.WCU_LIMIT_REFILL_AMOUNT == 1000
    assert schema.WCU_LIMIT_REFILL_PERIOD_SECONDS == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_schema.py -k "test_wcu_limit_constants" -v`
Expected: FAIL

**Step 3: Add constants to schema.py**

After the `BUCKET_FIELD_RF` line (59):

```python
# Infrastructure limit: DynamoDB partition write capacity ceiling
WCU_LIMIT_NAME = "wcu"
WCU_LIMIT_CAPACITY = 1000  # DynamoDB per-partition WCU/sec limit
WCU_LIMIT_REFILL_AMOUNT = 1000
WCU_LIMIT_REFILL_PERIOD_SECONDS = 1
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_schema.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/schema.py tests/unit/test_schema.py
git commit -m "feat(schema): add WCU infrastructure limit constants"
```

---

## Phase 2: Bucket Creation with New PK

### Task 4: Update build_composite_create for new PK scheme

**Files:**
- Modify: `src/zae_limiter/repository.py:1794-1861` (build_composite_create)
- Test: `tests/unit/test_repository.py`

**Step 1: Write failing test for new bucket PK and wcu injection**

```python
def test_build_composite_create_new_pk(mock_dynamodb, unique_name):
    """Bucket items use new PK scheme with wcu limit auto-injected."""
    repo = make_repo(mock_dynamodb, unique_name)
    limits = [Limit.per_minute("rpm", 100)]

    item = repo.build_composite_create(
        entity_id="user-1",
        resource="gpt-4",
        limits=limits,
        shard_id=0,
        shard_count=1,
    )

    put_item = item["Put"]["Item"]
    # New PK scheme
    assert put_item["PK"]["S"] == schema.pk_bucket(repo._namespace_id, "user-1", "gpt-4", 0)
    assert put_item["SK"]["S"] == schema.sk_state()

    # GSI3 projection for bucket discovery
    assert put_item["GSI3PK"]["S"] == schema.gsi3_pk_entity(repo._namespace_id, "user-1")
    assert put_item["GSI3SK"]["S"] == schema.gsi3_sk_bucket("gpt-4", 0)

    # wcu limit auto-injected
    assert schema.bucket_attr("wcu", "tk") in put_item
    assert schema.bucket_attr("wcu", "cp") in put_item

    # shard_count stored
    assert put_item["shard_count"]["N"] == "1"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repository.py -k "test_build_composite_create_new_pk" -v`
Expected: FAIL

**Step 3: Update build_composite_create**

Modify `build_composite_create()` to:
1. Accept `shard_id: int = 0` and `shard_count: int = 1` parameters
2. Use `schema.pk_bucket()` instead of `schema.pk_entity()` + `schema.sk_bucket()`
3. Set `SK` to `schema.sk_state()`
4. Add GSI3PK/GSI3SK for bucket discovery
5. Update GSI2SK to include shard_id
6. Auto-inject wcu limit attributes (capacity=1000*1000 milli, refill=1000*1000 milli, period=1000ms, tokens=1000*1000 milli)
7. Store `shard_count` attribute

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: PASS (new test + existing tests may need updates for new signature)

**Step 5: Commit**

```bash
git add src/zae_limiter/repository.py tests/unit/test_repository.py
git commit -m "feat(repository): update build_composite_create for new bucket PK"
```

---

### Task 5: Update _speculative_consume_single for new PK and wcu

**Files:**
- Modify: `src/zae_limiter/repository.py:2161-2253` (_speculative_consume_single)
- Test: `tests/unit/test_repository.py`

**Step 1: Write failing test**

```python
def test_speculative_consume_uses_new_pk(mock_dynamodb, unique_name):
    """speculative_consume targets new bucket PK and includes wcu condition."""
    repo = make_repo(mock_dynamodb, unique_name)
    # Create bucket with new PK scheme first
    # ... (create entity + bucket at new PK)

    result = await repo.speculative_consume(
        entity_id="user-1",
        resource="gpt-4",
        consume={"rpm": 1},
    )
    assert result.success is True
    assert result.shard_count == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repository.py -k "test_speculative_consume_uses_new_pk" -v`
Expected: FAIL

**Step 3: Update _speculative_consume_single**

Modify `_speculative_consume_single()` (lines 2161-2253) to:
1. Accept `shard_id: int = 0` parameter
2. Build Key using `schema.pk_bucket(self._namespace_id, entity_id, resource, shard_id)` + `schema.sk_state()`
3. Add `wcu` consumption to UpdateExpression: `ADD b_wcu_tk :neg_wcu, b_wcu_tc :pos_wcu`
4. Add `wcu` condition: `b_wcu_tk >= :thresh_wcu` (value: 1000 millitokens = 1 token)
5. Extract `shard_count` from ALL_NEW response
6. Add `shard_id` and `shard_count` to `SpeculativeResult`

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/repository.py tests/unit/test_repository.py
git commit -m "feat(repository): update speculative_consume for new bucket PK and wcu"
```

---

## Phase 3: Shard Selection and Entity Cache

### Task 6: Extend entity cache with shard_count per resource

**Files:**
- Modify: `src/zae_limiter/repository.py:129-131` (entity cache type)
- Modify: `src/zae_limiter/repository.py:2102-2159` (speculative_consume cache update)
- Test: `tests/unit/test_repository.py`

**Step 1: Write failing test**

```python
def test_entity_cache_stores_shard_count(mock_dynamodb, unique_name):
    """Entity cache includes shard_count per resource."""
    repo = make_repo(mock_dynamodb, unique_name)
    # Create entity with bucket (shard_count=1)
    # ... setup

    result = await repo.speculative_consume("user-1", "gpt-4", {"rpm": 1})
    assert result.success

    cache_entry = repo._entity_cache[(repo._namespace_id, "user-1")]
    # cache_entry now: (cascade, parent_id, {resource: shard_count})
    assert cache_entry[2]["gpt-4"] == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repository.py -k "test_entity_cache_stores_shard_count" -v`
Expected: FAIL

**Step 3: Update entity cache structure**

Change `_entity_cache` type from `dict[tuple, tuple[bool, str | None]]` to `dict[tuple, tuple[bool, str | None, dict[str, int]]]` where the third element is `{resource: shard_count}`.

Update all cache reads/writes in `speculative_consume()` and `_speculative_consume_single()`. On cache write, merge new resource shard_count into existing dict (don't replace entire entry).

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/repository.py tests/unit/test_repository.py
git commit -m "feat(repository): extend entity cache with shard_count per resource"
```

---

### Task 7: Add shard selection to speculative_consume

**Files:**
- Modify: `src/zae_limiter/repository.py:2102-2159` (speculative_consume)
- Test: `tests/unit/test_repository.py`

**Step 1: Write failing test**

```python
def test_speculative_consume_routes_to_random_shard(mock_dynamodb, unique_name):
    """With cached shard_count > 1, speculative_consume routes to a random shard."""
    repo = make_repo(mock_dynamodb, unique_name)
    # Pre-populate entity cache with shard_count=2
    repo._entity_cache[(repo._namespace_id, "user-1")] = (False, None, {"gpt-4": 2})
    # Create buckets at shard 0 and shard 1
    # ...

    shard_ids_hit = set()
    for _ in range(20):
        result = await repo.speculative_consume("user-1", "gpt-4", {"rpm": 1})
        if result.success:
            shard_ids_hit.add(result.shard_id)
    assert len(shard_ids_hit) == 2  # both shards hit
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repository.py -k "test_speculative_consume_routes_to_random_shard" -v`
Expected: FAIL

**Step 3: Add shard selection logic to speculative_consume**

In `speculative_consume()` before calling `_speculative_consume_single()`:

```python
import random

# Determine shard from entity cache
shard_count = 1
if cache_entry is not None:
    shard_counts = cache_entry[2]  # {resource: shard_count}
    shard_count = shard_counts.get(resource, 1)
shard_id = random.randrange(shard_count) if shard_count > 1 else 0
```

Pass `shard_id` to `_speculative_consume_single()`. Add `shard_id` to `SpeculativeResult`.

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/repository.py tests/unit/test_repository.py
git commit -m "feat(repository): add random shard selection to speculative_consume"
```

---

## Phase 4: Shard Retry and Doubling

### Task 8: Add shard retry on application limit exhaustion

**Files:**
- Modify: `src/zae_limiter/limiter.py` (acquire flow)
- Test: `tests/unit/test_limiter.py`

**Step 1: Write failing test**

```python
def test_acquire_retries_on_another_shard(mock_limiter):
    """When one shard's app limit is exhausted, retry on another shard."""
    # Setup: entity with 2 shards, shard 0 at 0 rpm tokens, shard 1 at full
    # ...
    async with limiter.acquire("user-1", "gpt-4", rpm=1) as lease:
        pass  # Should succeed via shard 1 after shard 0 fails
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_limiter.py -k "test_acquire_retries_on_another_shard" -v`
Expected: FAIL

**Step 3: Add retry logic**

In the acquire flow, when speculative_consume returns failure:
1. Inspect old_buckets to determine failure cause
2. If `wcu` exhausted → trigger doubling (Task 9)
3. If app limit exhausted but `wcu` has tokens → retry on different shard (up to 2 retries)
4. Track tried shard IDs to avoid repeats

```python
MAX_SHARD_RETRIES = 2

tried_shards = {result.shard_id}
for _ in range(MAX_SHARD_RETRIES):
    if _is_wcu_exhausted(result.old_buckets):
        break  # trigger doubling instead
    # Pick a new shard not yet tried
    new_shard = _pick_untried_shard(shard_count, tried_shards)
    if new_shard is None:
        break
    tried_shards.add(new_shard)
    result = await self._repository.speculative_consume(
        entity_id, resource, consume, shard_id=new_shard, ...
    )
    if result.success:
        return result
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_limiter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/limiter.py tests/unit/test_limiter.py
git commit -m "feat(limiter): add shard retry on application limit exhaustion"
```

---

### Task 9: Add shard doubling on wcu exhaustion

**Files:**
- Modify: `src/zae_limiter/repository.py` (add `bump_shard_count` method)
- Modify: `src/zae_limiter/limiter.py` (trigger doubling in acquire flow)
- Test: `tests/unit/test_limiter.py`

**Step 1: Write failing test**

```python
def test_acquire_doubles_shards_on_wcu_exhaustion(mock_limiter):
    """When wcu is exhausted, shard_count doubles and acquire retries."""
    # Setup: entity with shard_count=1, wcu at 0 tokens, rpm has tokens
    # ...
    async with limiter.acquire("user-1", "gpt-4", rpm=1) as lease:
        pass
    # Verify shard_count was bumped
    assert repo._entity_cache[(ns, "user-1")][2]["gpt-4"] == 2
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_limiter.py -k "test_acquire_doubles_shards_on_wcu_exhaustion" -v`
Expected: FAIL

**Step 3: Implement bump_shard_count and doubling trigger**

Add `bump_shard_count()` to repository:

```python
async def bump_shard_count(
    self, entity_id: str, resource: str, current_count: int
) -> int:
    """Double shard_count on shard 0 (conditional write).

    Returns the new shard_count, or the current if another client already doubled.
    """
    new_count = current_count * 2
    client = await self._get_client()
    try:
        await client.update_item(
            TableName=self.table_name,
            Key={
                "PK": {"S": schema.pk_bucket(self._namespace_id, entity_id, resource, 0)},
                "SK": {"S": schema.sk_state()},
            },
            UpdateExpression="SET shard_count = :new",
            ConditionExpression="shard_count = :old",
            ExpressionAttributeValues={
                ":old": {"N": str(current_count)},
                ":new": {"N": str(new_count)},
            },
        )
        return new_count
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return current_count  # Another client already doubled
        raise
```

In the acquire flow, after detecting `wcu` exhaustion from speculative failure:
1. Call `bump_shard_count(entity_id, resource, current_shard_count)`
2. Update entity cache with new shard_count
3. Retry acquire on a new shard (lazy creation via slow path if bucket doesn't exist)

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_limiter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/repository.py src/zae_limiter/limiter.py tests/unit/test_limiter.py
git commit -m "feat(limiter): add shard doubling on wcu exhaustion"
```

---

## Phase 5: Aggregator Updates

### Task 10: Update _parse_bucket_record for new PK

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py:258-349`
- Test: `tests/unit/test_processor.py`

**Step 1: Write failing test**

```python
def test_parse_bucket_record_new_pk():
    """Parser handles new BUCKET PK scheme."""
    record = make_modify_record(
        pk="ns1/BUCKET#user-1#gpt-4#0",
        sk="#STATE",
        new_image={...},  # bucket attributes with b_rpm_tc, b_wcu_tc, shard_count
        old_image={...},
    )
    result = _parse_bucket_record(record)
    assert result is not None
    assert result.namespace_id == "ns1"
    assert result.entity_id == "user-1"
    assert result.resource == "gpt-4"
    assert result.shard_id == 0
    assert result.shard_count == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_processor.py -k "test_parse_bucket_record_new_pk" -v`
Expected: FAIL

**Step 3: Update parser**

Modify `_parse_bucket_record()` to:
1. Check PK starts with `{ns}/BUCKET#` instead of checking SK for `#BUCKET#`
2. Parse entity_id, resource, shard_id from PK using `schema.parse_bucket_pk()`
3. Extract `shard_count` from NewImage
4. Add `shard_id` and `shard_count` to `ParsedBucketRecord`

Update `ParsedBucketRecord` dataclass to add `shard_id: int` and `shard_count: int`.

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "feat(aggregator): update parser for new bucket PK scheme"
```

---

### Task 11: Update aggregate_bucket_states to key by shard

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py:390-447`
- Test: `tests/unit/test_processor.py`

**Step 1: Write failing test**

```python
def test_aggregate_bucket_states_keys_by_shard():
    """Different shards for same (entity, resource) are aggregated separately."""
    records = [
        make_modify_record(pk="ns1/BUCKET#user-1#gpt-4#0", ...),
        make_modify_record(pk="ns1/BUCKET#user-1#gpt-4#1", ...),
    ]
    states = aggregate_bucket_states(records)
    assert ("ns1", "user-1", "gpt-4", 0) in states
    assert ("ns1", "user-1", "gpt-4", 1) in states
    assert len(states) == 2
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_processor.py -k "test_aggregate_bucket_states_keys_by_shard" -v`
Expected: FAIL

**Step 3: Update aggregate key to include shard_id**

Change key from `(namespace_id, entity_id, resource)` to `(namespace_id, entity_id, resource, shard_id)`.
Add `shard_id` and `shard_count` to `BucketRefillState`.

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "feat(aggregator): key bucket states by shard_id"
```

---

### Task 12: Update try_refill_bucket for new PK and effective limits

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py:450-554`
- Test: `tests/unit/test_processor.py`

**Step 1: Write failing test**

```python
def test_try_refill_bucket_new_pk_and_effective_limits():
    """Refill uses new PK and divides capacity by shard_count."""
    state = BucketRefillState(
        namespace_id="ns1",
        entity_id="user-1",
        resource="gpt-4",
        shard_id=0,
        shard_count=2,
        rf_ms=1000,
        limits={
            "rpm": LimitRefillInfo(
                tc_delta=5000_000,
                tk_milli=0,
                cp_milli=10000_000,  # original capacity 10000
                ra_milli=10000_000,
                rp_ms=60_000,
            ),
        },
    )
    # Effective capacity = 10000/2 = 5000
    result = try_refill_bucket(mock_table, state, now_ms=61_000)
    assert result is True
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert call_kwargs["Key"]["PK"] == "ns1/BUCKET#user-1#gpt-4#0"
    assert call_kwargs["Key"]["SK"] == "#STATE"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_processor.py -k "test_try_refill_bucket_new_pk_and_effective_limits" -v`
Expected: FAIL

**Step 3: Update try_refill_bucket**

1. Use `schema.pk_bucket(state.namespace_id, state.entity_id, state.resource, state.shard_id)` and `schema.sk_state()` for Key
2. Compute effective limits: `effective_cp = cp_milli // shard_count`, `effective_ra = ra_milli // shard_count`
3. Pass effective values to `refill_bucket()`

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "feat(aggregator): use new bucket PK and effective limits for refill"
```

---

### Task 13: Filter wcu from usage snapshots

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py:353-387` (extract_deltas)
- Test: `tests/unit/test_processor.py`

**Step 1: Write failing test**

```python
def test_extract_deltas_filters_wcu():
    """wcu limit deltas are excluded from usage snapshots."""
    record = make_modify_record(
        pk="ns1/BUCKET#user-1#gpt-4#0",
        sk="#STATE",
        new_image={"b_rpm_tc": {"N": "100"}, "b_wcu_tc": {"N": "50"}, ...},
        old_image={"b_rpm_tc": {"N": "0"}, "b_wcu_tc": {"N": "0"}, ...},
    )
    deltas = extract_deltas(record)
    limit_names = [d.limit_name for d in deltas]
    assert "rpm" in limit_names
    assert "wcu" not in limit_names
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_processor.py -k "test_extract_deltas_filters_wcu" -v`
Expected: FAIL

**Step 3: Add filter in extract_deltas**

After parsing limits, skip `wcu`:

```python
for limit_name, info in parsed.limits.items():
    if limit_name == schema.WCU_LIMIT_NAME:
        continue  # Internal infrastructure limit, not for usage snapshots
    ...
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "feat(aggregator): filter wcu from usage snapshot deltas"
```

---

### Task 14: Add aggregator proactive sharding

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py` (add `try_proactive_shard` function, call from `process_stream_records`)
- Test: `tests/unit/test_processor.py`

**Step 1: Write failing test**

```python
WCU_PROACTIVE_THRESHOLD = 0.8  # 80% of capacity

def test_try_proactive_shard_triggers_at_threshold():
    """When wcu tc_delta >= 80% of capacity, aggregator bumps shard_count."""
    state = BucketRefillState(
        namespace_id="ns1",
        entity_id="user-1",
        resource="gpt-4",
        shard_id=0,
        shard_count=1,
        rf_ms=1000,
        limits={},
    )
    wcu_tc_delta = 900_000  # 900 tokens consumed (90% > 80% threshold)
    wcu_capacity_milli = 1000_000  # 1000 tokens

    result = try_proactive_shard(mock_table, state, wcu_tc_delta, wcu_capacity_milli)
    assert result is True

    # Verify shard_count bumped on shard 0
    call_kwargs = mock_table.update_item.call_args.kwargs
    assert call_kwargs["Key"]["PK"] == "ns1/BUCKET#user-1#gpt-4#0"
    assert call_kwargs["ExpressionAttributeValues"][":new"]["N"] == "2"
    assert call_kwargs["ConditionExpression"] == "shard_count = :old"

def test_try_proactive_shard_skips_below_threshold():
    """Below threshold, no sharding."""
    state = BucketRefillState(...)
    wcu_tc_delta = 500_000  # 50% < 80%
    result = try_proactive_shard(mock_table, state, wcu_tc_delta, wcu_capacity_milli)
    assert result is False
    mock_table.update_item.assert_not_called()

def test_try_proactive_shard_skips_non_shard_0():
    """Only shard 0 can be bumped."""
    state = BucketRefillState(
        ..., shard_id=1, shard_count=2, ...
    )
    wcu_tc_delta = 900_000
    result = try_proactive_shard(mock_table, state, wcu_tc_delta, wcu_capacity_milli)
    assert result is False

def test_try_proactive_shard_conditional_check_failure():
    """If another writer already bumped, silently skip."""
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    result = try_proactive_shard(mock_table, state, 900_000, 1000_000)
    assert result is False  # Skipped, not raised
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_processor.py -k "test_try_proactive_shard" -v`
Expected: FAIL

**Step 3: Implement try_proactive_shard**

```python
WCU_PROACTIVE_THRESHOLD = 0.8  # Shard when wcu consumption >= 80% of capacity

def try_proactive_shard(
    table: Any,
    state: BucketRefillState,
    wcu_tc_delta: int,
    wcu_capacity_milli: int,
) -> bool:
    """Proactively double shard_count when wcu consumption approaches capacity.

    Only acts on shard 0 (source of truth for shard_count).
    Uses conditional write to prevent double-bumping.

    Args:
        table: boto3 Table resource
        state: Aggregated bucket state
        wcu_tc_delta: Accumulated wcu tc_delta in this batch (millitokens)
        wcu_capacity_milli: wcu capacity in millitokens

    Returns:
        True if shard_count was bumped, False otherwise
    """
    if state.shard_id != 0:
        return False

    if wcu_capacity_milli <= 0:
        return False

    consumption_ratio = wcu_tc_delta / wcu_capacity_milli
    if consumption_ratio < WCU_PROACTIVE_THRESHOLD:
        return False

    new_count = state.shard_count * 2

    try:
        table.update_item(
            Key={
                "PK": pk_bucket(state.namespace_id, state.entity_id, state.resource, 0),
                "SK": sk_state(),
            },
            UpdateExpression="SET shard_count = :new",
            ConditionExpression="shard_count = :old",
            ExpressionAttributeValues={
                ":old": state.shard_count,
                ":new": new_count,
            },
        )
        logger.info(
            "Proactive shard doubling",
            entity_id=state.entity_id,
            resource=state.resource,
            old_count=state.shard_count,
            new_count=new_count,
            consumption_ratio=round(consumption_ratio, 2),
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.debug(
                "Proactive shard skipped - concurrent bump",
                entity_id=state.entity_id,
                resource=state.resource,
            )
            return False
        raise
```

Call from `process_stream_records()` after `aggregate_bucket_states()`:

```python
# Proactive sharding (check wcu consumption per bucket)
for state in bucket_states.values():
    wcu_info = state.limits.get(schema.WCU_LIMIT_NAME)
    if wcu_info:
        try:
            try_proactive_shard(
                table, state,
                wcu_tc_delta=wcu_info.tc_delta,
                wcu_capacity_milli=wcu_info.cp_milli,
            )
        except Exception as e:
            errors.append(f"Error in proactive sharding: {e}")
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "feat(aggregator): add proactive sharding on high wcu consumption"
```

---

### Task 15: Add aggregator shard_count propagation

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py` (add `propagate_shard_count` function, call from `process_stream_records`)
- Test: `tests/unit/test_processor.py`

**Step 1: Write failing test**

```python
def test_propagate_shard_count_on_change():
    """When shard_count changes in stream record, propagate to other shards."""
    record = make_modify_record(
        pk="ns1/BUCKET#user-1#gpt-4#0",
        sk="#STATE",
        new_image={"shard_count": {"N": "4"}, ...},
        old_image={"shard_count": {"N": "2"}, ...},
    )
    propagated = propagate_shard_count(mock_table, record)
    assert propagated == 3  # Updated shards 1, 2, 3 (not 0)

    # Verify correct PKs were updated
    calls = mock_table.update_item.call_args_list
    updated_pks = {c.kwargs["Key"]["PK"] for c in calls}
    assert "ns1/BUCKET#user-1#gpt-4#1" in updated_pks
    assert "ns1/BUCKET#user-1#gpt-4#2" in updated_pks
    assert "ns1/BUCKET#user-1#gpt-4#3" in updated_pks
    assert "ns1/BUCKET#user-1#gpt-4#0" not in updated_pks  # source, not updated

def test_propagate_shard_count_no_change():
    """No propagation when shard_count unchanged."""
    record = make_modify_record(
        pk="ns1/BUCKET#user-1#gpt-4#0",
        new_image={"shard_count": {"N": "2"}, ...},
        old_image={"shard_count": {"N": "2"}, ...},
    )
    propagated = propagate_shard_count(mock_table, record)
    assert propagated == 0
    mock_table.update_item.assert_not_called()

def test_propagate_shard_count_conditional_prevents_downgrade():
    """Conditional write prevents overwriting a higher shard_count."""
    # shard 1 already has shard_count=8, we're trying to set 4
    mock_table.update_item.side_effect = ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem"
    )
    record = make_modify_record(
        pk="ns1/BUCKET#user-1#gpt-4#0",
        new_image={"shard_count": {"N": "4"}, ...},
        old_image={"shard_count": {"N": "2"}, ...},
    )
    propagated = propagate_shard_count(mock_table, record)
    assert propagated == 0  # All skipped (higher value already present)
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/test_processor.py -k "test_propagate_shard_count" -v`
Expected: FAIL

**Step 3: Implement propagate_shard_count**

```python
def propagate_shard_count(
    table: Any,
    record: dict[str, Any],
) -> int:
    """Propagate shard_count changes to all other shard items.

    Detects shard_count change in stream record (OldImage vs NewImage).
    Only propagates from shard 0. Uses conditional write to prevent
    overwriting a higher shard_count set by another writer.

    Args:
        table: boto3 Table resource
        record: DynamoDB stream record

    Returns:
        Number of shard items updated
    """
    dynamodb_data = record.get("dynamodb", {})
    new_image = dynamodb_data.get("NewImage", {})
    old_image = dynamodb_data.get("OldImage", {})

    new_count_raw = new_image.get("shard_count", {}).get("N")
    old_count_raw = old_image.get("shard_count", {}).get("N")
    if not new_count_raw or not old_count_raw:
        return 0

    new_count = int(new_count_raw)
    old_count = int(old_count_raw)
    if new_count <= old_count:
        return 0

    pk = new_image.get("PK", {}).get("S", "")
    try:
        namespace_id, entity_id, resource, shard_id = parse_bucket_pk(pk)
    except ValueError:
        return 0

    if shard_id != 0:
        return 0  # Only propagate from source of truth

    updated = 0
    for target_shard in range(1, new_count):
        try:
            table.update_item(
                Key={
                    "PK": pk_bucket(namespace_id, entity_id, resource, target_shard),
                    "SK": sk_state(),
                },
                UpdateExpression="SET shard_count = :new",
                ConditionExpression="attribute_not_exists(shard_count) OR shard_count < :new",
                ExpressionAttributeValues={
                    ":new": new_count,
                },
            )
            updated += 1
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # Higher value already present
            raise

    if updated > 0:
        logger.info(
            "Shard count propagated",
            entity_id=entity_id,
            resource=resource,
            new_count=new_count,
            shards_updated=updated,
        )
    return updated
```

Call from `process_stream_records()` at the end of record processing:

```python
# Propagate shard_count changes to other shards
for record in records:
    if record.get("eventName") != "MODIFY":
        continue
    try:
        propagate_shard_count(table, record)
    except Exception as e:
        errors.append(f"Error propagating shard_count: {e}")
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_processor.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter_aggregator/processor.py tests/unit/test_processor.py
git commit -m "feat(aggregator): add shard_count propagation on change detection"
```

---

## Phase 6: Bucket Discovery and Reads

### Task 16: Add bucket discovery via GSI3

**Files:**
- Modify: `src/zae_limiter/repository.py` (update `get_buckets` to use GSI3 + BatchGetItem)
- Test: `tests/unit/test_repository.py`

**Step 1: Write failing test**

```python
def test_get_buckets_uses_gsi3(mock_dynamodb, unique_name):
    """get_buckets queries GSI3 for bucket PKs then BatchGetItem."""
    repo = make_repo(mock_dynamodb, unique_name)
    # Create buckets at new PKs for 2 resources
    # ...
    buckets = await repo.get_buckets("user-1")
    assert len(buckets) >= 2
    resources = {b.resource for b in buckets}
    assert "gpt-4" in resources
    assert "gpt-3.5" in resources
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_repository.py -k "test_get_buckets_uses_gsi3" -v`
Expected: FAIL

**Step 3: Implement GSI3 bucket discovery**

Update `get_buckets()` to:
1. Query GSI3 with `GSI3PK={ns}/ENTITY#{entity_id}`
2. Extract PK/SK from the KEYS_ONLY response
3. BatchGetItem to fetch full bucket items
4. Deserialize and return

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_repository.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/repository.py tests/unit/test_repository.py
git commit -m "feat(repository): add GSI3 bucket discovery with BatchGetItem"
```

---

## Phase 7: Hide wcu from User-Facing Output

### Task 17: Filter wcu from RateLimitExceeded and get_status

**Files:**
- Modify: `src/zae_limiter/limiter.py` (filter wcu from LimitStatus lists)
- Test: `tests/unit/test_limiter.py`

**Step 1: Write failing test**

```python
def test_rate_limit_exceeded_hides_wcu(mock_limiter):
    """wcu violations never appear in RateLimitExceeded."""
    # Setup: entity where only wcu is exhausted but rpm has tokens
    # This should trigger doubling, not raise with wcu violation

def test_get_status_hides_wcu(mock_limiter):
    """get_status omits wcu from returned limit statuses."""
    status = await limiter.get_status("user-1", "gpt-4")
    limit_names = [ls.limit_name for ls in status]
    assert "wcu" not in limit_names
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/test_limiter.py -k "test_rate_limit_exceeded_hides_wcu or test_get_status_hides_wcu" -v`
Expected: FAIL

**Step 3: Add filtering**

In the limiter, filter `wcu` from any list of `LimitStatus` before returning to user or building `RateLimitExceeded`:

```python
statuses = [s for s in statuses if s.limit_name != schema.WCU_LIMIT_NAME]
```

**Step 4: Run tests**

Run: `uv run pytest tests/unit/test_limiter.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/zae_limiter/limiter.py tests/unit/test_limiter.py
git commit -m "feat(limiter): hide wcu infrastructure limit from user-facing output"
```

---

## Phase 8: Sync Code Generation

### Task 18: Generate sync code and verify

**Files:**
- Generated: All `sync_*.py` files (see CLAUDE.md for list)
- Test: Generated test files

**Step 1: Run sync code generation**

Run: `hatch run generate-sync`

**Step 2: Run sync tests**

Run: `uv run pytest tests/unit/test_sync_repository.py tests/unit/test_sync_limiter.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add src/zae_limiter/sync_*.py tests/unit/test_sync_*.py
git commit -m "chore: regenerate sync code for pre-shard bucket changes"
```

---

## Phase 9: Integration and Migration

### Task 19: Update CloudFormation template (if needed)

**Files:**
- Check: `src/zae_limiter/infra/cfn_template.yaml`

**Step 1: Verify GSI3 definition**

GSI3 already exists in the template. Verify it's KEYS_ONLY projection. No changes expected since bucket items opt-in to GSI3 by setting GSI3PK/GSI3SK attributes.

**Step 2: Commit if changes needed**

```bash
git add src/zae_limiter/infra/cfn_template.yaml
git commit -m "chore(infra): verify GSI3 supports bucket discovery"
```

---

### Task 20: Schema version bump

**Files:**
- Modify: `src/zae_limiter/version.py` (bump schema version)
- Modify: `src/zae_limiter/migrations/__init__.py` (add migration entry)
- Test: `tests/unit/test_version.py`

**Step 1: Write failing test**

```python
def test_schema_version_bumped():
    """Schema version reflects bucket PK change."""
    assert version.SCHEMA_VERSION >= NEW_VERSION
```

**Step 2: Bump version and add migration**

The migration is a clean break — new code creates buckets at new PKs on first access. Old bucket items are ignored. Add a migration entry that documents this.

**Step 3: Run tests**

Run: `uv run pytest tests/unit/ -v`
Expected: PASS

**Step 4: Commit**

```bash
git add src/zae_limiter/version.py src/zae_limiter/migrations/
git commit -m "feat(schema): bump version for bucket PK migration"
```

---

### Task 21: Integration test with LocalStack

**Files:**
- Create: `tests/integration/test_bucket_sharding.py`

**Step 1: Write integration test**

```python
@pytest.mark.integration
class TestBucketSharding:
    async def test_acquire_creates_bucket_at_new_pk(self, test_repo):
        """First acquire creates bucket at BUCKET# PK."""

    async def test_sharded_acquire_distributes_across_shards(self, test_repo):
        """With shard_count=2, acquires hit both shards."""

    async def test_wcu_exhaustion_triggers_doubling(self, test_repo):
        """When wcu is exhausted, shard_count doubles."""

    async def test_shard_retry_on_drained_shard(self, test_repo):
        """Retry on another shard when one is drained."""

    async def test_get_buckets_aggregates_shards(self, test_repo):
        """get_buckets returns aggregated view across shards."""

    async def test_aggregator_proactive_shard(self, test_repo):
        """Aggregator detects high wcu and proactively shards."""

    async def test_aggregator_propagates_shard_count(self, test_repo):
        """Aggregator propagates shard_count changes to all shards."""
```

**Step 2: Run with LocalStack**

Run: `uv run pytest tests/integration/test_bucket_sharding.py -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/integration/test_bucket_sharding.py
git commit -m "test(integration): add bucket sharding integration tests"
```

---

### Task 22: Update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

Update the DynamoDB Access Patterns table, bucket schema documentation, entity cache structure, speculative write pattern documentation, and aggregator responsibilities to reflect the new bucket PK scheme, wcu limit, proactive sharding, and shard_count propagation.

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for pre-shard bucket PK scheme"
```

---

## Task Dependency Graph

```
Phase 1: Schema Foundation
  Task 1 (PK builders) ──┐
  Task 2 (reserved names) ├── Phase 2: Bucket Creation
  Task 3 (WCU constants) ─┘
                              Task 4 (build_composite_create) ──┐
                              Task 5 (speculative_consume) ─────┤
                                                                │
Phase 3: Shard Selection                                        │
  Task 6 (entity cache) ──── Task 7 (shard selection) ─────────┤
                                                                │
Phase 4: Retry & Doubling                                       │
  Task 8 (shard retry) ──── Task 9 (doubling) ─────────────────┤
                                                                │
Phase 5: Aggregator                                             │
  Task 10 (parser) ── Task 11 (aggregate key) ── Task 12 (refill)
  Task 13 (wcu filter)                                          │
  Task 14 (proactive sharding) ── Task 15 (shard propagation) ─┤
                                                                │
Phase 6: Discovery                                              │
  Task 16 (GSI3 discovery) ────────────────────────────────────┤
                                                                │
Phase 7: User-facing                                            │
  Task 17 (hide wcu) ─────────────────────────────────────────┤
                                                                │
Phase 8: Sync                                                   │
  Task 18 (generate sync) ── depends on all source changes ───┤
                                                                │
Phase 9: Integration                                            │
  Task 19 (CFN) ── Task 20 (version) ── Task 21 (integration) ── Task 22 (docs)
```
