"""CLI commands for declarative limits management."""

from __future__ import annotations

import json
import sys
from typing import Any

import boto3
import click
import yaml


@click.group()
def limits() -> None:
    """Declarative limits management.

    Manage rate limits via YAML files. Apply through a Lambda provisioner
    that supports both CLI and CloudFormation paths.
    """


@limits.command("plan")
@click.option("--name", "-n", required=True, help="Stack identifier.")
@click.option("--region", help="AWS region.")
@click.option("--endpoint-url", help="AWS endpoint URL (e.g., LocalStack).")
@click.option("--namespace", "-N", default="default", help="Namespace.")
@click.option(
    "--file",
    "-f",
    "file_path",
    required=True,
    type=click.Path(exists=True),
    help="YAML limits file.",
)
def limits_plan(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    namespace: str,
    file_path: str,
) -> None:
    """Preview changes without applying (like terraform plan).

    Read-only: never deploys a stack, registers a namespace, writes the
    version record or updates a Lambda. A missing stack or namespace is an
    error, and so is a stack whose Lambdas are behind this client — the
    provisioner does the planning, so run 'zae-limiter upgrade' first.
    """
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "plan", manifest_data, preview="plan")

    for warning in _cascade_warnings(manifest_data):
        click.echo(f"Warning: {warning}", err=True)

    changes = result.get("changes", [])
    if not changes:
        click.echo("No changes. Infrastructure is up-to-date.")
        return

    click.echo(f"Plan: {len(changes)} change(s)\n")
    for change in changes:
        symbol = {"create": "+", "update": "~", "delete": "-"}.get(change["action"], "?")
        target = change.get("target") or "(system defaults)"
        click.echo(f"  {symbol} {change['action']} {change['level']}: {target}")


@limits.command("apply")
@click.option("--name", "-n", required=True, help="Stack identifier.")
@click.option("--region", help="AWS region.")
@click.option("--endpoint-url", help="AWS endpoint URL (e.g., LocalStack).")
@click.option("--namespace", "-N", default="default", help="Namespace.")
@click.option(
    "--file",
    "-f",
    "file_path",
    required=True,
    type=click.Path(exists=True),
    help="YAML limits file.",
)
def limits_apply(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    namespace: str,
    file_path: str,
) -> None:
    """Apply limits from YAML file (like terraform apply)."""
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "apply", manifest_data)

    changes = result.get("changes", [])
    if not changes:
        click.echo("No changes. Infrastructure is up-to-date.")
        return

    for change in changes:
        symbol = {"create": "+", "update": "~", "delete": "-"}.get(change["action"], "?")
        target = change.get("target") or "(system defaults)"
        click.echo(f"  {symbol} {change['action']} {change['level']}: {target}")

    click.echo(
        f"\nApplied: {result.get('created', 0)} created, "
        f"{result.get('updated', 0)} updated, "
        f"{result.get('deleted', 0)} deleted."
    )

    errors = result.get("errors", [])
    if errors:
        click.echo(f"\nErrors ({len(errors)}):", err=True)
        for err in errors:
            click.echo(f"  - {err}", err=True)
        sys.exit(1)


@limits.command("diff")
@click.option("--name", "-n", required=True, help="Stack identifier.")
@click.option("--region", help="AWS region.")
@click.option("--endpoint-url", help="AWS endpoint URL (e.g., LocalStack).")
@click.option("--namespace", "-N", default="default", help="Namespace.")
@click.option(
    "--file",
    "-f",
    "file_path",
    required=True,
    type=click.Path(exists=True),
    help="YAML limits file.",
)
def limits_diff(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    namespace: str,
    file_path: str,
) -> None:
    """Show drift between YAML and live DynamoDB state.

    Read-only, with the same refusals as 'limits plan': a missing stack or
    namespace, or a stack whose Lambdas are behind this client.
    """
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "plan", manifest_data, preview="diff")

    changes = result.get("changes", [])
    if not changes:
        click.echo("No drift detected. Live state matches YAML.")
        return

    click.echo(f"Drift detected: {len(changes)} difference(s)\n")
    for change in changes:
        symbol = {"create": "+", "update": "~", "delete": "-"}.get(change["action"], "?")
        target = change.get("target") or "(system defaults)"
        click.echo(f"  {symbol} {change['level']}: {target}")


