# View Mode

Display comprehensive issue details including full body, comments, labels, milestone, dependencies, and linked PRs. Designed to give complete context for working on an issue.

## Return Value Contract

**CRITICAL:** Your final message MUST be the complete formatted markdown from the Output Format section — nothing else. Do NOT summarize, truncate, or wrap it in commentary. The calling agent will display your output verbatim to the user. Every section (metadata, acceptance criteria, dependencies, full body sections, comments) must be present in your return value.

## Usage

```bash
# View by issue number (always fetches full details + comments)
/issue view 135
/issue view #135
```

## Tool Constraints

**CRITICAL:** Only use `gh` commands. All JSON processing MUST use `--jq` flags — never pipe to `python`, `python3`, `node`, or any other interpreter. The skill's `allowed-tools` only permits `Bash(gh:*)`.

## Process

1. **Identify Issue**: Get issue number from arguments (strip leading `#` if present)
2. **Fetch Data**: Run these `gh` commands (in parallel where possible):

**Step 2a — Full readable view with comments:**
```bash
gh issue view <number> --repo zeroae/zae-limiter --comments
```

**Step 2b — Structured metadata (labels, milestone, assignees):**
```bash
gh issue view <number> --repo zeroae/zae-limiter --json number,title,state,body,labels,milestone,assignees,author,createdAt,updatedAt --jq '{
  number,
  title,
  state,
  labels: [.labels[].name] | join(", "),
  milestone: (.milestone.title // "none"),
  milestone_due: (.milestone.dueOn // "none"),
  assignees: ([.assignees[].login] | join(", ") // "unassigned"),
  author: .author.login,
  created: .createdAt,
  updated: .updatedAt
}'
```

**Step 2c — Timeline cross-references (linked PRs and issues):**
```bash
gh api repos/zeroae/zae-limiter/issues/<number>/timeline --paginate --jq '[.[] | select(.event == "cross-referenced") | {number: .source.issue.number, title: .source.issue.title, state: .source.issue.state, is_pr: (.source.issue.pull_request != null)}]'
```

3. **Parse Dependencies**: From the body text in Step 2a, identify references matching patterns like `Depends on #X`, `Blocked by #X`, `After #X`, `Requires #X`. For each dependency found, fetch its status:
```bash
gh issue view <dep_number> --repo zeroae/zae-limiter --json number,title,state --jq '"\(.number) (\(.state)): \(.title)"'
```

4. **Format and Display**: Combine all data into the output format below. Reproduce the **full issue body** — do not summarize or omit sections.

## Output Format

Present issue details in this format:

```markdown
## Issue #<number>: <title>

**Status:** <state> | **Milestone:** <milestone> (due <date>) | **Labels:** <labels>
**Assignees:** <assignees or "unassigned"> | **Author:** <author>
**Created:** <date> | **Updated:** <date>

### Summary
<First paragraph or summary section from issue body>

### Acceptance Criteria (X of Y complete)

| Status | Criterion |
|--------|-----------|
| ✅ | <checked criterion> |
| ⬜ | <unchecked criterion> |

### Dependencies
<List issues this depends on, with their status>
- #123 (Open): <title>
- #456 (Closed): <title>

### Linked Issues / Pull Requests
<From timeline cross-references>
- #789 (PR, merged): <title>
- #790 (Issue, Open): <title>

### Additional Sections
<Reproduce ALL other sections from the issue body verbatim — implementation notes,
documentation updates, alternatives considered, non-goals, context, etc.
Do not summarize or omit content.>

### Comments (<count>)
<ALL comments in chronological order>

**@<author>** on <date>:
> <comment body>
```

## Acceptance Criteria Parsing

- Extract checkboxes from issue body
- `- [x]` → ✅ (completed)
- `- [ ]` → ⬜ (pending)
- Count and show progress: "X of Y criteria complete"

## Dependency Detection

Scan the issue body for these patterns (case-insensitive):
- `depends on #<number>`
- `blocked by #<number>`
- `after #<number>`
- `requires #<number>`
- `parent: #<number>` or `parent of #<number>`
- Task list items: `- [ ] #<number>` or `- [x] #<number>`

## Example

```
/issue view 135

## Issue #135: ✨ client-side config cache with configurable TTL

**Status:** Open | **Milestone:** v0.5.0 (due 2025-03-01) | **Labels:** area/limiter, performance
**Assignees:** sodre | **Author:** sodre
**Created:** 2024-11-15 | **Updated:** 2025-01-20

### Summary
Implement client-side caching for configuration data (system defaults, resource defaults)
to avoid per-request DynamoDB reads.

### Acceptance Criteria (3 of 10 complete)

| Status | Criterion |
|--------|-----------|
| ✅ | Config is cached with configurable TTL |
| ⬜ | Cache hits avoid DynamoDB reads |
| ⬜ | Thread-safe for sync wrapper |
...

### Dependencies
- #100 (Closed): ✨ centralized config design
- #133 (Closed): ⚡ batch get for bucket reads

### Linked Issues / Pull Requests
- #142 (PR, merged): ✨ feat(cache): add config cache with TTL
- #150 (Issue, Open): ♻️ extract repository protocol

### Additional Sections
#### Implementation Notes
- Use `time.monotonic()` for TTL tracking
- Cache per RateLimiter instance (not global)
...

### Comments (3)

**@sodre** on 2024-11-16:
> We should consider negative caching for entities without custom config...

**@contributor** on 2024-11-18:
> Good point. I'll add that to the acceptance criteria.
...
```
