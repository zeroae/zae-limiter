# ADR-000: ADR Format Standard

**Status:** Accepted
**Date:** 2026-01-19

## Context

Architecture Decision Records help teams understand why past decisions were made. Without consistent formatting, ADRs become hard to scan, compare, and maintain. Some ADRs balloon into design documents with code examples and implementation checklists, while others lack essential sections like alternatives considered.

The team needs a standard format that keeps ADRs focused on the "why" while ensuring they remain concise and actionable.

## Decision

All ADRs must follow this format:

1. **Maximum 100 lines** - If longer, split into multiple ADRs or move details to issues
2. **One decision per ADR** - Each ADR documents exactly one architectural choice
3. **Required sections:**
   - Context (2-3 paragraphs max)
   - Decision (1-2 sentences)
   - Consequences (positive and negative bullet lists)
   - Alternatives Considered (1 sentence rejection reason each)
4. **Excluded content** (belongs in issues or design docs):
   - Code examples or API signatures
   - Implementation checklists
   - Test cases
   - Phased rollout plans
   - Detailed cost calculations

## Consequences

**Positive:**
- ADRs are quick to read and compare
- Reviewers can focus on architectural reasoning, not implementation details
- Consistent structure makes it easy to find information

**Negative:**
- Some decisions require linking to external design documents
- Contributors must learn the format before writing ADRs

## Alternatives Considered

### Free-form documentation
Rejected because: Inconsistent formats make ADRs hard to scan and maintain over time.

### Detailed design documents
Rejected because: Design docs focus on "how" rather than "why", and become stale after implementation.