@limits.command("cfn-template")
@click.option("--name", "-n", required=True, help="Stack identifier (for ImportValue).")
@click.option(
    "--file",
    "-f",
    "file_path",
    required=True,
    type=click.Path(exists=True),
    help="YAML limits file.",
)
def limits_cfn_template(name: str, file_path: str) -> None:
    """Generate a CloudFormation template from YAML file."""
    manifest_data = _load_yaml(file_path)
    namespace = manifest_data.get("namespace", "default")

    properties: dict[str, Any] = {
        "ServiceToken": {"Fn::ImportValue": f"{name}-ProvisionerArn"},
        "TableName": name,
        "Namespace": namespace,
    }

    if "system" in manifest_data:
        system_props: dict[str, Any] = {}
        sys_data = manifest_data["system"]
        if "on_unavailable" in sys_data:
            system_props["OnUnavailable"] = sys_data["on_unavailable"]
        if "limits" in sys_data:
            system_props["Limits"] = _limits_to_cfn(sys_data["limits"])
        if "cascade" in sys_data:
            # Passed through so the provisioner rejects it with its reason
            # (ADR-146: no system-level policy), not dropped here unseen.
            system_props["Cascade"] = sys_data["cascade"]
        if "disabled" in sys_data:
            # Likewise (#693): ADR-125 has no system-level disable.
            system_props["Disabled"] = sys_data["disabled"]
        properties["System"] = system_props

    if "resources" in manifest_data:
        resources_props = {}
        for res_name, res_data in manifest_data["resources"].items():
            res_props: dict[str, Any] = {"Limits": _limits_to_cfn(res_data.get("limits", {}))}
            # Tri-state: only emit "Disabled" when the manifest declared it. An
            # absent key means "inherit" and must stay absent from the template;
            # an explicit False (carve-out) must round-trip as False, not be
            # dropped or coerced.
            if "disabled" in res_data:
                res_props["Disabled"] = res_data["disabled"]
            # The cascade policy (ADR-146), tri-state by the same rule.
            if "cascade" in res_data:
                res_props["Cascade"] = res_data["cascade"]
            resources_props[res_name] = res_props
        properties["Resources"] = resources_props

    if "entities" in manifest_data:
        entities_props = {}
        for ent_id, ent_data in manifest_data["entities"].items():
            ent_resources = {}
            for res_name, res_data in ent_data.get("resources", {}).items():
                ent_res_props: dict[str, Any] = {
                    "Limits": _limits_to_cfn(res_data.get("limits", {}))
                }
                if "disabled" in res_data:
                    ent_res_props["Disabled"] = res_data["disabled"]
                if "cascade" in res_data:
                    ent_res_props["Cascade"] = res_data["cascade"]
                ent_resources[res_name] = ent_res_props
            entities_props[ent_id] = {"Resources": ent_resources}
        properties["Entities"] = entities_props

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": f"Declarative limits for namespace '{namespace}'",
        "Resources": {
            "TenantLimits": {
                "Type": "Custom::ZaeLimiterLimits",
                "Properties": properties,
            },
        },
    }

    click.echo(yaml.dump(template, default_flow_style=False, sort_keys=False))


# Manifest schedule-entry field -> CloudFormation property name (#222).
#
# The snake_case column must stay exactly `zae_limiter_provisioner.manifest.
# _ENTRY_FIELDS`: that allowlist is strict, so a seventh property or one spelled
# differently here becomes a ValueError inside the provisioner Lambda at deploy
# time rather than at `limits cfn-template` time. `_reset` is deliberately
# absent — it is private, and a reset entry smuggled into the parameter tuple
# would win its window and then supply nothing.
#
# Note `refill_period_seconds` -> `RefillPeriodSeconds`, which is *not* the
# limit-level `refill_period` -> `RefillPeriod` pair a few lines below.
#
# `zae_limiter_provisioner.handler._CFN_SCHEDULE_KEYS` is the inverse. The two
# cannot share a module: the provisioner Lambda zip carries only a four-file
# `zae_limiter` stub, so it can never import this one. A unit test pins them as
# exact inverses of each other.
_SCHEDULE_KEYS: tuple[tuple[str, str], ...] = (
    ("cron", "Cron"),
    ("tz", "Tz"),
    ("scale", "Scale"),
    ("capacity", "Capacity"),
    ("refill_amount", "RefillAmount"),
    ("refill_period_seconds", "RefillPeriodSeconds"),
)


