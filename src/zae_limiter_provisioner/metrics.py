"""Opt-in timing metrics for one provisioner invocation (debug mode).

An apply that takes minutes in production cannot be profiled from moto: what
dominates there is round-trip latency, botocore CPU on a small Lambda, and
throttling, none of which an emulator reproduces. Debug mode measures them in
place and reports them in two places:

* one JSON line on stdout prefixed ``ZAEL_PROVISIONER_METRICS``, so CloudWatch
  Logs Insights can filter it (``filter @message like /ZAEL_PROVISIONER_METRICS/``);
* the invocation's response, under ``debug``, which ``limits plan|apply|diff
  --debug`` prints.

A run killed by the function timeout never reaches its final report, which is
exactly the run worth measuring, so a snapshot of everything so far is also
printed every ``HEARTBEAT_SECONDS`` as a ``ZAEL_PROVISIONER_METRICS_PARTIAL``
line. The last one before the timeout shows which phase it died in.

Enabled per invocation by the event's ``debug: true`` (the CLI's ``--debug``,
or a ``Debug: true`` property on ``Custom::ZaeLimiterLimits``), or for every
invocation by the function's ``ZAEL_PROVISIONER_DEBUG`` environment variable.

The report carries **no** entity ids, resource names or limit values: only
counts, operation names, levels (``system`` / ``resource`` / ``entity``),
fan-out kinds and timings, so it can be carried out of a restricted
environment without leaking what the manifest declares.

Disabled, every hook here is a no-op (``NULL_METRICS``), and nothing is
registered on the DynamoDB client.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

#: Environment variable that turns debug metrics on for every invocation.
DEBUG_ENV = "ZAEL_PROVISIONER_DEBUG"
#: Prefix of the log line carrying the metrics, for Logs Insights filters.
LOG_PREFIX = "ZAEL_PROVISIONER_METRICS"
#: Prefix of the periodic snapshot lines (see ``ApplyMetrics.start_heartbeat``).
PARTIAL_PREFIX = "ZAEL_PROVISIONER_METRICS_PARTIAL"
#: Seconds between partial snapshots.
HEARTBEAT_SECONDS = 30.0

# When this module was first imported: the first invocation of a fresh
# execution environment reports itself as a cold start.
_IMPORTED_AT = time.perf_counter()
_invocations = 0
_invocations_lock = threading.Lock()


def debug_requested(event: dict[str, Any]) -> bool:
    """Whether an invocation asked for metrics, by its event or the environment."""
    flag = event.get("debug")
    if flag is None and isinstance(event.get("ResourceProperties"), dict):
        flag = event["ResourceProperties"].get("Debug")
    if flag is True or (isinstance(flag, str) and flag.lower() == "true"):
        return True
    return os.environ.get(DEBUG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def _summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "total_ms": round(sum(ordered), 1),
        "p50_ms": round(_percentile(ordered, 0.50), 1),
        "p95_ms": round(_percentile(ordered, 0.95), 1),
        "p99_ms": round(_percentile(ordered, 0.99), 1),
        "max_ms": round(ordered[-1], 1) if ordered else 0.0,
    }


class ApplyMetrics:
    """Thread-safe timings for one invocation. Fan-outs run on a thread pool."""

    enabled = True

    def __init__(self, context: Any = None) -> None:
        global _invocations
        with _invocations_lock:
            _invocations += 1
            self._invocation = _invocations
        self._start = time.perf_counter()
        self._lock = threading.Lock()
        self._phases: dict[str, float] = {}
        self._calls: dict[str, list[float]] = {}
        self._errors: dict[str, dict[str, int]] = {}
        self._retries: dict[str, int] = {}
        self._fanouts: dict[str, list[float]] = {}
        self._counts: dict[str, Any] = {}
        self._clients = 0
        self._memory_mb = _int_attr(context, "memory_limit_in_mb")
        self._remaining_ms = _remaining(context)
        self._current_phase: str | None = None
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    def start_heartbeat(self, interval: float = HEARTBEAT_SECONDS) -> None:
        """Print a partial snapshot every ``interval`` seconds until ``emit``."""

        def _beat() -> None:
            while not self._stop.wait(interval):
                report = self.as_dict()
                print(f"{PARTIAL_PREFIX} {json.dumps(report, sort_keys=True)}", flush=True)

        self._heartbeat = threading.Thread(target=_beat, name="zael-metrics", daemon=True)
        self._heartbeat.start()

    # -- recording -------------------------------------------------------

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Time a step of the invocation; repeated names accumulate."""
        start = time.perf_counter()
        with self._lock:
            self._current_phase = name
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            with self._lock:
                self._phases[name] = self._phases.get(name, 0.0) + elapsed
                self._current_phase = None

    @contextmanager
    def fanout(self, kind: str, level: str) -> Iterator[None]:
        """Time one owed fan-out, keyed by kind and level only (never the target)."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = (time.perf_counter() - start) * 1000
            with self._lock:
                self._fanouts.setdefault(f"{kind}:{level}", []).append(elapsed)

    def count(self, name: str, value: Any) -> None:
        with self._lock:
            self._counts[name] = value

    def instrument(self, client: Any) -> Any:
        """Time every DynamoDB call the client makes, retries included."""
        with self._lock:
            self._clients += 1
        events = getattr(getattr(client, "meta", None), "events", None)
        if events is None:
            return client
        events.register("before-parameter-build.dynamodb", self._before)
        events.register("after-call.dynamodb", self._after)
        events.register("after-call-error.dynamodb", self._after_error)
        return client

    def _before(self, context: dict[str, Any] | None = None, **_kwargs: Any) -> None:
        if context is not None:
            context["zael_t0"] = time.perf_counter()

    def _elapsed(self, context: dict[str, Any] | None) -> float | None:
        start = (context or {}).get("zael_t0")
        return None if start is None else (time.perf_counter() - start) * 1000

    def _after(
        self,
        model: Any = None,
        parsed: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        op = getattr(model, "name", "unknown")
        elapsed = self._elapsed(context)
        metadata = (parsed or {}).get("ResponseMetadata", {})
        code = (parsed or {}).get("Error", {}).get("Code")
        with self._lock:
            if elapsed is not None:
                self._calls.setdefault(op, []).append(elapsed)
            retries = metadata.get("RetryAttempts") or 0
            if retries:
                self._retries[op] = self._retries.get(op, 0) + int(retries)
            if code:
                errors = self._errors.setdefault(op, {})
                errors[code] = errors.get(code, 0) + 1

    def _after_error(
        self,
        model: Any = None,
        exception: BaseException | None = None,
        context: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> None:
        op = getattr(model, "name", "unknown")
        elapsed = self._elapsed(context)
        with self._lock:
            if elapsed is not None:
                self._calls.setdefault(op, []).append(elapsed)
            errors = self._errors.setdefault(op, {})
            name = type(exception).__name__ if exception is not None else "Exception"
            errors[name] = errors.get(name, 0) + 1

    # -- reporting -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            calls = {op: _summary(values) for op, values in sorted(self._calls.items())}
            for op, n in self._retries.items():
                calls.setdefault(op, _summary([]))["retries"] = n
            for op, errors in self._errors.items():
                calls.setdefault(op, _summary([]))["errors"] = dict(sorted(errors.items()))
            fanouts = {key: _summary(values) for key, values in sorted(self._fanouts.items())}
            return {
                "elapsed_ms": round((time.perf_counter() - self._start) * 1000, 1),
                "cold_start": self._invocation == 1,
                "init_to_start_ms": round((self._start - _IMPORTED_AT) * 1000, 1),
                "memory_limit_mb": self._memory_mb,
                "remaining_ms_at_start": self._remaining_ms,
                "cpu_count": os.cpu_count(),
                "clients_created": self._clients,
                "phases_ms": {k: round(v, 1) for k, v in self._phases.items()},
                "current_phase": self._current_phase,
                "counts": dict(self._counts),
                "dynamodb_calls_total": sum(c["count"] for c in calls.values()),
                "dynamodb_calls": calls,
                "fanouts": fanouts,
            }

    def emit(self) -> dict[str, Any]:
        """Print the metrics as one log line and return them for the response."""
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join(timeout=1.0)
        report = self.as_dict()
        # stdout, not `logging`: the Lambda runtime's log level would hide INFO.
        print(f"{LOG_PREFIX} {json.dumps(report, sort_keys=True)}", flush=True)
        return report


class _NullMetrics:
    """The disabled recorder: every hook is free and records nothing."""

    enabled = False

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        yield

    @contextmanager
    def fanout(self, kind: str, level: str) -> Iterator[None]:
        yield

    def count(self, name: str, value: Any) -> None:
        return None

    def instrument(self, client: Any) -> Any:
        return client


NULL_METRICS = _NullMetrics()


def _int_attr(context: Any, name: str) -> int | None:
    try:
        return int(getattr(context, name))
    except (AttributeError, TypeError, ValueError):
        return None


def _remaining(context: Any) -> int | None:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(remaining):
        return None
    try:
        value = remaining()
    except Exception:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None
