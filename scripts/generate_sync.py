#!/usr/bin/env python3
"""Generate sync versions of async modules using AST transformation.

This script transforms async code (aioboto3) to sync code (boto3) by:
- Removing async/await keywords
- Renaming classes (RateLimiter -> SyncRateLimiter)
- Rewriting imports (aioboto3 -> boto3)
- Converting asyncio.gather() to self._run_in_executor() with configurable parallel_mode
- Injecting parallel_mode parameter and executor methods into SyncRepository

parallel_mode controls the execution strategy for concurrent operations (e.g., cascade):
- "auto" (default): gevent if monkey-patched, serial if single-CPU, threadpool if multi-CPU
- "gevent": forces gevent greenlets; warns (not errors) if monkey-patching is not active
- "threadpool": lazy ThreadPoolExecutor; warns on single-CPU hosts about GIL contention
- "serial": sequential execution (no parallelism)
All explicit modes warn on suboptimal conditions instead of raising errors.

Generated files are committed to git and verified by CI.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

# Project root
ROOT = Path(__file__).parent.parent
SRC = ROOT / "src" / "zae_limiter"
TESTS = ROOT / "tests" / "unit"

# Files to transform: (source, target)
SOURCE_TRANSFORMS = [
    ("repository_protocol.py", "sync_repository_protocol.py"),
    ("repository.py", "sync_repository.py"),
    ("limiter.py", "sync_limiter.py"),
    ("lease.py", "sync_lease.py"),
    ("config_cache.py", "sync_config_cache.py"),
    ("infra/stack_manager.py", "infra/sync_stack_manager.py"),
    ("infra/discovery.py", "infra/sync_discovery.py"),
]

# Test files to transform: (source, target) relative to TESTS
TEST_TRANSFORMS = [
    ("test_limiter.py", "test_sync_limiter.py"),
    ("test_repository.py", "test_sync_repository.py"),
    ("test_stack_manager.py", "test_sync_stack_manager.py"),
    ("test_discovery.py", "test_sync_discovery.py"),
    ("test_config_cache.py", "test_sync_config_cache.py"),
]

# Class renames
CLASS_RENAMES = {
    "RepositoryProtocol": "SyncRepositoryProtocol",
    "RateLimiter": "SyncRateLimiter",
    "Repository": "SyncRepository",
    "Lease": "SyncLease",
    "ConfigCache": "SyncConfigCache",
    "StackManager": "SyncStackManager",
    "InfrastructureDiscovery": "SyncInfrastructureDiscovery",
}

# Import module rewrites (also used for Name references like aioboto3.Session)
IMPORT_MODULE_REWRITES = {
    "aioboto3": "boto3",
}

# Method name rewrites for context manager calls
METHOD_NAME_REWRITES = {
    "__aenter__": "__enter__",
    "__aexit__": "__exit__",
}

# Attribute name rewrites (e.g., _async_lock -> _sync_lock)
ATTRIBUTE_NAME_REWRITES = {
    "_async_lock": "_sync_lock",
}

# Attribute access rewrites (module.attr -> replacement)
# Used for asyncio.Lock -> threading.Lock, asyncio.sleep -> time.sleep
ATTRIBUTE_ACCESS_REWRITES = {
    ("asyncio", "Lock"): ("threading", "Lock"),
    ("asyncio", "sleep"): ("time", "sleep"),
}

# Import path rewrites (for relative imports)
IMPORT_PATH_REWRITES = {
    ".repository_protocol": ".sync_repository_protocol",
    ".repository": ".sync_repository",
    ".lease": ".sync_lease",
    ".config_cache": ".sync_config_cache",
    ".infra.stack_manager": ".infra.sync_stack_manager",
    ".infra.discovery": ".infra.sync_discovery",
}

# Names to rewrite in from imports
IMPORT_NAME_REWRITES = {
    "RepositoryProtocol": "SyncRepositoryProtocol",
    "Repository": "SyncRepository",
    "Lease": "SyncLease",
    "ConfigCache": "SyncConfigCache",
    "StackManager": "SyncStackManager",
    "InfrastructureDiscovery": "SyncInfrastructureDiscovery",
    # Decorator rewrites
    "asynccontextmanager": "contextmanager",
}

# Type annotation rewrites
TYPE_REWRITES = {
    "AsyncIterator": "Iterator",
    "AsyncContextManager": "ContextManager",
    "AsyncGenerator": "Generator",
}

# Subscript type unwrapping (e.g., Awaitable[X] -> X, Coroutine[Any, Any, X] -> X)
UNWRAP_SUBSCRIPTS = {"Awaitable", "Coroutine"}

# Methods injected into SyncRepository for parallel execution (issue #318)
# asyncio.gather() is transformed to self._run_in_executor(lambda: a, lambda: b)
# and the method is injected into SyncRepository by visit_ClassDef.
# parallel_mode parameter ("auto", "gevent", "threadpool", "serial") controls strategy.
# "auto" checks: gevent patched -> serial (single-CPU) -> threadpool (multi-CPU).
# Explicit modes warn on suboptimal conditions (no errors).
# Resolution happens once at __init__ time; threadpool is created lazily on first use.
_EXECUTOR_METHODS = """\
@staticmethod
def _resolve_parallel_mode(mode: str) -> Any:
    if mode == "auto":
        try:
            from gevent import monkey, spawn, joinall
            if monkey.is_module_patched("socket"):
                def _executor(funcs: Any) -> Any:
                    greenlets = [spawn(fn) for fn in funcs]
                    joinall(greenlets, raise_error=True)
                    return tuple(g.value for g in greenlets)
                return _executor
        except ImportError:
            logger.debug("gevent not available; falling back to non-gevent strategy")
        import os
        if os.cpu_count() == 1:
            return lambda funcs: tuple(fn() for fn in funcs)  # serial on single-CPU
        return None  # threadpool
    elif mode == "gevent":
        from gevent import monkey, spawn, joinall
        if not monkey.is_module_patched("socket"):
            import warnings
            warnings.warn(
                "parallel_mode='gevent' without monkey-patching runs like serial. "
                "Call gevent.monkey.patch_all() before creating SyncRepository, "
                "or use parallel_mode='auto'.",
                stacklevel=3,
            )
        def _executor(funcs: Any) -> Any:
            greenlets = [spawn(fn) for fn in funcs]
            joinall(greenlets, raise_error=True)
            return tuple(g.value for g in greenlets)
        return _executor
    elif mode == "threadpool":
        import os
        if os.cpu_count() == 1:
            import warnings
            warnings.warn(
                "parallel_mode='threadpool' on a single-CPU host may cause GIL contention. "
                "Consider parallel_mode='auto' or 'serial'.",
                stacklevel=3,
            )
        return None  # sentinel: create ThreadPoolExecutor lazily
    elif mode == "serial":
        return lambda funcs: tuple(fn() for fn in funcs)
    else:
        raise ValueError(
            f"Invalid parallel_mode: {mode!r}. "
            "Must be 'auto', 'gevent', 'threadpool', or 'serial'."
        )