def _schedule_to_cfn(raw: Any, *, key: str, limit_name: str) -> list[dict[str, Any]]:
    """Convert manifest schedule entries to CFN PascalCase, omitting absent keys.

    Mirrors ``manifest._parse_entries``' tolerance for shape, so that the two
    ways of applying the same YAML — ``limits apply`` and a generated
    ``Custom::ZaeLimiterLimits`` — accept and reject the same documents. Entry
    *contents* stay unvalidated here (the generator walks raw dicts and never
    builds a ``LimitDecl``), so a bad cron still surfaces in the Lambda.
    """
    if not isinstance(raw, list):
        # `schedule:` with nothing under it is YAML null, not a list, and the
        # manifest parser reads that as "no schedule" — so must this, or the
        # same file would generate a template but fail to apply directly.
        if raw is None:
            return []
        raise click.ClickException(
            f"{limit_name}.{key} must be a list of entries, got {type(raw).__name__}."
        )
    entries = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise click.ClickException(
                f"{limit_name}.{key}[{i}] must be a mapping, got {type(entry).__name__}."
            )
        entries.append({pascal: entry[snake] for snake, pascal in _SCHEDULE_KEYS if snake in entry})
    return entries


def _limits_to_cfn(limits: dict[str, Any]) -> dict[str, Any]:
    """Convert manifest limits dict to CFN PascalCase format."""
    result = {}
    for name, limit in limits.items():
        cfn_limit: dict[str, Any] = {"Capacity": limit["capacity"]}
        if "refill_amount" in limit:
            cfn_limit["RefillAmount"] = limit["refill_amount"]
        if "refill_period" in limit:
            cfn_limit["RefillPeriod"] = limit["refill_period"]
        # ADR-139: the third recovery spelling, mirroring
        # `zae_limiter_provisioner.handler._CFN_LIMIT_OPTIONAL_KEYS`'s
        # `"ResetAfterSeconds": ("reset_after_seconds", _coerce_int)`.
        if "reset_after_seconds" in limit:
            cfn_limit["ResetAfterSeconds"] = limit["reset_after_seconds"]
        # #467, mirroring `_CFN_LIMIT_OPTIONAL_KEYS`'s `"Soft"`. Emitted only
        # when true, so a hard limit's template is unchanged.
        if limit.get("soft"):
            cfn_limit["Soft"] = True
        # Emitted only when non-empty, matching `LimitDecl.to_dict()`: an
        # unscheduled limit's template is byte-identical to what it was before
        # schedules existed, and an empty list never stands in for "absent".
        # Key *presence* is the wrong test here (unlike ADR-125's `Disabled`,
        # where False is a meaningful carve-out) — `schedule: []` and
        # `schedule:` both mean "no schedule".
        for key, prop in (("schedule", "Schedule"), ("reset_schedule", "ResetSchedule")):
            if key in limit:
                converted = _schedule_to_cfn(limit[key], key=key, limit_name=name)
                if converted:
                    cfn_limit[prop] = converted
        result[name] = cfn_limit
    return result


def _cascade_warnings(manifest_data: dict[str, Any]) -> list[str]:
    """Warn about a resource that cascades with no entity-level limits for it (ADR-146).

    A parent debited through cascade is limited by its own config for the
    resource; with none, it falls through to the resource defaults — the
    numbers written for one user — which is almost always a mistake.
    """
    covered = {
        resource
        for entity in (manifest_data.get("entities") or {}).values()
        for resource in ((entity or {}).get("resources") or {})
    }
    if "_default_" in covered:
        # An entity-wide `_default_` entry outranks the resource level for
        # every resource, so it covers them all.
        return []
    return [
        f"resources.{name} sets cascade: true, but no entity in this manifest has its own "
        f"limits for '{name}', so parents will be limited by the per-user resource defaults"
        for name, resource in (manifest_data.get("resources") or {}).items()
        if (resource or {}).get("cascade") is True and name not in covered
    ]


