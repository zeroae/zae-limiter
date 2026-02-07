"""Tests for load test orchestrator (Lambda worker pool management)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from zae_limiter.load.orchestrator import (
    LocustStats,
    ScalingConfig,
    WorkerPool,
    calculate_desired_workers,
    get_connected_workers,
    get_locust_stats,
    get_task_ip,
)


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

    def test_connected_decrease_preserves_pending(self):
        """Disconnections don't affect pending count."""
        pool = WorkerPool()
        pool.add_pending(5)
        pool.connected(actual_count=1, previous_count=3)
        assert pool.pending_count == 5

    def test_connected_moves_to_active(self):
        """When workers connect, they move from pending to active."""
        pool = WorkerPool()
        pool.add_pending(3)
        assert pool.pending_count == 3
        assert pool.active_count == 0

        pool.connected(actual_count=2, previous_count=0)
        assert pool.pending_count == 1
        assert pool.active_count == 2

    def test_connected_decrease_preserves_active(self):
        """When workers disconnect, active is NOT modified.

        Workers are only removed from active via mark_replaced().
        This ensures replacement workers' timestamps aren't accidentally
        removed when old workers disconnect after being replaced.
        """
        pool = WorkerPool()
        pool.add_pending(3)
        pool.connected(actual_count=3, previous_count=0)
        assert pool.active_count == 3

        # Disconnections don't remove from active
        pool.connected(actual_count=1, previous_count=3)
        assert pool.active_count == 3  # Still 3, not removed

    def test_count_expiring_soon_none_expiring(self):
        """No workers expiring when all are fresh."""
        pool = WorkerPool(lambda_timeout=300, replacement_pct=0.8)
        pool.add_pending(2)
        pool.connected(actual_count=2, previous_count=0)
        assert pool.count_expiring_soon() == 0

    def test_count_expiring_soon_with_old_workers(self):
        """Workers past replacement threshold are counted as expiring."""
        pool = WorkerPool(lambda_timeout=1.0, replacement_pct=0.5)
        pool.add_pending(2)
        pool.connected(actual_count=2, previous_count=0)
        # Wait past 50% of 1s timeout
        time.sleep(0.6)
        assert pool.count_expiring_soon() == 2

    def test_mark_replaced_removes_oldest(self):
        """mark_replaced removes oldest active workers."""
        pool = WorkerPool()
        pool.add_pending(3)
        pool.connected(actual_count=3, previous_count=0)
        assert pool.active_count == 3

        pool.mark_replaced(2)
        assert pool.active_count == 1

    def test_connected_adopts_untracked_workers(self):
        """Workers connected without going through pending are adopted."""
        pool = WorkerPool()
        # No pending workers, but 2 are connected (e.g., orchestrator restart)
        pool.connected(actual_count=2, previous_count=0)
        assert pool.active_count == 2  # Adopted the untracked workers

    def test_connected_adopts_partial_untracked(self):
        """Only untracked workers are adopted, pending ones go through normal flow."""
        pool = WorkerPool()
        pool.add_pending(1)  # 1 pending
        # 3 connected: 1 from pending, 2 untracked
        pool.connected(actual_count=3, previous_count=0)
        assert pool.pending_count == 0  # Pending moved to active
        assert pool.active_count == 3  # 1 from pending + 2 adopted


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


