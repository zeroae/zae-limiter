"""Lint all Python code examples in documentation."""

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests.doctest.conftest import DOCS_EXAMPLES_CONFIG


@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_lint(example: CodeExample, eval_example: EvalExample):
    """Every Python code block in docs/ must pass ruff linting.

    Uses lint_ruff() directly since the project uses ruff for formatting,
    not black. The lint() method runs both black and ruff.
    """
    eval_example.config = DOCS_EXAMPLES_CONFIG
    eval_example.lint_ruff(example)
