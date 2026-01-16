# Design Validation

When implementing features that derive data from state changes (e.g., consumption from token deltas, metrics from stream events), use the `design-validator` agent before implementation.

## Quick Check

Ask yourself: **Does this feature calculate something from `old_state - new_state`?**

If yes, the derivation may fail when recovery/refill exceeds the change being measured.

## Example Failure and Fix (Issue #179)

The snapshot aggregator used `old_tokens - new_tokens` to derive consumption. With 10M TPM and 100ms latency:
- Refill during operation: 16,667 tokens
- Typical consumption: 1,000 tokens
- Result: `delta = -15,667` (wrong!)

**Fix:** Added a `total_consumed_milli` counter that tracks net consumption independently
of token bucket refill. The counter is stored as a flat top-level DynamoDB attribute
to enable atomic operations. Delta is now `new_counter - old_counter`.

## When to Invoke the Agent

- Adding metrics/analytics derived from state deltas
- Building aggregation systems for stream events
- Implementing billing/metering from usage inference
- Any `new - old` derivation in a system with recovery/refill
