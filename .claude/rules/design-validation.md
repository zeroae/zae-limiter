# Design Validation Rules

Guidelines for validating feature designs before implementation.

## Derived Data Validation

When a feature derives data from state changes (like calculating consumption from token deltas), validate the derivation formula against boundary conditions:

### Required Analysis

1. **Formula Constraints**: List all mathematical conditions that must hold for the formula to produce correct results

2. **Boundary Conditions**: Test the formula with:
   - Maximum realistic parameter values
   - Minimum realistic parameter values
   - Edge cases where parameters interact adversarially

3. **Real-World Scenarios**: Validate with production-like parameters, not just test values

### Example: Delta-Based Consumption Tracking

**Formula**: `consumption = old_value - new_value`

**Hidden Constraint**: This only works when `consumption > recovery_during_observation_window`

**Boundary Test**:
```
rate_limit = 10,000,000/minute (10M TPM)
recovery_rate = 166,667/second
observation_window = 100ms (network latency)
recovery_during_window = 16,667 tokens
consumption = 1,000 tokens

Result: old_value - new_value = 1000 - 16667 = -15667 (WRONG!)
```

**Conclusion**: Formula fails for high-rate-limit, low-consumption scenarios.

## Checklist for Stateful Feature Designs

Before implementing features that track or aggregate state changes:

- [ ] **Document the derivation formula** and its mathematical assumptions
- [ ] **List boundary conditions** where the formula might fail
- [ ] **Test with production-scale parameters** (not just test-friendly values)
- [ ] **Consider timing/latency effects** on state observation
- [ ] **Validate with adversarial scenarios** (high rates + low consumption + high latency)
- [ ] **Propose alternative approaches** if the primary approach has significant limitations

## When to Apply This Rule

Apply design validation when implementing:
- Metrics/analytics derived from state deltas
- Aggregation systems processing stream events
- Any feature where `new_state - old_state` is used to infer actions
- Rate limiting or quota tracking systems
- Billing/metering based on usage inference

## References

- Issue #179: Snapshot aggregator fails with high rate limits
- Root cause: Delta-based consumption tracking fails when refill > consumption
