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


# Three gather shapes. The keyword guard runs before any of them is handled,
# so all three must refuse a keyword. `generic_starred` is not a translatable
# shape at all (#666): without a keyword it aborts on its own, see below.
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


TRANSLATABLE_GATHER_SHAPES = sorted(set(GATHER_SHAPES) - {"generic_starred"})


@pytest.mark.parametrize("shape", TRANSLATABLE_GATHER_SHAPES)
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


@pytest.mark.parametrize(
    "starred",
    [
        pytest.param("tasks", id="name"),
        pytest.param("[self._a(), self._b()]", id="list_display"),
        pytest.param("{f(x) for x in xs}", id="set_comprehension"),
        pytest.param("map(f, xs)", id="map_call"),
    ],
)
def test_gather_non_comprehension_starred_aborts_generation(starred: str) -> None:
    """A starred iterable of already-built awaitables has nothing left to defer.

    Translating it would call every element eagerly and then call its result
    (#666), so generation aborts, naming file, line and the argument.
    """
    source = "async def run(self):\n    x = 1\n    return " + (f"await asyncio.gather(*{starred})")

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="repository.py")

    message = str(excinfo.value)
    assert "repository.py:3:" in message
    assert f"`{starred}`" in message
    assert "list comprehension" in message


def test_gather_tasks_list_built_earlier_aborts_generation() -> None:
    """The `tasks = [...]; gather(*tasks)` idiom aborts rather than running eagerly."""
    source = (
        "async def run(self):\n"
        "    tasks = [self._one(x) for x in items]\n"
        "    return await asyncio.gather(*tasks)\n"
    )

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="repository.py")

    assert "repository.py:3:" in str(excinfo.value)


