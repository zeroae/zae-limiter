# Comment Mode

When arguments start with `comment`:

Post a **new comment** on an existing issue. This never modifies the issue body.

## Why this mode exists

Before it did, `comment` matched no row in SKILL.md's mode table and fell through to
Update mode, whose documented action is `gh issue edit --body`. That **replaces** the issue
body. An agent asked to "post findings as a comment" and following the fallthrough would have
silently destroyed the issue's contents and substituted its own text.

Nothing was lost only because the agents that hit it stopped and asked instead of improvising
a body. Do not rely on that happening again.

## Steps

1. Parse the issue number from the arguments (`comment 554`, `comment #554`).
2. Confirm the issue exists and read enough of it to know what you are replying to:
   `gh issue view <number> --json number,title,state`
   If it is CLOSED, say so in the comment or ask — a comment on a closed issue is often a
   mistake, and occasionally exactly right (recording that something resurfaced).
3. Write the comment body to a file rather than inlining it. Shell quoting mangles backticks,
   `$`, and newlines, and a mangled `--body` is silently posted rather than rejected.
4. Post it:

```bash
gh issue comment <number> --body-file <path>
```

5. Report the comment URL that `gh` prints. Do not claim success without it.

## Never

- **Never `gh issue edit`.** That is Update mode. If the request genuinely needs the body
  changed, stop and say so — replacing a body is destructive and is not what "comment" means.
- Never pass `--body` with an inlined heredoc for anything longer than one line.
- Never invent content. If you were told to post findings and do not have them, ask.

## Verify

`gh issue view <number> --comments | tail -40` — confirm your comment is the last one and the
body above it is unchanged.
