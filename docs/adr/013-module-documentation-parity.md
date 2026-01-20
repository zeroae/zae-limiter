# ADR-013: Bidirectional Source-Documentation Parity

**Status:** Proposed
**Date:** 2026-01-19
**Supersedes:** ADR-012

## Context

Source code and documentation frequently drift out of sync. When `limiter.py` adds a `repository` parameter, the getting-started guide still shows the old constructor. When `cli.py` adds `--enable-tracing`, the CLI reference is missing it. When someone updates docs to describe a planned feature, users try to use APIs that don't exist yet.

The project has narrow-scope ADRs (ADR-011 for API/CLI parity, ADR-012 for CloudFormation docs), but contributors must guess which documentation files need updating for changes outside those areas.

## Decision

Maintain a bidirectional mapping between source modules and documentation. When either side changes, verify the other is synchronized.

| Source | Documentation | What to Sync |
|--------|---------------|--------------|
| `limiter.py` | `docs/api/limiter.md`, `docs/guide/basic-usage.md` | Constructor signatures, method examples |
| `models.py` | `docs/api/models.md` | Dataclass fields, factory methods |
| `exceptions.py` | `docs/api/exceptions.md` | Exception classes, error fields |
| `cli.py` | `docs/cli.md` | Commands, flags, usage examples |
| `repository.py` | `docs/contributing/architecture.md` | Protocol methods, schema patterns |
| `schema.py` | `docs/contributing/architecture.md` | Key patterns, GSI definitions |
| `bucket.py` | `docs/guide/token-bucket.md` | Algorithm formulas, refill math |
| `infra/cfn_template.yaml` | `docs/infra/cloudformation.md` | Mermaid diagram, parameters, outputs |
| `infra/` (other) | `docs/infra/deployment.md` | CLI examples, StackOptions fields |
| `aggregator/` | `docs/guide/usage-snapshots.md` | Stream processing, snapshot schema |
| `__init__.py` | `docs/api/index.md` | Public exports, module docstring |

## Consequences

**Positive:**
- Contributors know exactly which files to update for any change
- Users can trust that documented examples match actual behavior
- Reviewers can check documentation coverage during PR review

**Negative:**
- Every code change requires checking corresponding documentation
- Matrix needs updating when new modules are added

## Alternatives Considered

- **Convention-based mapping** (e.g., `foo.py` → `docs/foo.md`): Rejected; our docs structure doesn't mirror source structure, and implicit rules are easily forgotten
- **Documentation-only reviews**: Rejected; catching drift after the fact means users already hit broken examples