@pytest.mark.parametrize(
    ("comprehension", "construct", "remedy"),
    [
        pytest.param(
            "[f(x) for self.x in xs]", "self.x", "Bind a plain name", id="attribute_target"
        ),
        pytest.param("[f(x) for d[0] in xs]", "d[0]", "Bind a plain name", id="subscript_target"),
        pytest.param(
            "[f(x) async for x in xs]", "async for", "Materialise the async", id="async_for"
        ),
        pytest.param("[f(y) for x in xs if (y := x)]", "y := x", "Compute the value", id="walrus"),
    ],
)
def test_gather_untranslatable_comprehension_aborts_generation(
    comprehension: str, construct: str, remedy: str
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
    assert remedy in message


# ---------------------------------------------------------------------------
# asyncio.create_task -> self._spawn (background tasks)
# ---------------------------------------------------------------------------


def test_create_task_to_an_attribute_spawns_and_its_await_is_result() -> None:
    source = (
        "class Lease:\n"
        "    async def start(self, checks):\n"
        "        self.task = asyncio.create_task(self._work(checks, 1))\n"
        "    async def finish(self):\n"
        "        await self.task\n"
        "        self.task.cancel()\n"
    )
    result = transform(source)

    assert "self.task = self._spawn(lambda self=self, checks=checks: self._work(checks, 1))" in (
        result
    )
    assert "self.task.result()" in result
    assert "self.task.cancel()" in result
    assert "create_task" not in result


def test_create_task_to_a_local_name_captures_its_arguments() -> None:
    source = (
        "async def run(self, item):\n"
        "    task = asyncio.create_task(work(item, key=item))\n"
        "    return await task\n"
    )
    result = transform(source)

    assert "task = self._spawn(lambda work=work, item=item: work(item, key=item))" in result
    assert "return task.result()" in result


def test_a_handle_returned_and_awaited_through_an_annotated_field() -> None:
    """A field annotated `asyncio.Task[...]` is tracked across classes and modules."""
    source = (
        "class Repository:\n"
        "    def start(self, checks) -> asyncio.Task[None]:\n"
        "        return asyncio.create_task(self._work(checks))\n"
        "class Lease:\n"
        "    task: asyncio.Task[None] | None = None\n"
        "    async def finish(self):\n"
        "        if self.task is not None:\n"
        "            await self.task\n"
    )
    result = transform(source)

    assert "return self._spawn(lambda self=self, checks=checks: self._work(checks))" in result
    assert "task: concurrent.futures.Future[None] | None = None" in result
    assert "self.task.result()" in result
    assert "-> concurrent.futures.Future[None]" in result


def test_an_untracked_await_keeps_its_plain_rewrite() -> None:
    """Only a tracked handle's await becomes `.result()`; everything else is unchanged."""
    result = transform("async def run(self):\n    return await self.other()\n")

    assert "return self.other()" in result


@pytest.mark.parametrize(
    ("statement", "construct"),
    [
        pytest.param(
            "self.t = asyncio.create_task(work(x), name='w')", "keyword argument(s) name", id="name"
        ),
        pytest.param(
            "self.t = asyncio.create_task(work(x), **opts)", "keyword argument(s) **kwargs", id="kw"
        ),
        pytest.param("self.t = asyncio.create_task(coro)", "not a single call", id="non_call"),
        pytest.param(
            "self.t = asyncio.create_task(work(self.x))",
            "argument `self.x` is not a name or a constant",
            id="attribute_argument",
        ),
        pytest.param(
            "self.t = asyncio.create_task(work(**opts))", "`**kwargs` in the call", id="call_kwargs"
        ),
        pytest.param(
            "self.t = asyncio.create_task(factory()(x))",
            "callee `factory()` is not a name",
            id="computed_callee",
        ),
        pytest.param(
            "self.t = asyncio.create_task(work(key=items[0]))",
            "keyword `key` `items[0]` is not a name",
            id="subscript_keyword",
        ),
        pytest.param(
            "self.tasks.append(asyncio.create_task(work(x)))",
            "its handle is not assigned",
            id="container",
        ),
        pytest.param("asyncio.create_task(work(x))", "its handle is not assigned", id="bare"),
        pytest.param(
            "self.tasks[0] = asyncio.create_task(work(x))",
            "its handle is not assigned",
            id="subscript_target",
        ),
        pytest.param(
            "return asyncio.create_task(work(x))",
            "not annotated to return",
            id="unannotated_return",
        ),
    ],
)
def test_untranslatable_create_task_aborts_generation(statement: str, construct: str) -> None:
    """Any other create_task use aborts, naming file, line and construct (#491, #666)."""
    source = f"async def run(self, x, items, opts, coro):\n    {statement}\n"

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="lease.py")

    message = str(excinfo.value)
    assert "lease.py:2:" in message
    assert construct in message
    assert "self._spawn(" in message  # the help names the translation


def test_create_task_inside_nested_blocks_is_tracked() -> None:
    source = (
        "async def run(self, x):\n"
        "    try:\n"
        "        if x:\n"
        "            task = asyncio.create_task(work(x))\n"
        "    except ValueError:\n"
        "        self.t: asyncio.Task[None] = asyncio.create_task(work(x))\n"
        "    finally:\n"
        "        pass\n"
        "    await task\n"
        "    await self.t\n"
    )
    result = transform(source)

    assert "task.result()" in result
    assert "self.t.result()" in result


@pytest.mark.parametrize(
    ("use", "construct"),
    [
        pytest.param(
            "await asyncio.wait_for(task, 1)", "used in `asyncio.wait_for(", id="wait_for"
        ),
        pytest.param("await asyncio.gather(task)", "used in `asyncio.gather(task)`", id="gather"),
        pytest.param("consume(task)", "used in `consume(task)`", id="call_argument"),
        pytest.param("self.tasks = [task]", "used in `[task]`", id="container"),
        pytest.param("return task", "used in `return task`", id="unannotated_return"),
        pytest.param("task.add_done_callback(f)", "used in `task.add_done_callback`", id="method"),
        pytest.param("task.cancel('why')", "used in `task.cancel`", id="cancel_with_msg"),
        pytest.param("ok = task == other", "used in `task == other`", id="compare"),
    ],
)
def test_a_handle_used_any_other_way_aborts_generation(use: str, construct: str) -> None:
    """A `Future` keeps only a task's await, cancel and None checks (#716)."""
    source = f"async def run(self, f, other):\n    task = asyncio.create_task(work())\n    {use}\n"

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="lease.py")

    message = str(excinfo.value)
    assert "lease.py:3:" in message
    assert "the task handle `task`" in message
    assert construct in message


