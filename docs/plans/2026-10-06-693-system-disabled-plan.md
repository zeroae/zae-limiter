# Reject `disabled` on `system` in a Limits Manifest Implementation Plan (#693)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `disabled` key under `system:` in a limits manifest (or `System.Disabled` in a
`Custom::ZaeLimiterLimits` resource) fails parsing with a clear error instead of being silently
dropped.

**Architecture:** Mirror the ADR-146 `cascade` rejection. `SystemDecl.from_dict`
(`src/zae_limiter_provisioner/manifest.py`) raises `ValueError` when `disabled` is present. The
CloudFormation path carries `System.Disabled` through to the manifest in both directions —
`handler._cfn_properties_to_manifest` (CFN → manifest) and `limits_cli` `cfn-template`
(manifest → CFN) — so the provisioner rejects it with the same reason rather than either side
dropping it unseen. Validation only: ADR-125 scopes system-level disable out.

**Tech Stack:** Python, pytest.

**Spec:** GitHub issue #693 (owner decision: reject, mirror `cascade`).

## Global Constraints

- Error text: says system-level `disabled` is not supported and points at
  `resources.<name>` or `entities.<id>.resources.<name>`; contains
  `not supported at the system level` (same phrase as the `cascade` rejection).
- The error is raised at parse time, before any write (`handler` parses via
  `LimitsManifest.from_dict` before `plan`/`apply`/`diff`).
- No `noqa` / `type: ignore`.

## Review Focus

- `system: {disabled: false}` — a false value is still rejected (key presence, not truthiness).
- `System.Disabled: "false"` as a CloudFormation string — coerced like `Cascade`, still rejected.
- A manifest with both `cascade` and `disabled` on `system` — fails (either message is fine).
- `system` without `disabled` keeps parsing unchanged (existing tests cover this).
- `limits cfn-template` round trip emits `System.Disabled` so the stack fails instead of the
  generated template silently losing it.

---

### Task 1: Reject `system.disabled` in the manifest and carry it through CloudFormation

**Files:**
- Modify: `src/zae_limiter_provisioner/manifest.py` (`SystemDecl.from_dict`)
- Modify: `src/zae_limiter_provisioner/handler.py` (`_cfn_properties_to_manifest`, System block)
- Modify: `src/zae_limiter/limits_cli.py` (`cfn-template` System block)
- Test: `tests/unit/test_provisioner_manifest.py`, `tests/unit/test_limits_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_provisioner_manifest.py, class TestManifestDisabled
@pytest.mark.parametrize("value", [True, False])
def test_rejected_at_the_system_level(self, value):
    with pytest.raises(ValueError, match="'disabled' is not supported at the system level"):
        LimitsManifest.from_dict(
            {
                "namespace": "default",
                "system": {"disabled": value, "limits": {"rpm": {"capacity": 1}}},
            }
        )


def test_cloudformation_system_disabled_is_carried_through_to_be_rejected(self):
    from zae_limiter_provisioner.handler import _cfn_properties_to_manifest

    manifest = _cfn_properties_to_manifest({"System": {"Disabled": "false"}})
    assert manifest["system"]["disabled"] is False
    with pytest.raises(ValueError, match="'disabled' is not supported at the system level"):
        LimitsManifest.from_dict(manifest)
```

```python
# tests/unit/test_limits_cli.py, beside test_a_system_cascade_is_carried_through_to_be_rejected
def test_a_system_disabled_is_carried_through_to_be_rejected(self):
    from zae_limiter_provisioner.handler import _cfn_properties_to_manifest
    from zae_limiter_provisioner.manifest import LimitsManifest

    props = self._run_cfn_template({"namespace": "x", "system": {"disabled": True}})
    assert props["System"]["Disabled"] is True
    manifest = _cfn_properties_to_manifest(props)
    with pytest.raises(ValueError, match="'disabled' is not supported at the system level"):
        LimitsManifest.from_dict(manifest)
```

- [ ] **Step 2: Run them, expect FAIL** (no error raised; `Disabled` missing from CFN props)

Run: `uv run pytest tests/unit/test_provisioner_manifest.py tests/unit/test_limits_cli.py -k "system" -v`

- [ ] **Step 3: Implement**

`manifest.py`, in `SystemDecl.from_dict` after the `cascade` check:

```python
if "disabled" in d:
    # Not silently dropped (#693): ADR-125 scopes disabling out at the system
    # level, so a value here would look applied and do nothing.
    raise ValueError(
        "system: 'disabled' is not supported at the system level; set it on "
        "resources.<name> or entities.<id>.resources.<name>"
    )
```

`handler.py`, System block, beside `Cascade`:

```python
if "Disabled" in cfn_system:
    system["disabled"] = _coerce_bool(cfn_system["Disabled"], "System.Disabled")
```

`limits_cli.py`, System block, beside `cascade`:

```python
if "disabled" in sys_data:
    system_props["Disabled"] = sys_data["disabled"]
```

- [ ] **Step 4: Run tests, expect PASS**; then full unit suite.

- [ ] **Step 5: Commit** — `🐛 fix(provisioner): reject disabled on system in a limits manifest`,
  body explains why, footer `Fixes #693`.

### Task 2: Docs

**Files:** `CLAUDE.md` (`disabled` (ADR-125) paragraph), `docs/cli.md` (Cascade and Disabled),
`docs/infra/deployment.md` (manifest and CloudFormation paragraphs).

- [ ] Replace "`disabled` there is ignored" / "Not supported on `system`" with "an error", and
  say a `Disabled` under `System` fails the stack like `Cascade`.
- [ ] Commit — `📝 docs: say a manifest rejects disabled on system`, `Refs #693`.
