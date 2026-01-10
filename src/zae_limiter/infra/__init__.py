"""Infrastructure as code for zae-limiter."""

from pathlib import Path

CFN_TEMPLATE_PATH = Path(__file__).parent / "cfn_template.yaml"


def get_cfn_template() -> str:
    """Get the CloudFormation template as a string."""
    return CFN_TEMPLATE_PATH.read_text()