def test_a_handle_may_be_compared_with_none_and_returned_when_annotated() -> None:
    source = (
        "async def run(self) -> asyncio.Task[None]:\n"
        "    task = asyncio.create_task(work())\n"
        "    if task is not None and None is not task:\n"
        "        task.cancel()\n"
        "    return task\n"
    )
    result = transform(source)

    assert "if task is not None and None is not task:" in result
    assert "return task" in result


def test_handles_are_scoped_to_their_function_and_class() -> None:
    """A local of the same spelling elsewhere is not a handle: its await stays plain."""
    source = (
        "class Lease:\n"
        "    task: asyncio.Task[None] | None = None\n"
        "    async def finish(self):\n"
        "        await self.task\n"
        "async def spawn():\n"
        "    t = asyncio.create_task(work())\n"
        "    await t\n"
        "async def plain(t):\n"
        "    await t\n"
    )
    result = transform(source)

    assert result.count("self.task.result()") == 1
    assert result.count("t.result()") == 1
    assert result.rstrip().endswith("def plain(t):\n    t")  # the await's operand, unchanged


def test_another_class_reading_a_task_attribute_name_aborts() -> None:
    """An attribute is not scoped like a local: another class's `self.task` could be the
    same field (a subclass, a mixin), so it fails closed instead of reading as plain."""
    source = (
        "class Lease:\n"
        "    task: asyncio.Task[None] | None = None\n"
        "class Other:\n"
        "    async def finish(self):\n"
        "        await self.task\n"
    )

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError, match="`self.task` reads"):
        transform(source)


def test_a_nested_function_sees_the_enclosing_handle() -> None:
    source = (
        "async def run():\n"
        "    t = asyncio.create_task(work())\n"
        "    async def join():\n"
        "        await t\n"
        "    await join()\n"
    )

    assert "t.result()" in transform(source)


@pytest.mark.parametrize(
    "annotation",
    ["asyncio.TaskGroup", "asyncio.Tasks[None]", "my.asyncio.Task[None]", "'asyncio.TaskGroup'"],
)
def test_only_an_exact_asyncio_task_annotation_names_a_handle(annotation: str) -> None:
    source = (
        f"class Lease:\n    group: {annotation}\n"
        "    async def finish(self):\n        await self.group\n"
    )

    assert "self.group.result()" not in transform(source)


@pytest.mark.parametrize(
    "annotation",
    ["asyncio.Task", "'asyncio.Task[None]'", "Optional[asyncio.Task[None]]", "None | asyncio.Task"],
)
def test_an_asyncio_task_annotation_names_a_handle_in_every_spelling(annotation: str) -> None:
    source = (
        f"class Lease:\n    t: {annotation}\n    async def finish(self):\n        await self.t\n"
    )

    assert "self.t.result()" in transform(source)


def test_an_unparseable_quoted_annotation_names_no_handle() -> None:
    source = (
        "class Lease:\n    t: 'not valid ('\n    async def finish(self):\n        await self.t\n"
    )

    assert "self.t.result()" not in transform(source)


# A handle the tracker cannot see fails closed (#716 review, shapes A-D)

_TASK_METHOD = (
    "class Repo:\n"
    "    def start(self, work) -> asyncio.Task[None]:\n"
    "        return asyncio.create_task(work())\n"
)
_TASK_FIELD = "class Lease:\n    t: asyncio.Task[None] | None = None\n"


