"""Tests for RateLimiterSession instrumented methods in zae_limiter.locust."""

from __future__ import annotations

import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter import Entity, Limit
from zae_limiter.limiter import OnUnavailable


@pytest.fixture()
def mock_limiter():
    return MagicMock()


@pytest.fixture()
def mock_event():
    return MagicMock()


@pytest.fixture()
def session(mock_limiter, mock_event):
    with patch.dict("sys.modules", {"locust": MagicMock(), "locust.exception": MagicMock()}):
        from zae_limiter.locust import RateLimiterSession

        return RateLimiterSession(
            limiter=mock_limiter,
            request_event=mock_event,
            user=MagicMock(),
        )


# ---------------------------------------------------------------------------
# System-level defaults
# ---------------------------------------------------------------------------


class TestSetSystemDefaults:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        limits = [Limit.per_minute("rpm", 100)]
        session.set_system_defaults(limits, on_unavailable=OnUnavailable.ALLOW)

        mock_limiter.set_system_defaults.assert_called_once_with(
            limits, on_unavailable=OnUnavailable.ALLOW
        )
        mock_event.fire.assert_called_once()
        call_kwargs = mock_event.fire.call_args.kwargs
        assert call_kwargs["request_type"] == "SET_SYSTEM_DEFAULTS"
        assert call_kwargs["name"] == "system"
        assert call_kwargs["exception"] is None

    def test_custom_name(self, session, mock_event):
        session.set_system_defaults([], name="custom")
        assert mock_event.fire.call_args.kwargs["name"] == "custom"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.set_system_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.set_system_defaults([])
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestGetSystemDefaults:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        expected = ([Limit.per_minute("rpm", 100)], OnUnavailable.ALLOW)
        mock_limiter.get_system_defaults.return_value = expected

        result = session.get_system_defaults()

        assert result == expected
        mock_limiter.get_system_defaults.assert_called_once()
        assert mock_event.fire.call_args.kwargs["request_type"] == "GET_SYSTEM_DEFAULTS"
        assert mock_event.fire.call_args.kwargs["name"] == "system"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.get_system_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.get_system_defaults()
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestDeleteSystemDefaults:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        session.delete_system_defaults()

        mock_limiter.delete_system_defaults.assert_called_once()
        assert mock_event.fire.call_args.kwargs["request_type"] == "DELETE_SYSTEM_DEFAULTS"
        assert mock_event.fire.call_args.kwargs["name"] == "system"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.delete_system_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.delete_system_defaults()
        assert mock_event.fire.call_args.kwargs["exception"] is err


# ---------------------------------------------------------------------------
# Resource-level defaults
# ---------------------------------------------------------------------------


class TestSetResourceDefaults:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        limits = [Limit.per_minute("rpm", 100)]
        session.set_resource_defaults("gpt-4", limits)

        mock_limiter.set_resource_defaults.assert_called_once_with("gpt-4", limits)
        assert mock_event.fire.call_args.kwargs["request_type"] == "SET_RESOURCE_DEFAULTS"
        assert mock_event.fire.call_args.kwargs["name"] == "gpt-4"

    def test_custom_name(self, session, mock_event):
        session.set_resource_defaults("gpt-4", [], name="custom")
        assert mock_event.fire.call_args.kwargs["name"] == "custom"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.set_resource_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.set_resource_defaults("gpt-4", [])
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestGetResourceDefaults:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        expected = [Limit.per_minute("rpm", 100)]
        mock_limiter.get_resource_defaults.return_value = expected

        result = session.get_resource_defaults("gpt-4")

        assert result == expected
        mock_limiter.get_resource_defaults.assert_called_once_with("gpt-4")
        assert mock_event.fire.call_args.kwargs["request_type"] == "GET_RESOURCE_DEFAULTS"
        assert mock_event.fire.call_args.kwargs["name"] == "gpt-4"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.get_resource_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.get_resource_defaults("gpt-4")
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestDeleteResourceDefaults:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        session.delete_resource_defaults("gpt-4")

        mock_limiter.delete_resource_defaults.assert_called_once_with("gpt-4")
        assert mock_event.fire.call_args.kwargs["request_type"] == "DELETE_RESOURCE_DEFAULTS"
        assert mock_event.fire.call_args.kwargs["name"] == "gpt-4"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.delete_resource_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.delete_resource_defaults("gpt-4")
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestListResourcesWithDefaults:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        mock_limiter.list_resources_with_defaults.return_value = ["gpt-4", "gpt-3.5"]

        result = session.list_resources_with_defaults()

        assert result == ["gpt-4", "gpt-3.5"]
        assert mock_event.fire.call_args.kwargs["request_type"] == "LIST_RESOURCES_WITH_DEFAULTS"
        assert mock_event.fire.call_args.kwargs["name"] == "system"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.list_resources_with_defaults.side_effect = err
        with pytest.raises(RuntimeError):
            session.list_resources_with_defaults()
        assert mock_event.fire.call_args.kwargs["exception"] is err


