"""Per-session isolation of the shared CloudFormation stacks (issue #577).

Two concurrent pytest invocations used to collide on a fixed stack name:
the second silently adopted the first one's live stack, then deleted it from
``pytest_sessionfinish`` while the first was still writing to the table.
These tests pin the three things that make that unrepresentable — a session
key every worker agrees on, a stack name carrying it, and a cleanup path that
refuses anything else.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from tests.fixtures.stacks import (
    OWNER_FILE,
    PENDING_SUFFIX,
    SharedStack,
    cleanup_shared_stacks,
    reap_orphan_stacks,
    record_session_owner,
    session_key,
    session_root,
    session_stack_name,
)
from zae_limiter.naming import validate_name

# The literals passed to get_or_create_shared_stack(): two in
# tests/fixtures/stacks.py, one in tests/e2e/conftest.py.
STACK_BASES = ["shared-minimal", "shared-aggregator", "shared-full"]


class _FakeSyncRepository:
    """Records delete_stack() calls instead of talking to CloudFormation."""

    deleted: list[str] = []
    fail_on: set[str] = set()

    def __init__(self, *, name: str, region: str, endpoint_url: str | None) -> None:
        self.name = name

    def delete_stack(self) -> None:
        if self.name in self.fail_on:
            raise RuntimeError("boom")
        self.deleted.append(self.name)

    def close(self) -> None:
        pass


@pytest.fixture
def fake_repo(monkeypatch):
    """Patch the SyncRepository that _delete_recorded_stack imports."""
    import zae_limiter.sync_repository as sync_repository

    _FakeSyncRepository.deleted = []
    _FakeSyncRepository.fail_on = set()
    monkeypatch.setattr(sync_repository, "SyncRepository", _FakeSyncRepository)
    return _FakeSyncRepository


def _write_record(root: Path, base: str, name: str, suffix: str = ".json") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data_file = root / f"{base}{suffix}"
    data_file.write_text(
        json.dumps({"name": name, "region": "us-east-1", "endpoint_url": "http://localhost:4566"})
    )
    return data_file


def _dead_pid() -> int:
    """A pid that has certainly exited (and been reaped)."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class TestSessionRoot:
    def test_worker_basetemp_resolves_to_the_controller(self):
        controller = Path("/tmp/pytest-of-me/pytest-42")
        assert session_root(controller / "popen-gw0") == controller
        assert session_root(controller / "popen-gw11") == controller

    def test_controller_basetemp_is_its_own_root(self):
        controller = Path("/tmp/pytest-of-me/pytest-42")
        assert session_root(controller) == controller

    def test_an_explicit_basetemp_is_not_mistaken_for_a_worker(self):
        # --basetemp=/somewhere/popen-gwX-ish must not be walked up.
        explicit = Path("/somewhere/popen-gw-manual")
        assert session_root(explicit) == explicit

    def test_every_worker_of_one_session_agrees(self):
        controller = Path("/tmp/pytest-of-me/pytest-42")
        keys = {session_key(session_root(controller / f"popen-gw{n}")) for n in range(8)}
        assert keys == {session_key(controller)}


class TestSessionKey:
    def test_is_stable_for_one_path(self):
        root = Path("/tmp/pytest-of-me/pytest-42")
        assert session_key(root) == session_key(root)

    def test_differs_between_concurrent_sessions(self):
        parent = Path("/tmp/pytest-of-me")
        assert session_key(parent / "pytest-42") != session_key(parent / "pytest-43")

    def test_is_eight_hex_characters(self):
        key = session_key(Path("/tmp/pytest-of-me/pytest-42"))
        assert len(key) == 8
        assert all(c in "0123456789abcdef" for c in key)