@pytest.mark.parametrize(
    ("source", "line", "construct"),
    [
        pytest.param(
            _TASK_METHOD + "async def run(repo, work):\n    t = repo.start(work)\n    await t\n",
            5,
            "`repo.start(work)` returns an `asyncio.Task`",
            id="A_unannotated_local",
        ),
        pytest.param(
            _TASK_FIELD + "async def exit(lease):\n    await lease.t\n",
            4,
            "`lease.t` reads a task attribute outside the class",
            id="B_other_object",
        ),
        pytest.param(
            _TASK_FIELD + "class Child(Lease):\n    async def join(self):\n        await self.t\n",
            5,
            "`self.t` reads a task attribute outside the class",
            id="C_subclass",
        ),
        pytest.param(
            _TASK_FIELD + "async def exit(lease):\n    await asyncio.wait_for(lease.t, 5)\n",
            4,
            "`lease.t` reads a task attribute outside the class",
            id="D_wait_for_other_object",
        ),
        pytest.param(
            _TASK_METHOD + "async def run(repo, work):\n    consume(repo.start(work))\n",
            5,
            "`repo.start(work)` returns an `asyncio.Task`",
            id="call_result_as_argument",
        ),
    ],
)
def test_a_handle_the_tracker_cannot_see_aborts_generation(
    source: str, line: int, construct: str
) -> None:
    """Translated as a plain value these would silently drop the join: fail closed."""
    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="lease.py")

    message = str(excinfo.value)
    assert f"lease.py:{line}:" in message
    assert construct in message


def test_a_task_method_result_landing_where_it_is_tracked_translates() -> None:
    source = _TASK_METHOD + (
        "class Lease:\n"
        "    t: asyncio.Task[None] | None = None\n"
        "    async def run(self, repo, work):\n"
        "        self.t = repo.start(work)\n"
        "        local: asyncio.Task[None] = repo.start(work)\n"
        "        await local\n"
        "        await self.t\n"
        "    def again(self, repo, work) -> asyncio.Task[None]:\n"
        "        return repo.start(work)\n"
    )
    result = transform(source)

    assert "local.result()" in result
    assert "self.t.result()" in result


