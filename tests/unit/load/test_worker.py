"""Tests for Lambda worker handler."""

from __future__ import annotations

import importlib
from unittest.mock import MagicMock, patch

# 'lambda' is a Python keyword, so we import via importlib
worker_mod = importlib.import_module("zae_limiter.load.lambda.worker")
handler = worker_mod.handler


class TestHandler:
    """Tests for the handler dispatch function."""

    def test_defaults_to_headless_mode(self):
        """Handler defaults to headless when mode not specified."""
        with patch.object(worker_mod, "_run_headless") as mock_headless:
            mock_headless.return_value = {"total_requests": 100}
            result = handler({"config": {}}, None)
            mock_headless.assert_called_once()
            assert result == {"total_requests": 100}

    def test_dispatches_headless_explicitly(self):
        """Handler dispatches to headless when mode=headless."""
        with patch.object(worker_mod, "_run_headless") as mock_headless:
            mock_headless.return_value = {"total_requests": 50}
            result = handler({"config": {"mode": "headless"}}, None)
            mock_headless.assert_called_once()
            assert result == {"total_requests": 50}

    def test_dispatches_to_worker_mode(self):
        """Handler dispatches to worker mode when mode=worker."""
        with patch.object(worker_mod, "_run_as_worker") as mock_worker:
            mock_worker.return_value = {"status": "worker_completed"}
            result = handler({"config": {"mode": "worker"}}, MagicMock())
            mock_worker.assert_called_once()
            assert result["status"] == "worker_completed"

    def test_passes_context_to_worker(self):
        """Handler passes context to _run_as_worker."""
        mock_context = MagicMock()
        with patch.object(worker_mod, "_run_as_worker") as mock_worker:
            mock_worker.return_value = {"status": "done"}
            handler({"config": {"mode": "worker"}}, mock_context)
            args = mock_worker.call_args
            assert args[0][1] is mock_context

    def test_sets_target_stack_name(self):
        """Handler sets TARGET_STACK_NAME from config."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=False):
                handler({"config": {"target_stack_name": "my-limiter"}}, None)

                import os

                assert os.environ["TARGET_STACK_NAME"] == "my-limiter"

    def test_does_not_set_target_stack_name_when_missing(self):
        """Handler skips TARGET_STACK_NAME when not in config."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=True):
                handler({"config": {}}, None)

                import os

                assert "TARGET_STACK_NAME" not in os.environ

    def test_sets_baseline_rpm(self):
        """Handler sets BASELINE_RPM from config."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=False):
                handler({"config": {"baseline_rpm": 200}}, None)

                import os

                assert os.environ["BASELINE_RPM"] == "200"

    def test_sets_spike_config(self):
        """Handler sets spike parameters from config."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=False):
                handler(
                    {"config": {"spike_rpm": 800, "spike_probability": 0.05}},
                    None,
                )

                import os

                assert os.environ["SPIKE_RPM"] == "800"
                assert os.environ["SPIKE_PROBABILITY"] == "0.05"

    def test_default_environment_variables(self):
        """Handler uses default values when config is minimal."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=False):
                handler({"config": {}}, None)

                import os

                assert os.environ["BASELINE_RPM"] == "400"
                assert os.environ["SPIKE_RPM"] == "1500"
                assert os.environ["SPIKE_PROBABILITY"] == "0.1"

    def test_sets_target_region_default(self):
        """Handler defaults TARGET_REGION to us-east-1."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=False):
                handler({"config": {}}, None)

                import os

                assert os.environ["TARGET_REGION"] == "us-east-1"

    def test_sets_target_region_from_config(self):
        """Handler sets TARGET_REGION from config."""
        import os

        # Remove any leftover from previous tests since handler uses setdefault
        os.environ.pop("TARGET_REGION", None)
        with patch.object(worker_mod, "_run_headless", return_value={}):
            with patch.dict("os.environ", {}, clear=False):
                handler({"config": {"region": "eu-west-1"}}, None)
                assert os.environ["TARGET_REGION"] == "eu-west-1"

    def test_empty_event_defaults(self):
        """Handler works with empty event."""
        with patch.object(worker_mod, "_run_headless", return_value={}):
            handler({}, None)

    def test_headless_receives_config(self):
        """Handler passes config dict to _run_headless."""
        config = {"users": 20, "duration_seconds": 120}
        with patch.object(worker_mod, "_run_headless") as mock_headless:
            mock_headless.return_value = {}
            handler({"config": config}, None)
            mock_headless.assert_called_once_with(config)

    def test_worker_receives_config_and_context(self):
        """Handler passes config and context to _run_as_worker."""
        config = {"mode": "worker", "master_host": "10.0.0.1"}
        mock_context = MagicMock()
        with patch.object(worker_mod, "_run_as_worker") as mock_worker:
            mock_worker.return_value = {}
            handler({"config": config}, mock_context)
            mock_worker.assert_called_once_with(config, mock_context)