class TestStackName:
    @pytest.mark.parametrize("base", STACK_BASES)
    def test_is_a_legal_stack_name(self, base):
        name = session_stack_name(base, Path("/tmp/pytest-of-me/pytest-42"))
        validate_name(name)  # raises ValidationError if not

    @pytest.mark.parametrize("base", STACK_BASES)
    def test_fits_the_length_limit(self, base):
        # naming.validate_name caps stack names at 55 (IAM role constraints).
        name = session_stack_name(base, Path("/tmp/pytest-of-me/pytest-42"))
        assert len(name) <= 55

    @pytest.mark.parametrize("base", STACK_BASES)
    def test_carries_the_session_key(self, base):
        root = Path("/tmp/pytest-of-me/pytest-42")
        assert session_stack_name(base, root) == f"{base}-{session_key(root)}"

    def test_two_sessions_never_share_a_name(self):
        parent = Path("/tmp/pytest-of-me")
        a = session_stack_name("shared-minimal", parent / "pytest-42")
        b = session_stack_name("shared-minimal", parent / "pytest-43")
        assert a != b


class TestCleanup:
    def test_deletes_this_sessions_stack_and_drops_the_record(self, tmp_path, fake_repo):
        root = tmp_path / "pytest-1"
        name = session_stack_name("shared-minimal", root)
        data_file = _write_record(root, "shared-minimal", name)

        cleanup_shared_stacks(root)

        assert fake_repo.deleted == [name]
        assert not data_file.exists()

    def test_accepts_a_worker_basetemp(self, tmp_path, fake_repo):
        # pytest_sessionfinish passes the controller basetemp, but the helper
        # must normalise either spelling.
        root = tmp_path / "pytest-1"
        name = session_stack_name("shared-minimal", root)
        _write_record(root, "shared-minimal", name)

        cleanup_shared_stacks(root / "popen-gw3")

        assert fake_repo.deleted == [name]

    def test_refuses_a_record_that_is_not_this_sessions(self, tmp_path, fake_repo):
        # A pre-#577 metadata file, whose stack name carries no session key,
        # may belong to a concurrently running older revision.
        root = tmp_path / "pytest-1"
        data_file = _write_record(root, "shared-minimal", "shared-minimal")

        cleanup_shared_stacks(root)

        assert fake_repo.deleted == []
        assert data_file.exists()

    def test_refuses_a_record_naming_another_sessions_key(self, tmp_path, fake_repo):
        root = tmp_path / "pytest-1"
        peer_name = session_stack_name("shared-minimal", tmp_path / "pytest-2")
        _write_record(root, "shared-minimal", peer_name)

        cleanup_shared_stacks(root)

        assert fake_repo.deleted == []

    def test_keeps_the_record_when_deletion_fails(self, tmp_path, fake_repo):
        root = tmp_path / "pytest-1"
        name = session_stack_name("shared-minimal", root)
        data_file = _write_record(root, "shared-minimal", name)
        fake_repo.fail_on = {name}

        with pytest.warns(ResourceWarning):
            cleanup_shared_stacks(root)

        assert data_file.exists()

    def test_deletes_a_stack_that_was_still_being_created(self, tmp_path, fake_repo):
        # A .pending record names a stack whose build() never finished.
        root = tmp_path / "pytest-1"
        name = session_stack_name("shared-minimal", root)
        pending = _write_record(root, "shared-minimal", name, PENDING_SUFFIX)

        cleanup_shared_stacks(root)

        assert fake_repo.deleted == [name]
        assert not pending.exists()

    def test_tolerates_an_unreadable_record(self, tmp_path, fake_repo):
        root = tmp_path / "pytest-1"
        root.mkdir(parents=True)
        (root / "shared-minimal.json").write_text("not json")

        cleanup_shared_stacks(root)

        assert fake_repo.deleted == []


