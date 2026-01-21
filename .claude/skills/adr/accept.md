# Accept Mode

When arguments start with `accept`:

1. Parse the ADR number from arguments (e.g., `accept 5` or `accept 005`)
2. Read the ADR file `docs/adr/NNN-*.md`
3. **Run consistency check against all other Accepted ADRs** to check for conflicts:
   - For each Accepted ADR (excluding Superseded and the ADR being accepted):
   - Check if the new ADR's Decision section conflicts with existing decisions
   - Use the consistency logic from `consistency.md` to validate
4. If conflicts found: Report them and ask user how to proceed:
   - **Proceed anyway** - Accept despite conflicts
   - **Update the new ADR** - Modify to align with existing decisions
   - **Supersede conflicting ADR** - Use `/adr supersede` first
   - **Cancel** - Don't accept until resolved
5. If no conflicts (or user chooses to proceed): Use Edit to change `**Status:** Proposed` to `**Status:** Accepted`
6. Confirm the change to the user