class TestGetLocustStats:
    """Tests for get_locust_stats."""

    def test_returns_stats_from_api(self):
        stats_data = {
            "user_count": 100,
            "total_rps": 50.5,
            "worker_count": 5,
            "state": "running",
        }
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(stats_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            stats = get_locust_stats()
            assert stats is not None
            assert stats.user_count == 100
            assert stats.total_rps == 50.5
            assert stats.worker_count == 5
            assert stats.state == "running"

    def test_returns_none_on_error(self):
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            assert get_locust_stats() is None

    def test_handles_missing_fields(self):
        stats_data = {}
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(stats_data).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            stats = get_locust_stats()
            assert stats is not None
            assert stats.user_count == 0
            assert stats.total_rps == 0.0
            assert stats.worker_count == 0


class TestScalingConfig:
    """Tests for ScalingConfig."""

    def test_default_values(self):
        config = ScalingConfig()
        assert config.users_per_worker == 10
        assert config.min_workers == 1
        assert config.max_workers is None
        assert config.startup_lead_time == 20.0

    def test_from_env(self):
        env = {
            "USERS_PER_WORKER": "5",
            "MIN_WORKERS": "2",
            "MAX_WORKERS": "100",
            "STARTUP_LEAD_TIME": "30",
        }
        with patch.dict("os.environ", env, clear=False):
            config = ScalingConfig.from_env()
            assert config.users_per_worker == 5
            assert config.min_workers == 2
            assert config.max_workers == 100
            assert config.startup_lead_time == 30.0

    def test_from_env_defaults(self):
        with patch.dict("os.environ", {}, clear=False):
            # Clear any potentially set env vars
            import os

            os.environ.pop("USERS_PER_WORKER", None)
            os.environ.pop("MIN_WORKERS", None)
            os.environ.pop("MAX_WORKERS", None)
            os.environ.pop("STARTUP_LEAD_TIME", None)

            config = ScalingConfig.from_env()
            assert config.users_per_worker == 10
            assert config.min_workers == 1
            assert config.max_workers is None
            assert config.startup_lead_time == 20.0


class TestCalculateDesiredWorkers:
    """Tests for calculate_desired_workers auto-scaling logic."""

    def test_returns_min_when_stats_none(self):
        config = ScalingConfig(min_workers=3)
        assert calculate_desired_workers(None, config) == 3

    def test_returns_min_when_test_stopped(self):
        config = ScalingConfig(min_workers=2)
        stats = LocustStats(user_count=100, total_rps=50, worker_count=5, state="stopped")
        assert calculate_desired_workers(stats, config) == 2

    def test_returns_min_when_test_ready(self):
        config = ScalingConfig(min_workers=2)
        stats = LocustStats(user_count=0, total_rps=0, worker_count=0, state="ready")
        assert calculate_desired_workers(stats, config) == 2

    def test_scales_by_users(self):
        config = ScalingConfig(users_per_worker=10, min_workers=1)
        # 100 users / 10 per worker = 10 workers
        stats = LocustStats(user_count=100, total_rps=10, worker_count=3, state="running")
        assert calculate_desired_workers(stats, config) == 10

    def test_scales_by_rps(self):
        config = ScalingConfig(users_per_worker=10, min_workers=1)
        # 50 users / 10 per worker = 5 workers (RPS no longer factors in)
        stats = LocustStats(user_count=50, total_rps=200, worker_count=2, state="running")
        assert calculate_desired_workers(stats, config) == 5

    def test_takes_max_of_users_and_rps(self):
        config = ScalingConfig(users_per_worker=10, min_workers=1)
        # 60 users / 10 per worker = 6 workers
        stats = LocustStats(user_count=60, total_rps=300, worker_count=3, state="running")
        assert calculate_desired_workers(stats, config) == 6

    def test_respects_min_workers(self):
        config = ScalingConfig(users_per_worker=10, min_workers=5)
        # Only needs 1 worker for 5 users, but min is 5
        stats = LocustStats(user_count=5, total_rps=5, worker_count=1, state="running")
        assert calculate_desired_workers(stats, config) == 5

    def test_respects_max_workers(self):
        config = ScalingConfig(users_per_worker=10, min_workers=1, max_workers=10)
        # Needs 100 workers for 1000 users, but max is 10
        stats = LocustStats(user_count=1000, total_rps=100, worker_count=5, state="running")
        assert calculate_desired_workers(stats, config) == 10

    def test_spawning_state_triggers_scaling(self):
        config = ScalingConfig(users_per_worker=10, min_workers=1)
        # During spawning, start scaling based on target
        stats = LocustStats(user_count=50, total_rps=10, worker_count=1, state="spawning")
        assert calculate_desired_workers(stats, config) == 5  # ceil(50/10)

    def test_rounds_up_workers(self):
        config = ScalingConfig(users_per_worker=10, min_workers=1)
        # 11 users should need 2 workers (ceil(11/10) = 2)
        stats = LocustStats(user_count=11, total_rps=1, worker_count=1, state="running")
        assert calculate_desired_workers(stats, config) == 2


class TestMain:
    """Tests for the main orchestration loop."""

    def test_invokes_workers_when_needed(self):
        """Main loop invokes Lambda workers to fill gap."""
        mock_lambda = MagicMock()
        call_count = 0

        def fake_get_stats():
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # 30 users / 10 per worker = 3 workers needed
                return LocustStats(user_count=30, total_rps=0, worker_count=0, state="running")
            return LocustStats(user_count=30, total_rps=0, worker_count=3, state="running")

        env = {
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "USERS_PER_WORKER": "10",
            "MIN_WORKERS": "1",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch(
                "zae_limiter.load.orchestrator.get_locust_stats",
                side_effect=fake_get_stats,
            ),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should have invoked 3 Lambda workers (30 users / 10 per worker)
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

        def fake_get_stats():
            # Fire SIGTERM handler, then return stats
            signal_handlers[15](15, None)
            return LocustStats(user_count=0, total_rps=0, worker_count=1, state="ready")

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch(
                "zae_limiter.load.orchestrator.get_locust_stats",
                side_effect=fake_get_stats,
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

        def fake_get_stats():
            # Always return 0 workers — workers never connect, so pending will expire
            return LocustStats(user_count=0, total_rps=0, worker_count=0, state="ready")

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
                "zae_limiter.load.orchestrator.get_locust_stats",
                side_effect=fake_get_stats,
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
        """Main loop handles master not ready (stats is None)."""
        mock_lambda = MagicMock()

        env = {
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "MIN_WORKERS": "2",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_locust_stats", return_value=None),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should still invoke min_workers when master not ready
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
                "zae_limiter.load.orchestrator.get_locust_stats",
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

        def fake_get_stats():
            return LocustStats(user_count=0, total_rps=0, worker_count=0, state="ready")

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
            patch("zae_limiter.load.orchestrator.get_locust_stats", side_effect=fake_get_stats),
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

        def fake_get_stats():
            return LocustStats(user_count=0, total_rps=0, worker_count=0, state="ready")

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
            patch("zae_limiter.load.orchestrator.get_locust_stats", side_effect=fake_get_stats),
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

        def fake_get_stats():
            return LocustStats(user_count=0, total_rps=0, worker_count=0, state="ready")

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
            patch("zae_limiter.load.orchestrator.get_locust_stats", side_effect=fake_get_stats),
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

    def test_auto_scaling_mode(self):
        """Main loop auto-scales based on user count."""
        mock_lambda = MagicMock()
        call_count = 0

        def fake_get_stats():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First poll: 40 users running, need 4 workers (40/10)
                return LocustStats(user_count=40, total_rps=10, worker_count=0, state="running")
            # Second poll: workers connected
            return LocustStats(user_count=40, total_rps=10, worker_count=4, state="running")

        env = {
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "USERS_PER_WORKER": "10",
            "MIN_WORKERS": "1",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_locust_stats", side_effect=fake_get_stats),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should have invoked 4 workers (40 users / 10 per worker)
            assert mock_lambda.invoke.call_count == 4

    def test_auto_scaling_by_rps(self):
        """Main loop scales by users only (RPS no longer drives scaling)."""
        mock_lambda = MagicMock()
        call_count = 0

        def fake_get_stats():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 40 users / 10 per worker = 4 workers
                return LocustStats(user_count=40, total_rps=200, worker_count=0, state="running")
            return LocustStats(user_count=40, total_rps=200, worker_count=4, state="running")

        env = {
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "USERS_PER_WORKER": "10",
            "MIN_WORKERS": "1",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_locust_stats", side_effect=fake_get_stats),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should have invoked 4 workers (40 users / 10 per worker)
            assert mock_lambda.invoke.call_count == 4

    def test_auto_scaling_scale_down(self):
        """Only needed workers are replaced when scaling down."""
        mock_lambda = MagicMock()
        call_count = 0

        def fake_get_stats():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First poll: 100 users = need 5 workers, but we have 10 connected.
                # On first poll, all 10 are adopted as near-expiring.
                # Orchestrator replaces 5 (needed) and lets 5 expire.
                return LocustStats(user_count=100, total_rps=10, worker_count=10, state="running")
            # Subsequent polls: same state
            return LocustStats(user_count=100, total_rps=10, worker_count=10, state="running")

        env = {
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "USERS_PER_WORKER": "20",  # 100 users / 20 = 5 workers needed
            "MIN_WORKERS": "1",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        with (
            patch.dict("os.environ", env, clear=False),
            patch("zae_limiter.load.orchestrator.get_task_ip", return_value="10.0.0.1"),
            patch("zae_limiter.load.orchestrator.get_locust_stats", side_effect=fake_get_stats),
            patch("zae_limiter.load.orchestrator.boto3") as mock_boto3,
            patch("time.sleep", side_effect=[None, KeyboardInterrupt]),
            patch("signal.signal"),
        ):
            mock_boto3.client.return_value = mock_lambda

            from zae_limiter.load.orchestrator import main

            with pytest.raises(KeyboardInterrupt):
                main()

            # Should replace only 5 workers (desired), letting the other 5 expire
            assert mock_lambda.invoke.call_count == 5

    def test_main_guard(self):
        """The if __name__ == '__main__' guard calls main()."""
        env = {
            "DESIRED_WORKERS": "1",
            "WORKER_FUNCTION_NAME": "test-worker",
            "MASTER_PORT": "5557",
            "POLL_INTERVAL": "0",
            "PENDING_TIMEOUT": "30",
            "ECS_CONTAINER_METADATA_URI_V4": "http://meta",
        }

        # Build a mock urlopen that returns metadata JSON
        metadata = json.dumps(
            {
                "Containers": [
                    {"Networks": [{"NetworkMode": "awsvpc", "IPv4Addresses": ["10.0.0.1"]}]}
                ]
            }
        ).encode()
        mock_response = MagicMock()
        mock_response.read.return_value = metadata
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with (
            patch.dict("os.environ", env, clear=False),
            patch("urllib.request.urlopen", return_value=mock_response),
            patch("boto3.client", return_value=MagicMock()),
            patch("time.sleep", side_effect=KeyboardInterrupt),
            patch("signal.signal"),
        ):
            import runpy

            with pytest.raises(KeyboardInterrupt):
                runpy.run_module(
                    "zae_limiter.load.orchestrator",
                    run_name="__main__",
                    alter_sys=True,
                )
