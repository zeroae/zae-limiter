"""Structural checks on a built Lambda deployment package.

An unvendored module is invisible in CI: every unit test imports from the
*installed* ``zae_limiter``, where every module is present, so the missing copy
only shows up as an ``ImportError`` at Lambda cold start in a deployed stack.
The closure check below reads what the built zip actually contains and asserts
that nothing it imports is missing from it.
"""

import ast
import io
import sys
import zipfile

# Distributions whose import name differs from the name pip knows them by.
_IMPORT_ALIASES = {"pyyaml": "yaml"}

# Provided by the Lambda Python runtime image, so deliberately not packaged.
_RUNTIME_PROVIDED = {"boto3", "botocore"}


def _requirement_roots(requirements: list[str]) -> set[str]:
    """Import roots the declared requirements make available."""
    roots = set()
    for req in requirements:
        name = req.split(";")[0].strip()
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<", "["):
            name = name.split(sep)[0]
        name = name.strip().lower()
        roots.add(_IMPORT_ALIASES.get(name, name.replace("-", "_")))
    return roots


def _imports(tree: ast.AST, package: str) -> set[str]:
    """Every module name imported by one source file, relative ones resolved."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found.add(f"{package}.{node.module}" if node.module else package)
            elif node.module:
                found.add(node.module)
    return found


def assert_package_imports_resolve(zip_bytes: bytes, requirements: list[str]) -> None:
    """Every module the packaged code imports is vendored, declared, or stdlib.

    Args:
        zip_bytes: A built Lambda deployment package.
        requirements: The builder's ``_get_runtime_requirements()`` output.
    """
    allowed_roots = _requirement_roots(requirements) | _RUNTIME_PROVIDED
    allowed_roots |= sys.stdlib_module_names

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = set(zf.namelist())
        sources = {n: zf.read(n).decode() for n in names if n.endswith(".py")}

    top_level_dirs = {n.split("/")[0] for n in names if "/" in n}
    missing: list[str] = []

    for path, source in sources.items():
        package = path.rsplit("/", 1)[0].replace("/", ".") if "/" in path else ""
        for module in _imports(ast.parse(source), package):
            root = module.split(".")[0]
            if root in top_level_dirs:
                # A packaged package: the exact submodule must be there too.
                expected = module.replace(".", "/")
                if f"{expected}.py" in names or f"{expected}/__init__.py" in names:
                    continue
                # A name re-exported by the package's __init__ resolves at
                # runtime even without a module file of its own.
                if f"{root}/__init__.py" in names and "." not in module:
                    continue
                missing.append(f"{path} imports {module}, which is not in the package")
            elif root not in allowed_roots:
                missing.append(f"{path} imports {module}, which is neither vendored nor declared")

    assert not missing, "Lambda package has unresolvable imports:\n  " + "\n  ".join(missing)
