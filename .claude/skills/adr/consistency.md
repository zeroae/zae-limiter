# Consistency Mode

When arguments are `consistency`:

**Immediately delegate to the adr-consistency agent. Do not read any ADR files yourself.**

Use the Task tool:
- subagent_type: "adr-consistency"
- prompt: "Check all ADRs in docs/adr/ for consistency issues"

Return the agent's result verbatim.
