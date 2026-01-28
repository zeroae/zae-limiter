"""Run Python code examples that require LocalStack."""

import os

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests.doctest.conftest import DOCS_EXAMPLES_CONFIG


@pytest.mark.integration
@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_integration(example: CodeExample, eval_example: EvalExample):
    """Run doc examples tagged with requires-localstack."""
    tags = example.prefix_tags()
    if "requires-localstack" not in tags:
        pytest.skip("not tagged requires-localstack")

    endpoint = os.environ.get("AWS_ENDPOINT_URL")
    if not endpoint:
        pytest.skip("AWS_ENDPOINT_URL not set (LocalStack not running)")

    eval_example.config = DOCS_EXAMPLES_CONFIG
    eval_example.lint_ruff(example)
    eval_example.run(example)
