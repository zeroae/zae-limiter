# Supersede Mode

When arguments are `supersede <old> <new>`:

1. Parse both ADR numbers from arguments
2. Read the old ADR file
3. Use Edit to change status to `**Status:** Superseded by ADR-<new>`
4. Read the new ADR file
5. If the new ADR doesn't mention superseding, use Edit to add `**Supersedes:** ADR-<old>` after the Status line
6. Confirm both changes to the user
