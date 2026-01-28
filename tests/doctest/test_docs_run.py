"""Run Python code examples in documentation against moto."""

import pytest
from pytest_examples import CodeExample, EvalExample, find_examples

from tests.doctest.conftest import DOCS_EXAMPLES_CONFIG, SKIP_TAGS, should_skip


@pytest.mark.parametrize("example", find_examples("docs/"), ids=str)
def test_docs_run(example: CodeExample, eval_example: EvalExample, moto_env):
    """Run doc examples that don't require special infrastructure."""
    reason = should_skip(example, SKIP_TAGS)
    if reason:
        pytest.skip(reason)

    eval_example.config = DOCS_EXAMPLES_CONFIG
    eval_example.lint_ruff(example)

    try:
        eval_example.run(example)
    except Exception as e:
        pytest.fail(
            f"Example failed at {example.path}:{example.start_line}\n"
            f"Source:\n{example.source}\n"
            f"Error: {e}"
        )
