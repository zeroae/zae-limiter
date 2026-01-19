# ADR-012: CloudFormation Documentation Parity

**Status:** Accepted
**Date:** 2026-01-19

## Context

The CloudFormation template (`cfn_template.yaml`) defines all infrastructure resources for zae-limiter. The operator documentation (`docs/infra/cloudformation.md`) includes a Mermaid diagram and detailed documentation of parameters, resources, and outputs.

When the template is modified (new parameters, resources, conditions, or outputs), the documentation can become stale, leading to:
- Operators discovering features only by reading the template
- Mermaid diagram missing new resource relationships
- Parameter tables incomplete or outdated
- Output tables missing new exports

## Decision

Whenever the CloudFormation template is modified, the documentation at `docs/infra/cloudformation.md` must be updated to reflect:

1. **Mermaid diagram** - Add/remove resources and relationships
2. **Parameters table** - Add/remove/update parameter descriptions
3. **Outputs table** - Add/remove stack outputs
4. **Resource sections** - Document new resource types

This is enforced through the `docs-updater` agent which is invoked after infrastructure changes.

## Consequences

**Positive:**
- Operators can rely on documentation being current
- Mermaid diagram provides accurate visual overview
- Parameter and output tables are authoritative references

**Negative:**
- Additional work when modifying CloudFormation template
- Requires discipline to invoke docs-updater agent

## Alternatives Considered

- **Auto-generate docs from template**: Rejected; loses narrative context and customization examples
- **No parity requirement**: Rejected; documentation drift degrades operator experience
