# ADR-004: Declarative Infrastructure with StackOptions

**Status:** Accepted
**Date:** 2026-01-12
**PR:** [#69](https://github.com/zeroae/zae-limiter/pull/69)
**Milestone:** v0.2.0

## Context

The initial `create_stack=True` boolean parameter was insufficient for production deployments. Users needed to configure Lambda memory, alarms, SNS topics, and other stack parameters. The API had grown to include both `create_stack: bool` and `stack_parameters: dict[str, str]`, which was error-prone and not type-safe.

## Decision

Replace `create_stack` and `stack_parameters` with a unified `stack_options: StackOptions | None` parameter.

**Implementation:**
- `StackOptions` frozen dataclass with all configuration
- Validation in `__post_init__` for bounds checking
- `to_parameters()` method for CloudFormation conversion
- `None` = don't manage infrastructure; `StackOptions()` = manage with defaults

## Consequences

**Positive:**
- Type-safe configuration with IDE autocomplete
- Self-documenting API (all options visible in dataclass)
- Validation at construction time, not deployment time
- Clear semantics: presence of StackOptions = infrastructure management enabled
- Enables "self-deploying" applications

**Negative:**
- Breaking change from `create_stack` parameter
- More verbose for simple cases (`StackOptions()` vs `create_stack=True`)

## Alternatives Considered

- **Keep boolean + dict**: Rejected; not type-safe, no validation, poor discoverability
- **Builder pattern**: Rejected; more complex API for same outcome
- **Separate configuration file**: Rejected; adds deployment complexity, harder to keep in sync