def test_the_registry_spans_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A field annotated in another generator-covered file is still a task attribute."""
    monkeypatch.setattr(generate_sync, "TASK_REGISTRY", ({"t"}, {"start"}))

    with pytest.raises(generate_sync.UnsupportedAsyncConstructError, match="`lease.t` reads"):
        transform("async def exit(lease):\n    await lease.t\n")
    with pytest.raises(generate_sync.UnsupportedAsyncConstructError, match="returns an"):
        transform("async def run(repo):\n    t = repo.start()\n")


def test_main_fills_the_registry_from_every_covered_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    src, tests = tmp_path / "src", tmp_path / "tests"
    src.mkdir()
    tests.mkdir()
    (src / "a.py").write_text(_TASK_FIELD)
    (tests / "test_a.py").write_text(_TASK_METHOD)
    registry: tuple[set[str], set[str]] = (set(), set())
    monkeypatch.setattr(generate_sync, "TASK_REGISTRY", registry)
    foreign: dict[str, str] = {}
    monkeypatch.setattr(generate_sync, "FOREIGN_TASK_NAMES", foreign)
    (src / "helper.py").write_text(
        "def later(work):\n    return asyncio.create_task(work())\n"
        "class Box:\n    held: asyncio.Task[None]\n"
    )
    monkeypatch.setattr(generate_sync, "SRC", src)
    monkeypatch.setattr(generate_sync, "TESTS", tests)
    monkeypatch.setattr(
        generate_sync, "SOURCE_TRANSFORMS", [("a.py", "sync_a.py"), ("gone.py", "x.py")]
    )
    monkeypatch.setattr(generate_sync, "TEST_TRANSFORMS", [("test_a.py", "test_sync_a.py")])
    monkeypatch.setattr(generate_sync, "transform_file", lambda *_a, **_k: False)

    generate_sync.main()

    assert registry == ({"t"}, {"start"})
    # Everything under SRC's parent that is not a transformed source is foreign
    # (in this layout that includes tests/; the real tree keeps tests outside src/).
    assert foreign == {
        "later": "src/helper.py",
        "held": "src/helper.py",
        "start": "tests/test_a.py",
    }


# Shapes that hide a task from the tracker abort instead of dropping the join
# (#716 review, item 2)

_START_BACKGROUND = (
    "class Repository:\n"
    "    def start_background(self, work) -> asyncio.Task[None]:\n"
    "        task: asyncio.Task[None] = asyncio.create_task(work())\n"
    "        return task\n"
)
_OWNER_FIELD = "class Lease:\n    _owner_task: asyncio.Task[None] | None = None\n"


def _aborts(source: str) -> str:
    with pytest.raises(generate_sync.UnsupportedAsyncConstructError) as excinfo:
        transform(source, source_file="lease.py")
    return str(excinfo.value)


def test_a_bound_method_alias_of_a_task_function_aborts() -> None:
    message = _aborts(
        _START_BACKGROUND + _OWNER_FIELD + "    async def run(self, work):\n"
        "        f = self.repository.start_background\n"
        "        self._owner_task = f(work)\n"
    )

    assert "referenced without being called (a bound-method alias)" in message


def test_a_task_name_from_an_unscanned_module_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(generate_sync, "FOREIGN_TASK_NAMES", {"spawn_later": "zae_limiter/x.py"})

    message = _aborts("async def run(helpers, work):\n    helpers.spawn_later(work)\n")

    assert "defined in zae_limiter/x.py, a module the generator does not scan" in message


@pytest.mark.parametrize(
    "use",
    [
        'await getattr(self, "_owner_task")',
        'setattr(self, "_owner_task", None)',
        'delattr(self, "_owner_task")',
    ],
)
def test_a_task_attribute_reached_through_getattr_aborts(use: str) -> None:
    message = _aborts(_OWNER_FIELD + f"    async def run(self):\n        {use}\n")

    assert "the task attribute `_owner_task` is reached through" in message


@pytest.mark.parametrize("kind", ["nonlocal", "global"])
def test_a_handle_rebound_through_nonlocal_or_global_aborts(kind: str) -> None:
    if kind == "nonlocal":
        source = (
            "async def run(work):\n"
            "    t = asyncio.create_task(work())\n"
            "    def reset():\n"
            "        nonlocal t\n"
            "        t = None\n"
            "    await t\n"
        )
    else:
        source = "t = asyncio.create_task(work())\nasync def run():\n    global t\n    await t\n"

    message = _aborts(source)

    assert f"is rebound through `{kind}`" in message


_GETATTR_PREFIX = (
    _START_BACKGROUND + _OWNER_FIELD + "    async def run(self, work):\n"
    '        start = getattr(self.repository, "start_background", None)\n'
    "        if start is None:\n"
    "            return\n"
)


def test_the_lease_getattr_pattern_still_generates() -> None:
    """lease.py's `_start_owner_checks`: the result lands in a registered task attribute."""
    result = transform(_GETATTR_PREFIX + "        self._owner_task = start(work)\n")

    assert "start = getattr(self.repository, 'start_background', None)" in result
    assert "self._owner_task = start(work)" in result


@pytest.mark.parametrize(
    ("tail", "construct"),
    [
        pytest.param("        start(work)\n", "is not assigned directly", id="discarded"),
        pytest.param("        h = start(work)\n", "is not assigned directly", id="plain_local"),
        pytest.param("        return start\n", "escapes in `return start`", id="escapes"),
    ],
)
def test_a_getattr_fetched_task_function_elsewhere_aborts(tail: str, construct: str) -> None:
    message = _aborts(_GETATTR_PREFIX + tail)

    assert "fetched through `getattr`" in message
    assert construct in message


def test_a_getattr_fetched_task_function_not_bound_to_a_local_aborts() -> None:
    message = _aborts(
        _START_BACKGROUND + _OWNER_FIELD + "    async def run(self, work):\n"
        '        self._owner_task = getattr(self.repository, "start_background")(work)\n'
    )

    assert "without being bound to a local" in message
