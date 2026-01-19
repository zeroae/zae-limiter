# ADR Rules

## Enforcement Hierarchy

**The ADR is the source of truth. If there's a conflict between an ADR and the code, the code is wrong.**

## ADR Status Handling

| Status | Editable | Used for Enforcement |
|--------|----------|---------------------|
| Proposed | Yes (unless used in enforcement context) | No |
| Accepted | No | Yes |
| Superseded | No | No (completely ignored) |

## Rules

1. **Accepted ADRs are immutable**: Once an ADR is accepted, it must not be modified. To change a decision, create a new ADR that supersedes the old one.

2. **Superseded ADRs are ignored**: When enforcing ADRs against code, completely skip any ADR with status "Superseded". They no longer represent active architectural decisions.

3. **Proposed ADRs are drafts**: A proposed ADR can be freely edited during review. However, once an ADR is referenced in an enforcement context (e.g., PR review, code audit), treat it as a constraint until the proposal is either accepted or rejected.

4. **Code follows ADRs**: When enforcement finds a mismatch between an accepted ADR and the implementation, the implementation must be updated to match the ADR—not the other way around.

## Changing Accepted Decisions

To modify an accepted architectural decision:

1. Create a new ADR (next available number)
2. Reference the old ADR being superseded
3. Explain why the decision is changing
4. Use `/adr supersede <old> <new>` to update both ADRs