# ---------------------------------------------------------------------------
# Entity management
# ---------------------------------------------------------------------------


class TestCreateEntity:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        entity = Entity(id="user-1")
        mock_limiter.create_entity.return_value = entity

        result = session.create_entity("user-1")

        assert result == entity
        mock_limiter.create_entity.assert_called_once_with(entity_id="user-1")
        assert mock_event.fire.call_args.kwargs["request_type"] == "CREATE_ENTITY"
        assert mock_event.fire.call_args.kwargs["name"] == "user-1"

    def test_passes_kwargs(self, session, mock_limiter):
        entity = Entity(id="user-1", parent_id="org-1")
        mock_limiter.create_entity.return_value = entity

        session.create_entity("user-1", parent_id="org-1", cascade=True)

        mock_limiter.create_entity.assert_called_once_with(
            entity_id="user-1", parent_id="org-1", cascade=True
        )

    def test_custom_name(self, session, mock_limiter, mock_event):
        mock_limiter.create_entity.return_value = Entity(id="user-1")
        session.create_entity("user-1", name="custom")
        assert mock_event.fire.call_args.kwargs["name"] == "custom"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.create_entity.side_effect = err
        with pytest.raises(RuntimeError):
            session.create_entity("user-1")
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestGetEntity:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        entity = Entity(id="user-1")
        mock_limiter.get_entity.return_value = entity

        result = session.get_entity("user-1")

        assert result == entity
        mock_limiter.get_entity.assert_called_once_with(entity_id="user-1")
        assert mock_event.fire.call_args.kwargs["request_type"] == "GET_ENTITY"
        assert mock_event.fire.call_args.kwargs["name"] == "user-1"

    def test_returns_none(self, session, mock_limiter):
        mock_limiter.get_entity.return_value = None
        assert session.get_entity("missing") is None

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.get_entity.side_effect = err
        with pytest.raises(RuntimeError):
            session.get_entity("user-1")
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestDeleteEntity:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        session.delete_entity("user-1")

        mock_limiter.delete_entity.assert_called_once_with(entity_id="user-1")
        assert mock_event.fire.call_args.kwargs["request_type"] == "DELETE_ENTITY"
        assert mock_event.fire.call_args.kwargs["name"] == "user-1"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.delete_entity.side_effect = err
        with pytest.raises(RuntimeError):
            session.delete_entity("user-1")
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestGetChildren:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        children = [Entity(id="child-1"), Entity(id="child-2")]
        mock_limiter.get_children.return_value = children

        result = session.get_children("org-1")

        assert result == children
        mock_limiter.get_children.assert_called_once_with(parent_id="org-1")
        assert mock_event.fire.call_args.kwargs["request_type"] == "GET_CHILDREN"
        assert mock_event.fire.call_args.kwargs["name"] == "org-1"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.get_children.side_effect = err
        with pytest.raises(RuntimeError):
            session.get_children("org-1")
        assert mock_event.fire.call_args.kwargs["exception"] is err


# ---------------------------------------------------------------------------
# Entity-level limits
# ---------------------------------------------------------------------------


