# DynamoDB Schema Patterns

## Flat Schema Mandate (ADR-111)

All DynamoDB records use flat schema (top-level attributes). The nested `data.M` wrapper was removed in v0.6.0.

- Deserialization reads flat format only (no backward-compatible nested fallback)
- Serialization always writes flat format
- See [ADR-111](docs/adr/111-flatten-all-records.md) and issue #180

## Reserved Words

Flat attribute names `name`, `resource`, `action`, and `timestamp` are DynamoDB reserved words. All expressions referencing these must use `ExpressionAttributeNames` aliases.

```python
# Correct - use aliases
ExpressionAttributeNames={"#resource": "resource", "#action": "action"}

# Wrong - will fail with ValidationException
UpdateExpression="SET resource = :val"
```

## Anti-Patterns

### Overlapping SET + ADD Paths (Issue #168)

DynamoDB throws `ValidationException: Two document paths overlap` when a single `UpdateExpression` uses SET on a map path and ADD on a sub-path within that map.

```python
# WRONG - overlapping paths error
UpdateExpression="""
    SET #data = if_not_exists(#data, :initial_data)
    ADD #data.counter :delta, #data.total_events :one
"""
```

```python
# CORRECT - flat schema avoids overlapping paths
UpdateExpression="""
    SET entity_id = :entity_id,
        #resource = if_not_exists(#resource, :resource)
    ADD #limit_name :delta, #total_events :one
"""
```

### Missing if_not_exists Guards

When using ADD on an attribute that may not exist yet (first write), guard initialization with `if_not_exists` in a SET clause.

```python
# WRONG - ADD fails if attribute doesn't exist on first write
UpdateExpression="ADD counter :delta"

# CORRECT - SET initializes, ADD increments atomically
UpdateExpression="""
    SET entity_id = if_not_exists(entity_id, :entity_id)
    ADD counter :delta
"""
```

### Deriving Consumption from Token Deltas (Issue #179)

Do not derive consumption as `old_tokens - new_tokens`. With high refill rates (e.g., 10M TPM), refill during operation latency exceeds consumption, producing negative deltas.

```python
# WRONG - negative when refill > consumption
tokens_delta = old_tokens - new_tokens  # -15,667 with 10M TPM + 100ms latency

# CORRECT - use explicit counter
total_consumed_milli += consumption  # Always accurate
```

Use the `design-validator` agent for any feature that derives data from state changes.

## When Implementing DynamoDB Operations

Before writing any `UpdateExpression`:

1. Are all attributes flat (no nested `#data.field` paths)?
2. Do SET and ADD target non-overlapping paths?
3. Are all first-write attributes guarded with `if_not_exists`?
4. Are reserved words aliased via `ExpressionAttributeNames`?