class TestReapOrphans:
    def test_reaps_a_dead_peers_stack(self, tmp_path, fake_repo):
        mine = tmp_path / "pytest-2"
        mine.mkdir()
        peer = tmp_path / "pytest-1"
        name = session_stack_name("shared-minimal", peer)
        data_file = _write_record(peer, "shared-minimal", name)
        (peer / OWNER_FILE).write_text(str(_dead_pid()))

        reap_orphan_stacks(mine)

        assert fake_repo.deleted == [name]
        assert not data_file.exists()

    def test_reaps_a_stack_a_dead_peer_never_finished_creating(self, tmp_path, fake_repo):
        # The window #577's first repro landed in: killed after
        # CREATE_COMPLETE, before the .json was written.
        mine = tmp_path / "pytest-2"
        mine.mkdir()
        peer = tmp_path / "pytest-1"
        name = session_stack_name("shared-minimal", peer)
        pending = _write_record(peer, "shared-minimal", name, PENDING_SUFFIX)
        (peer / OWNER_FILE).write_text(str(_dead_pid()))

        reap_orphan_stacks(mine)

        assert fake_repo.deleted == [name]
        assert not pending.exists()

    def test_leaves_a_live_peers_stack_alone(self, tmp_path, fake_repo):
        mine = tmp_path / "pytest-2"
        mine.mkdir()
        peer = tmp_path / "pytest-1"
        _write_record(peer, "shared-minimal", session_stack_name("shared-minimal", peer))
        record_session_owner(peer)  # this process — very much alive

        reap_orphan_stacks(mine)

        assert fake_repo.deleted == []

    def test_treats_a_peer_without_a_marker_as_dead_but_still_checks_ownership(
        self, tmp_path, fake_repo
    ):
        mine = tmp_path / "pytest-2"
        mine.mkdir()
        peer = tmp_path / "pytest-1"
        # Pre-#577 record: no marker, and a name with no session key.
        data_file = _write_record(peer, "shared-minimal", "shared-minimal")

        reap_orphan_stacks(mine)

        assert fake_repo.deleted == []
        assert data_file.exists()

    def test_never_touches_its_own_root(self, tmp_path, fake_repo):
        mine = tmp_path / "pytest-2"
        _write_record(mine, "shared-minimal", session_stack_name("shared-minimal", mine))
        (mine / OWNER_FILE).write_text(str(_dead_pid()))

        reap_orphan_stacks(mine)

        assert fake_repo.deleted == []

    def test_ignores_the_pytest_current_symlink(self, tmp_path, fake_repo):
        # Reached through the symlink, the same directory hashes differently
        # and its own records would read as another session's.
        mine = tmp_path / "pytest-2"
        mine.mkdir()
        peer = tmp_path / "pytest-1"
        _write_record(peer, "shared-minimal", session_stack_name("shared-minimal", peer))
        (peer / OWNER_FILE).write_text(str(_dead_pid()))
        (tmp_path / "pytest-current").symlink_to(peer, target_is_directory=True)

        reap_orphan_stacks(mine)

        # Reaped exactly once, via the real directory.
        assert fake_repo.deleted == [session_stack_name("shared-minimal", peer)]

    def test_ignores_directories_that_are_not_pytest_roots(self, tmp_path, fake_repo):
        mine = tmp_path / "pytest-2"
        mine.mkdir()
        other = tmp_path / "garbage-abc"
        _write_record(other, "shared-minimal", session_stack_name("shared-minimal", other))
        (other / OWNER_FILE).write_text(str(_dead_pid()))

        reap_orphan_stacks(mine)

        assert fake_repo.deleted == []


def test_record_session_owner_writes_this_pid(tmp_path):
    import os

    root = tmp_path / "pytest-1"
    record_session_owner(root)
    assert (root / OWNER_FILE).read_text() == str(os.getpid())


def test_shared_stack_roundtrips_through_its_record(tmp_path):
    stack = SharedStack(name="shared-minimal-deadbeef", region="us-east-1", endpoint_url=None)
    data_file = tmp_path / "shared-minimal.json"
    data_file.write_text(json.dumps(asdict(stack)))
    assert SharedStack(**json.loads(data_file.read_text())) == stack
