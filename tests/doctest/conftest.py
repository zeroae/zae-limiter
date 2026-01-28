"""Configuration for documentation example tests."""

import pytest
from pytest_examples.config import ExamplesConfig

# Ruff config for doc examples: ignore rules that are inherent to documentation style.
# - F704: yield/await outside function (async examples use bare await)
# - F706: return outside function (snippets showing return values)
# - F811: redefinition of unused name (multiple examples redefine same var)
# - F821: undefined name (snippets reference variables from prose context)
# - F841: local variable assigned but never used (illustrative assignments)
# - E741: ambiguous variable name (e.g., single-letter vars in examples)
# - I001: import not sorted (cosmetic, doc examples prioritize readability)
# - F401: imported but unused (doc examples import for illustration)
# - N807: function name should not start/end with __ (e.g., __version__)
DOCS_EXAMPLES_CONFIG = ExamplesConfig(
    line_length=100,
    target_version="py311",
    ruff_select=["E", "F", "I", "N", "W", "UP"],
    ruff_ignore=["F401", "F704", "F706", "F811", "F821", "F841", "E741", "I001", "N807"],
)


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    """Add doctest marker to all items in this directory."""
    for item in items:
        if "doctest" in str(item.fspath):
            item.add_marker(pytest.mark.doctest)
