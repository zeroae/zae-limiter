# Progress Mode

Check off completed checkboxes in issue body based on work done.

## Process

1. **Identify Issue**: Infer from branch, commits, or conversation
2. **Fetch Issue Body**:
```bash
gh issue view <number> --json body --jq '.body'
```
3. **Parse Checkboxes**: Find all `- [ ]` items
4. **Match to Context**: Compare each checkbox against:
   - Recent commits (`git log --oneline -10`)
   - Files changed (`git diff --name-only origin/main`)
   - Test results mentioned in conversation
   - Explicit user statements ("I finished X")
5. **Preview Changes**: Show which boxes will be checked
```
Checking off in issue #123:
- [x] Add unit tests for caching  ← matches commit "test: add caching tests"
- [x] Update documentation        ← matches changed docs/guide/caching.md
- [ ] Performance benchmarks      ← no matching work found

Proceed? [Y/n]
```
6. **Update Issue Body**: Replace `- [ ]` with `- [x]` for completed items
```bash
gh issue edit <number> --body "$(cat <<'EOF'
<body with checked boxes>
EOF
)"
```

## Issue Number Inference

Detect issue references from context:
- Explicit: "#123", "issue 123", "issue #123"
- From branch name: `git branch --show-current` → extract issue number
- From recent commits: `git log -1 --format=%s` → look for "Closes #N"
- From conversation: recently discussed issue numbers

## Checkbox Matching Rules

| Evidence | Checkbox Text | Match? |
|----------|---------------|--------|
| Commit "test: add unit tests" | "Add unit tests" | ✅ Yes |
| Changed `docs/guide/*.md` | "Update documentation" | ✅ Yes |
| User says "benchmarks done" | "Performance benchmarks" | ✅ Yes |
| No related commits/files | Any checkbox | ❌ No (ask) |

## Output

Return issue URL with list of checkboxes marked complete.
