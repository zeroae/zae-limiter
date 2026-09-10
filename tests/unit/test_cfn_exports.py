"""Regression tests for CloudFormation Output exports (issue #445).

CloudFormation rejects any exported Output whose value resolves to an empty or
whitespace-only string, and rolls the whole stack back::

    Cannot export output PermissionBoundaryArn.
    Exported values must not be empty or whitespace-only.

``zae-limiter deploy --name x --region us-east-1`` hit exactly that:
``PermissionBoundaryArn`` and ``RoleNameFormat`` carried unconditional
``Export:`` blocks while their source parameters default to ``''``.

LocalStack does not enforce the rule, so integration/e2e CI cannot catch this
class of failure. These tests evaluate the template's Conditions and Output
values for a given parameter set and assert every export that is actually
emitted carries a non-empty value.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from importlib.resources import files
from typing import Any

import pytest
import yaml


class _CfnLoader(yaml.SafeLoader):
    """YAML loader that preserves CloudFormation short-form intrinsics.

    The stock ``SafeLoader`` chokes on ``!Ref``/``!Sub``/... and the loader in
    ``test_cfn_iam_parity`` discards the tag name, which is exactly what we need
    to evaluate here. This one round-trips ``!Ref Foo`` to ``{"Ref": "Foo"}``.
    """


def _cfn_constructor(tag: str) -> Callable[[yaml.Loader, yaml.Node], dict[str, Any]]:
    key = tag if tag in ("Ref", "Condition") else f"Fn::{tag}"

    def construct(loader: yaml.Loader, node: yaml.Node) -> dict[str, Any]:
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
            if key == "Fn::GetAtt":
                value = value.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        else:
            value = loader.construct_mapping(node, deep=True)
        return {key: value}

    return construct


for _tag in (
    "Ref",
    "Condition",
    "Sub",
    "GetAtt",
    "If",
    "Select",
    "Split",
    "Join",
    "Equals",
    "Not",
    "And",
    "Or",
    "FindInMap",
    "ImportValue",
    "Base64",
):
    _CfnLoader.add_constructor(f"!{_tag}", _cfn_constructor(_tag))


# Pseudo-parameters resolve to representative, always non-empty values.
_PSEUDO = {
    "AWS::StackName": "test-stack",
    "AWS::Region": "us-east-1",
    "AWS::AccountId": "123456789012",
    "AWS::Partition": "aws",
    "AWS::URLSuffix": "amazonaws.com",
}


def _load_template() -> dict[str, Any]:
    text = files("zae_limiter.infra").joinpath("cfn_template.yaml").read_text()
    return yaml.load(text, Loader=_CfnLoader)


def _default_parameters(template: dict[str, Any]) -> dict[str, str]:
    """Parameter values for a deploy that passes no explicit parameters."""
    return {
        name: str(spec["Default"]) if "Default" in spec else f"<{name}>"
        for name, spec in template["Parameters"].items()
    }


def _resolve(node: Any, params: dict[str, str], condition: Callable[[str], bool]) -> Any:
    """Evaluate a CloudFormation expression against a concrete parameter set.

    ``Ref``/``GetAtt`` on a *resource* stands in for a value only known at
    deploy time; it is rendered as a non-empty placeholder, since a created
    resource never yields an empty physical id.
    """
    if isinstance(node, list):
        return [_resolve(item, params, condition) for item in node]
    if not isinstance(node, dict) or len(node) != 1:
        return node

    ((key, raw),) = node.items()

    if key == "Ref":
        if raw in _PSEUDO:
            return _PSEUDO[raw]
        return params[raw] if raw in params else f"<{raw}>"
    if key == "Condition":
        return condition(raw)
    if key == "Fn::GetAtt":
        parts = raw if isinstance(raw, list) else raw.split(".", 1)
        return "<{}>".format(".".join(parts))
    if key == "Fn::If":
        name, when_true, when_false = raw
        return _resolve(when_true if condition(name) else when_false, params, condition)
    if key == "Fn::Equals":
        left, right = (_resolve(item, params, condition) for item in raw)
        return left == right
    if key == "Fn::Not":
        return not _resolve(raw[0], params, condition)
    if key == "Fn::And":
        return all(_resolve(item, params, condition) for item in raw)
    if key == "Fn::Or":
        return any(_resolve(item, params, condition) for item in raw)
    if key == "Fn::Select":
        index, values = raw
        return _resolve(values, params, condition)[int(_resolve(index, params, condition))]
    if key == "Fn::Split":
        delimiter, source = raw
        return str(_resolve(source, params, condition)).split(
            str(_resolve(delimiter, params, condition))
        )
    if key == "Fn::Join":
        delimiter, values = raw
        return str(delimiter).join(str(v) for v in _resolve(values, params, condition))
    if key == "Fn::Sub":
        return _resolve_sub(raw, params, condition)

    raise AssertionError(f"unsupported intrinsic in template Outputs/Conditions: {key}")


def _resolve_sub(raw: Any, params: dict[str, str], condition: Callable[[str], bool]) -> str:
    if isinstance(raw, list):
        body, variables = raw[0], raw[1]
    else:
        body, variables = raw, {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in variables:
            return str(_resolve(variables[name], params, condition))
        if name in _PSEUDO:
            return _PSEUDO[name]
        if name in params:
            return params[name]
        return f"<{name}>"

    return re.sub(r"\$\{([^}]+)\}", replace, body)


def _evaluate_conditions(template: dict[str, Any], params: dict[str, str]) -> dict[str, bool]:
    resolved: dict[str, bool] = {}

    def condition(name: str) -> bool:
        if name not in resolved:
            resolved[name] = bool(_resolve(template["Conditions"][name], params, condition))
        return resolved[name]

    for name in template.get("Conditions", {}):
        condition(name)
    return resolved


def _emitted_exports(overrides: dict[str, str]) -> dict[str, str]:
    """Map export name -> resolved value for every export CloudFormation creates."""
    template = _load_template()
    params = {**_default_parameters(template), **overrides}
    conditions = _evaluate_conditions(template, params)
    condition = conditions.__getitem__

    exports: dict[str, str] = {}
    for name, spec in template["Outputs"].items():
        if "Export" not in spec:
            continue
        guard = spec.get("Condition")
        if guard is not None and not conditions[guard]:
            continue
        exports[name] = _resolve(spec["Value"], params, condition)
    return exports


BOUNDARY_ARN = "arn:aws:iam::aws:policy/PowerUserAccess"

# Each entry is a `zae-limiter deploy` invocation from the issue's repro list.
SCENARIOS: dict[str, dict[str, str]] = {
    "all-defaults": {},
    "no-iam-no-aggregator": {
        "EnableIAM": "false",
        "EnableAggregator": "false",
        "EnableProvisioner": "false",
    },
    "role-format-only": {"RoleNameFormat": "PowerUserPB-{}"},
    "boundary-only": {"PermissionBoundary": BOUNDARY_ARN},
    "boundary-by-name": {"PermissionBoundary": "PowerUserAccess"},
    "boundary-and-format": {
        "PermissionBoundary": BOUNDARY_ARN,
        "RoleNameFormat": "PowerUserPB-{}",
    },
    "no-alarms-no-archival": {"EnableAlarms": "false", "EnableAuditArchival": "false"},
}


@pytest.mark.parametrize("overrides", SCENARIOS.values(), ids=list(SCENARIOS))
def test_no_exported_output_is_empty(overrides: dict[str, str]) -> None:
    """CloudFormation rolls the stack back on an empty export (issue #445)."""
    exports = _emitted_exports(overrides)
    assert exports, "expected at least one export for every deploy configuration"

    empty = sorted(name for name, value in exports.items() if not str(value).strip())
    assert not empty, (
        f"Outputs {empty} export an empty or whitespace-only value. CloudFormation "
        "rejects these and rolls the stack back; gate the Output on a Condition."
    )


def test_iam_config_exports_omitted_when_unset() -> None:
    """The default deploy must not emit the two IAM-config exports at all."""
    exports = _emitted_exports({})
    assert "PermissionBoundaryArn" not in exports
    assert "RoleNameFormat" not in exports


def test_iam_config_exports_present_when_set() -> None:
    """Dependent stacks can still import both values when they are configured."""
    exports = _emitted_exports(SCENARIOS["boundary-and-format"])
    assert exports["PermissionBoundaryArn"] == BOUNDARY_ARN
    assert exports["RoleNameFormat"] == "PowerUserPB-{}"


def test_permission_boundary_name_is_expanded_to_an_arn() -> None:
    """A bare policy name is exported as a full ARN, partition-aware."""
    exports = _emitted_exports(SCENARIOS["boundary-by-name"])
    assert exports["PermissionBoundaryArn"] == ("arn:aws:iam::123456789012:policy/PowerUserAccess")
