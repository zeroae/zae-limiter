"""Tests for Lambda worker handler."""

from __future__ import annotations

import importlib
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("gevent")
pytestmark = pytest.mark.gevent

# 'lambda' is a Python keyword, so we import via importlib
worker_mod = importlib.import_module("zae_limiter.loadtest.lambda.worker")
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
        """Handler passes config dict and context to _run_headless."""
        config = {"users": 20, "duration_seconds": 120}
        with patch.object(worker_mod, "_run_headless") as mock_headless:
            mock_headless.return_value = {}
            handler({"config": config}, None)
            mock_headless.assert_called_once_with(config, None)

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

    def test_config_locustfile_takes_precedence_over_env_var(self, monkeypatch):
        """config["locustfile"] wins over LOCUSTFILE when resolving the module."""
        mock_mod = self._make_mock_module(MyUser=self._make_user_class("MyUser"))

        monkeypatch.setenv("LOCUSTFILE", "from_env.py")
        with patch.object(
            worker_mod.importlib, "import_module", return_value=mock_mod
        ) as mock_import:
            _load_user_classes({"locustfile": "locustfiles/from_config.py"})
            mock_import.assert_called_once_with("locustfiles.from_config")

    def test_default_module_is_locustfile(self, monkeypatch):
        """With neither config nor LOCUSTFILE, the module is ``locustfile``."""
        mock_mod = self._make_mock_module(MyUser=self._make_user_class("MyUser"))

        monkeypatch.delenv("LOCUSTFILE", raising=False)
        monkeypatch.delenv("LOCUST_USER_CLASSES", raising=False)
        with patch.object(
            worker_mod.importlib, "import_module", return_value=mock_mod
        ) as mock_import:
            _load_user_classes({})
            mock_import.assert_called_once_with("locustfile")

    def test_auto_discovery_skips_abstract_users(self, monkeypatch):
        """An ``abstract = True`` User subclass is not run."""
        concrete = self._make_user_class("Concrete")
        base = self._make_user_class("Base", abstract=True)
        mock_mod = self._make_mock_module(Concrete=concrete, Base=base)

        monkeypatch.delenv("LOCUST_USER_CLASSES", raising=False)
        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            assert _load_user_classes({}) == [concrete]

    def test_auto_discovery_skips_non_user_classes(self, monkeypatch):
        """Helper classes in the locustfile are not mistaken for users."""
        concrete = self._make_user_class("Concrete")
        helper = type("Helper", (), {})
        mock_mod = self._make_mock_module(Concrete=concrete, Helper=helper)

        monkeypatch.delenv("LOCUST_USER_CLASSES", raising=False)
        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            assert _load_user_classes({}) == [concrete]

    def test_class_names_are_trimmed(self):
        """``"ClassA, ClassB"`` loads both classes."""
        cls_a = self._make_user_class("ClassA")
        cls_b = self._make_user_class("ClassB")
        mock_mod = self._make_mock_module(ClassA=cls_a, ClassB=cls_b)

        with patch.object(worker_mod.importlib, "import_module", return_value=mock_mod):
            assert _load_user_classes({"user_classes": " ClassA,  ClassB "}) == [cls_a, cls_b]


def _session_client() -> Any:
    """``boto3.Session.client`` as the worker leaves it, untyped for stand-in calls."""
    import boto3

    return getattr(boto3.Session, "client")


class TestConfigureBoto3Pool:
    """Tests for _configure_boto3_pool, which patches boto3.Session.client once."""

    @pytest.fixture(autouse=True)
    def _isolate_boto3_session(self):
        """Give each test an unpatched boto3.Session.client and restore it after.

        handler() calls _configure_boto3_pool for real, so earlier tests in this
        process may already have patched boto3 and set the once-only flag.
        """
        import boto3

        original = boto3.Session.client
        had_flag = hasattr(worker_mod._configure_boto3_pool, "_configured")
        flag = getattr(worker_mod._configure_boto3_pool, "_configured", None)
        if had_flag:
            del worker_mod._configure_boto3_pool._configured
        self.calls: list[tuple[str, dict]] = []

        def recorder(_session, service_name, **kwargs):
            self.calls.append((service_name, kwargs))
            return service_name

        setattr(boto3.Session, "client", recorder)
        yield
        setattr(boto3.Session, "client", original)
        if had_flag:
            worker_mod._configure_boto3_pool._configured = flag
        elif hasattr(worker_mod._configure_boto3_pool, "_configured"):
            del worker_mod._configure_boto3_pool._configured

    def test_is_idempotent(self):
        """A second call does not wrap the already-patched client again."""

        worker_mod._configure_boto3_pool(max_connections=50)
        patched = _session_client()
        worker_mod._configure_boto3_pool(max_connections=7)
        assert _session_client() is patched

        _session_client()(object(), "dynamodb")
        assert self.calls[0][1]["config"].max_pool_connections == 50

    def test_applies_pool_size_to_dynamodb_clients(self):

        worker_mod._configure_boto3_pool(max_connections=50)
        _session_client()(object(), "dynamodb")

        service, kwargs = self.calls[0]
        assert service == "dynamodb"
        assert kwargs["config"].max_pool_connections == 50

    def test_leaves_an_explicit_dynamodb_config_alone(self):

        worker_mod._configure_boto3_pool(max_connections=50)
        mine = object()
        _session_client()(object(), "dynamodb", config=mine)
        assert self.calls[0][1]["config"] is mine

    def test_leaves_other_services_alone(self):

        worker_mod._configure_boto3_pool(max_connections=50)
        _session_client()(object(), "s3")
        assert self.calls[0] == ("s3", {})


