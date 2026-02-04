"""Tests for Lambda worker handler."""

from __future__ import annotations

import importlib
import types
from unittest.mock import MagicMock, patch

import pytest

# 'lambda' is a Python keyword, so we import via importlib
worker_mod = importlib.import_module("zae_limiter.load.lambda.worker")
handler = worker_mod.handler
_load_user_classes = worker_mod._load_user_classes


# Fake base class used as stand-in for locust.User in tests.
# Avoids importing the real locust.User which triggers gevent monkey-patching
# and causes RecursionError in the test process.
class _FakeUserBase:
    abstract = True


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


def _setup_fake_locust():
    """Set up fake locust.User in sys.modules.

    Returns a restore function to call in cleanup.
    """
    import sys

    locust_mod = sys.modules.get("locust")
    if locust_mod is None:
        locust_mod = types.ModuleType("locust")
        sys.modules["locust"] = locust_mod
        created = True
    else:
        created = False
    original = getattr(locust_mod, "User", None)
    locust_mod.User = _FakeUserBase  # type: ignore[attr-defined]

    def restore():
        if created:
            sys.modules.pop("locust", None)
        elif original is not None:
            locust_mod.User = original  # type: ignore[attr-defined]

    return restore


class TestLoadUserClasses:
    """Tests for _load_user_classes dynamic class loading."""

    @staticmethod
    def _make_mock_module(**attrs):
        """Create a mock module with given attributes."""
        mod = types.ModuleType("fake_locustfile")
        for k, v in attrs.items():
            setattr(mod, k, v)
        return mod

    @staticmethod
    def _make_user_class(name, abstract=False):
        """Create a fake Locust User subclass using a mock base class."""
        cls = type(name, (_FakeUserBase,), {"abstract": abstract})
        return cls

    @pytest.fixture(autouse=True)
    def _patch_locust_user(self):
        """Replace locust.User with _FakeUserBase to avoid gevent monkey-patching."""
        restore = _setup_fake_locust()
        yield
        restore()

    def test_loads_from_config(self):
        """Loads specific class from config user_classes."""
        my_user = self._make_user_class("MyUser")
        mock_mod = self._make_mock_module(MyUser=my_user)

        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            classes = _load_user_classes({"user_classes": "MyUser"})
            assert classes == [my_user]

    def test_loads_from_env_var(self, monkeypatch):
        """Loads class from LOCUST_USER_CLASSES env var when config empty."""
        my_user = self._make_user_class("MyUser")
        mock_mod = self._make_mock_module(MyUser=my_user)

        monkeypatch.setenv("LOCUST_USER_CLASSES", "MyUser")
        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            classes = _load_user_classes({})
            assert classes == [my_user]

    def test_auto_discovers(self, monkeypatch):
        """Auto-discovers non-abstract User subclasses from module."""
        discovered_cls = self._make_user_class("DiscoveredUser")
        mock_mod = self._make_mock_module(DiscoveredUser=discovered_cls)

        monkeypatch.delenv("LOCUST_USER_CLASSES", raising=False)
        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            classes = _load_user_classes({})
            assert discovered_cls in classes

    def test_config_precedence(self, monkeypatch):
        """Config user_classes takes precedence over env var."""
        cls_a = self._make_user_class("ClassA")
        cls_b = self._make_user_class("ClassB")
        mock_mod = self._make_mock_module(ClassA=cls_a, ClassB=cls_b)

        monkeypatch.setenv("LOCUST_USER_CLASSES", "ClassB")
        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            classes = _load_user_classes({"user_classes": "ClassA"})
            assert classes == [cls_a]

    def test_multiple_classes(self):
        """Loads multiple comma-separated classes."""
        cls_a = self._make_user_class("ClassA")
        cls_b = self._make_user_class("ClassB")
        mock_mod = self._make_mock_module(ClassA=cls_a, ClassB=cls_b)

        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            classes = _load_user_classes({"user_classes": "ClassA,ClassB"})
            assert classes == [cls_a, cls_b]

    def test_raises_class_not_found(self):
        """Raises ValueError when named class doesn't exist in module."""
        mock_mod = self._make_mock_module()

        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            with pytest.raises(ValueError, match="User class 'NoSuchClass' not found"):
                _load_user_classes({"user_classes": "NoSuchClass"})

    def test_raises_no_classes(self, monkeypatch):
        """Raises ValueError when no User subclasses found during auto-discovery."""
        mock_mod = self._make_mock_module()

        monkeypatch.delenv("LOCUST_USER_CLASSES", raising=False)
        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            with pytest.raises(ValueError, match="No User subclasses found"):
                _load_user_classes({})

    def test_custom_locustfile_module(self, monkeypatch):
        """Derives module path from LOCUSTFILE env var."""
        my_user = self._make_user_class("MyUser")
        mock_mod = self._make_mock_module(MyUser=my_user)

        monkeypatch.setenv("LOCUSTFILE", "my_locustfiles/api.py")
        monkeypatch.delenv("LOCUST_USER_CLASSES", raising=False)
        with patch.object(
            worker_mod.importlib, "import_module", return_value=mock_mod
        ) as mock_import:
            _load_user_classes({})
            mock_import.assert_called_once_with("my_locustfiles.api")
