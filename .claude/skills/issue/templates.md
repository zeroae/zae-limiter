# Issue Body Templates

Templates derived from `.github/ISSUE_TEMPLATE/` files. Use the appropriate template based on issue type.

## Bug 🐛

```markdown
## Description

<What happened? What did you expect to happen?>

## Steps to Reproduce

1. <step>
2. <step>
3. <observe error>

## Environment

- zae-limiter version: <version>
- Python: <version>
- AWS Region: <region>
```

**Required fields:** Description, Steps to Reproduce
**Optional fields:** Environment

## Feature ✨

```markdown
## Problem or Use Case

<What problem does this solve? What's the use case?>

## Proposed Solution

<How should this work? Include API examples if relevant.>

```python
# Example usage
limiter = RateLimiter(...)
result = limiter.new_feature()
```

## Alternatives Considered

<What other approaches were considered?>
```

**Required fields:** Problem or Use Case
**Optional fields:** Proposed Solution, Alternatives Considered

## Task 📋

```markdown
## Description

<What needs to be done?>

## Acceptance Criteria

- [ ] Criterion 1
- [ ] Criterion 2
```

**Required fields:** Description
**Optional fields:** Acceptance Criteria

## Chore 🔧

```markdown
## Description

<What maintenance work is needed?>

## Details

<Specifics: dependencies to update, refactoring scope, etc.>
```

**Required fields:** Description
**Optional fields:** Details

## Epic 🎯

Use GitHub's sub-issues feature to link child issues to this Epic.

```markdown
## Summary

<What is this Epic about? Why does it matter?>

## Goals

1. Users can...
2. System supports...
3. Documentation covers...

## Success Criteria

- [ ] All sub-issues closed
- [ ] Documentation updated
- [ ] Feature tested in production

## Out of Scope

<What is explicitly NOT part of this Epic?>
```

**Required fields:** Summary, Goals, Success Criteria
**Optional fields:** Out of Scope

## Theme 🎨

Themes are high-level strategic goals. Use GitHub's sub-issues to link Epics.

```markdown
## Vision

<What future state are we working toward?>

## Motivation

<Why is this important? What's the business or user value?>

## Scope

<What areas/components does this theme touch?>

## Timeframe

<Expected duration: Q1 2025, H1 2025, etc.>
```

**Required fields:** Vision, Motivation
**Optional fields:** Scope, Timeframe
