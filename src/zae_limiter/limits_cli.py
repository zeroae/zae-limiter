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
    """Preview changes without applying (like terraform plan)."""
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "plan", manifest_data)

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
    """Show drift between YAML and live DynamoDB state."""
    manifest_data = _load_yaml(file_path)
    result = _invoke_provisioner(name, region, endpoint_url, "plan", manifest_data)

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
        properties["System"] = system_props

    if "resources" in manifest_data:
        resources_props = {}
        for res_name, res_data in manifest_data["resources"].items():
            resources_props[res_name] = {"Limits": _limits_to_cfn(res_data.get("limits", {}))}
        properties["Resources"] = resources_props

    if "entities" in manifest_data:
        entities_props = {}
        for ent_id, ent_data in manifest_data["entities"].items():
            ent_resources = {}
            for res_name, res_data in ent_data.get("resources", {}).items():
                ent_resources[res_name] = {"Limits": _limits_to_cfn(res_data.get("limits", {}))}
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


def _limits_to_cfn(limits: dict[str, Any]) -> dict[str, Any]:
    """Convert manifest limits dict to CFN PascalCase format."""
    result = {}
    for name, limit in limits.items():
        cfn_limit: dict[str, Any] = {"Capacity": limit["capacity"]}
        if "refill_amount" in limit:
            cfn_limit["RefillAmount"] = limit["refill_amount"]
        if "refill_period" in limit:
            cfn_limit["RefillPeriod"] = limit["refill_period"]
        result[name] = cfn_limit
    return result


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
) -> dict[str, Any]:
    """Invoke the provisioner Lambda function.

    Args:
        name: Stack name (used to derive Lambda function name and table name).
        region: AWS region.
        endpoint_url: AWS endpoint URL (for LocalStack).
        action: "plan" or "apply".
        manifest_data: Parsed YAML manifest as dict.

    Returns:
        Lambda response payload.
    """
    import asyncio

    from .exceptions import NamespaceNotFoundError
    from .repository import Repository

    async def _resolve() -> str:
        ns = manifest_data.get("namespace", "default")
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

    try:
        namespace_id = asyncio.run(_resolve())
    except Exception:
        namespace_id = ""

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

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )

    response_payload: dict[str, Any] = json.loads(response["Payload"].read())

    if "errorMessage" in response_payload:
        msg = response_payload["errorMessage"]
        click.echo(f"Error: Lambda execution failed: {msg}", err=True)
        sys.exit(1)

    return response_payload
