# ADR Rules

## Enforcement Hierarchy

**The ADR is the source of truth. If there's a conflict between an ADR and the code, the code is wrong.**

## ADR Status Handling

| Status | Editable | Used for Enforcement |
|--------|----------|---------------------|
| Proposed | Yes (unless used in enforcement context) | No |
| Accepted | No | Yes |
| Superseded | No | No (completely ignored) |

An ADR is Proposed until the release it describes ships—see [When to Accept](#when-to-accept).

## Rules

1. **Accepted ADRs are immutable**: Once an ADR is accepted, it must not be modified. To change a decision, create a new ADR that supersedes the old one.

2. **Superseded ADRs are ignored**: When enforcing ADRs against code, completely skip any ADR with status "Superseded". They no longer represent active architectural decisions.

3. **Proposed ADRs are drafts**: A proposed ADR can be freely edited during review. However, once an ADR is referenced in an enforcement context (e.g., PR review, code audit), treat it as a constraint until the proposal is either accepted or rejected.

4. **Code follows ADRs**: When enforcement finds a mismatch between an accepted ADR and the implementation, the implementation must be updated to match the ADR—not the other way around.

5. **ADRs are accepted at release time**: An ADR stays Proposed for as long as the release it describes is in development, and is accepted as part of shipping that release.

## When to Accept

An ADR describes code. While that code is unreleased it is still moving, so an ADR accepted at design time is guaranteed to drift—and rule 1 then makes every correction cost either an owner exception or a superseding ADR.

That is not hypothetical. On 2026-09-15 the immutability rule was overridden by explicit owner exception **four times in one day**: ADR-138 during drafting, then ADR-136, ADR-137 and ADR-138 again. Each grant was individually justified—all four records were unreleased—but four exceptions to an immutability rule inside two days is the rule reporting a problem upstream of itself.

The false statements were not sloppy. They were *true when written*:

| ADR | Said | Falsified by |
|-----|------|--------------|
| 115 | refill must not be stored in `tk` | `build_composite_normal` and the aggregator, which materialize refill into `tk` under the `rf` lock |
| 114 | the pre-sharding bucket item key | GHSA-76rv pre-shard buckets |
| 136 | "ADR-119's time-to-fill TTL formula is unchanged" | #532, #557 |
| 137 | three Negatives describing obligations | #222, which had discharged them |

So: **Proposed while the release it describes is in development; Accepted as part of shipping that release.** Being Proposed means freely editable, which is the point—the record can track the implementation as that settles.

### The cost, stated plainly

A Proposed ADR is **not used for enforcement** (see the status table). During a development cycle the design record is therefore advisory: `/adr audit` skips it, and no PR can be rejected for contradicting it. That is the price of letting the record follow the code, and it is the cheaper half of the trade—a binding record that is wrong is worse than an advisory one that is right.

The mitigation is that acceptance must be a release checklist item rather than an afterthought. It is stated in `.claude/rules/release-planning.md` and executed as a step in the `/pr release` flow (`.claude/skills/pr/release.md`): a release PR that leaves a Proposed ADR describing shipped work is not ready.

### Not retroactive

This governs ADRs written from here. Every existing ADR is Accepted or Superseded—including 135-138, which describe unreleased v0.14.0 work. Do not un-accept anything.

## Changing Accepted Decisions

To modify an accepted architectural decision:

1. Create a new ADR (next available number)
2. Reference the old ADR being superseded
3. Explain why the decision is changing
4. Use `/adr supersede <old> <new>` to update both ADRs
