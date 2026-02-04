"""Tests for load test orchestrator (Lambda worker pool management)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter.load.orchestrator import WorkerPool, get_connected_workers, get_task_ip


class TestWorkerPool:
    """Tests for the WorkerPool closed-loop controller."""

    def test_initial_state(self):
        pool = WorkerPool(pending_timeout=30.0)
        assert pool.pending_count == 0

    def test_add_pending(self):
        pool = WorkerPool()
        pool.add_pending(3)
        assert pool.pending_count == 3

    def test_add_pending_default_count(self):
        pool = WorkerPool()
        pool.add_pending()
        assert pool.pending_count == 1

    def test_expire_old_removes_expired(self):
        pool = WorkerPool(pending_timeout=0.1)
        pool.add_pending(2)
        time.sleep(0.15)
        expired = pool.expire_old()
        assert expired == 2
        assert pool.pending_count == 0

    def test_expire_old_keeps_recent(self):
        pool = WorkerPool(pending_timeout=10.0)
        pool.add_pending(3)
        expired = pool.expire_old()
        assert expired == 0
        assert pool.pending_count == 3

    def test_connected_reduces_pending(self):
        pool = WorkerPool()
        pool.add_pending(5)
        pool.connected(actual_count=3, previous_count=0)
        assert pool.pending_count == 2

    def test_connected_no_change(self):
        pool = WorkerPool()
        pool.add_pending(5)
        pool.connected(actual_count=3, previous_count=3)
        assert pool.pending_count == 5

    def test_connected_with_empty_pending(self):
        pool = WorkerPool()
        pool.connected(actual_count=3, previous_count=0)
        assert pool.pending_count == 0

    def test_connected_decrease_ignored(self):
        pool = WorkerPool()
        pool.add_pending(5)
        pool.connected(actual_count=1, previous_count=3)
        assert pool.pending_count == 5


class TestGetTaskIp:
    """Tests for get_task_ip."""

    def test_raises_when_env_not_set(self):
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="ECS_CONTAINER_METADATA_URI_V4 not set"):
                get_task_ip()

    def test_returns_ip_from_metadata(self):
        metadata = {
            "Containers": [
                {
                    "Networks": [
                        {
                            "NetworkMode": "awsvpc",
                            "IPv4Addresses": ["10.0.1.42"],
                        }
                    ]
                }
            ]
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(metadata).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4"}):
            with patch("urllib.request.urlopen", return_value=mock_response):
                ip = get_task_ip()
                assert ip == "10.0.1.42"

    def test_raises_when_no_ip_found(self):
        metadata = {"Containers": [{"Networks": []}]}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(metadata).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch.dict("os.environ", {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4"}):
            with patch("urllib.request.urlopen", return_value=mock_response):
                with pytest.raises(RuntimeError, match="Could not determine task IP"):
                    get_task_ip()


class TestGetConnectedWorkers:
    """Tests for get_connected_workers."""

    def test_returns_worker_count(self):
        stats_data = {"worker_count": 5}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(stats_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            assert get_connected_workers() == 5

    def test_returns_zero_when_no_workers(self):
        stats_data = {"worker_count": 0}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(stats_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            assert get_connected_workers() == 0

    def test_returns_negative_one_on_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            assert get_connected_workers() == -1

    def test_returns_zero_when_worker_count_none(self):
        stats_data = {}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(stats_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            assert get_connected_workers() == 0


class TestMain:
    """Tests for the main orchestration loop."""

    def test_invokes_workers_when_needed(self):
        """Main loop invokes Lambda workers to fill gap."""
        mock_lambda = MagicMock()
        call_count = 0

        def fake_get_connected():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return 0
            return 3  # workers connected

        env = {
            "DESIRED_WORKERS": "3",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch(
                "zae_limiter.load.orchestrator.get_connected_workers",
                side_effect=fake_get_connected,
            ),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should have invoked Lambda workers
            assert mock_lambda.invoke.call_count == 3

    def test_shutdown_on_sigterm(self):
        """Main loop exits cleanly on shutdown signal."""
        env = {
            "DESIRED_WORKERS": "1",
            "WORKER_FUNCTION_NAME": "test-worker",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
        }

        signal_handlers = {}

        def capture_signal(signum, handler):
            signal_handlers[signum] = handler

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch(
                "zae_limiter.load.orchestrator.get_connected_workers",
                side_effect=lambda: signal_handlers[15](15, None)
                or 1,  # fire SIGTERM handler  # noqa: E501
            ),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep"),
            patch("signal.signal", side_effect=capture_signal),
        ):
            mock_boto3.client.return_value = MagicMock()
            from zae_limiter.load.orchestrator import main

            main()  # Should exit cleanly after SIGTERM

    def test_logs_expired_pending(self):
        """Main loop logs warning when pending invocations expire."""
        mock_lambda = MagicMock()
        call_count = 0

        def fake_get_connected():
            nonlocal call_count
            call_count += 1
            # Always return 0 — workers never connect, so pending will expire
            return 0

        env = {
            "DESIRED_WORKERS": "2",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "0",  # Expire immediately
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch(
                "zae_limiter.load.orchestrator.get_connected_workers",
                side_effect=fake_get_connected,
            ),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

    def test_master_not_ready_branch(self):
        """Main loop handles master not ready (connected < 0)."""
        mock_lambda = MagicMock()

        env = {
            "DESIRED_WORKERS": "2",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_connected_workers", return_value=-1),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should still invoke workers when master not ready
            assert mock_lambda.invoke.call_count == 2

    def test_exception_in_loop_is_caught(self):
        """Main loop catches and logs exceptions without crashing."""
        env = {
            "DESIRED_WORKERS": "1",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch(
                "zae_limiter.load.orchestrator.get_connected_workers",
                side_effect=[RuntimeError("connection error"), KeyboardInterrupt],
            ),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep"),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = MagicMock()

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

    def test_forwards_user_classes_env(self):
        """Main loop includes user_classes in Lambda payload when LOCUST_USER_CLASSES is set."""
        mock_lambda = MagicMock()

        env = {
            "DESIRED_WORKERS": "1",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "LOCUST_USER_CLASSES": "MyUser,OtherUser",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_connected_workers", return_value=0),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Check that the payload contains user_classes
            call_args = mock_lambda.invoke.call_args
            payload = json.loads(call_args[1]["Payload"])
            assert payload["config"]["user_classes"] == "MyUser,OtherUser"

    def test_forwards_locustfile_env(self):
        """Main loop includes locustfile in Lambda payload when LOCUSTFILE is set."""
        mock_lambda = MagicMock()

        env = {
            "DESIRED_WORKERS": "1",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "LOCUSTFILE": "my_locustfiles/api.py",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_connected_workers", return_value=0),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            call_args = mock_lambda.invoke.call_args
            payload = json.loads(call_args[1]["Payload"])
            assert payload["config"]["locustfile"] == "my_locustfiles/api.py"

    def test_no_extras_by_default(self):
        """Default payload omits user_classes and locustfile."""
        mock_lambda = MagicMock()

        env = {
            "DESIRED_WORKERS": "1",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_connected_workers", return_value=0),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            # Remove env vars that might leak from other tests
            import os

            from zae_limiter.load.orchestrator import main

            os.environ.pop("LOCUST_USER_CLASSES", None)
            os.environ.pop("LOCUSTFILE", None)

            with pytest.raises(KeyboardInterrupt):
                main()

            call_args = mock_lambda.invoke.call_args
            payload = json.loads(call_args[1]["Payload"])
            assert "user_classes" not in payload["config"]
            assert "locustfile" not in payload["config"]