class TestSetLimits:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        limits = [Limit.per_minute("rpm", 100)]
        session.set_limits("user-1", limits, resource="gpt-4")

        mock_limiter.set_limits.assert_called_once_with("user-1", limits, "gpt-4")
        assert mock_event.fire.call_args.kwargs["request_type"] == "SET_LIMITS"
        assert mock_event.fire.call_args.kwargs["name"] == "gpt-4"

    def test_default_resource(self, session, mock_limiter):
        session.set_limits("user-1", [])
        mock_limiter.set_limits.assert_called_once_with("user-1", [], "_default_")

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.set_limits.side_effect = err
        with pytest.raises(RuntimeError):
            session.set_limits("user-1", [])
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestGetLimits:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        expected = [Limit.per_minute("rpm", 100)]
        mock_limiter.get_limits.return_value = expected

        result = session.get_limits("user-1", resource="gpt-4")

        assert result == expected
        mock_limiter.get_limits.assert_called_once_with("user-1", "gpt-4")
        assert mock_event.fire.call_args.kwargs["request_type"] == "GET_LIMITS"

    def test_default_resource(self, session, mock_limiter):
        mock_limiter.get_limits.return_value = []
        session.get_limits("user-1")
        mock_limiter.get_limits.assert_called_once_with("user-1", "_default_")

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.get_limits.side_effect = err
        with pytest.raises(RuntimeError):
            session.get_limits("user-1")
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestDeleteLimits:
    def test_calls_limiter_and_fires_event(self, session, mock_limiter, mock_event):
        session.delete_limits("user-1", resource="gpt-4")

        mock_limiter.delete_limits.assert_called_once_with("user-1", "gpt-4")
        assert mock_event.fire.call_args.kwargs["request_type"] == "DELETE_LIMITS"

    def test_default_resource(self, session, mock_limiter):
        session.delete_limits("user-1")
        mock_limiter.delete_limits.assert_called_once_with("user-1", "_default_")

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.delete_limits.side_effect = err
        with pytest.raises(RuntimeError):
            session.delete_limits("user-1")
        assert mock_event.fire.call_args.kwargs["exception"] is err


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


class TestTimeUntilAvailable:
    def test_returns_result_and_fires_event(self, session, mock_limiter, mock_event):
        mock_limiter.time_until_available.return_value = 5.0

        result = session.time_until_available("user-1", "gpt-4", {"rpm": 1})

        assert result == 5.0
        mock_limiter.time_until_available.assert_called_once_with(
            entity_id="user-1", resource="gpt-4", needed={"rpm": 1}
        )
        assert mock_event.fire.call_args.kwargs["request_type"] == "TIME_UNTIL_AVAILABLE"
        assert mock_event.fire.call_args.kwargs["name"] == "gpt-4"

    def test_custom_name(self, session, mock_limiter, mock_event):
        mock_limiter.time_until_available.return_value = 0.0
        session.time_until_available("user-1", "gpt-4", {"rpm": 1}, name="custom")
        assert mock_event.fire.call_args.kwargs["name"] == "custom"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.time_until_available.side_effect = err
        with pytest.raises(RuntimeError):
            session.time_until_available("user-1", "gpt-4", {"rpm": 1})
        assert mock_event.fire.call_args.kwargs["exception"] is err


class TestIsAvailable:
    def test_returns_true_and_fires_event(self, session, mock_limiter, mock_event):
        mock_limiter.is_available.return_value = True

        result = session.is_available()

        assert result is True
        mock_limiter.is_available.assert_called_once_with(timeout=1.0)
        assert mock_event.fire.call_args.kwargs["request_type"] == "IS_AVAILABLE"
        assert mock_event.fire.call_args.kwargs["name"] == "system"

    def test_custom_timeout(self, session, mock_limiter):
        mock_limiter.is_available.return_value = False
        session.is_available(timeout=5.0)
        mock_limiter.is_available.assert_called_once_with(timeout=5.0)

    def test_custom_name(self, session, mock_limiter, mock_event):
        mock_limiter.is_available.return_value = True
        session.is_available(name="health")
        assert mock_event.fire.call_args.kwargs["name"] == "health"

    def test_exception_fires_with_error(self, session, mock_limiter, mock_event):
        err = RuntimeError("boom")
        mock_limiter.is_available.side_effect = err
        with pytest.raises(RuntimeError):
            session.is_available()
        assert mock_event.fire.call_args.kwargs["exception"] is err