def _run_in_executor(self, *funcs: Any) -> Any:
    executor_fn = self._executor_fn
    if executor_fn is not None:
        return executor_fn(funcs)
    # Lazy ThreadPoolExecutor creation (threadpool mode only)
    if self._thread_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        self._thread_pool = ThreadPoolExecutor(max_workers=2)
    futures = [self._thread_pool.submit(fn) for fn in funcs]
    return tuple(f.result() for f in futures)

def _cleanup_thread_pool(self) -> None:
    pool = getattr(self, "_thread_pool", None)
    if pool is not None:
        pool.shutdown(wait=False)
        self._thread_pool = None

def __del__(self) -> None:
    self._cleanup_thread_pool()
"""

# Statements injected into SyncRepository.__init__ for parallel_mode support.
_INIT_PARALLEL_STMTS = """\
self._parallel_mode = parallel_mode
self._executor_fn = self._resolve_parallel_mode(parallel_mode)
self._thread_pool: Any = None
"""

# Methods/functions to remove (already have sync equivalents)
REMOVE_METHODS = {
    "get_system_defaults_sync",
    "get_resource_defaults_sync",
    "get_entity_limits_sync",
    "invalidate_sync",
}

# Module-level assignments to remove (unused in sync code).
# These are removed during cleanup if ruff doesn't catch them.
REMOVE_ASSIGNMENTS = {"_T"}

# Classes to skip (import from async module instead of duplicating).
# Prevents enum identity mismatches when both modules define the same enum.
# Maps class name -> source module for the import statement.
SKIP_CLASS_DEFINITIONS = {
    "OnUnavailable": ".limiter",
    "CacheStats": ".config_cache",
}

# Test-specific: fixture name rewrites (parameter names in test functions)
FIXTURE_NAME_REWRITES = {
    "limiter": "sync_limiter",
    "repository": "sync_repository",
}

# Test-specific: decorators to remove
REMOVE_DECORATORS = {"pytest.mark.asyncio"}

# Test-specific: method name rewrites (remove _sync suffix methods that
# were removed from sync classes since the main method IS already sync)
# Test functions that cannot be auto-transformed (rely on async-only patterns)
TEST_SKIP_FUNCTIONS = {
    # Uses asyncio.gather for concurrency - no sync equivalent
    "test_async_operations_are_concurrent_safe",
    # Uses asyncio.wait_for timeout cancellation - time.sleep blocks the thread
    "test_is_available_returns_false_on_timeout",
    # Tests async client __aexit__ cleanup - sync boto3 clients don't use context managers
    "test_close_cleans_up_client",
    # Uses asyncio.Barrier + asyncio.gather for concurrent leases - no sync equivalent
    "test_concurrent_adjust_no_lost_tokens",
}

TEST_METHOD_NAME_REWRITES = {
    "get_system_defaults_sync": "get_system_defaults",
    "get_resource_defaults_sync": "get_resource_defaults",
    "get_entity_limits_sync": "get_entity_limits",
    "invalidate_sync": "invalidate",
}

# Test-specific: additional class renames for imports
# (Classes already in IMPORT_NAME_REWRITES are handled by the base class.)
TEST_IMPORT_NAME_REWRITES = {
    "AsyncMock": "MagicMock",
    "RateLimiter": "SyncRateLimiter",
}

# Test-specific: additional import path rewrites
TEST_IMPORT_PATH_REWRITES = {
    "zae_limiter.infra.discovery": "zae_limiter.infra.sync_discovery",
    "zae_limiter.infra.stack_manager": "zae_limiter.infra.sync_stack_manager",
    "zae_limiter.repository": "zae_limiter.sync_repository",
    "zae_limiter.limiter": "zae_limiter.sync_limiter",
    "zae_limiter.config_cache": "zae_limiter.sync_config_cache",
    "zae_limiter.lease": "zae_limiter.sync_lease",
}

GENERATED_HEADER = '''"""AUTO-GENERATED by scripts/generate_sync.py - DO NOT EDIT.

