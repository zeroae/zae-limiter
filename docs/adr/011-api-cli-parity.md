# ADR-011: API/CLI Interface Parity

**Status:** Accepted
**Date:** 2026-01-15
**PRs:** [#141](https://github.com/zeroae/zae-limiter/pull/141), [#153](https://github.com/zeroae/zae-limiter/pull/153), [#188](https://github.com/zeroae/zae-limiter/pull/188)
**Milestone:** v0.3.0

## Context

zae-limiter serves two distinct user personas:

1. **Developers** integrate rate limiting into applications via Python API
2. **Operators** manage infrastructure, debug issues, and run reports via CLI

Early development added features to one interface without considering the other, creating gaps:
- `get_status()` existed in API but no CLI equivalent
- `audit list` existed in CLI but no API method
- Naming inconsistencies between interfaces

## Decision

Maintain interface parity with explicit exceptions based on use case.

**Parity matrix:**

| Feature Type | API | CLI | Rationale |
|--------------|-----|-----|-----------|
| Infrastructure ops | ✅ | ✅ | Both personas manage stacks |
| Data queries | ✅ | ✅ | Debugging from code or terminal |
| Admin actions | ✅ | ✅ | Automation needs both |
| Runtime limiting | ✅ | ❌ | Only meaningful in application context |
| Template exports | ❌ | ✅ | One-time ops task, not programmatic |

**Naming alignment:** API method names map predictably to CLI commands.

## Consequences

**Positive:**
- Consistent user experience across interfaces
- Features discoverable in both contexts
- Automation scripts can use either interface
- Documentation covers both without gaps

**Negative:**
- More implementation work per feature (two interfaces)
- Must maintain naming discipline across interfaces
- Some features feel forced in one interface (e.g., `cfn-template` as API)

## Alternatives Considered

- **CLI wraps API only**: Rejected; some operations are CLI-native (interactive prompts, template export)
- **API only**: Rejected; operators need CLI for shell scripts and debugging
- **No parity requirement**: Rejected; leads to fragmented UX and documentation gaps