class _FakeStats:
    """Stand-in for locust's aggregated ``env.stats.total`` entry."""

    num_requests = 120
    num_failures = 3
    avg_response_time = 12.5
    min_response_time = 4.0
    max_response_time = 80.0
    total_rps = 2.0
    fail_ratio = 0.025

    def get_response_time_percentile(self, pct):
        return {0.50: 10.0, 0.95: 40.0, 0.99: 70.0}[pct]


class _FakeEnvironment:
    """Stand-in for ``locust.env.Environment`` that records how it was driven."""

    instances: list[_FakeEnvironment] = []
    greenlet_len = 0
    """Size of the worker runner's greenlet group; 0 means the master stopped it."""

    def __init__(self, user_classes, events, host):
        self.user_classes = user_classes
        self.events = events
        self.host = host
        self.runner: Any = None
        self.stats = types.SimpleNamespace(total=_FakeStats())
        self.worker_runner_args = None
        self.unique_id_at_create = None
        _FakeEnvironment.instances.append(self)

    def create_local_runner(self):
        self.runner = MagicMock(name="local_runner")

    def create_worker_runner(self, master_host, master_port):
        import os

        self.worker_runner_args = (master_host, master_port)
        self.unique_id_at_create = os.environ.get("LOCUST_UNIQUE_ID")
        self.runner = MagicMock(name="worker_runner")
        self.runner.greenlet = MagicMock(name="greenlet_group")
        self.runner.greenlet.__len__.return_value = _FakeEnvironment.greenlet_len


def _context(remaining_ms, request_id=None):
    """A Lambda context returning ``remaining_ms`` values in turn."""
    ctx = types.SimpleNamespace(get_remaining_time_in_millis=MagicMock(side_effect=remaining_ms))
    if request_id is not None:
        ctx.aws_request_id = request_id
    return ctx


@pytest.fixture
def fake_locust():
    """Install fake ``locust`` and ``locust.env`` modules for one test.

    The real locust triggers gevent monkey-patching on import, which the test
    process must avoid (see _FakeUserBase).
    """
    import os
    import sys

    locust_mod = types.ModuleType("locust")
    locust_mod.User = _FakeUserBase  # type: ignore[attr-defined]
    locust_mod.events = MagicMock(name="global_events")  # type: ignore[attr-defined]
    env_mod = types.ModuleType("locust.env")
    env_mod.Environment = _FakeEnvironment  # type: ignore[attr-defined]
    _FakeEnvironment.instances = []
    _FakeEnvironment.greenlet_len = 0

    user = type("LoadUser", (_FakeUserBase,), {"abstract": False})
    with (
        patch.dict(sys.modules, {"locust": locust_mod, "locust.env": env_mod}),
        patch.dict(os.environ, {"TARGET_STACK_NAME": "limiter"}),
        patch.object(worker_mod, "_load_user_classes", return_value=[user]) as load,
    ):
        yield types.SimpleNamespace(module=locust_mod, user=user, load=load)