Source: {source_file}

This module provides synchronous versions of the async classes.
Changes should be made to the source file, then regenerated.
"""

'''


class AsyncToSyncTransformer(ast.NodeTransformer):
    """Transform async Python code to sync."""

    def __init__(self, source_file: str):
        self.source_file = source_file
        super().__init__()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.FunctionDef:  # noqa: N802
        """Convert async def to def."""
        # Transform the body first
        new_body = [self.visit(stmt) for stmt in node.body]

        # Handle __aenter__ -> __enter__, __aexit__ -> __exit__
        new_name = node.name
        if new_name == "__aenter__":
            new_name = "__enter__"
        elif new_name == "__aexit__":
            new_name = "__exit__"

        # Transform argument annotations (for Awaitable[X] -> X, etc.)
        for arg in node.args.args:
            if arg.annotation:
                arg.annotation = self._transform_annotation(arg.annotation)
        for arg in node.args.posonlyargs:
            if arg.annotation:
                arg.annotation = self._transform_annotation(arg.annotation)
        for arg in node.args.kwonlyargs:
            if arg.annotation:
                arg.annotation = self._transform_annotation(arg.annotation)
        if node.args.vararg and node.args.vararg.annotation:
            node.args.vararg.annotation = self._transform_annotation(node.args.vararg.annotation)
        if node.args.kwarg and node.args.kwarg.annotation:
            node.args.kwarg.annotation = self._transform_annotation(node.args.kwarg.annotation)

        # Create sync function
        new_node = ast.FunctionDef(
            name=new_name,
            args=node.args,
            body=new_body,
            decorator_list=[self.visit(d) for d in node.decorator_list],
            returns=self._transform_annotation(node.returns) if node.returns else None,
            type_comment=node.type_comment,
        )
        return ast.copy_location(new_node, node)

    def visit_Await(self, node: ast.Await) -> ast.AST:  # noqa: N802
        """Remove await, keep the expression."""
        return self.visit(node.value)

    def visit_Call(self, node: ast.Call) -> ast.AST:  # noqa: N802
        """Handle special function call transformations."""
        # First visit children
        node.func = self.visit(node.func)
        node.args = [self.visit(arg) for arg in node.args]
        node.keywords = [self.visit(kw) for kw in node.keywords]

        # Handle asyncio.wait_for(coro, timeout) -> just coro (remove wait_for)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "wait_for"
            and len(node.args) >= 1
        ):
            # Return just the first argument (the coroutine), skip timeout
            return node.args[0]

        # Handle asyncio.gather(a, b, ...) -> self._run_in_executor(lambda: a, ...)
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "asyncio"
            and node.func.attr == "gather"
        ):
            # Wrap each arg in a lambda for executor submission
            lambda_args = [
                ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[],
                        args=[],
                        kwonlyargs=[],
                        kw_defaults=[],
                        defaults=[],
                    ),
                    body=arg,
                )
                for arg in node.args
            ]
            return ast.copy_location(
                ast.Call(
                    func=ast.Attribute(
                        value=ast.Name(id="self", ctx=ast.Load()),
                        attr="_run_in_executor",
                        ctx=ast.Load(),
                    ),
                    args=lambda_args,
                    keywords=[],
                ),
                node,
            )

        # Handle boto3/aioboto3 client context manager pattern:
        # session.client("dynamodb", ...)__aenter__() -> session.client("dynamodb", ...)
        # After __aenter__ -> __enter__ rewrite, we get .__enter__() which we need to remove
        # for boto3 sync clients (they're not context managers)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "__enter__"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Attribute)
            and node.func.value.func.attr == "client"
        ):
            # Remove the .__enter__() call, return just the client creation
            return node.func.value

        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST | None:  # noqa: N802
        """Handle expression statements, removing boto3 __exit__ calls."""
        # Visit the expression first
        node.value = self.visit(node.value)

        # Remove self._client.__exit__(None, None, None) calls
        # boto3 sync clients don't have __exit__ - they don't need explicit cleanup
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "__exit__"
        ):
            return None  # Remove the statement entirely

        return node

    def visit_AsyncWith(self, node: ast.AsyncWith) -> ast.AST:  # noqa: N802
        """Convert async with to with, or unwrap boto3 client creation.

        aioboto3 uses `async with session.client(...) as client:` but
        boto3 clients don't support context manager protocol. Detect
        `.client()` calls and convert to simple assignment.
        """
        # Check if this is a single-item `async with x.client(...) as var:`
        if len(node.items) == 1:
            item = node.items[0]
            ctx_expr = item.context_expr
            # Match pattern: something.client(...)
            if (
                isinstance(ctx_expr, ast.Call)
                and isinstance(ctx_expr.func, ast.Attribute)
                and ctx_expr.func.attr == "client"
                and item.optional_vars is not None
            ):
                # Convert to: var = something.client(...)
                assign = ast.Assign(
                    targets=[self.visit(item.optional_vars)],
                    value=self.visit(ctx_expr),
                    lineno=node.lineno,
                    col_offset=node.col_offset,
                )
                ast.fix_missing_locations(assign)
                body = [self.visit(stmt) for stmt in node.body]
                return [assign] + body  # type: ignore[return-value]

        new_node = ast.With(
            items=[self.visit(item) for item in node.items],
            body=[self.visit(stmt) for stmt in node.body],
        )
        return ast.copy_location(new_node, node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> ast.For:  # noqa: N802
        """Convert async for to for."""
        new_node = ast.For(
            target=self.visit(node.target),
            iter=self.visit(node.iter),
            body=[self.visit(stmt) for stmt in node.body],
            orelse=[self.visit(stmt) for stmt in node.orelse],
        )
        return ast.copy_location(new_node, node)

    def visit_ClassDef(self, node: ast.ClassDef) -> ast.ClassDef | None:  # noqa: N802
        """Rename or skip classes."""
        # Skip classes that should be imported from the async module
        if node.name in SKIP_CLASS_DEFINITIONS:
            return None

        if node.name in CLASS_RENAMES:
            node.name = CLASS_RENAMES[node.name]

        # Transform base classes (handle string annotations in bases)
        new_bases = []
        for base in node.bases:
            new_bases.append(self._transform_base_class(base))
        node.bases = new_bases

        # Filter out methods that should be removed (sync duplicates)
        # and rename _async_lock -> _sync_lock in field definitions
        new_body = []
        for item in node.body:
            # Check if this is a method to remove
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                if item.name in REMOVE_METHODS:
                    continue
            # Check if this is an annotated assignment
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                # Remove _sync_lock (duplicate after _async_lock is renamed)
                if item.target.id == "_sync_lock":
                    continue
                # Rename _async_lock -> _sync_lock
                if item.target.id == "_async_lock":
                    item.target.id = "_sync_lock"
            new_body.append(item)
        node.body = new_body

        # Continue visiting children
        self.generic_visit(node)

        # Inject parallel_mode support and executor methods into SyncRepository
        if node.name == "SyncRepository":
            # 1. Inject parallel_mode parameter into __init__
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    # Add parallel_mode: str = "auto" parameter
                    item.args.args.append(
                        ast.arg(
                            arg="parallel_mode",
                            annotation=ast.Name(id="str", ctx=ast.Load()),
                        )
                    )
                    item.args.defaults.append(ast.Constant(value="auto"))
                    # Add init body statements
                    init_stmts = ast.parse(_INIT_PARALLEL_STMTS).body
                    item.body.extend(init_stmts)
                    break

            # 2. Inject cleanup into close()
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "close":
                    cleanup_stmts = ast.parse("self._cleanup_thread_pool()").body
                    item.body.extend(cleanup_stmts)
                    break

            # 3. Inject executor methods
            executor_stmts = ast.parse(_EXECUTOR_METHODS).body
            node.body.extend(executor_stmts)

        return node

    def _transform_base_class(self, node: ast.AST) -> ast.AST:
        """Transform base class references."""
        if isinstance(node, ast.Name) and node.id in CLASS_RENAMES:
            node.id = CLASS_RENAMES[node.id]
        elif isinstance(node, ast.Subscript):
            # Handle Generic[T] style bases
            node.value = self._transform_base_class(node.value)
        return node

    def visit_Import(self, node: ast.Import) -> ast.Import | list[ast.Import]:  # noqa: N802
        """Rewrite import statements."""
        new_names: list[ast.alias] = []
        extra_imports: list[ast.Import] = []
        for alias in node.names:
            if alias.name in IMPORT_MODULE_REWRITES:
                alias.name = IMPORT_MODULE_REWRITES[alias.name]
                new_names.append(alias)
            elif alias.name == "asyncio":
                # asyncio maps to multiple modules via ATTRIBUTE_ACCESS_REWRITES
                needed = sorted({mod for (mod, _) in ATTRIBUTE_ACCESS_REWRITES.values()})
                for mod in needed:
                    extra_imports.append(ast.Import(names=[ast.alias(name=mod)]))
            else:
                new_names.append(alias)
        if extra_imports:
            if new_names:
                node.names = new_names
                return [node, *extra_imports]
            return extra_imports
        return node

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:  # noqa: N802
        """Rewrite from ... import statements."""
        # Rewrite module path
        if node.module:
            # Check for exact module rewrites
            if node.module in IMPORT_MODULE_REWRITES:
                node.module = IMPORT_MODULE_REWRITES[node.module]

            # Check for relative import path rewrites
            module_key = (
                f".{node.module}" if node.level == 0 else "." * node.level + (node.module or "")
            )
            for old_path, new_path in IMPORT_PATH_REWRITES.items():
                if module_key.endswith(old_path.lstrip(".")):
                    # Replace the suffix
                    prefix = module_key[: -len(old_path.lstrip("."))]
                    new_module = prefix + new_path.lstrip(".")
                    node.module = new_module.lstrip(".")
                    break

        # For relative imports, skip names handled by SKIP_CLASS_DEFINITIONS
        # (they are already added as explicit re-exports at the top of the file)
        if node.level > 0:
            node.names = [alias for alias in node.names if alias.name not in SKIP_CLASS_DEFINITIONS]
            if not node.names:
                return node
        for alias in node.names:
            if alias.name in IMPORT_NAME_REWRITES:
                old_name = alias.name
                alias.name = IMPORT_NAME_REWRITES[alias.name]
                # Also update asname if it was the same as name
                if alias.asname == old_name:
                    alias.asname = alias.name
            # Rewrite type names
            if alias.name in TYPE_REWRITES:
                alias.name = TYPE_REWRITES[alias.name]

        return node

    def visit_Name(self, node: ast.Name) -> ast.Name:  # noqa: N802
        """Rewrite class name references, module names, and decorator names."""
        if node.id in CLASS_RENAMES:
            node.id = CLASS_RENAMES[node.id]
        if node.id in TYPE_REWRITES:
            node.id = TYPE_REWRITES[node.id]
        # Also rewrite module names (e.g., aioboto3 -> boto3)
        if node.id in IMPORT_MODULE_REWRITES:
            node.id = IMPORT_MODULE_REWRITES[node.id]
        # Rewrite decorator names (e.g., asynccontextmanager -> contextmanager)
        if node.id in IMPORT_NAME_REWRITES:
            node.id = IMPORT_NAME_REWRITES[node.id]
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.Attribute:  # noqa: N802
        """Rewrite attribute access for renamed modules and async methods."""
        # Rewrite __aenter__ -> __enter__, __aexit__ -> __exit__ for method calls
        if node.attr in METHOD_NAME_REWRITES:
            node.attr = METHOD_NAME_REWRITES[node.attr]

        # Rewrite _async_lock -> _sync_lock
        if node.attr in ATTRIBUTE_NAME_REWRITES:
            node.attr = ATTRIBUTE_NAME_REWRITES[node.attr]

        # Rewrite asyncio.Lock -> threading.Lock
        if (
            isinstance(node.value, ast.Name)
            and (node.value.id, node.attr) in ATTRIBUTE_ACCESS_REWRITES
        ):
            new_module, new_attr = ATTRIBUTE_ACCESS_REWRITES[(node.value.id, node.attr)]
            node.value.id = new_module
            node.attr = new_attr

        self.generic_visit(node)
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:  # noqa: N802
        """Rewrite string annotations."""
        if isinstance(node.value, str):
            value = node.value
            # Rewrite class names in string annotations
            for old_name, new_name in CLASS_RENAMES.items():
                value = re.sub(rf"\b{old_name}\b", new_name, value)
            for old_name, new_name in TYPE_REWRITES.items():
                value = re.sub(rf"\b{old_name}\b", new_name, value)
            node.value = value
        return node

    def _transform_annotation(self, node: ast.AST | None) -> ast.AST | None:
        """Transform type annotations."""
        if node is None:
            return None
        return self.visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:  # noqa: N802
        """Transform sync function definitions (for annotations)."""
        # Transform return annotation
        if node.returns:
            node.returns = self._transform_annotation(node.returns)

        # Transform argument annotations
        for arg in node.args.args:
            if arg.annotation:
                arg.annotation = self._transform_annotation(arg.annotation)

        self.generic_visit(node)
        return node

    def visit_AnnAssign(self, node: ast.AnnAssign) -> ast.AnnAssign:  # noqa: N802
        """Transform annotated assignments."""
        node.annotation = self._transform_annotation(node.annotation)
        self.generic_visit(node)
        return node

    def visit_Tuple(self, node: ast.Tuple) -> ast.Tuple:  # noqa: N802
        """Visit tuple elements (for type annotations like Callable[[...], ...])."""
        node.elts = [self.visit(elt) for elt in node.elts]
        return node

    def visit_List(self, node: ast.List) -> ast.List:  # noqa: N802
        """Visit list elements (for type annotations like Callable[[arg], ...])."""
        node.elts = [self.visit(elt) for elt in node.elts]
        return node

    def visit_Subscript(self, node: ast.Subscript) -> ast.AST:  # noqa: N802
        """Unwrap Awaitable[X] and Coroutine[Any, Any, X] to X."""
        # First, visit children to transform nested subscripts
        node.value = self.visit(node.value)
        node.slice = self.visit(node.slice)

        # Check if this is Awaitable[X] or Coroutine[..., X]
        if isinstance(node.value, ast.Name) and node.value.id in UNWRAP_SUBSCRIPTS:
            # For Awaitable[X], return X (already visited above)
            if node.value.id == "Awaitable":
                return node.slice
            # For Coroutine[Any, Any, X], return X (third element of tuple)
            if node.value.id == "Coroutine" and isinstance(node.slice, ast.Tuple):
                if len(node.slice.elts) >= 3:
                    return node.slice.elts[2]

        return node


class TestAsyncToSyncTransformer(AsyncToSyncTransformer):
    """Extended transformer for test files with additional rewrites.

    Handles fixture parameter renaming (including body references),
    decorator removal, and test-specific import rewrites.
    """

    def __init__(self, source_file: str):
        super().__init__(source_file)
        # Active fixture renames within the current function scope
        self._active_fixture_renames: dict[str, str] = {}

    def _process_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, str]:
        """Rename fixture parameters and return the rename mapping."""
        renames: dict[str, str] = {}
        for arg in node.args.args:
            if arg.arg in FIXTURE_NAME_REWRITES:
                renames[arg.arg] = FIXTURE_NAME_REWRITES[arg.arg]
                arg.arg = FIXTURE_NAME_REWRITES[arg.arg]

        # Remove @pytest.mark.asyncio decorators
        node.decorator_list = [
            d for d in node.decorator_list if not self._is_removable_decorator(d)
        ]
        return renames

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.FunctionDef | None:  # noqa: N802
        """Convert async test functions and rewrite fixture parameter names."""
        if node.name in TEST_SKIP_FUNCTIONS:
            return None
        renames = self._process_function(node)

        # Set active renames for body traversal, then restore
        prev = self._active_fixture_renames
        self._active_fixture_renames = renames
        result = super().visit_AsyncFunctionDef(node)
        self._active_fixture_renames = prev
        return result

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:  # noqa: N802
        """Rewrite fixture parameter names in sync test functions."""
        renames = self._process_function(node)

        prev = self._active_fixture_renames
        self._active_fixture_renames = renames
        result = super().visit_FunctionDef(node)
        self._active_fixture_renames = prev
        return result

    def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.ImportFrom:  # noqa: N802
        """Rewrite test-specific imports."""
        path_rewritten = False

        # Apply test-specific module path rewrites first (absolute imports)
        if node.module and node.level == 0:
            for old_path, new_path in TEST_IMPORT_PATH_REWRITES.items():
                if node.module == old_path:
                    node.module = new_path
                    path_rewritten = True
                    break

        # Apply test-specific name rewrites
        for alias in node.names:
            if alias.name in TEST_IMPORT_NAME_REWRITES:
                old_name = alias.name
                alias.name = TEST_IMPORT_NAME_REWRITES[alias.name]
                if alias.asname == old_name:
                    alias.asname = alias.name

        if path_rewritten:
            # Skip base class module path rewrite (already done), but still apply
            # base class name rewrites (IMPORT_NAME_REWRITES, TYPE_REWRITES)
            for alias in node.names:
                if alias.name in IMPORT_NAME_REWRITES:
                    old_name = alias.name
                    alias.name = IMPORT_NAME_REWRITES[alias.name]
                    if alias.asname == old_name:
                        alias.asname = alias.name
                if alias.name in TYPE_REWRITES:
                    alias.name = TYPE_REWRITES[alias.name]
            return node

        return super().visit_ImportFrom(node)

    def visit_Constant(self, node: ast.Constant) -> ast.Constant:  # noqa: N802
        """Rewrite module paths and module names in string constants (e.g., patch targets)."""
        node = super().visit_Constant(node)
        if isinstance(node.value, str):
            for old_path, new_path in TEST_IMPORT_PATH_REWRITES.items():
                node.value = node.value.replace(old_path, new_path)
            # Also rewrite module names (aioboto3 -> boto3) in patch targets
            for old_mod, new_mod in IMPORT_MODULE_REWRITES.items():
                node.value = node.value.replace(old_mod, new_mod)
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST:  # noqa: N802
        """Keep __exit__ calls in test files (base class removes them for boto3 clients)."""
        node.value = self.visit(node.value)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.Attribute:  # noqa: N802
        """Rewrite _sync suffix method names removed from sync classes."""
        if node.attr in TEST_METHOD_NAME_REWRITES:
            node.attr = TEST_METHOD_NAME_REWRITES[node.attr]
        return super().visit_Attribute(node)

    def visit_Name(self, node: ast.Name) -> ast.Name:  # noqa: N802
        """Rewrite AsyncMock -> MagicMock and fixture references in function bodies."""
        if node.id in TEST_IMPORT_NAME_REWRITES:
            node.id = TEST_IMPORT_NAME_REWRITES[node.id]
        # Rewrite fixture references within function bodies
        if node.id in self._active_fixture_renames:
            node.id = self._active_fixture_renames[node.id]
        return super().visit_Name(node)

    def _is_removable_decorator(self, node: ast.AST) -> bool:
        """Check if a decorator should be removed."""
        # Match @pytest.mark.asyncio
        if isinstance(node, ast.Attribute):
            parts = []
            current = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            full_name = ".".join(reversed(parts))
            return full_name in REMOVE_DECORATORS
        return False


class CleanupTransformer(ast.NodeTransformer):
    """Post-processing pass to clean up artifacts from async-to-sync transformation.

    Handles patterns like try/except/finally blocks where the try body
    became empty (e.g., after removing __exit__ calls).
    """

    def visit_Assign(self, node: ast.Assign) -> ast.Assign | None:  # noqa: N802
        """Remove module-level assignments that are unused in sync code."""
        if (
            len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in REMOVE_ASSIGNMENTS
        ):
            return None
        return node

    def _is_empty_body(self, body: list[ast.stmt]) -> bool:
        """Check if a block is empty or contains only pass statements."""
        return len(body) == 0 or all(isinstance(stmt, ast.Pass) for stmt in body)

    def visit_Try(self, node: ast.Try) -> ast.AST:  # noqa: N802
        """Collapse try blocks where the body is empty.

        Pattern: try: <empty> except: ... finally: body  ->  body
        The __aexit__ call in try was removed, leaving an empty body.
        The finally body is what we actually want to keep.
        """
        self.generic_visit(node)

        if self._is_empty_body(node.body) and node.finalbody:
            # try body is empty - collapse to finally body
            return node.finalbody  # type: ignore[return-value]

        if self._is_empty_body(node.body) and not node.finalbody:
            # try body is empty and no finally - collapse to just pass
            return ast.Pass()

        return node


class TestCleanupTransformer(ast.NodeTransformer):
    """Post-processing pass for test files to unwrap context manager mock patterns.

    Handles the pattern where async tests mock context managers:
        mock_client_cm = MagicMock()
        mock_client_cm.__enter__ = MagicMock(return_value=mock_lambda)
        mock_client_cm.__exit__ = MagicMock()
        mock_session.client.return_value = mock_client_cm

    Becomes (sync doesn't use context managers for boto3 clients):
        mock_session.client.return_value = mock_lambda
    """

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:  # noqa: N802
        """Process each function to find and unwrap context manager mock patterns."""
        # Phase 1: Scan for __enter__ assignments to find CM wrapper -> unwrapped value
        cm_wrappers: dict[str, str] = {}  # wrapper_name -> unwrapped_name
        cm_wrapper_names: set[str] = set()

        for stmt in node.body:
            self._scan_stmt(stmt, cm_wrappers, cm_wrapper_names)

        if not cm_wrappers:
            self.generic_visit(node)
            return node

        # Phase 2: Rewrite the function body (recursively into with/if/try blocks)
        node.body = self._rewrite_body(node.body, cm_wrappers, cm_wrapper_names)

        self.generic_visit(node)
        return node

    def _scan_stmt(
        self,
        stmt: ast.stmt,
        cm_wrappers: dict[str, str],
        cm_wrapper_names: set[str],
    ) -> None:
        """Recursively scan statements for CM wrapper patterns."""
        if isinstance(stmt, ast.Assign):
            self._scan_assign(stmt, cm_wrappers, cm_wrapper_names)
        elif isinstance(stmt, ast.With):
            for child in stmt.body:
                self._scan_stmt(child, cm_wrappers, cm_wrapper_names)
        elif isinstance(stmt, ast.If):
            for child in stmt.body + stmt.orelse:
                self._scan_stmt(child, cm_wrappers, cm_wrapper_names)
        elif isinstance(stmt, ast.Try):
            for child in stmt.body + stmt.handlers + stmt.finalbody + stmt.orelse:
                if isinstance(child, ast.ExceptHandler):
                    for sub in child.body:
                        self._scan_stmt(sub, cm_wrappers, cm_wrapper_names)
                else:
                    self._scan_stmt(child, cm_wrappers, cm_wrapper_names)

    def _scan_assign(
        self,
        stmt: ast.Assign,
        cm_wrappers: dict[str, str],
        cm_wrapper_names: set[str],
    ) -> None:
        """Detect: X.__enter__ = MagicMock(return_value=Y) where X != Y.

        Self-wrapping patterns (X.__enter__ = MagicMock(return_value=X)) are
        tracked only for __enter__/__exit__ removal, not for assignment removal.
        """
        if len(stmt.targets) != 1:
            return
        target = stmt.targets[0]

        if (
            isinstance(target, ast.Attribute)
            and target.attr == "__enter__"
            and isinstance(target.value, ast.Name)
            and isinstance(stmt.value, ast.Call)
        ):
            # Extract return_value keyword argument
            for kw in stmt.value.keywords:
                if kw.arg == "return_value" and isinstance(kw.value, ast.Name):
                    wrapper_name = target.value.id
                    unwrapped_name = kw.value.id
                    # Skip self-wrapping (X wraps X) — only remove __enter__/__exit__
                    if wrapper_name == unwrapped_name:
                        cm_wrapper_names.add(wrapper_name)
                        # Don't add to cm_wrappers (no assignment rewriting needed)
                    else:
                        cm_wrappers[wrapper_name] = unwrapped_name
                        cm_wrapper_names.add(wrapper_name)

    def _rewrite_body(
        self,
        body: list[ast.stmt],
        cm_wrappers: dict[str, str],
        cm_wrapper_names: set[str],
    ) -> list[ast.stmt]:
        """Rewrite a list of statements, recursing into compound statements."""
        new_body = []
        for stmt in body:
            result = self._rewrite_stmt(stmt, cm_wrappers, cm_wrapper_names)
            if result is not None:
                new_body.append(result)
        return new_body

    def _rewrite_stmt(
        self,
        stmt: ast.stmt,
        cm_wrappers: dict[str, str],
        cm_wrapper_names: set[str],
    ) -> ast.stmt | None:
        """Rewrite a statement, removing CM mock assignments and replacing references."""
        # Recurse into compound statements
        if isinstance(stmt, ast.With):
            stmt.body = self._rewrite_body(stmt.body, cm_wrappers, cm_wrapper_names)
            return stmt
        if isinstance(stmt, ast.If):
            stmt.body = self._rewrite_body(stmt.body, cm_wrappers, cm_wrapper_names)
            stmt.orelse = self._rewrite_body(stmt.orelse, cm_wrappers, cm_wrapper_names)
            return stmt
        if isinstance(stmt, ast.Try):
            stmt.body = self._rewrite_body(stmt.body, cm_wrappers, cm_wrapper_names)
            stmt.finalbody = self._rewrite_body(stmt.finalbody, cm_wrappers, cm_wrapper_names)
            for handler in stmt.handlers:
                if isinstance(handler, ast.ExceptHandler):
                    handler.body = self._rewrite_body(handler.body, cm_wrappers, cm_wrapper_names)
            return stmt

        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            return stmt

        target = stmt.targets[0]

        # Remove: X.__enter__ = MagicMock(...)
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "__enter__"
            and isinstance(target.value, ast.Name)
            and target.value.id in cm_wrapper_names
        ):
            return None

        # Remove: X.__exit__ = MagicMock(...)
        if (
            isinstance(target, ast.Attribute)
            and target.attr == "__exit__"
            and isinstance(target.value, ast.Name)
            and target.value.id in cm_wrapper_names
        ):
            return None

        # Remove: X = MagicMock() where X is a non-self CM wrapper
        if (
            isinstance(target, ast.Name)
            and target.id in cm_wrappers  # Only when it wraps a DIFFERENT variable
        ):
            return None

        # Replace: Y.something.return_value = X  ->  Y.something.return_value = unwrapped
        if isinstance(stmt.value, ast.Name) and stmt.value.id in cm_wrappers:
            stmt.value = ast.Name(id=cm_wrappers[stmt.value.id], ctx=ast.Load())
            ast.fix_missing_locations(stmt)

        return stmt


def run_ruff_on_content(content: str, target_path: Path) -> str:
    """Run ruff check --fix and format on content, return formatted content.

    Raises FileNotFoundError if ruff is not installed.
    """
    import shutil
    import tempfile

    if shutil.which("ruff") is None:
        raise FileNotFoundError(
            "ruff is required for sync code generation. Install it with: pip install ruff"
        )

    # Write to a temp file with the same path structure for correct config resolution
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", dir=target_path.parent, delete=False
    ) as f:
        f.write(content)
        temp_path = Path(f.name)

    try:
        # Run ruff check --fix
        subprocess.run(
            ["ruff", "check", "--fix", str(temp_path)],
            capture_output=True,
            check=False,
        )

        # Run ruff format
        subprocess.run(
            ["ruff", "format", str(temp_path)],
            capture_output=True,
            check=False,
        )

        # Read back the formatted content
        return temp_path.read_text()
    finally:
        temp_path.unlink()


def transform_file(
    source_path: Path,
    target_path: Path,
    *,
    transformer_cls: type[AsyncToSyncTransformer] = AsyncToSyncTransformer,
    base_dir: Path = SRC,
    add_skip_class_imports: bool = True,
) -> bool:
    """Transform a single file from async to sync.

    Args:
        source_path: Path to the async source file.
        target_path: Path to write the sync output file.
        transformer_cls: AST transformer class to use.
        base_dir: Base directory for computing the relative source path in the header.
        add_skip_class_imports: Whether to add imports for SKIP_CLASS_DEFINITIONS.
            Set to False for test files (they import from the package, not relatively).

    Returns True if file was changed, False otherwise.
    """
    source_code = source_path.read_text()

    # Parse AST
    tree = ast.parse(source_code)

    # Remove the original module docstring (we'll add our own header)
    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, ast.Constant)
        and isinstance(tree.body[0].value.value, str)
    ):
        tree.body = tree.body[1:]

    # Transform async -> sync
    transformer = transformer_cls(source_path.name)
    new_tree = transformer.visit(tree)

    # Clean up transformation artifacts (e.g., try: pass after __exit__ removal)
    cleanup = CleanupTransformer()
    new_tree = cleanup.visit(new_tree)

    # For test files, unwrap context manager mock patterns
    if transformer_cls is TestAsyncToSyncTransformer:
        test_cleanup = TestCleanupTransformer()
        new_tree = test_cleanup.visit(new_tree)

    # Fix missing locations
    ast.fix_missing_locations(new_tree)

    # Generate code
    new_code = ast.unparse(new_tree)

    # Add imports for skipped class definitions (source files only)
    skip_imports = []
    if add_skip_class_imports:
        for class_name, module in SKIP_CLASS_DEFINITIONS.items():
            if class_name in new_code:
                # Use `as X` for explicit re-export (mypy strict mode)
                skip_imports.append(f"from {module} import {class_name} as {class_name}")

    # Add header
    relative_source = source_path.relative_to(base_dir)
    header = GENERATED_HEADER.format(source_file=relative_source)
    skip_import_block = "\n".join(skip_imports) + "\n" if skip_imports else ""
    raw_code = header + skip_import_block + new_code + "\n"

    # Run ruff to format the code consistently
    target_path.parent.mkdir(parents=True, exist_ok=True)
    final_code = run_ruff_on_content(raw_code, target_path)

    # Check if changed
    if target_path.exists():
        existing = target_path.read_text()
        if existing == final_code:
            return False

    # Write
    target_path.write_text(final_code)
    return True


def main() -> int:
    """Run all transformations."""
    changed = []

    # Transform source files (async -> sync)
    for source_name, target_name in SOURCE_TRANSFORMS:
        source_path = SRC / source_name
        target_path = SRC / target_name

        if not source_path.exists():
            print(f"WARNING: Source file not found: {source_path}")
            continue

        if transform_file(source_path, target_path):
            changed.append(target_name)
            print(f"Generated: {target_name}")
        else:
            print(f"Unchanged: {target_name}")

    # Transform test files (async tests -> sync tests)
    for source_name, target_name in TEST_TRANSFORMS:
        source_path = TESTS / source_name
        target_path = TESTS / target_name

        if not source_path.exists():
            print(f"WARNING: Test source not found: {source_path}")
            continue

        if transform_file(
            source_path,
            target_path,
            transformer_cls=TestAsyncToSyncTransformer,
            base_dir=TESTS,
            add_skip_class_imports=False,
        ):
            changed.append(f"tests/{target_name}")
            print(f"Generated: tests/{target_name}")
        else:
            print(f"Unchanged: tests/{target_name}")

    if changed:
        print(f"\n{len(changed)} file(s) updated")
    else:
        print("\nAll files up to date")

    return 0


if __name__ == "__main__":
    sys.exit(main())
