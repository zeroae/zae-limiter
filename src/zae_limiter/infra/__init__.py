"""Infrastructure as code for zae-limiter."""

from pathlib import Path

CFN_TEMPLATE_PATH = Path(__file__).parent / "cfn_template.yaml"

# The provisioner Lambda's `Timeout` in cfn_template.yaml (pinned by a unit test).
PROVISIONER_LAMBDA_TIMEOUT_SECONDS = 300


def get_cfn_template() -> str:
    """Get the CloudFormation template as a string."""
    return CFN_TEMPLATE_PATH.read_text()
