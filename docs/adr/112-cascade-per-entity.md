# ADR-112: Move Cascade from Per-Call to Per-Entity Configuration

**Status:** Accepted
**Date:** 2026-01-25

## Context

Cascade mode controls whether `acquire()` consumes tokens from both a child entity and its parent. Currently cascade is a per-call boolean parameter on `acquire(cascade=True)`, requiring callers to remember to pass it on every call. This is error-prone: forgetting `cascade=True` silently skips parent enforcement, and different call sites for the same entity may pass inconsistent values.

Cascade is a property of the entity's relationship to its parent — it describes *how* the child participates in the hierarchy, not a per-request decision. Storing it on the entity makes the behavior consistent and eliminates a class of caller errors.

The library is pre-1.0, so breaking parameter removal is acceptable without a deprecation period.

## Decision

The `cascade` parameter must be removed from `acquire()` and stored as a `cascade: bool = False` attribute on the child entity's `#META` record. `create_entity()` must accept `cascade` as a parameter. `_do_acquire()` must read `entity.cascade` to determine whether to include the parent in the transaction.

`cascade` is a DynamoDB reserved word and must use `ExpressionAttributeNames` aliases in all expressions, consistent with the existing handling of `name`, `resource`, `action`, and `timestamp` (ADR-111).

Existing entities without the `cascade` attribute must deserialize with `cascade=False` (backward compatible default).

## Consequences

**Positive:**
- Cascade behavior is consistent for all calls to the same entity
- Callers cannot accidentally omit cascade
- Entity metadata is self-describing — inspecting the entity reveals its cascade behavior
- No new DynamoDB reads: `acquire()` already fetches the entity's `#META` record

**Negative:**
- Breaking change: all callers passing `cascade=True` to `acquire()` must move it to `create_entity()`
- Changing cascade for an existing entity requires entity recreation or a new `update_entity()` API

## Alternatives Considered

### Keep cascade as per-call parameter
Rejected because: Error-prone, inconsistent across call sites, and conflates a static entity property with a runtime decision.

### Store as enum (`none|parent|all_ancestors`)
Rejected because: The codebase only supports 2-level hierarchy. A boolean is sufficient; a future ADR can supersede if deeper cascading is needed.

### Store on parent entity instead of child
Rejected because: The child decides whether to cascade up. Storing on the parent would require the parent to know about all children's behavior, adding coordination overhead.
