"""Limit manifests taken from the documentation.

Read from the pages rather than copied, so a documented example cannot drift
from what the tests check.
"""

from pathlib import Path

_DOCS = Path(__file__).parents[2] / "docs"


def documented_yaml(page: str, marker: str) -> str:
    """The ``yaml`` code block on ``docs/<page>`` whose first line is ``# <marker>``."""
    text = (_DOCS / page).read_text()
    for block in text.split("```yaml\n")[1:]:
        body = block.split("```", 1)[0]
        if body.startswith(f"# {marker}\n"):
            return body
    raise AssertionError(f"no yaml block starting with '# {marker}' in docs/{page}")


def documented_anchor_manifest() -> str:
    """The ``limits-anchors.yaml`` example from the operator guide."""
    return documented_yaml("infra/deployment.md", "limits-anchors.yaml")
