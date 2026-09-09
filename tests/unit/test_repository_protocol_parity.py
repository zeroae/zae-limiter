"""Signature parity between Repository and RepositoryProtocol (ADR-108).

`RepositoryProtocol` is the contract an alternative backend implements. It
is hand-written, so a method added to `Repository` — or a parameter added
to an existing one — silently drifts unless something checks. Nothing did,
which is how the ADR-125 disable work landed with `get_entity_disabled` /
`get_resource_disabled` and the `disabled` keyword absent from the
protocol: callers typed against it could not reach the new behaviour, and
a conforming backend would not know to provide it.

These tests compare the two directly rather than enumerating known gaps,
so the next addition is caught the same way.
"""

import inspect

import pytest

from zae_limiter.repository import Repository
from zae_limiter.repository_protocol import RepositoryProtocol


def _protocol_methods() -> list[str]:
    """Public methods declared on the protocol."""
    return sorted(
        name
        for name, member in vars(RepositoryProtocol).items()
        if not name.startswith("_") and inspect.isfunction(member)
    )


def _params(func) -> list[str]:
    """Parameter names, tagged with kind so keyword-only drift is caught too.

    A parameter that is positional on one side and keyword-only on the other
    is a real incompatibility — mypy rejects the Protocol match — but comparing
    bare names would call them equal.
    """
    return [
        f"*{p.name}" if p.kind is inspect.Parameter.KEYWORD_ONLY else p.name
        for p in inspect.signature(func).parameters.values()
        if p.name != "self"
    ]


class TestRepositoryProtocolParity:
    def test_protocol_declares_methods(self) -> None:
        """Sanity check that introspection finds the contract at all."""
        assert len(_protocol_methods()) > 20

    @pytest.mark.parametrize("name", _protocol_methods())
    def test_repository_implements_protocol_method(self, name: str) -> None:
        """Every protocol method must exist on Repository."""
        assert hasattr(Repository, name), (
            f"RepositoryProtocol declares {name}() but Repository does not implement it"
        )

    @pytest.mark.parametrize("name", _protocol_methods())
    def test_signatures_match(self, name: str) -> None:
        """Parameter names must agree, in order.

        A backend written against the protocol calls with these names, so a
        rename or an addition on either side is a real incompatibility, not
        cosmetic.
        """
        protocol_params = _params(getattr(RepositoryProtocol, name))
        repo_params = _params(getattr(Repository, name))

        assert protocol_params == repo_params, (
            f"{name}() signature drift:\n"
            f"  protocol:   {protocol_params}\n"
            f"  Repository: {repo_params}"
        )


class TestDisableSurfaceIsInTheProtocol:
    """The ADR-125 readers are part of the backend contract.

    `cli.py` calls both, so a backend that lacks them cannot serve the CLI.
    """

    @pytest.mark.parametrize("name", ["get_entity_disabled", "get_resource_disabled"])
    def test_reader_declared(self, name: str) -> None:
        assert hasattr(RepositoryProtocol, name), (
            f"Repository.{name}() is called from cli.py but is not in the protocol"
        )

    @pytest.mark.parametrize("name", ["set_limits", "set_resource_defaults"])
    def test_disabled_keyword_declared(self, name: str) -> None:
        assert "*disabled" in _params(getattr(RepositoryProtocol, name)), (
            f"{name}() takes a tri-state `disabled` on Repository but the protocol "
            f"does not declare it, so callers typed against the protocol cannot pass it"
        )