# ---------------------------------------------------------------------------
# Timing verification
# ---------------------------------------------------------------------------


class TestTimingFired:
    """Verify that all methods fire events with positive response_time."""

    def test_response_time_is_positive(self, session, mock_limiter, mock_event):
        mock_limiter.get_system_defaults.return_value = ([], None)
        session.get_system_defaults()
        response_time = mock_event.fire.call_args.kwargs["response_time"]
        assert response_time >= 0

    def test_response_length_is_zero(self, session, mock_limiter, mock_event):
        mock_limiter.get_system_defaults.return_value = ([], None)
        session.get_system_defaults()
        assert mock_event.fire.call_args.kwargs["response_length"] == 0


# ---------------------------------------------------------------------------
# Acquire / Commit event split
# ---------------------------------------------------------------------------


class TestAcquireCommit:
    """Verify that acquire() fires separate ACQUIRE and COMMIT events."""

    def test_happy_path_fires_acquire_and_commit(self, session, mock_event):
        with session.acquire("user-1", "gpt-4", {"rpm": 1}):
            pass

        assert mock_event.fire.call_count == 2
        acquire_call = mock_event.fire.call_args_list[0].kwargs
        commit_call = mock_event.fire.call_args_list[1].kwargs

        assert acquire_call["request_type"] == "ACQUIRE"
        assert acquire_call["name"] == "gpt-4"
        assert acquire_call["exception"] is None

        assert commit_call["request_type"] == "COMMIT"
        assert commit_call["name"] == "gpt-4"
        assert commit_call["exception"] is None

    def test_acquire_failure_fires_acquire_only(self, session, mock_limiter, mock_event):
        err = RuntimeError("rate limit exceeded")
        mock_limiter.acquire.side_effect = err

        with pytest.raises(RuntimeError):
            with session.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass  # pragma: no cover

        assert mock_event.fire.call_count == 1
        call_kwargs = mock_event.fire.call_args.kwargs
        assert call_kwargs["request_type"] == "ACQUIRE"
        assert call_kwargs["exception"] is err

    def test_commit_failure_fires_both(self, session, mock_limiter, mock_event):
        commit_err = RuntimeError("transact_write failed")

        @contextmanager
        def failing_exit():
            yield MagicMock()
            raise commit_err

        mock_limiter.acquire.return_value = failing_exit()

        with pytest.raises(RuntimeError):
            with session.acquire("user-1", "gpt-4", {"rpm": 1}):
                pass

        assert mock_event.fire.call_count == 2
        acquire_call = mock_event.fire.call_args_list[0].kwargs
        commit_call = mock_event.fire.call_args_list[1].kwargs

        assert acquire_call["request_type"] == "ACQUIRE"
        assert acquire_call["exception"] is None

        assert commit_call["request_type"] == "COMMIT"
        assert commit_call["exception"] is commit_err

    def test_user_code_failure_no_commit_event(self, session, mock_event):
        with pytest.raises(ValueError, match="user error"):
            with session.acquire("user-1", "gpt-4", {"rpm": 1}):
                raise ValueError("user error")

        # Only ACQUIRE fires; user code failure is not our concern
        assert mock_event.fire.call_count == 1
        call_kwargs = mock_event.fire.call_args.kwargs
        assert call_kwargs["request_type"] == "ACQUIRE"
        assert call_kwargs["exception"] is None

    def test_commit_timing_excludes_user_code(self, session, mock_event):
        user_code_seconds = 0.05

        with session.acquire("user-1", "gpt-4", {"rpm": 1}):
            time.sleep(user_code_seconds)

        commit_call = mock_event.fire.call_args_list[1].kwargs
        assert commit_call["request_type"] == "COMMIT"
        # COMMIT response_time should be much less than the user code sleep
        assert commit_call["response_time"] < user_code_seconds * 1000
