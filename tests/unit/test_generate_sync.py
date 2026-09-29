"""Tests for the async -> sync code generator (``scripts/generate_sync.py``).

These drive ``AsyncToSyncTransformer`` directly rather than asserting on the
committed generated files: the behaviour under test (issue #491) concerns input
the tree does not currently contain, so a "generated output is unchanged" check
would prove nothing.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_sync.py"


def _load_generator() -> ModuleType:
    """Import ``scripts/generate_sync.py`` (not an installed package) by path."""
    spec = importlib.util.spec_from_file_location("zae_generate_sync", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generate_sync = _load_generator()


def transform(source: str, *, source_file: str = "repository.py") -> str:
    """Run the async->sync transformer over `source` and return the sync code."""
    tree = ast.parse(source)
    transformer = generate_sync.AsyncToSyncTransformer(source_file)
    new_tree = transformer.visit(tree)
    ast.fix_missing_locations(new_tree)
    return ast.unparse(new_tree)


# The three distinct shapes the gather rewrite recognises, each reaching a
# different `keywords=[]` construction site in `visit_Call`.
GATHER_SHAPES = {
    "fixed_positional": "await asyncio.gather(work(1), work(2){kw})",
    "generic_starred": "await asyncio.gather(*tasks{kw})",
    "starred_listcomp": "await asyncio.gather(*[work(x) for x in items]{kw})",
}


def _wrap(call: str) -> str:
    return f"async def run(self):\n    return {call}\n"


# ---------------------------------------------------------------------------
# asyncio.gather: positional-only input still transforms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(GATHER_SHAPES))
def test_gather_positional_only_rewrites_to_run_in_executor(shape: str) -> None:
    """Every gather shape without keywords still becomes _run_in_executor."""
    result = transform(_wrap(GATHER_SHAPES[shape].format(kw="")))

    assert "self._run_in_executor(" in result
    assert "asyncio.gather" not in result


def test_gather_starred_listcomp_captures_loop_variable() -> None:
    """The listcomp path keeps its default-arg capture (no late-binding bug)."""
    result = transform(_wrap(GATHER_SHAPES["starred_listcomp"].format(kw="")))

    assert "lambda x=x: work(x)" in result


# ---------------------------------------------------------------------------
# asyncio.gather: keywords are rejected, not dropped (issue #491)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", sorted(GATHER_SHAPES))
def test_gather_return_exceptions_aborts_generation(shape: str) -> None:
    """Each construction site refuses `return_exceptions` rather than dropping it."""
    source = _wrap(GATHER_SHAPES[shape].format(kw=", return_exceptions=True"))

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source)

    message = str(excinfo.value)
    assert "return_exceptions" in message
    assert "asyncio.gather" in message


@pytest.mark.parametrize("shape", sorted(GATHER_SHAPES))
def test_gather_keyword_error_names_file_and_line(shape: str) -> None:
    """The error locates the offending call for the author."""
    source = "async def run(self):\n    x = 1\n    return " + GATHER_SHAPES[shape].format(
        kw=", return_exceptions=True"
    )

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="repository.py")

    # The gather call is on line 3 of the snippet above.
    assert "repository.py:3:" in str(excinfo.value)


def test_gather_error_suggests_the_portable_rewrite() -> None:
    """The message tells the author what to write instead."""
    source = _wrap(GATHER_SHAPES["starred_listcomp"].format(kw=", return_exceptions=True"))

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source)

    message = str(excinfo.value)
    assert "_run_in_executor" in message
    assert "except Exception" in message


@pytest.mark.parametrize("shape", sorted(GATHER_SHAPES))
def test_gather_double_star_kwargs_aborts_generation(shape: str) -> None:
    """A `**kwargs` splat has no `kw.arg`; it must still be rejected."""
    source = _wrap(GATHER_SHAPES[shape].format(kw=", **opts"))

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source)

    assert "**kwargs" in str(excinfo.value)


def test_gather_unknown_future_keyword_aborts_generation() -> None:
    """The guard rejects any keyword, not just the one we know about today."""
    source = _wrap("await asyncio.gather(*tasks, some_future_option=1)")

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source)

    assert "some_future_option" in str(excinfo.value)


# ---------------------------------------------------------------------------
# asyncio.wait_for: `timeout` is dropped by design, anything else is rejected
# ---------------------------------------------------------------------------


def test_wait_for_timeout_keyword_is_dropped_by_design() -> None:
    """Dropping the wrapper (and its timeout) is the documented rewrite."""
    result = transform(_wrap("await asyncio.wait_for(self.ping(), timeout=timeout)"))

    assert "self.ping()" in result
    assert "wait_for" not in result


def test_wait_for_positional_timeout_is_dropped_by_design() -> None:
    result = transform(_wrap("await asyncio.wait_for(self.ping(), 5)"))

    assert "self.ping()" in result
    assert "wait_for" not in result


def test_wait_for_unknown_keyword_aborts_generation() -> None:
    """Only `timeout` is an allowed casualty of the wait_for rewrite."""
    source = _wrap("await asyncio.wait_for(self.ping(), timeout=1, some_option=True)")

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source)

    message = str(excinfo.value)
    assert "some_option" in message
    assert "timeout" not in message.split("\n\n")[0]  # not listed as dropped


# ---------------------------------------------------------------------------
# Audit: the remaining rewrites in visit_Call must not drop keywords
# ---------------------------------------------------------------------------


def test_aenter_client_strip_preserves_client_keywords() -> None:
    """The `__aenter__` strip returns the inner call, keywords intact."""
    result = transform(
        _wrap('await session.create_client("dynamodb", region_name=region).__aenter__()')
    )

    assert "region_name=region" in result
    assert "__aenter__" not in result


def test_ordinary_call_keywords_are_preserved() -> None:
    """The guard must not disturb keywords on non-asyncio calls."""
    result = transform(_wrap("await self.client.update_item(Key=key, ReturnValues='ALL_NEW')"))

    assert "Key=key" in result
    assert "ReturnValues='ALL_NEW'" in result


# ---------------------------------------------------------------------------
# asyncio.gather over a comprehension: every target name is deferred (#666)
# ---------------------------------------------------------------------------


def _run_generated(comprehension: str, **env: object) -> tuple[list[object], list[object]]:
    """Generate the sync twin of ``gather(*<comprehension>)``, run it, return results.

    The executor records a marker before invoking any callable, so a sync twin
    that evaluated an element eagerly shows up as a ``call`` logged before
    ``"executor"`` -- and, since the eager result is then called, usually as a
    ``TypeError`` too.
    """
    async_source = (
        "class Worker:\n"
        "    async def run(self):\n"
        f"        return await asyncio.gather(*{comprehension})\n"
    )
    sync_source = transform(async_source)
    log: list[object] = []

    def _run_in_executor(self: object, *funcs: Callable[[], object]) -> list[object]:
        log.append("executor")
        assert all(callable(fn) for fn in funcs), funcs
        return [fn() for fn in funcs]

    def f(*args: object) -> object:
        log.append(("call", args))
        return sum(a for a in args if isinstance(a, int))

    namespace: dict[str, object] = {"f": f, **env}
    exec(compile(sync_source, "<generated>", "exec"), namespace)
    worker_cls: Any = namespace["Worker"]
    worker_cls._run_in_executor = _run_in_executor
    results: list[object] = worker_cls().run()
    return results, log


def test_gather_tuple_target_defers_every_bound_name() -> None:
    """The issue's shape: a tuple target is captured whole, and nothing runs eagerly."""
    source = _wrap("await asyncio.gather(*[f(a, b) for a, b in pairs])")
    assert "lambda a=a, b=b: f(a, b)" in transform(source)

    results, log = _run_generated("[f(a, b) for a, b in pairs]", pairs=[(1, 2), (3, 4)])

    assert results == [3, 7]
    assert log == ["executor", ("call", (1, 2)), ("call", (3, 4))]


