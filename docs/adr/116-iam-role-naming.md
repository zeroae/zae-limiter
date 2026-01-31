# ADR-116: IAM Role Naming Convention

**Status:** Proposed
**Date:** 2026-01-30
**Issue:** [#252](https://github.com/zeroae/zae-limiter/issues/252)

## Context

IAM role names can exceed AWS's 64-character limit when using `role_name_format` with long stack names. The current implementation has two bugs:

1. All roles embed `-aggregator-role` in their names instead of component-specific suffixes (e.g., AppRole becomes `{format}-aggregator-role-app` instead of something shorter)
2. Validation checks stack name and format template separately, not the combined result

With enterprise environments using permission boundaries (e.g., `role_name_format="PowerUserPB-{}"`), users hit the 64-char limit unexpectedly when derived roles append additional suffixes like `-readonly`.

## Decision

IAM role names must follow the pattern: `{format}.replace("{}", f"{stack_name}-{component}")` where component is one of: `aggr` (4 chars), `app` (3 chars), `admin` (5 chars), `read` (4 chars). All component names must be ≤ 8 characters to ensure users who choose valid stack names today won't break on library upgrades.

## Consequences

**Positive:**
- Shorter role names leave room for longer stack names and format templates
- Each role has a distinct, meaningful name (not all embedding "aggregator")
- Validation catches length violations early with actionable error messages
- 8-char invariant provides upgrade safety margin for future components

**Negative:**
- Breaking change: existing role names change (e.g., `{stack}-aggregator-role` → `{stack}-aggr`)
- CloudFormation will replace IAM roles during stack updates, causing brief Lambda failures

## Alternatives Considered

### Keep `-aggregator-role` suffix for Lambda only
Rejected because: The current bug embeds this verbose suffix in all roles via incorrect logic, wasting character budget.

### Use numeric suffixes (role-1, role-2)
Rejected because: Not self-documenting; operators can't identify role purpose from name.