def _load_yaml(file_path: str) -> dict[str, Any]:
    """Load and parse a YAML file."""
    with open(file_path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        click.echo("Error: YAML file must contain a mapping", err=True)
        sys.exit(1)
    return data


def _invoke_provisioner(
    name: str,
    region: str | None,
    endpoint_url: str | None,
    action: str,
    manifest_data: dict[str, Any],
    *,
    preview: str | None = None,
) -> dict[str, Any]:
    """Invoke the provisioner Lambda function.

    Args:
        name: Stack name (used to derive Lambda function name and table name).
        region: AWS region.
        endpoint_url: AWS endpoint URL (for LocalStack).
        action: "plan" or "apply".
        manifest_data: Parsed YAML manifest as dict.
        preview: The read-only command being run (``"plan"`` or ``"diff"``),
            or None for ``apply``. A preview connects read-only (#648): it
            never provisions, registers the namespace or updates a Lambda,
            and refuses when the stack's Lambdas are behind this client.
            ``apply`` keeps ``Repository.open()`` and auto-registers the
            manifest's namespace.

    Returns:
        Lambda response payload.
    """
    import asyncio

    from .exceptions import NamespaceNotFoundError
    from .repository import Repository

    ns = manifest_data.get("namespace", "default")

    async def _resolve_read_only(command: str) -> str:
        from .cli import _connect_read_only

        repo = await _connect_read_only(
            name, region, endpoint_url, ns, require_current_lambdas=command
        )
        try:
            return repo.namespace_id
        finally:
            await repo.close()

    async def _resolve() -> str:
        try:
            repo = await Repository.open(
                ns,
                stack=name,
                region=region,
                endpoint_url=endpoint_url,
            )
            try:
                return repo._namespace_id
            finally:
                await repo.close()
        except NamespaceNotFoundError:
            # Auto-register namespace on first apply
            repo = await Repository.open(
                stack=name,
                region=region,
                endpoint_url=endpoint_url,
            )
            try:
                await repo.register_namespace(ns)
                scoped = await repo.namespace(ns)
                return scoped._namespace_id
            finally:
                await repo.close()

    from .exceptions import StackOperationError, VersionError

    if preview is not None:
        namespace_id = asyncio.run(_resolve_read_only(preview))
    else:
        try:
            namespace_id = asyncio.run(_resolve())
        except NamespaceNotFoundError:
            namespace_id = ""
        except (VersionError, StackOperationError) as e:
            # A client below the stack's minimum, or a failed Lambda auto-update
            # (#638). Carrying on would hand the manifest to a provisioner whose
            # version nothing checked — a pre-v0.15 one stores a reset_after limit
            # as a dripping limit, silently.
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

    function_name = f"{name}-limits-provisioner"

    kwargs: dict[str, Any] = {}
    if region:
        kwargs["region_name"] = region
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url

    lambda_client = boto3.client("lambda", **kwargs)

    payload = {
        "action": action,
        "table_name": name,
        "namespace_id": namespace_id,
        "manifest": manifest_data,
    }

    try:
        response = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
    except lambda_client.exceptions.ResourceNotFoundException:
        click.echo(
            f"Error: Lambda function '{function_name}' not found.\n"
            "The limits provisioner is not deployed for this stack. It is skipped by "
            "'zae-limiter deploy --no-provisioner' and by '--no-iam' (which leaves no "
            "role for it). Redeploy the stack with the provisioner enabled.",
            err=True,
        )
        sys.exit(1)

    response_payload: dict[str, Any] = json.loads(response["Payload"].read())

    if "errorMessage" in response_payload:
        msg = response_payload["errorMessage"]
        click.echo(f"Error: Lambda execution failed: {msg}", err=True)
        sys.exit(1)

    return response_payload