@pytest.mark.parametrize(
    ("comprehension", "env", "expected_args"),
    [
        pytest.param(
            "[f(a, b, c) for a, (b, c) in rows]",
            {"rows": [(1, (2, 3)), (4, (5, 6))]},
            [(1, 2, 3), (4, 5, 6)],
            id="nested_tuple",
        ),
        pytest.param(
            "[f(a, b) for [a, b] in rows]",
            {"rows": [[1, 2], [3, 4]]},
            [(1, 2), (3, 4)],
            id="list_target",
        ),
        pytest.param(
            "[f(a, *rest) for a, *rest in rows]",
            {"rows": [(1, 2, 3), (4,)]},
            [(1, 2, 3), (4,)],
            id="starred_target",
        ),
        pytest.param(
            "[f(a, b) for a in xs for b in ys]",
            {"xs": [1, 2], "ys": [10, 20]},
            [(1, 10), (1, 20), (2, 10), (2, 20)],
            id="two_for_clauses",
        ),
        pytest.param(
            "[f(a, b) for a, b in pairs if a > 1]",
            {"pairs": [(1, 2), (3, 4)]},
            [(3, 4)],
            id="if_filter",
        ),
        pytest.param(
            "(f(a, b) for a, b in pairs)",
            {"pairs": [(1, 2), (3, 4)]},
            [(1, 2), (3, 4)],
            id="generator_expression",
        ),
        pytest.param(
            "[f(x) for x in xs]",
            {"xs": [5, 6]},
            [(5,), (6,)],
            id="single_name",
        ),
    ],
)
def test_gather_comprehension_calls_are_deferred_and_correct(
    comprehension: str, env: dict[str, object], expected_args: list[tuple[object, ...]]
) -> None:
    """Every translatable target shape runs each call inside the executor, once."""
    _, log = _run_generated(comprehension, **env)

    assert log == ["executor", *[("call", args) for args in expected_args]]


def test_gather_single_name_listcomp_output_is_unchanged() -> None:
    """The pre-#666 single-name translation is byte-identical."""
    result = transform(_wrap("await asyncio.gather(*[self._one(x) for x in items])"))

    assert "self._run_in_executor(*[lambda x=x: self._one(x) for x in items])" in result


def test_gather_generic_starred_name_is_unchanged() -> None:
    """A plain ``*tasks`` (not a comprehension) keeps the generic translation."""
    result = transform(_wrap("await asyncio.gather(*tasks)"))

    assert "self._run_in_executor(*[lambda fn=fn: fn() for fn in tasks])" in result


@pytest.mark.parametrize(
    ("comprehension", "construct"),
    [
        pytest.param("[f(x) for self.x in xs]", "self.x", id="attribute_target"),
        pytest.param("[f(x) for d[0] in xs]", "d[0]", id="subscript_target"),
        pytest.param("[f(x) async for x in xs]", "async for", id="async_for"),
        pytest.param("[f(y) for x in xs if (y := x)]", "y := x", id="walrus"),
    ],
)
def test_gather_untranslatable_comprehension_aborts_generation(
    comprehension: str, construct: str
) -> None:
    """A shape whose bindings cannot be captured aborts, naming file, line and construct."""
    source = "async def run(self):\n    x = 1\n    return " + (
        f"await asyncio.gather(*{comprehension})"
    )

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="repository.py")

    message = str(excinfo.value)
    assert "repository.py:3:" in message
    assert construct in message
