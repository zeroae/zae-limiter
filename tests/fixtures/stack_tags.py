"""CloudFormation stack tag helpers for the upgrade e2e tests (#663).

``zae-limiter upgrade`` refreshes the stack's version tags with a tag-only
``UpdateStack``. These helpers read a deployed stack's tags and parameters, and
make its tags stale the same way a stack deployed by an older release carries
them, so a test can check what ``upgrade`` (and nothing else) changes.

Real AWS only: LocalStack 4.14 rejects ``UsePreviousTemplate`` ("Specify
exactly one of 'TemplateBody' or 'TemplateUrl'") and answers a tag-only update
with the template passed explicitly by "No updates are to be performed".
"""

from typing import Any


def stack_description(cfn: Any, stack: str) -> dict[str, Any]:
    stacks: list[dict[str, Any]] = cfn.describe_stacks(StackName=stack)["Stacks"]
    return stacks[0]


def stack_tags(cfn: Any, stack: str) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in stack_description(cfn, stack).get("Tags", [])}


def stack_parameters(cfn: Any, stack: str) -> dict[str, str]:
    return {
        p["ParameterKey"]: p.get("ParameterValue", "")
        for p in stack_description(cfn, stack).get("Parameters", [])
    }


def retag_stack(cfn: Any, stack: str, overrides: dict[str, str]) -> None:
    """Overwrite some stack tags, keeping the template, parameters and other tags.

    Waits for the update to finish, so the next step sees the new tags on the
    stack and propagated to its resources.
    """
    description = stack_description(cfn, stack)
    tags = {t["Key"]: t["Value"] for t in description.get("Tags", [])}
    tags.update(overrides)
    cfn.update_stack(
        StackName=stack,
        UsePreviousTemplate=True,
        Parameters=[
            {"ParameterKey": p["ParameterKey"], "UsePreviousValue": True}
            for p in description.get("Parameters", [])
        ],
        Tags=[{"Key": k, "Value": v} for k, v in tags.items() if not k.startswith("aws:")],
        Capabilities=["CAPABILITY_NAMED_IAM"],
    )
    cfn.get_waiter("stack_update_complete").wait(
        StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 90}
    )
