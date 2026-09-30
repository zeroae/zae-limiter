# Quota Shard Grant Record Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair #637 (a sharded quota discards reclaimed surplus → under-admission, 187/1000) and #642 (a new or seeded quota shard can be granted a share a sibling already spent → over-admission, 1250 and 1300/1000) with one per-shard grant record.

**Architecture:** Every quota shard stores `b_{q}_gc`, the shard count its current-period grant was sized at. A new or seeded quota shard is funded by **moving** tokens off the current-period sibling that covers its slot (`j mod gc == i mod gc`, largest `gc`), or granted a fresh `C // S` only when no sibling covers it. The move rides in the acquire's own `TransactWriteItems`, so it is atomic. A quota shard's ceiling becomes `C // gc`, so nothing is trimmed away. One pure decision function in `models.py` is shared by the client and the aggregator.

**Tech Stack:** Python 3.11+, aioboto3/boto3 (DynamoDB), moto (unit tests), pytest + pytest-asyncio, the repo's AST sync generator (`scripts/generate_sync.py`).

**Spec:** `docs/plans/2026-09-28-quota-shard-grant-design.md` (PR #662). Read it before any task; section numbers below (§N) refer to it.

## Global Constraints

- **Worktree / venv:** work in a worktree created by `scripts/new-worktree.sh`; never share the main checkout's venv (CLAUDE.md "Worktrees need their own venv").
- **Edit async sources only.** `sync_repository.py`, `sync_repository_protocol.py`, `sync_limiter.py`, `sync_lease.py`, `tests/unit/test_sync_limiter.py`, `tests/unit/test_sync_repository.py` are generated: run `uv run python scripts/generate_sync.py` after touching `repository.py`, `repository_protocol.py`, `limiter.py`, `lease.py`, `tests/unit/test_limiter.py` or `tests/unit/test_repository.py`, and commit the regenerated files in the same commit.
- **`asyncio.gather` takes no keywords** in generator-covered source (#491).
- **Expression tokens are positional** (#634): every `#…`/`:…` token is built from a loop index, alphanumeric only; every new bucket write builder is added to `tests/unit/test_expression_tokens.py`.
- **Fast path unchanged:** `speculative_consume` must stay 0 RCU + 1 WCU and must not read or write `gc`.
- **`schedule.py` imports nothing from `models.py`**; the aggregator zip vendors only `schema.py`, `bucket.py`, `models.py`, `exceptions.py`, `schedule.py` — the shared planner must import nothing else.
- **Never suppress a lint rule** (`# noqa`, `# type: ignore`) without asking the owner.
- **One commit per task**, conventional commit + gitmoji; reference issues only as `Refs #637` / `Refs #642`. **Never** put close/fix/resolve next to `#597`, `#637` or `#642` in any commit — the final PR closes them.
- **Fixes to pre-existing behaviour get their own `🐛 fix(...)` commit** (`.claude/rules/commits.md`).
- **ADR number is 145.** 143 and 144 are taken by open PR #661 (limit inheritance by merge). Re-check `ls docs/adr` and open PRs before writing it.
- **No IAM change.** `TransactWriteItems` items are authorised by `PutItem`/`UpdateItem`, which every role already has (design §6, AWS docs "Using IAM with DynamoDB transactions"). Do not use a `ConditionCheck` item — that would need `dynamodb:ConditionCheckItem`.
- **Item with a quota but no `gc`** reads as `gc = its shard_count` (design §9, owner decision option 1).
- Run unit tests with xdist (never `-o addopts=` on the whole `tests/unit/`): `uv run pytest tests/unit/ -q`.

## Review Focus

1. **Transaction conflict on a hot donor.** The donor is usually the busiest shard, so `TransactionCanceled` with a `TransactionConflict` reason is likely under load; the acquire must retry with jitter and, if exhausted, still admit or reject correctly — never raise a raw `ClientError` to the caller. Pinned in Task 7 (`test_conflicted_move_retries_then_succeeds`).
2. **A cascade that creates both a child shard and a parent shard, each with a donor.** Two donor debits plus two bucket writes in one transaction; per-index cancellation reasons must still map to the right group. Pinned in Task 7 (`test_cascade_child_and_parent_moves_share_one_transaction`).
3. **Several quotas on one item with different donors.** Each quota picks its own donor; the transaction carries one debit per (donor shard, quota), merged into one `Update` per donor item (DynamoDB rejects two operations on one item in a transaction). Pinned in Task 6 (`test_two_quotas_same_donor_merge_into_one_update`).
4. **A legacy item (quota present, no `gc`) as donor.** Reads as `gc = shard_count`; the debit's condition must be `attribute_not_exists(gc) OR gc = :gc`, or every move off a v0.14 shard fails. Pinned in Task 6 (`test_legacy_donor_without_gc_can_donate`).
5. **Capacity decrease mid-period** (`set_limits` lowering a quota): the next pass trims each shard to `C_new // gc`, and the total stays ≤ `C_new`. Pinned in Task 3 (`test_capacity_decrease_trims_to_new_grant_share`).

---

## File Map

| File | Change |
|---|---|
| `docs/adr/145-sharded-quota-conserves-allowance.md` | new ADR |
| `mkdocs.yml` | nav entry for ADR-145 |
| `src/zae_limiter/schema.py` | `BUCKET_FIELD_GC` |
| `src/zae_limiter/models.py` | `BucketState.grant_count`, `reset_target_milli`, `ceiling_milli`, `report_shard_count`; remove `effective_capacity_milli`, `new_shard_starting_tokens_milli`; `from_limit(starting_tokens_milli=…)`; `QuotaSibling`, `QuotaGrant`, `plan_quota_grant`, `quota_grant_is_current` |
| `src/zae_limiter/bucket.py` | callers of the split capacity roles |
| `src/zae_limiter/repository.py` (+ protocol) | `gc` in `_limit_item_attrs` / deserializer / `_SEED_TOKEN`; `grant_counts` + `pin_shard_count` on `build_composite_normal`; `plan_quota_shard`, `build_quota_donor_debits`; remove `reclaim_quota_surplus`, `reclaim_quota_seed`, `_clamp_quota_shard`, `persist_seed` |
| `src/zae_limiter/limiter.py` | stamp `grant_count` on reset/roll; one `_quota_grants` call; re-plan once on a lost move |
| `src/zae_limiter/lease.py` | `LeaseEntry._donor`; donor debits in `_commit_initial`; zero-consumption commit on rejection; remove `persist_transfer_seeds` |
| `src/zae_limiter_aggregator/processor.py` | parse `gc`; reset/roll write `gc` + pin; quota clamp at `C // gc`; Path 2 via `plan_quota_grant` + transaction; remove `_reclaim_quota_surplus` |
| `tests/fixtures/quota_model.py` | the reference model (oracle) |
| `tests/unit/test_quota_grant_plan.py` | planner + model tests |
| `tests/unit/test_quota_shard_creation.py`, `test_window_shard_creation.py`, `test_processor.py`, `test_limiter.py`, `test_expression_tokens.py`, `test_schedule_encoding.py`, `test_bucket.py`, `test_models.py`, `tests/benchmark/test_capacity.py` | updated / new tests |
| `tests/unit/test_quota_conservation_fuzz.py` | model-based fuzz (I8) |
| `tests/unit/test_bucket_writer_registry.py` | writer registry guard |
| CLAUDE.md, `.claude/rules/write-on-enter.md`, `.claude/rules/code-review.md`, `docs/guide/session-quotas.md`, ADR-140/141/142 cross-refs | docs |

---

### Task 1: ADR-145

**Files:**
- Create: `docs/adr/145-sharded-quota-conserves-allowance.md`
- Modify: `mkdocs.yml` (ADR nav, after the ADR-142 line, ~209)

**Interfaces:** Produces the decision every later task implements. No code.

- [ ] **Step 1: Confirm the number is free**

Run: `ls docs/adr | tail -3; gh pr list --state open --json number,title --jq '.[].title' | rg -i adr`
Expected: highest local ADR is 142; PR #661 holds 143/144. If 145 is also taken, use the next free number everywhere in this plan.

- [ ] **Step 2: Write the ADR** (≤ 100 lines, one decision, no code/tests/cost math — ADR-000)

```markdown
# ADR-145: A sharded quota conserves its allowance

**Status:** Proposed
**Date:** 2026-09-28
**Issue:** [#637](https://github.com/zeroae/zae-limiter/issues/637), [#642](https://github.com/zeroae/zae-limiter/issues/642)
**Related:** ADR-133, ADR-134, ADR-137, [ADR-140](140-duration-window-shard-coherence.md), [#587](https://github.com/zeroae/zae-limiter/issues/587), [#477](https://github.com/zeroae/zae-limiter/issues/477)

## Context

A quota (ADR-137) never drips, so on a sharded entity each shard holds a slice of the
period's allowance and nothing refills it before the next reset. When a doubling adds
shards, the allowance must be divided among them without being created or destroyed.

Until now the only record of a shard's slice was its balance. The #587 reclaim clamped
every sibling to the new share and kept one share for the new shard, discarding the
rest (#637: a full, unspent quota of 1000 fell to 187 spendable after a 1→32 walk). A
new or seeded shard was granted a full share unless a sibling visibly held a surplus,
so a share already granted at a lower count and spent was granted again (#642: 1250 and
1300 admitted against 1000). Both follow from one gap: nothing records how much of the
period's allowance each shard was handed.

Balances alone cannot close it. A shard created after a reset is owed its own share,
while a shard split off a sibling granted at a lower count is owed only what that
sibling still holds; the two look identical in the tokens.

## Decision

Every quota shard must record the shard count its current-period grant was sized at,
and a new or seeded quota shard must be funded by an atomic move from the
current-period sibling whose grant covers its slot, receiving fresh allowance only
when no such sibling exists.

## Consequences

**Positive:**
- A doubling neither creates nor destroys quota allowance: per period, admitted plus
  held plus still-grantable equals the configured capacity.
- #637 and #642 close together, including the aggregator's proactive clone.
- The speculative fast path is untouched.

**Negative:**
- A quota shard's ceiling is its grant, not `capacity // shard_count`, so balances
  across shards can be uneven, and a draw on an empty shard can reject while the
  entity holds tokens elsewhere, until the next reset.
- A shard creation that moves tokens is a transaction, and can conflict with writes
  on a busy donor.
- A shard written before the record exists is read as granted at its stored count, so
  #642's residual survives at most one period after upgrade.
- The rule applies to the `divided` sharding regime; the choice of regime is #477.

## Alternatives Considered

### Full capacity per shard, refill divided
Rejected: burst after idle reaches `shard_count × capacity`, and a quota would grant that per period.

### Borrow tokens from a sibling on rejection
Rejected: a genuinely exhausted entity would lose its free fast rejection; it belongs to #477.

### A grant amount with no holding item
Rejected: two concurrent creators both claim the same unheld remainder.

### One per-entity allowance item
Rejected: a write per shard creation on one hot item reintroduces the partition limit sharding avoids.
```

- [ ] **Step 3: Add the nav entry**

In `mkdocs.yml`, after the line `- ADR-142 A reset_after limit's config is hidden from pre-v0.15 readers: adr/142-hide-reset-after-config.md`, add (same indentation):

```yaml
          - ADR-145 A sharded quota conserves its allowance: adr/145-sharded-quota-conserves-allowance.md
```

- [ ] **Step 4: Check length and build**

Run: `wc -l docs/adr/145-sharded-quota-conserves-allowance.md && uv run mkdocs build --strict 2>&1 | tail -5`
Expected: ≤ 100 lines; build succeeds (a missing 143/144 nav entry is not an error).

- [ ] **Step 5: Commit**

```bash
git add docs/adr/145-sharded-quota-conserves-allowance.md mkdocs.yml
git commit -m "📝 docs(adr): add ADR-145, a sharded quota conserves its allowance

Refs #637, Refs #642."
```

---

### Task 2: Store and read `gc`

**Files:**
- Modify: `src/zae_limiter/schema.py` (after `BUCKET_FIELD_WTC`, ~146)
- Modify: `src/zae_limiter/models.py` (`BucketState`, after `window_consumed_mark_milli` ~1265)
- Modify: `src/zae_limiter/repository.py` (`_SEED_TOKEN` 75-86, `_limit_item_attrs` 2497-2540, `_deserialize_composite_bucket` ~6268-6325)
- Modify: `src/zae_limiter_aggregator/processor.py` (`LimitRefillInfo` 118-146, `ParsedBucketLimit` 342-356, `_parse_bucket_record` ~567-580, `aggregate_bucket_states` ~735/749)
- Test: `tests/unit/test_repository.py`, `tests/unit/test_processor.py`, `tests/unit/test_schedule_encoding.py`

**Interfaces:**
- Produces: `schema.BUCKET_FIELD_GC = "gc"`; `BucketState.grant_count: int | None = None`; `LimitRefillInfo.grant_count: int | None = None`; `ParsedBucketLimit.grant_count: int | None = None`. `_limit_item_attrs(state)` emits `gc` iff `state.grant_count is not None`.

- [ ] **Step 1: Failing tests**

In `tests/unit/test_repository.py` (sync twin is generated), add:

```python
class TestGrantCountStorage:
    """`b_{q}_gc` round-trips through a bucket item (Refs #637, #642)."""

    def test_limit_item_attrs_emits_gc_only_when_set(self, repo):
        state = BucketState.from_limit(
            "e1", "gpt-4", Limit.quota("rpd", 1000, cron="0 0 * * *"), 1_000, shard_count=4
        )
        assert schema.BUCKET_FIELD_GC not in repo._limit_item_attrs(state)
        state.grant_count = 4
        assert repo._limit_item_attrs(state)[schema.BUCKET_FIELD_GC] == {"N": "4"}

    async def test_deserialize_reads_gc(self, repo):
        item = {
            "PK": {"S": schema.pk_bucket(repo._namespace_id, "e1", "gpt-4", 1)},
            "SK": {"S": schema.sk_state()},
            "entity_id": {"S": "e1"},
            "resource": {"S": "gpt-4"},
            "rf": {"N": "1000"},
            "shard_count": {"N": "4"},
            "b_rpd_tk": {"N": "250000"},
            "b_rpd_cp": {"N": "1000000"},
            "b_rpd_ra": {"N": "0"},
            "b_rpd_rp": {"N": "1000"},
            "b_rpd_tc": {"N": "0"},
            "b_rpd_gc": {"N": "2"},
        }
        (state,) = [s for s in repo._deserialize_composite_bucket(item) if s.limit_name == "rpd"]
        assert state.grant_count == 2

    async def test_deserialize_missing_gc_is_none(self, repo):
        item = {
            "PK": {"S": schema.pk_bucket(repo._namespace_id, "e1", "gpt-4", 0)},
            "SK": {"S": schema.sk_state()},
            "entity_id": {"S": "e1"},
            "resource": {"S": "gpt-4"},
            "rf": {"N": "1000"},
            "b_rpd_tk": {"N": "1"},
            "b_rpd_cp": {"N": "1000000"},
            "b_rpd_ra": {"N": "0"},
            "b_rpd_rp": {"N": "1000"},
        }
        (state,) = [s for s in repo._deserialize_composite_bucket(item) if s.limit_name == "rpd"]
        assert state.grant_count is None
```

(Use the existing `repo` fixture of that file; if `_deserialize_composite_bucket` is not async, drop `async`.)

In `tests/unit/test_processor.py`, add to the parse tests:

```python
def test_parse_bucket_record_reads_gc():
    record = _stream_record(
        new_image={**_quota_image(shard=1, shard_count=4), "b_rpd_gc": {"N": "2"}}
    )
    parsed = _parse_bucket_record(record)
    assert parsed.limits["rpd"].grant_count == 2
```

(Build `_stream_record` / `_quota_image` from the helpers already used by `TestQuotaShardCloneIsATransfer` at ~3342; reuse, do not duplicate — if no such helper exists, construct the image dict inline exactly as those tests do.)

In `tests/unit/test_schedule_encoding.py` `TestSizeBudget`, extend the "+ one rolling limit" test (515-532) so the hand-built item also carries `b_lim0_wa`, `b_lim0_wtc` and `b_lim0_gc` (values `{"N": "1757000000000"}`, `{"N": "0"}`, `{"N": "32"}`) and still asserts `< 1024`; update the exact-byte assertion only if one exists for that case, recording the new headroom in the test's docstring.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_repository.py -k GrantCount tests/unit/test_processor.py -k gc -q`
Expected: FAIL (`AttributeError: BUCKET_FIELD_GC` / `grant_count`).

- [ ] **Step 3: Implement**

`schema.py`, after `BUCKET_FIELD_WTC`:

```python
# `b_{name}_gc` (ADR-145): the shard count this shard's current-period grant of a
# quota was sized at. A shard with grant count `g` covers every slot `j` with
# `j mod g == its own id mod g`, and its balance is the unspent part of those
# slots' allowance. Written only by a reset, window roll, create or seed — never
# by the fast path. Absent on items written before ADR-145; read as the item's
# `shard_count` (owner decision, design §9).
BUCKET_FIELD_GC = "gc"
```

`models.py` `BucketState`, after `window_consumed_mark_milli`:

```python
    # `b_{name}_gc` (ADR-145): the shard count this shard's current-period quota
    # grant was sized at. `None` for a rate limit and for a quota item written
    # before ADR-145 (read as `shard_count`, see `grant_shard_count`).
    grant_count: int | None = None
```

`repository.py` `_SEED_TOKEN`: add `schema.BUCKET_FIELD_GC: "k",`.

`_limit_item_attrs`, before `if include_window:`:

```python
        if state.grant_count is not None:
            attrs[schema.BUCKET_FIELD_GC] = {"N": str(state.grant_count)}
```

`_deserialize_composite_bucket`, beside the `wa` decode:

```python
gc_name = schema.bucket_attr(name, schema.BUCKET_FIELD_GC)
grant_count = self._decode_stored_window_int(gc_name, item.get(gc_name, {}).get("N"))
```

and pass `grant_count=grant_count` into the `BucketState(...)` constructor call.

`processor.py`: import `BUCKET_FIELD_GC`; add `grant_count: int | None = None` to `LimitRefillInfo` and `ParsedBucketLimit`; in `_parse_bucket_record` read it exactly as `wa` is read (same int parsing), and copy it wherever `window_applied_ms` is copied in `aggregate_bucket_states` (~735, ~749).

- [ ] **Step 4: Regenerate sync and run**

Run: `uv run python scripts/generate_sync.py && uv run pytest tests/unit/test_repository.py tests/unit/test_sync_repository.py tests/unit/test_processor.py tests/unit/test_schedule_encoding.py tests/unit/test_expression_tokens.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "✨ feat(schema): store the quota grant count b_{q}_gc

Refs #637, Refs #642. ADR-145: the shard count a quota shard's
current-period grant was sized at, read and written by the client and
parsed by the aggregator. Nothing acts on it yet."
```

---

### Task 3: Split the capacity roles (ceiling vs reset target)

**Files:**
- Modify: `src/zae_limiter/models.py` (`effective_capacity_milli` 1291-1299, `window_roll_target_milli` 1362-1384, `from_limit` 1452-1527)
- Modify: `src/zae_limiter/bucket.py` (135, 240, 290, 417, 444, 479, 582)
- Modify: `src/zae_limiter/limiter.py` (1539, 1597, 1729, 2735, 2757)
- Modify: `src/zae_limiter/lease.py` (290, 311, 535, 580, 610, 1193, 1229)
- Test: `tests/unit/test_models.py`, `tests/unit/test_bucket.py`, and the four test files that call `effective_capacity_milli` (`test_bucket.py`, `test_models.py`, `test_quota_shard_creation.py`, `tests/e2e/test_localstack.py`)

**Interfaces:**
- Produces on `BucketState`:
  - `is_quota_state -> bool` (property): `bool(self.reset_sched) or self.reset_after_seconds is not None`
  - `grant_shard_count -> int` (property): `self.grant_count if self.grant_count is not None else self.shard_count`
  - `reset_target_milli(now_ms) -> int`: `cp_in_force // shard_count` (today's `effective_capacity_milli`)
  - `ceiling_milli(now_ms) -> int`: `cp_in_force // grant_shard_count` for a quota, else `cp_in_force // shard_count`
  - `report_shard_count -> int` (property): `grant_shard_count` for a quota, else `shard_count`
- Removes: `effective_capacity_milli`.

- [ ] **Step 1: Failing tests** (`tests/unit/test_models.py`)

```python
class TestCapacityRoles:
    """ADR-145: a quota's ceiling is its grant, not the current share."""

    def _quota_state(self, shard_count: int, grant_count: int | None, tokens: int) -> BucketState:
        limit = Limit.quota("rpd", 1000, cron="0 0 * * *")
        state = BucketState.from_limit("e", "r", limit, 0, shard_count=shard_count)
        state.grant_count = grant_count
        state.tokens_milli = tokens
        return state

    def test_quota_ceiling_is_capacity_over_grant_count(self):
        state = self._quota_state(shard_count=4, grant_count=2, tokens=500_000)
        assert state.ceiling_milli(0) == 500_000
        assert state.reset_target_milli(0) == 250_000

    def test_missing_grant_count_reads_as_shard_count(self):
        state = self._quota_state(shard_count=4, grant_count=None, tokens=250_000)
        assert state.grant_shard_count == 4
        assert state.ceiling_milli(0) == 250_000

    def test_rate_limit_ceiling_unchanged(self):
        state = BucketState.from_limit("e", "r", Limit.per_minute("rpm", 100), 0, shard_count=4)
        state.grant_count = 1  # ignored for a rate limit
        assert state.ceiling_milli(0) == 25_000
        assert state.report_shard_count == 4

    def test_report_shard_count_follows_grant_for_a_quota(self):
        state = self._quota_state(shard_count=4, grant_count=2, tokens=0)
        assert state.report_shard_count == 2

    def test_capacity_decrease_trims_to_new_grant_share(self):
        # Review focus 5: C 1000 -> 600 mid-period, shard granted at count 2.
        state = self._quota_state(shard_count=4, grant_count=2, tokens=500_000)
        state.capacity_milli = 600_000
        refill = refill_bucket(state.tokens_milli, 0, 1, state.ceiling_milli(1), 0, 1_000)
        assert refill.new_tokens_milli == 300_000
```

(Import `refill_bucket` from `zae_limiter.bucket`.)

In `tests/unit/test_bucket.py`:

```python
def test_try_consume_keeps_a_quota_surplus_held_for_covered_slots():
    # #637: a shard granted at count 2 holds 500 at count 4; the pass must not trim it.
    limit = Limit.quota("rpd", 1000, cron="0 0 * * *")
    state = BucketState.from_limit("e", "r", limit, 0, shard_count=4)
    state.reset_sched = limit.reset_schedule
    state.grant_count = 2
    state.tokens_milli = 500_000
    result = try_consume(state, 1, 10)
    assert result.success and result.new_tokens_milli == 499_000
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_models.py -k CapacityRoles tests/unit/test_bucket.py -k covered_slots -q`
Expected: FAIL (`AttributeError: ceiling_milli`).

- [ ] **Step 3: Implement in `models.py`**

Replace `effective_capacity_milli` with:

```python
@property
def is_quota_state(self) -> bool:
    """Whether this state is a quota (ADR-137): a reset schedule or a duration window."""
    return bool(self.reset_sched) or self.reset_after_seconds is not None


@property
def grant_shard_count(self) -> int:
    """The count this shard's quota grant was sized at (ADR-145).

    An item written before the grant record reads as its stored count
    (design §9, owner decision option 1).
    """
    return self.grant_count if self.grant_count is not None else self.shard_count


@property
def report_shard_count(self) -> int:
    """The divisor a reported per-shard limit uses (`Limit.per_shard`).

    A quota shard reports what it was granted (`C // gc`), so a 429 never
    shows more available than capacity. Everything else reports the share.
    """
    return self.grant_shard_count if self.is_quota_state else self.shard_count


def reset_target_milli(self, now_ms: int) -> int:
    """The balance a reset or window roll sets: this shard's share at ``now_ms``.

    Scale first, divide second (#222 §2.1).
    """
    cp, _ra, _rp = self._scheduled_params(now_ms)
    return cp // self.shard_count


def ceiling_milli(self, now_ms: int) -> int:
    """The most this shard may hold at ``now_ms`` — the refill clamp (ADR-145).

    A quota shard holds the unspent allowance of every slot its grant
    covers, so its ceiling is ``C // gc``; trimming it to ``C // S`` would
    discard allowance no other shard holds (#637). A dripping limit keeps
    ``C // S``: its shards' ceilings must sum to the capacity.
    """
    cp, _ra, _rp = self._scheduled_params(now_ms)
    divisor = self.grant_shard_count if self.is_quota_state else self.shard_count
    return cp // divisor
```

`window_roll_target_milli`: replace `self.effective_capacity_milli(now_ms)` with `self.reset_target_milli(now_ms)`.

`from_limit`: its token line (1522) becomes `self`-independent in Task 7; for now replace `state.effective_capacity_milli(now_ms)` with `state.reset_target_milli(now_ms)`.

- [ ] **Step 4: Update every caller by role**

Ceiling (`ceiling_milli`): `bucket.py` 135 (`try_consume`), 417, 444, 479.
Reset target (`reset_target_milli`): `bucket.py` 240, 290; `limiter.py` 1539, 1597, 2735, 2757; `lease.py` 535, 580, 610; `models.py` 1381, 1523.
Reporting: every `per_shard(state.shard_count, …)` / `per_shard(entry.state.shard_count, …)` → `per_shard(<state>.report_shard_count, …)` at `bucket.py` 582, `limiter.py` 1729, `lease.py` 290, 311, 1193, 1229.

Then: `rg -n "effective_capacity_milli" src tests` must return nothing; update the test call sites to `reset_target_milli` (they assert a reset/start share) unless the test is about the refill clamp, then `ceiling_milli`.

- [ ] **Step 5: Regenerate, run the whole unit suite**

Run: `uv run python scripts/generate_sync.py && uv run pytest tests/unit/ -q && uv run mypy`
Expected: PASS. A quota with `grant_count is None` behaves exactly as before, so no existing test should change behaviour.

- [ ] **Step 6: Commit**

```bash
git add -A src tests
git commit -m "♻️ refactor(models): split the refill ceiling from the reset target

Refs #637. effective_capacity_milli served two roles: the balance a reset
sets and the most a shard may hold. ADR-145 makes them differ for a quota
(C // gc vs C // S), so it is removed and every caller now names its role.
Behaviour is unchanged until something writes gc."
```

---

### Task 4: The pure planner and the reference model

**Files:**
- Modify: `src/zae_limiter/models.py` (new section at the end of the module)
- Create: `tests/fixtures/quota_model.py`
- Create: `tests/unit/test_quota_grant_plan.py`

**Interfaces:**
- Produces (in `zae_limiter.models`, not exported from `__init__`):

```python
@dataclass(frozen=True)
class QuotaSibling:
    shard_id: int
    tokens_milli: int
    grant_count: int  # stored gc, or the item's shard_count if absent
    current: bool  # the grant belongs to the current period


@dataclass(frozen=True)
class QuotaGrant:
    donor_shard: int | None  # None = fresh grant
    tokens_milli: int  # what the new/seeded shard starts with (before consumption)
    donor_grant_count: int | None  # the donor's gc as read (condition value)


def plan_quota_grant(
    siblings: Sequence[QuotaSibling], shard_id: int, shard_count: int, share_milli: int
) -> QuotaGrant: ...


def quota_grant_is_current(
    limit: Limit,
    rf_ms: int,
    window_start_ms: int | None,
    window_applied_ms: int | None,
    now_ms: int,
) -> bool: ...
```

- `tests.fixtures.quota_model.QuotaModel` — the design's model: `QuotaModel(capacity, rule="NEW"|"OLD")` with `grant(j)`, `double()`, `spend(i, n) -> bool`, `touch(i)`, `next_period()`, `accounted() -> int`, `shards`, `S`, `admitted`.

- [ ] **Step 1: Failing planner tests** (`tests/unit/test_quota_grant_plan.py`)

```python
"""The ADR-145 grant decision and its reference model (Refs #637, #642)."""

from datetime import timedelta
import random

from zae_limiter import Limit
from zae_limiter.models import QuotaSibling, plan_quota_grant, quota_grant_is_current
from tests.fixtures.quota_model import QuotaModel

C = 1_000_000  # 1000 tokens, milli


def sib(i, tk, gc, current=True):
    return QuotaSibling(shard_id=i, tokens_milli=tk, grant_count=gc, current=current)


class TestPlanQuotaGrant:
    def test_no_sibling_is_a_fresh_share(self):
        g = plan_quota_grant([], shard_id=0, shard_count=1, share_milli=C)
        assert (g.donor_shard, g.tokens_milli) == (None, C)

    def test_split_moves_from_the_parent(self):
        g = plan_quota_grant([sib(0, C, 1)], shard_id=1, shard_count=2, share_milli=C // 2)
        assert (g.donor_shard, g.tokens_milli, g.donor_grant_count) == (0, C // 2, 1)

    def test_parent_spent_gives_nothing(self):  # the #642 case
        g = plan_quota_grant([sib(1, 0, 2), sib(0, C // 2, 2)], 3, 4, C // 4)
        assert (g.donor_shard, g.tokens_milli) == (1, 0)

    def test_after_a_reset_an_uncovered_slot_is_fresh(self):
        g = plan_quota_grant([sib(0, C // 4, 4)], 3, 4, C // 4)
        assert (g.donor_shard, g.tokens_milli) == (None, C // 4)

    def test_stale_sibling_never_covers(self):  # the 1300 case
        g = plan_quota_grant([sib(1, C // 2, 2, current=False)], 3, 4, C // 4)
        assert g.donor_shard is None

    def test_closest_ancestor_wins(self):
        g = plan_quota_grant([sib(0, C, 1), sib(1, C // 2, 2)], 3, 4, C // 4)
        assert g.donor_shard == 1

    def test_lazy_creation_reaches_the_root(self):
        # shard 5 at count 8 before shard 1 exists: shard 0 (gc 1) covers it
        g = plan_quota_grant([sib(0, C, 1)], 5, 8, C // 8)
        assert (g.donor_shard, g.tokens_milli) == (0, C // 8)

    def test_tie_takes_lowest_shard_id(self):
        g = plan_quota_grant([sib(3, C // 4, 2), sib(1, C // 4, 2)], 5, 8, C // 8)
        assert g.donor_shard == 1


class TestQuotaGrantIsCurrent:
    def test_calendar_current_when_rf_at_or_after_last_edge(self):
        limit = Limit.quota("rpd", 10, cron="0 0 * * *")
        midnight = 1_757_030_400_000  # 2025-09-05T00:00Z
        assert quota_grant_is_current(limit, midnight, None, None, midnight + 5_000)
        assert not quota_grant_is_current(limit, midnight - 1, None, None, midnight + 5_000)

    def test_session_current_when_window_live_and_applied(self):
        limit = Limit.quota("s", 10, reset_after=timedelta(hours=5))
        ws = 1_757_000_000_000
        assert quota_grant_is_current(limit, ws, ws, ws, ws + 1_000)
        assert not quota_grant_is_current(limit, ws, ws, ws - 1, ws + 1_000)  # pending roll
        assert not quota_grant_is_current(limit, ws, ws, ws, ws + 5 * 3_600_000)  # ended


class TestReferenceModel:
    def test_old_rule_reproduces_637(self):
        rng = random.Random(0)
        m = QuotaModel(1024, rule="OLD")
        m.spend(0, 0)
        while m.S < 32:
            m.double()
            m.grant(rng.choice([j for j in range(m.S) if j not in m.shards]))
        for j in range(m.S):
            if j not in m.shards:
                m.grant(j)
        assert sum(q.tk for q in m.shards.values()) == 192

    def test_new_rule_conserves_on_every_order(self):
        for seed in range(50):
            rng = random.Random(seed)
            m = QuotaModel(1024, rule="NEW")
            m.spend(0, 0)
            while m.S < 32:
                m.double()
                m.grant(rng.choice([j for j in range(m.S) if j not in m.shards]))
            for j in rng.sample(range(m.S), m.S):
                if j not in m.shards:
                    m.grant(j)
            assert sum(q.tk for q in m.shards.values()) == 1024
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_quota_grant_plan.py -q`
Expected: FAIL (ImportError).

- [ ] **Step 3: Implement the planner** (end of `models.py`)

```python
# --- ADR-145: the quota grant decision ---------------------------------------


@dataclass(frozen=True)
class QuotaSibling:
    """One existing shard of a quota, as the grant decision sees it (ADR-145)."""

    shard_id: int
    tokens_milli: int
    grant_count: int
    current: bool


@dataclass(frozen=True)
class QuotaGrant:
    """What a new or seeded quota shard starts with, and where it comes from."""

    donor_shard: int | None
    tokens_milli: int
    donor_grant_count: int | None = None


def plan_quota_grant(
    siblings: Sequence[QuotaSibling],
    shard_id: int,
    shard_count: int,
    share_milli: int,
) -> QuotaGrant:
    """Decide how shard ``shard_id`` at count ``shard_count`` is funded (ADR-145).

    A current-period sibling ``i`` whose grant was sized at count ``g`` covers
    every slot ``j`` with ``j % g == i % g``. If one covers ``shard_id``, the
    closest (largest ``g``, then lowest id) donates ``min(share, its tokens)``
    — a move, never a mint. If none does, nobody has been granted this slot's
    share this period, and it is granted fresh. Shared by the client and the
    aggregator's Path 2 clone; imports nothing outside the vendored stub.
    """
    covering = [
        s
        for s in siblings
        if s.current
        and s.shard_id != shard_id
        and shard_id % s.grant_count == s.shard_id % s.grant_count
    ]
    if not covering:
        return QuotaGrant(donor_shard=None, tokens_milli=share_milli)
    donor = min(covering, key=lambda s: (-s.grant_count, s.shard_id))
    return QuotaGrant(
        donor_shard=donor.shard_id,
        tokens_milli=max(0, min(share_milli, donor.tokens_milli)),
        donor_grant_count=donor.grant_count,
    )


def quota_grant_is_current(
    limit: "Limit",
    rf_ms: int,
    window_start_ms: int | None,
    window_applied_ms: int | None,
    now_ms: int,
) -> bool:
    """Whether a sibling's quota grant belongs to the current period (design §5).

    The exact negation of "a reset is pending": for a calendar quota, no reset
    edge after ``rf`` (`RateLimiter._apply_reset_edge`); for a session quota, a
    live window already applied (``BucketState.window_rolled``).
    """
    if limit.reset_after_seconds is not None:
        if window_start_ms is None:
            return False
        if window_start_ms + limit.reset_after_seconds * 1000 <= now_ms:
            return False
        applied = window_applied_ms if window_applied_ms is not None else rf_ms
        return window_start_ms <= applied
    edge = prev_reset_edge(limit.reset_schedule, now_ms)
    return edge is None or edge <= rf_ms
```

(`prev_reset_edge` is already imported by `models.py` from `schedule`; if not, import it from `.schedule`. `Sequence` from `collections.abc`.) Line-length the covering comprehension to satisfy ruff.

- [ ] **Step 4: Write the reference model** (`tests/fixtures/quota_model.py`)

Port `sim.py` from the design session verbatim in behaviour (the design §3.1 table is its output). Classes `Q(tk, gc, period)` and `QuotaModel(capacity, rule)` with the methods listed under Interfaces; `NEW.grant` must implement the rule independently of `plan_quota_grant` (it is the oracle, not a wrapper), and `OLD.grant` is today's clamp rule (clamp every sibling above `C // S`, new shard `min(taken, C // S)`, full share when no sibling exists). `touch(i)` applies a pending reset (`tk = C // S, gc = S`) then clamps to `C // gc` (NEW) or `C // S` (OLD). `accounted()` = admitted this period + held by current shards + `C // S` for every slot that is neither current nor covered.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/unit/test_quota_grant_plan.py -q && uv run mypy`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/zae_limiter/models.py tests/fixtures/quota_model.py tests/unit/test_quota_grant_plan.py
git commit -m "✨ feat(models): add the ADR-145 quota grant decision

Refs #637, Refs #642. plan_quota_grant decides whether a new or seeded
quota shard is funded by a move from the covering sibling or granted
fresh; quota_grant_is_current is the negation of a pending reset. Adds
the reference model the design was validated on, as a test oracle."
```

---

### Task 5: Resets and rolls stamp `gc`, pinned on the count

**Files:**
- Modify: `src/zae_limiter/limiter.py` (`_apply_reset_edge` 1505-1540, `_apply_window_roll` 1542-1604)
- Modify: `src/zae_limiter/lease.py` (`LeaseEntry` 36-125; `_commit_initial` normal branch 623-662)
- Modify: `src/zae_limiter/repository.py` (+ protocol): `build_composite_normal` — add `grant_counts: dict[str, int] | None = None`; rename `seed_shard_count` → `pin_shard_count`
- Test: `tests/unit/test_limiter.py`, `tests/unit/test_expression_tokens.py`

**Interfaces:**
- Consumes: `BucketState.grant_count` (Task 2).
- Produces: `LeaseEntry._granted: bool = False` (a reset/roll/create/seed set this entry's grant this pass); `build_composite_normal(..., grant_counts=..., pin_shard_count=...)`. Tokens `#gc{i}` / `:gc{i}`.

- [ ] **Step 1: Failing tests**

`tests/unit/test_limiter.py`:

```python
class TestResetStampsGrantCount:
    """ADR-145 I3/I4: a reset writes gc = the item's shard count, pinned."""

    async def test_calendar_reset_writes_gc(self, limiter, frozen_clock):
        limit = Limit.quota("rpd", 1000, cron="0 0 * * *")
        await limiter.set_limits("e1", [limit], resource="gpt-4")
        async with limiter.acquire("e1", "gpt-4", {"rpd": 1}):
            pass
        frozen_clock.advance(days=1)  # cross midnight
        async with limiter.acquire("e1", "gpt-4", {"rpd": 1}):
            pass
        (state,) = [
            s
            for s in await limiter._repository.get_buckets("e1", resource="gpt-4")
            if s.limit_name == "rpd"
        ]
        assert state.grant_count == 1

    def test_normal_write_pins_shard_count_when_granting(self, repo):
        kwargs = repo.build_composite_normal(
            entity_id="e1",
            resource="gpt-4",
            consumed={"rpd": 1000},
            refill_amounts={"rpd": 0},
            now_ms=10,
            expected_rf=5,
            grant_counts={"rpd": 4},
            pin_shard_count=4,
        )["Update"]
        assert "#gc0 = :gc0" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][":gc0"] == {"N": "4"}
        assert "#pinsc <= :pinsc" in kwargs["ConditionExpression"]
```

(Use the `frozen_clock` helper the existing reset-edge tests in `test_limiter.py` use — search `class TestResetEdge` / `freeze_clock` and reuse the same fixture name. `get_buckets` returns `BucketState`s; if it filters by shard, pass `shard_id=0`.)

`tests/unit/test_expression_tokens.py` `TestCompositeBuilders`:

```python
def test_normal_with_grant_counts(self):
    assert_expression_safe(
        _repo().build_composite_normal(
            entity_id="e",
            resource="r",
            consumed={DOTTED: 1000, HYPHENATED: 0},
            refill_amounts={},
            now_ms=2,
            expected_rf=1,
            grant_counts={DOTTED: 4, HYPHENATED: 4},
            pin_shard_count=4,
        )["Update"]
    )
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_limiter.py -k ResetStampsGrantCount tests/unit/test_expression_tokens.py -k grant_counts -q`
Expected: FAIL (unexpected keyword `grant_counts`).

- [ ] **Step 3: Implement**

`limiter.py`, in `_apply_reset_edge` after setting `state.tokens_milli`:

```python
        state.grant_count = state.shard_count
```

In `_apply_window_roll`, beside the `window_applied_ms` stamp (1603): `state.grant_count = state.shard_count`.

`lease.py` `LeaseEntry`: add

```python
    # ADR-145: this pass set the entry's quota grant (reset, roll, create or
    # seed), so the write must stamp `gc` and pin `shard_count` (I3, I4).
    _granted: bool = False
```

In `limiter.py`'s existing-branch, after the reset/roll calls (2304-2308), record the flag: capture `before = state.grant_count`, call the two `_apply_*`, then `granted = limit.is_quota and state.grant_count is not None and (reset_applied or roll_applied)` using the two booleans the functions already return; pass `_granted=granted` into `LeaseEntry(...)`.

`repository.py` `build_composite_normal`: rename parameter `seed_shard_count` → `pin_shard_count` (update the protocol and the one caller in `lease.py`), add `grant_counts: dict[str, int] | None = None`, and before the condition-part assembly:

```python
        # ADR-145 I3: a reset or roll on this write re-grants its quota at the
        # item's count. Positional tokens (`#gc{i}`), disjoint from the rest.
        for i, (name, count) in enumerate(sorted((grant_counts or {}).items())):
            attr_names[f"#gc{i}"] = schema.bucket_attr(name, schema.BUCKET_FIELD_GC)
            set_parts.append(f"#gc{i} = :gc{i}")
            attr_values[f":gc{i}"] = {"N": str(count)}
```

`lease.py` `_commit_initial` normal branch: compute

```python
                grant_counts = {
                    e.limit.name: e.state.grant_count
                    for e in group_entries
                    if e._granted and not e._seed and e.state.grant_count is not None
                }
                pin = [e.state.shard_count for e in group_entries if e._seed and e.limit.is_quota]
                pin += list(grant_counts.values())
```

and pass `grant_counts=grant_counts, pin_shard_count=max(pin, default=None)` (replacing the old `seed_shard_count=` argument). A lost pin already falls to the consumption-only retry, which grants nothing — design R5.

- [ ] **Step 4: Regenerate, run**

Run: `uv run python scripts/generate_sync.py && uv run pytest tests/unit/test_limiter.py tests/unit/test_sync_limiter.py tests/unit/test_expression_tokens.py tests/unit/test_repository.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A src tests
git commit -m "✨ feat(limiter): stamp the quota grant count on reset and roll

Refs #637, Refs #642. ADR-145 I3/I4: a reset or window roll re-grants a
quota shard at the item's shard count and writes gc on the same rf-locked
write, pinned on shard_count so a reset that read a lagging count fails
and retries instead of covering a slot someone else was just granted."
```

---

### Task 6: Repository — plan a quota shard and build the donor debit

**Files:**
- Modify: `src/zae_limiter/repository.py` (replace `reclaim_quota_surplus` 6880-6941, `_clamp_quota_shard` 6960-6988, `reclaim_quota_seed` 6990-7105, `persist_seed` 3131-3240; keep `_entity_bucket_items`)
- Modify: `src/zae_limiter/repository_protocol.py` (824-879, 686-701)
- Test: `tests/unit/test_quota_shard_creation.py` (replace `TestReclaimQuotaSurplus` 237-323), `tests/unit/test_expression_tokens.py`

**Interfaces:**
- Consumes: `plan_quota_grant`, `quota_grant_is_current`, `QuotaSibling`, `QuotaGrant` (Task 4); `BUCKET_FIELD_GC` (Task 2); `_propagate_shard_count` (existing, 3792).
- Produces:

```python
@dataclass(frozen=True)
class QuotaDonorDebit:            # in models.py, beside QuotaGrant
    shard_id: int
    limit_name: str
    tokens_milli: int
    grant_count: int
    guard_rf_ms: int | None       # calendar: donor rf must stay >= this edge
    guard_wa_ms: int | None       # session: donor wa must equal this

async def plan_quota_shard(
    self, entity_id: str, resource: str, limits: Sequence[Limit],
    shard_id: int, shard_count: int, now_ms: int,
) -> tuple[int, dict[str, QuotaGrant], list[QuotaDonorDebit]]
    # -> (count used, grant per quota name, debits to append to the commit)

def build_quota_donor_debits(
    self, entity_id: str, resource: str, debits: Sequence[QuotaDonorDebit],
) -> list[dict[str, Any]]     # one {"Update": {...}} per donor shard
```

- [ ] **Step 1: Failing tests** (`tests/unit/test_quota_shard_creation.py`, replacing `TestReclaimQuotaSurplus`)

```python
class TestPlanQuotaShard:
    """Repository.plan_quota_shard (ADR-145)."""

    QUOTA = Limit.quota("rpd", 1000, cron="0 0 * * *")

    async def test_no_shards_is_a_fresh_grant(self, limiter):
        count, grants, debits = await limiter._repository.plan_quota_shard(
            "e1",
            RESOURCE,
            [self.QUOTA],
            shard_id=1,
            shard_count=2,
            now_ms=freeze(limiter._repository),
        )
        assert grants["rpd"].donor_shard is None and debits == []

    async def test_split_plans_a_move_from_the_parent(self, limiter):
        now = freeze(limiter._repository)
        await seed_shard0(limiter, "e1", self.QUOTA, now, RESOURCE)  # 1000 held, count 1
        count, grants, debits = await limiter._repository.plan_quota_shard(
            "e1", RESOURCE, [self.QUOTA], shard_id=1, shard_count=2, now_ms=now
        )
        assert grants["rpd"].tokens_milli == 500_000
        assert debits == [
            QuotaDonorDebit(0, "rpd", 500_000, 1, guard_rf_ms=ANY_OR_NONE, guard_wa_ms=None)
        ]

    async def test_lagging_sibling_count_is_propagated_before_a_fresh_grant(self, limiter):
        # R5: shard 1 still says count 2 while the caller is at 4.
        ...  # write shard 0 (gc 4, count 4) and shard 1 (gc 2, count 2, current); plan shard 2
        # assert shard 1's stored shard_count is 4 afterwards

    async def test_legacy_donor_without_gc_can_donate(self, limiter):
        # Review focus 4: a v0.14 item (no gc) reads as gc = shard_count.
        ...  # _write_shard(... tokens 1000, no gc, shard_count 1); plan shard 1 at 2;
        # run the debit through transact_write and assert shard 0 holds 500

    def test_two_quotas_same_donor_merge_into_one_update(self, limiter):
        # Review focus 3.
        items = limiter._repository.build_quota_donor_debits(
            "e1",
            RESOURCE,
            [
                QuotaDonorDebit(0, "rpd", 500_000, 1, None, None),
                QuotaDonorDebit(0, "rpm2", 50_000, 1, None, None),
            ],
        )
        assert len(items) == 1
        expr = items[0]["Update"]["UpdateExpression"]
        assert expr.count("#qt") == 2
```

Fill each `...` with concrete item writes using the file's `_write_shard` helper extended by a `grant_count: int | None = None` keyword (write `b_{name}_gc` when given) and a `shard_count` keyword. Use `unittest.mock.ANY` for `guard_rf_ms` in the split test. No step may be left as `...` in the committed test.

`tests/unit/test_expression_tokens.py`:

```python
def test_quota_donor_debits(self):
    for item in _repo().build_quota_donor_debits(
        "e",
        "r",
        [
            QuotaDonorDebit(0, DOTTED, 1, 1, 5, None),
            QuotaDonorDebit(0, HYPHENATED, 1, 2, None, 7),
        ],
    ):
        assert_expression_safe(item["Update"])
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_quota_shard_creation.py -k PlanQuotaShard tests/unit/test_expression_tokens.py -k donor -q`
Expected: FAIL.

- [ ] **Step 3: Implement `plan_quota_shard`**

```python
async def plan_quota_shard(
    self,
    entity_id: str,
    resource: str,
    limits: Sequence[Limit],
    shard_id: int,
    shard_count: int,
    now_ms: int,
) -> tuple[int, dict[str, QuotaGrant], list[QuotaDonorDebit]]:
    """Plan the grant of every quota in ``limits`` for shard ``shard_id`` (ADR-145).

    One GSI3 KEYS_ONLY query + one BatchGetItem, as the #587 reclaim cost.
    The count is the largest of the caller's and every sibling's stored
    count. Before a fresh grant, a sibling still below that count has it
    raised (R5), a write only when a lag is seen. Writes nothing else: the
    returned debits ride in the acquire's own transaction (I5).
    """
    quotas = [limit for limit in limits if limit.is_quota]
    if not quotas or shard_count <= 1:
        return shard_count, {}, []
    items = await self._entity_bucket_items(entity_id, resource)
    stored = {
        schema.parse_bucket_pk(item["PK"]["S"])[3]: item
        for item in items
        if schema.parse_bucket_pk(item["PK"]["S"])[3] != shard_id
    }
    count = max(
        [shard_count] + [int(i.get("shard_count", {}).get("N", "1")) for i in stored.values()]
    )
    grants: dict[str, QuotaGrant] = {}
    debits: list[QuotaDonorDebit] = []
    for limit in quotas:
        share = (
            effective_params(
                limit.capacity * 1000, 0, limit.refill_period_seconds * 1000, limit.schedule, now_ms
            )[0]
            // count
        )
        siblings = [
            s
            for s in (self._quota_sibling(sid, item, limit, now_ms) for sid, item in stored.items())
            if s is not None
        ]
        grant = plan_quota_grant(siblings, shard_id, count, share)
        grants[limit.name] = grant
        if grant.donor_shard is not None and grant.tokens_milli > 0:
            donor = stored[grant.donor_shard]
            debits.append(self._donor_debit(grant, limit, donor, now_ms))
    if any(g.donor_shard is None for g in grants.values()):
        lagging = [int(i.get("shard_count", {}).get("N", "1")) for i in stored.values()]
        if lagging and min(lagging) < count:
            await self._propagate_shard_count(entity_id, resource, count, count)
    return count, grants, debits
```

`_propagate_shard_count(entity_id, resource, old_count, new_count)` updates shards `1..old_count-1` under `shard_count < :new`; passing `old_count=count` covers every existing shard below `count`. Verify shard 0 is included in that range — if it starts at 1, also call `bump_shard_count`'s shard-0 update path, or extend `_propagate_shard_count` with `start: int = 1` and pass `start=0`.

Helpers:

```python
@staticmethod
def _quota_sibling(
    shard_id: int, item: dict[str, Any], limit: Limit, now_ms: int
) -> QuotaSibling | None:
    tk = item.get(schema.bucket_attr(limit.name, schema.BUCKET_FIELD_TK), {}).get("N")
    if tk is None:
        return None
    count = int(item.get("shard_count", {}).get("N", "1"))
    gc = item.get(schema.bucket_attr(limit.name, schema.BUCKET_FIELD_GC), {}).get("N")
    ws = item.get(schema.bucket_attr(limit.name, schema.BUCKET_FIELD_WS), {}).get("N")
    wa = item.get(schema.bucket_attr(limit.name, schema.BUCKET_FIELD_WA), {}).get("N")
    rf = int(item.get("rf", {}).get("N", "0"))
    return QuotaSibling(
        shard_id=shard_id,
        tokens_milli=int(tk),
        grant_count=int(gc) if gc is not None else count,
        current=quota_grant_is_current(
            limit, rf, int(ws) if ws else None, int(wa) if wa else None, now_ms
        ),
    )


@staticmethod
def _donor_debit(
    grant: QuotaGrant, limit: Limit, donor: dict[str, Any], now_ms: int
) -> QuotaDonorDebit:
    wa = donor.get(schema.bucket_attr(limit.name, schema.BUCKET_FIELD_WA), {}).get("N")
    edge = None if limit.reset_after is not None else prev_reset_edge(limit.reset_schedule, now_ms)
    return QuotaDonorDebit(
        shard_id=grant.donor_shard,
        limit_name=limit.name,
        tokens_milli=grant.tokens_milli,
        grant_count=grant.donor_grant_count,
        guard_rf_ms=edge,
        guard_wa_ms=int(wa) if (limit.reset_after is not None and wa) else None,
    )
```

- [ ] **Step 4: Implement `build_quota_donor_debits`**

One `Update` per donor shard; per debit `i` in that shard:

```python
def build_quota_donor_debits(
    self, entity_id: str, resource: str, debits: Sequence[QuotaDonorDebit]
) -> list[dict[str, Any]]:
    """The donor side of each ADR-145 move, one ``Update`` per donor shard.

    ``ADD tk -x`` under: the donor still exists; still holds ``x``; its
    grant count is the one read (or absent and equal to its shard count —
    a v0.14 item, design §9); and its grant is still the current period
    (calendar ``rf >= edge``; session ``wa`` unchanged). Several quotas
    moving off one donor share one ``Update``: a transaction may touch an
    item once.
    """
    by_shard: dict[int, list[QuotaDonorDebit]] = {}
    for debit in debits:
        by_shard.setdefault(debit.shard_id, []).append(debit)
    items: list[dict[str, Any]] = []
    for shard, group in sorted(by_shard.items()):
        names: dict[str, str] = {"#qsc": "shard_count", "#qrf": "rf"}
        values: dict[str, dict[str, str]] = {}
        adds: list[str] = []
        conds: list[str] = ["attribute_exists(PK)"]
        for i, d in enumerate(group):
            names[f"#qt{i}"] = schema.bucket_attr(d.limit_name, schema.BUCKET_FIELD_TK)
            names[f"#qg{i}"] = schema.bucket_attr(d.limit_name, schema.BUCKET_FIELD_GC)
            values[f":qx{i}"] = {"N": str(d.tokens_milli)}
            values[f":qn{i}"] = {"N": str(-d.tokens_milli)}
            values[f":qg{i}"] = {"N": str(d.grant_count)}
            adds.append(f"#qt{i} :qn{i}")
            conds.append(f"#qt{i} >= :qx{i}")
            conds.append(f"(#qg{i} = :qg{i} OR (attribute_not_exists(#qg{i}) AND #qsc = :qg{i}))")
            if d.guard_rf_ms is not None:
                values[f":qe{i}"] = {"N": str(d.guard_rf_ms)}
                conds.append(f"#qrf >= :qe{i}")
            if d.guard_wa_ms is not None:
                names[f"#qw{i}"] = schema.bucket_attr(d.limit_name, schema.BUCKET_FIELD_WA)
                values[f":qw{i}"] = {"N": str(d.guard_wa_ms)}
                conds.append(f"#qw{i} = :qw{i}")
        if not any(d.guard_rf_ms is not None for d in group):
            del names["#qrf"]
        items.append(
            {
                "Update": {
                    "TableName": self.table_name,
                    "Key": {
                        "PK": {
                            "S": schema.pk_bucket(self._namespace_id, entity_id, resource, shard)
                        },
                        "SK": {"S": schema.sk_state()},
                    },
                    "UpdateExpression": "ADD " + ", ".join(adds),
                    "ConditionExpression": " AND ".join(conds),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": values,
                }
            }
        )
    return items
```

Drop `#qsc` from `names` too if you restructure so it is always used (it is: every debit's gc condition references it). Match the attribute used for the namespace id to what `build_composite_create` uses (`self._namespace_id` or equivalent — check).

- [ ] **Step 5: Remove the old reclaim API**

Delete `reclaim_quota_surplus`, `_clamp_quota_shard`, `reclaim_quota_seed`, `persist_seed` from `repository.py` and their declarations from `repository_protocol.py`; add `plan_quota_shard` and `build_quota_donor_debits` to the protocol. The callers in `limiter.py`/`lease.py` break here and are rewired in Task 7 — **do Task 6 and Task 7 on one branch and do not push between them**; commit Task 6 with the limiter still referencing the old names only if the suite passes, otherwise fold Steps 5 of this task into Task 7's commit and note it in the Task 6 commit body.

- [ ] **Step 6: Regenerate, run**

Run: `uv run python scripts/generate_sync.py && uv run pytest tests/unit/test_quota_shard_creation.py tests/unit/test_expression_tokens.py -q && uv run mypy`
Expected: the new tests PASS (if Step 5 was deferred, the full suite still passes).

- [ ] **Step 7: Commit**

```bash
git add -A src tests
git commit -m "✨ feat(repository): plan quota shard grants and build donor debits

Refs #637, Refs #642. ADR-145: plan_quota_shard reads the siblings once
and returns, per quota, a move off the covering sibling or a fresh grant;
build_quota_donor_debits builds the donor side, conditioned on the donor
still holding the tokens, still at the grant count read, and still in
the current period."
```

---

### Task 7: Wire the grant into acquire (create, seed, reject, lost lock)

**Files:**
- Modify: `src/zae_limiter/limiter.py` (`_do_acquire` 2065-2376; remove `_quota_transfer` 2416-2480 and `_quota_seed_transfer` 2525-2566; `acquire` ~700-800)
- Modify: `src/zae_limiter/lease.py` (`LeaseEntry`; `_commit_initial` 395-837; remove `persist_transfer_seeds` 128-147)
- Modify: `src/zae_limiter/models.py` (`from_limit`: replace `reclaimed_milli` with `starting_tokens_milli: int | None = None`; delete `new_shard_starting_tokens_milli`)
- Test: `tests/unit/test_quota_shard_creation.py`, `tests/unit/test_window_shard_creation.py`, `tests/unit/test_limiter.py` (`TestLimitAddedToExistingShards` 11759-12172, `TestPersistSeed` 12174-12227), `tests/fixtures/sharding.py`

**Interfaces:**
- Consumes: `plan_quota_shard`, `build_quota_donor_debits`, `QuotaDonorDebit`, `QuotaGrant` (Task 6); `_granted` (Task 5).
- Produces: `LeaseEntry._donor_debit: QuotaDonorDebit | None = None`; internal `class QuotaMoveLost(Exception)` in `lease.py` (not exported); `tests.fixtures.sharding.walk_doublings_no_spend(limiter, entity_id, limit_name, generations=5, resource=RESOURCE) -> int` (final `shard_count`).

- [ ] **Step 1: Failing acceptance tests**

`tests/fixtures/sharding.py`, add:

```python
async def walk_doublings_no_spend(limiter, entity_id, limit_name, generations=5, resource=RESOURCE):
    """Drive `wcu` doublings 1 -> 2**generations without spending the quota (#637).

    Each generation drains `wcu` on a shard with zero-cost acquires of another
    limit, so only `wcu` trips; then materialises every shard so each exists.
    Returns the final shard count.
    """
```

Implement it with the existing `drain_wcu` + `materialise` helpers, acquiring `{limit_name: 0}` (a declared zero consumption) so the quota is never debited.

`tests/unit/test_quota_shard_creation.py`:

```python
class TestAcceptance:
    """Design §10. C = 1000, clock frozen in one period."""

    QUOTA = Limit.quota("rpd", 1000, cron="0 0 * * *")

    async def test_1_nobody_spends_walk_keeps_the_whole_quota(self, limiter):
        freeze(limiter._repository)
        await limiter.set_limits("e1", [self.QUOTA], resource=RESOURCE)
        count = await walk_doublings_no_spend(limiter, "e1", "rpd")
        assert count == 32
        assert await spendable(limiter._repository, "e1", "rpd", count, RESOURCE) == 1000

    async def test_4_587_walk_with_spending_never_exceeds(self, limiter):
        freeze(limiter._repository)
        await limiter.set_limits("e1", [self.QUOTA], resource=RESOURCE)
        admitted, count, spends = await walk_doublings(limiter, "e1", "rpd")
        assert admitted + await spendable(limiter._repository, "e1", "rpd", count, RESOURCE) <= 1000

    async def test_7_a_split_conserves_the_total(self, limiter):
        ...  # seed shard 0 with 1000, create shard 1 via acquire at count 2,
        # assert sum(shard_balances) == 1000 - consumed

    async def test_3_idle_across_the_reset_then_seed(
        self, limiter
    ): ...  # port scratchpad rv633d/test_total.py (the 1300 repro): assert total admitted <= 1000

    async def test_rejected_acquire_keeps_the_move(self, limiter):
        # A move is never lost: the create is committed with nothing consumed.
        ...  # shard 0 holds 1000 at count 1; bump to 2; acquire 600 on shard 1
        # (moved 500 < 600 -> RateLimitExceeded); assert shard 1 exists holding 500,
        # shard 0 holds 500, total 1000

    async def test_conflicted_move_retries_then_succeeds(self, limiter, monkeypatch):
        # Review focus 1: first transact_write raises TransactionCanceled/TransactionConflict.
        ...

    async def test_lost_move_is_replanned_once(self, limiter, monkeypatch):
        # Donor spent between plan and commit -> condition fails -> re-plan -> admitted from fresh read.
        ...

    async def test_cascade_child_and_parent_moves_share_one_transaction(self, limiter):
        # Review focus 2.
        ...
```

Port the three scratchpad repros (`rv633c/test_clone.py` 1250 — that one belongs to Task 8; `rv633d/test_total.py` 1300; `rv633c/test_race.py` R3/1000) into real tests here; their paths are in the design session's scratchpad (`/private/tmp/claude-502/.../scratchpad/`). If the scratchpad is gone, rebuild each from its description in design §1 and §8. Every `...` must be replaced by a complete test before committing.

`tests/unit/test_window_shard_creation.py`: add `test_10_session_quota_walk_keeps_the_whole_quota` (Test 1 with `SESSION_10` scaled to 1000 → use `Limit.quota("session", 1000, reset_after=timedelta(hours=5))`) and the session variant of Test 3 (window ended between shards → fresh grant, not a move).

Update `TestLimitAddedToExistingShards` / delete `TestPersistSeed` in `test_limiter.py`: tests asserting the old clamp-all behaviour (`transfer…clamps every sibling`, `persist…`) are rewritten to assert the ADR-145 behaviour (one donor debited by exactly the new share; others untouched). Keep every test that asserts a **bound** (`<= 1000`); those must still pass.

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_quota_shard_creation.py -k Acceptance -q`
Expected: FAIL (`test_1` reports 187-ish; others fail on the old API).

- [ ] **Step 3: `from_limit`**

Replace the `reclaimed_milli` parameter with `starting_tokens_milli: int | None = None`; the token line becomes:

```python
        state.tokens_milli = (
            state.reset_target_milli(now_ms) if starting_tokens_milli is None else starting_tokens_milli
        )
        if limit.is_quota:
            state.grant_count = shard_count
```

Delete `new_shard_starting_tokens_milli` and its tests (`TestNewShardStartingTokens`, `TestFromLimitStartingBalance` → rewrite the latter for `starting_tokens_milli`).

- [ ] **Step 4: `_do_acquire`**

Replace the `_quota_seed_transfer(...)` and `_quota_transfer(...)` calls (2097-2110) with one call, made only when something is being created or seeded on a sharded entity:

```python
quota_needing = (
    [lim for lim in missing if lim.is_quota]
    if any_existing
    else [lim for lim in entity_limits[eid] if lim.is_quota]
)
grant_count, grants, donor_debits = (
    await self._repository.plan_quota_shard(
        eid,
        resource,
        quota_needing,
        eid_shard,
        seed_shard_count if any_existing else eid_shard_count,
        now_ms,
    )
    if quota_needing and max(seed_shard_count, eid_shard_count) > 1
    else (max(seed_shard_count, eid_shard_count), {}, [])
)
debit_for = {d.limit_name: d for d in donor_debits}
```

In the seed branch and the create branch, replace `reclaimed_milli=…` with:

```python
shard_count = (grant_count,)
starting_tokens_milli = (
    (
        None
        if window_live is False
        else grants[limit.name].tokens_milli
        if limit.name in grants
        else None
    ),
)
```

and, when `window_live is not False and limit.name in debit_for`, set `donor = debit_for[limit.name]`; pass `_donor_debit=donor, _granted=limit.is_quota` into the `LeaseEntry`. When `window_live is False` (the joined window ended) the grant is fresh and no debit rides (design §5). Delete the `period_start`/`seed_initial` persist gate (2164-2176) — the rejection path below replaces it — and remove `seed_initial` / `_seed_initial` everywhere.

Rejection (2372-2377): replace `persist_transfer_seeds` with

```python
if violations:
    if any(e._donor_debit is not None for e in entries):
        # ADR-145 I5: a move is never lost. Commit the lease with
        # nothing consumed — the rf-locked write also applies any
        # pending reset (the 1300 case) — then reject.
        for entry in entries:
            entry.consumed = 0
        await Lease(
            repository=self._repository, entries=entries, carriers=carriers
        )._commit_initial()
    raise RateLimitExceeded(statuses)
```

(Match `Lease(...)`'s real constructor arguments — copy them from the `return Lease(...)` right below.)

- [ ] **Step 5: `_commit_initial`**

After the per-group loop builds `items`, append the donor debits **after** every bucket item (so per-index cancellation reasons of bucket groups keep their positions):

```python
        donor_debits = [e._donor_debit for e in (*self.entries, *self._carriers) if e._donor_debit]
        donor_items: list[dict[str, Any]] = []
        if donor_debits:
            by_bucket: dict[tuple[str, str], list[QuotaDonorDebit]] = {}
            for e in self.entries:
                if e._donor_debit is not None:
                    by_bucket.setdefault((e.entity_id, e.resource), []).append(e._donor_debit)
            for (entity_id, resource), debits in by_bucket.items():
                donor_items += repo.build_quota_donor_debits(entity_id, resource, debits)
        items += donor_items
```

The TransactionConflict retry loop (678-705) is kept; add full jitter to its delay: `delay = random.uniform(0, _CONFLICT_BASE_DELAY_S * (2**attempt))` (import `random`).

On `condition_failed` **with** donor items present, do not run the consumption-only retry — the whole transaction rolled back, the donor is untouched, and the plan may be stale:

```python
        if condition_failed and donor_items:
            raise QuotaMoveLost from condition_exc
```

Delete the `persist_transfer_seeds` call (711) and the function (128-147).

- [ ] **Step 6: Re-plan once in `acquire`**

Where `acquire` calls `_do_acquire` then `lease._commit_initial()` (~780), wrap the pair:

```python
            for attempt in range(2):
                lease = await self._do_acquire(...)  # same arguments as today
                try:
                    await lease._commit_initial()
                    break
                except QuotaMoveLost:
                    if attempt == 1:
                        raise RateLimiterUnavailable(
                            "quota shard grant lost twice to concurrent writers"
                        ) from None
```

(`RateLimiterUnavailable` is then handled by `on_unavailable` like any backend error. Check its constructor signature in `exceptions.py`.) Apply the same wrap to `_try_parent_only_acquire`'s commit at 1932 if it can create a parent quota shard (it can: cascade parent shard creation) — re-plan by returning `None` so the caller falls back to the full slow path.

- [ ] **Step 7: Regenerate, run everything**

Run: `uv run python scripts/generate_sync.py && uv run pytest tests/unit/ -q && uv run mypy && uv run ruff check .`
Expected: PASS, including every pre-existing bound test.

- [ ] **Step 8: Commit** (two commits — the bugs are pre-existing on main, `.claude/rules/commits.md`)

```bash
git add -A src tests
git commit -m "🐛 fix(limiter): move a quota's surplus instead of discarding it

Refs #637. A quota shard created by a doubling now takes its share by an
atomic move off the sibling that covers it (ADR-145) instead of clamping
every sibling and keeping one share. A full, unspent quota walked 1->32
keeps all 1000 spendable (was 187). Replaces reclaim_quota_surplus,
reclaim_quota_seed and persist_seed; a rejected acquire that carried a
move commits it with nothing consumed."
```

If the #642 part (seed from a covering sibling, stale sibling never donates, re-plan on a lost move) can be staged separately with `git add -p`, commit it as `🐛 fix(limiter): never grant a quota share a sibling already holds` with `Refs #642`; if the two cannot be separated cleanly, keep one commit and name both issues in its body with `Refs`.

---

### Task 8: The aggregator

**Files:**
- Modify: `src/zae_limiter_aggregator/processor.py` (`try_refill_bucket` 853-1254; `propagate_shard_count` Path 2 1536-1648; delete `_reclaim_quota_surplus` 1417-1462)
- Test: `tests/unit/test_processor.py` (`TestQuotaShardCloneIsATransfer` 3342 → rewrite), `tests/unit/test_window_shard_creation.py` (`TestAggregatorCloneOfAnUnappliedShardZero` 439), `tests/unit/test_expression_tokens.py` (`TestAggregatorWrites` 317)

**Interfaces:**
- Consumes: `plan_quota_grant`, `QuotaSibling`, `quota_grant_is_current` (Task 4) via `from zae_limiter.models import …` (vendored); `LimitRefillInfo.grant_count` (Task 2).

- [ ] **Step 1: Failing tests**

`tests/unit/test_processor.py` — replace the greedy test with:

```python
class TestQuotaShardCloneIsAMove:
    """ADR-145: Path 2 funds each clone from its parent, atomically."""

    def test_2_clone_of_an_unseeded_shard_zero_never_exceeds(self, moto_table):
        # The 1250 repro: shard 1 seeded at count 2 and spent; shard 0 unseeded;
        # the aggregator doubles to 4. Total admitted over the period <= 1000.
        ...

    def test_clone_takes_its_share_from_its_parent_in_one_transaction(self, moto_table):
        # shard 0 holds 1000 at gc 1, count 1 -> 2: shard 1 is created with 500,
        # shard 0 left with 500, via one transact_write_items call.
        ...

    def test_clone_move_that_fails_its_condition_is_skipped(self, moto_table):
        # parent spent between the stream image and the write -> clone not created;
        # the client creates it lazily later (no exception escapes).
        ...
```

Use a real moto table (the `TestAggregatorCloneOfAnUnappliedShardZero` class in `test_window_shard_creation.py` shows the pattern: boto3 `Table` on moto + `propagate_shard_count`). Replace every `...` with a complete test.

Refill tests:

```python
    def test_calendar_reset_writes_gc_and_pins_the_count(self):
        # try_refill_bucket on a quota whose edge passed: UpdateExpression carries
        # `#gq0 = :gq0` with the item's shard_count and the condition
        # `(attribute_not_exists(shard_count) OR shard_count <= :gqpin)`.

    def test_quota_clamp_uses_the_grant_count(self):
        # quota shard at count 4 holding 500 with gc 2: no negative delta is written.
```

- [ ] **Step 2: Run to see them fail**

Run: `uv run pytest tests/unit/test_processor.py -k "IsAMove or writes_gc or grant_count" -q`
Expected: FAIL.

- [ ] **Step 3: `try_refill_bucket`**

- Quota ceiling: where `effective_cp = scaled_cp // state.shard_count` (974) feeds the drip/clamp (`refill_bucket(capacity_milli=effective_cp)`, 1068-1103), use `scaled_cp // (info.grant_count or state.shard_count)` **for a quota limit only** (`info.reset_sched or info.reset_after_seconds is not None`); keep `effective_cp` for the reset/roll target.
- Reset branch (1046-1057) and roll branch (1003-1027): for each limit reset or rolled, also `SET #gq{i} = :gq{i}` (`b_{name}_gc` = `state.shard_count`), and add once to the condition `(attribute_not_exists(#gqsc) OR #gqsc <= :gqpin)` with `#gqsc` → `shard_count`, `:gqpin` = `state.shard_count`.

- [ ] **Step 4: Path 2**

Replace the quota pool (`_reclaim_quota_surplus` call + greedy loop, 1597-1611) with, per target shard, a move from its parent `parent = target_shard % old_count`:

```python
client = table.meta.client
serializer = TypeSerializer()
for target_shard in range(old_count, new_count):
    item = ...  # built exactly as today, starting tokens for dripping limits unchanged
    donor_updates = []
    for limit_name, share in quota_shares.items():
        parent_item = parent_items.get(target_shard % old_count)
        sibling = (
            _quota_sibling_from_image(parent_item, limit_name, now_ms) if parent_item else None
        )
        grant = plan_quota_grant([sibling] if sibling else [], target_shard, new_count, share)
        item[bucket_attr(limit_name, BUCKET_FIELD_TK)] = grant.tokens_milli
        item[bucket_attr(limit_name, BUCKET_FIELD_GC)] = new_count
        if grant.donor_shard is not None and grant.tokens_milli > 0:
            donor_updates.append((grant, limit_name))
    transact = [
        {
            "Put": {
                "TableName": table.name,
                "Item": {k: serializer.serialize(v) for k, v in item.items()},
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        }
    ] + _donor_update_items(table.name, namespace_id, entity_id, resource, donor_updates)
    try:
        if len(transact) == 1:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        else:
            client.transact_write_items(TransactItems=transact)
        updated += 1
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("ConditionalCheckFailedException", "TransactionCanceledException"):
            continue  # client created it, or the parent moved: the client creates it lazily
        raise
```

`parent_items`: read the old shards once per doubling with `table.get_item(..., ConsistentRead=True)` for each `0..old_count-1` that is some target's parent (at most `old_count` reads — replaces the `old_count` reclaim writes). `_quota_sibling_from_image` mirrors `Repository._quota_sibling` on native values (the Table resource returns native Python types); `_donor_update_items` mirrors `Repository.build_quota_donor_debits` in low-level form (values `{"N": str(...)}`) with the same condition and positional tokens. `quota_grant_is_current` needs the `Limit` — build it from the image's `cp`/`rsched`/`rsa` via the existing parsing (`_extract_limit_attrs`, `_decode_schedule`) as `Limit.quota(name, cp // 1000, cron=…)` / `reset_after=…`, or add a small `quota_period_is_current(reset_sched, reset_after_seconds, rf, ws, wa, now)` beside `quota_grant_is_current` in `models.py` that takes the raw pieces and have `quota_grant_is_current` delegate to it — prefer the latter (one rule, no fake `Limit`). Delete `_reclaim_quota_surplus`.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/unit/test_processor.py tests/unit/test_window_shard_creation.py tests/unit/test_expression_tokens.py tests/unit/test_lambda_builder.py -q && uv run mypy`
Expected: PASS (the lambda builder test proves the vendored import closure still holds).

- [ ] **Step 6: Commit**

```bash
git add -A src tests
git commit -m "🐛 fix(aggregator): fund a quota clone from its parent, atomically

Refs #642. Path 2 no longer clamps every old shard and hands the pool out
greedily: each clone takes its share off its parent (target % old_count)
in one transaction with the Put (ADR-145). A clone of an unseeded shard 0
no longer lets a spent sibling's share be granted twice (was 1250/1000).
Resets and rolls now stamp gc, pinned on shard_count."
```

---

### Task 9: Guards and documentation

**Files:**
- Create: `tests/unit/test_quota_conservation_fuzz.py`, `tests/unit/test_bucket_writer_registry.py`
- Modify: `tests/benchmark/test_capacity.py`
- Modify: CLAUDE.md, `.claude/rules/write-on-enter.md`, `.claude/rules/code-review.md`, `docs/guide/session-quotas.md` (335-339), `docs/adr/140-duration-window-shard-coherence.md` (22, 48, 97), `docs/adr/141-reset-after-version-gate.md` (14), `docs/adr/142-hide-reset-after-config.md` (25-26)

**Interfaces:** Consumes everything above. Produces no API.

- [ ] **Step 1: Model-based fuzz test** (`tests/unit/test_quota_conservation_fuzz.py`)

```python
"""I8 against the real repository on moto (ADR-145). Seeded, so reproducible."""

import random

import pytest

from tests.fixtures.quota_model import QuotaModel

SEEDS = range(20)


@pytest.mark.parametrize("seed", SEEDS)
async def test_admitted_never_exceeds_and_nothing_is_lost(limiter, seed):
    rng = random.Random(seed)
    ...  # drive `limiter` with the same random op sequence as QuotaModel(rule="NEW"):
    # spend -> acquire on a pinned shard; double -> bump_shard_count; late seed ->
    # set_limits adding the quota after shards exist; next_period -> advance the frozen
    # clock past midnight. After every op assert:
    #   admitted_this_period <= 1000, and
    #   admitted_this_period + spendable() + still_grantable(model) == model.accounted()
```

Keep the op count small (≤ 60 per seed) so the file runs in seconds under xdist. Replace `...` with the full driver before committing.

- [ ] **Step 2: Writer registry** (`tests/unit/test_bucket_writer_registry.py`)

```python
"""Every bucket write builder declares whether it writes a quota's tk or gc (ADR-145 I1/I3)."""

import inspect

from zae_limiter.repository import Repository

# builder name -> (writes quota tk, writes gc)
REGISTRY = {
    "build_composite_create": (True, True),
    "build_composite_normal": (True, True),
    "build_composite_retry": (True, False),
    "build_composite_adjust": (True, False),
    "build_quota_donor_debits": (True, False),
}


def test_every_bucket_write_builder_is_registered():
    builders = {
        name
        for name, _ in inspect.getmembers(Repository, inspect.isfunction)
        if name.startswith("build_")
    }
    assert builders == set(REGISTRY), (
        "A new bucket write builder must be added to REGISTRY with whether it "
        "writes a quota's tk or gc, and to test_expression_tokens.py (ADR-145)."
    )
```

- [ ] **Step 3: Capacity tests** (`tests/benchmark/test_capacity.py`)

Add to the quota classes: the speculative success path on a quota still costs `update_item == 1`, no reads; a shard creation with a donor costs `query == 1`, `batch_get_item == 1` (the sibling read) plus one `transact_write_items`, and no `update_item` clamps. Use the existing `capacity_counter` pattern (`with capacity_counter.counting(), pinned_shard(1):`).

- [ ] **Step 4: Run all guards**

Run: `uv run pytest tests/unit/test_quota_conservation_fuzz.py tests/unit/test_bucket_writer_registry.py -q && uv run pytest tests/benchmark/test_capacity.py -o addopts= -q -k quota`
Expected: PASS.

- [ ] **Step 5: Documentation** (invoke the `docs-updater` agent with this list, then review its diff)

- CLAUDE.md: rewrite the #587 paragraph (814-815), the Path 2 greedy bullet (826), the #633 seed bullet's reclaim/persist/residual text (1429), the writer-table rows for seed, persist (delete), create, reclaim (replace with "Quota donor debit, per donor shard (ADR-145)": `ADD tk -x` under `attribute_exists(PK) AND tk >= :x AND (gc = :gc OR (attribute_not_exists(gc) AND shard_count = :gc))` + period guard; "Touches rf? No") and the normal-path row (`+ b_{n}_gc = :gc` per reset/roll, pinned); add `#gc{i}`/`:gc{i}`, `#q*{i}`, `#gq{i}` to the token-schemes paragraph; add a short **Quota grant invariants (ADR-145)** list (I1–I8) under "Important Invariants"; update the pricing line for a quota shard creation (reads unchanged; a move is a 2-item transaction, 4 WCU).
- `.claude/rules/write-on-enter.md` Key Invariant 1: the two pre-rejection writes become one — "the ADR-145 move, committed with nothing consumed".
- `.claude/rules/code-review.md`: add a section "Quota grants (changes touching a quota's `tk` or `gc`)": must pass `test_quota_conservation_fuzz.py`, the acceptance tests and the writer registry; run the `design-validator` agent for any change to grant logic.
- `docs/guide/session-quotas.md` 335-339: remove the #642 exception bullet; state that a doubling neither creates nor destroys allowance.
- ADR-140/141/142 (Proposed, editable): replace mentions of "#633's transfer, with #642's residual" / the reclaim with a reference to ADR-145.

- [ ] **Step 6: Commit**

```bash
git add -A tests docs CLAUDE.md .claude/rules
git commit -m "✅ test(limiter): guard the quota grant invariants and document ADR-145

Refs #637, Refs #642. A seeded model-based fuzz test asserts I8 against
the real repository, a writer registry makes every new bucket write
declare whether it touches a quota's tk or gc, and capacity tests pin the
fast path and the creation cost. CLAUDE.md, the write-on-enter and
code-review rules, the session-quotas guide and ADR-140..142 now describe
the move instead of the reclaim."
```

---

## After the last task

- Full run: `uv run pytest tests/unit/ -q`, `uv run pytest tests/unit/ -m gevent -n 0 -q`, `uv run mypy`, `uv run ruff check .`, `pre-commit run --all-files`.
- Integration on LocalStack (`zae-limiter local up`): `uv run pytest tests/integration/test_bucket_sharding.py -q` (`TestQuotaShardCreationIsATransfer` at 1009 must be rewritten to the move semantics in Task 7 if it asserts the clamp).
- Fresh-context code review (user rule), then `/pr` for the implementation branch. Only that PR's body carries GitHub closing keywords, for the two bug issues this plan fixes — never for the epic #597.