class TestRunHeadless:
    """Tests for _run_headless, the self-contained Lambda load test."""

    def test_runs_the_loaded_users_and_returns_stats(self, fake_locust):
        config = {"users": 20, "spawn_rate": 4, "duration_seconds": 10}
        with patch("gevent.sleep") as sleep:
            result = worker_mod._run_headless(config)

        fake_locust.load.assert_called_once_with(config)
        env = _FakeEnvironment.instances[0]
        assert env.user_classes == [fake_locust.user]
        assert env.events is fake_locust.module.events
        assert env.host == "limiter"
        env.runner.start.assert_called_once_with(20, spawn_rate=4)
        env.runner.quit.assert_called_once_with()
        assert [c.args[0] for c in sleep.call_args_list] == [5, 5]
        assert result == {
            "total_requests": 120,
            "total_failures": 3,
            "avg_response_time": 12.5,
            "min_response_time": 4.0,
            "max_response_time": 80.0,
            "p50": 10.0,
            "p95": 40.0,
            "p99": 70.0,
            "requests_per_second": 2.0,
            "failure_rate": 0.025,
        }

    def test_stops_early_when_the_lambda_is_about_to_time_out(self, fake_locust):
        # 100 s at start -> a 10 s buffer; 5 s left on the first check.
        ctx = _context([100_000, 5_000])
        with patch("gevent.sleep") as sleep:
            worker_mod._run_headless({"duration_seconds": 60}, ctx)

        sleep.assert_not_called()
        _FakeEnvironment.instances[0].runner.quit.assert_called_once_with()

    def test_shutdown_buffer_is_a_fraction_of_the_initial_time(self, fake_locust):
        # 200 s at start, 25% -> a 50 s buffer: 60 s left keeps running,
        # 49.999 s left stops.
        ctx = _context([200_000, 60_000, 49_999])
        with patch("gevent.sleep") as sleep:
            worker_mod._run_headless({"duration_seconds": 600}, ctx, shutdown_buffer_pct=0.25)

        sleep.assert_called_once_with(5)


class TestRunAsWorker:
    """Tests for _run_as_worker, which joins a Fargate master."""

    @pytest.fixture(autouse=True)
    def _restore_unique_id(self):
        import os

        with patch.dict(os.environ):
            yield

    def test_worker_id_comes_from_the_request_id(self, fake_locust):
        ctx = _context([100_000, 90_000], request_id="abcdef1234567890")
        result = worker_mod._run_as_worker({"master_host": "10.0.0.1"}, ctx)
        assert result["worker_id"] == "lambda_abcdef12"

    @pytest.mark.parametrize("has_context", [False, True], ids=["no-context", "no-request-id"])
    def test_worker_id_falls_back_to_a_uuid(self, fake_locust, has_context):
        """No context at all, or a context that carries no ``aws_request_id``."""
        ctx = _context([100_000, 90_000]) if has_context else None
        with patch("uuid.uuid4", return_value=types.SimpleNamespace(hex="0123456789abcdef")):
            result = worker_mod._run_as_worker({"master_host": "10.0.0.1"}, ctx)
        assert result["worker_id"] == "lambda_01234567"

    def test_unique_id_is_set_before_the_runner_is_created(self, fake_locust):
        ctx = _context([100_000, 90_000], request_id="feedface00000000")
        worker_mod._run_as_worker({"master_host": "10.0.0.1", "master_port": 6000}, ctx)

        env = _FakeEnvironment.instances[0]
        assert env.unique_id_at_create == "lambda_feedface"
        assert env.worker_runner_args == ("10.0.0.1", 6000)

    def test_quits_gracefully_when_the_lambda_is_about_to_time_out(self, fake_locust):
        # 100 s at start -> a 10 s buffer; 5 s left on the first check.
        ctx = _context([100_000, 5_000], request_id="abcdef1234567890")
        result = worker_mod._run_as_worker({"master_host": "10.0.0.1"}, ctx)

        runner = _FakeEnvironment.instances[0].runner
        runner.quit.assert_called_once_with()
        runner.greenlet.join.assert_not_called()
        assert result == {"status": "worker_completed", "worker_id": "lambda_abcdef12"}

    def test_shutdown_buffer_is_a_fraction_of_the_initial_time(self, fake_locust):
        # 200 s at start, 25% -> a 50 s buffer: 60 s left keeps waiting on the
        # master, 49.999 s left quits.
        _FakeEnvironment.greenlet_len = 1  # the master has not stopped the worker
        ctx = _context([200_000, 60_000, 49_999], request_id="abcdef1234567890")
        worker_mod._run_as_worker({"master_host": "10.0.0.1"}, ctx, shutdown_buffer_pct=0.25)

        runner = _FakeEnvironment.instances[0].runner
        runner.greenlet.join.assert_called_once_with(timeout=5)
        runner.quit.assert_called_once_with()

    def test_returns_completed_when_the_master_stops_the_worker(self, fake_locust):
        result = worker_mod._run_as_worker({"master_host": "10.0.0.1"}, None)

        runner = _FakeEnvironment.instances[0].runner
        runner.quit.assert_not_called()
        runner.greenlet.join.assert_called_once_with(timeout=5)
        assert result["status"] == "worker_completed"
        assert result["worker_id"].startswith("lambda_")
