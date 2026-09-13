"""Qt side of the job protocol: one engine call at a time in a worker thread.

The engine never imports Qt. It takes ``progress: Callable[[str], None]`` and
``cancel: CancelToken`` (``engine/jobs.py``) and raises ``JobCancelled`` when the
token is set. ``JobRunner`` wraps exactly that protocol:

    runner.start("quick", process_dataset, dataset, options, output)

``fn`` is called as ``fn(*args, progress=..., cancel=token, **kwargs)``; the two
keywords are dropped when the callable does not accept them. Progress strings are
logged to ``etaluma.engine`` at INFO and, when they contain a percentage, parsed
into ``percent``. Signals are emitted on the GUI thread.
"""
from __future__ import annotations

import inspect
import logging
import re
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Qt, Signal

from ..engine.jobs import CancelToken

log = logging.getLogger("etaluma.ui")
engine_log = logging.getLogger("etaluma.engine")


class EngineUnavailable(RuntimeError):
    """The engine facade (or one of its functions) is not importable yet."""


def engine():
    """Import the engine facade lazily.

    The UI must keep working while the engine port is in progress, so nothing
    imports ``etaluma_video.engine`` at module level; every call site asks here and
    shows the failure as a log line and a dialog instead of a traceback at startup.
    """
    try:
        from .. import engine as engine_module  # noqa: WPS433 - deliberate lazy import
    except Exception as exc:  # pragma: no cover - depends on the engine port
        raise EngineUnavailable(f"The processing engine could not be imported: {exc}") from exc
    return engine_module


def engine_attr(name: str):
    """One engine facade symbol, or EngineUnavailable with a message naming it."""
    mod = engine()
    value = getattr(mod, name, None)
    if value is None:
        raise EngineUnavailable(f"The processing engine does not provide {name}() yet.")
    return value

#: "12 %", "12%", "done 99.5 %" -> 12 / 99 (first match wins)
_PERCENT = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*%")


def parse_percent(message: str) -> int | None:
    """Percentage carried by a progress message, or None."""
    m = _PERCENT.search(message or "")
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:  # pragma: no cover - regex guarantees a number
        return None
    return max(0, min(100, int(value)))


def _accepts(fn: Callable, name: str) -> bool:
    """True when ``fn`` takes a keyword argument called ``name`` (or **kwargs)."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins, C callables
        return False
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if p.name == name and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY):
            return True
    return False


def _is_cancellation(exc: BaseException) -> bool:
    """JobCancelled, or a ported ProcessingCancelled, without importing either eagerly."""
    return "cancel" in type(exc).__name__.lower()


class _Worker(QObject):
    """Lives in the worker thread; owns nothing but the callable and the token."""

    progressed = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str, str)  # message, traceback text
    cancelled = Signal()

    def __init__(self, fn: Callable, args: tuple, kwargs: dict, token: CancelToken) -> None:
        super().__init__()
        self._fn, self._args, self._kwargs, self._token = fn, args, dict(kwargs), token

    def run(self) -> None:
        call_kwargs = dict(self._kwargs)
        if _accepts(self._fn, "progress") and "progress" not in call_kwargs:
            call_kwargs["progress"] = self.progressed.emit
        if _accepts(self._fn, "cancel") and "cancel" not in call_kwargs:
            call_kwargs["cancel"] = self._token
        try:
            result = self._fn(*self._args, **call_kwargs)
        except BaseException as exc:  # noqa: BLE001 - every failure is reported, never raised into Qt
            if _is_cancellation(exc):
                self.cancelled.emit()
            else:
                self.failed.emit(f"{type(exc).__name__}: {exc}", traceback.format_exc())
            return
        if self._token.cancelled:
            self.cancelled.emit()
        else:
            self.succeeded.emit(result)


class JobRunner(QObject):
    """One engine call at a time in a QThread. Widgets never block on the engine."""

    started = Signal(str)  # kind
    progress = Signal(str)  # message
    percent = Signal(int)  # 0..100, -1 = indeterminate
    finished = Signal(str, object)  # kind, result
    failed = Signal(str, str)  # kind, message
    cancelled = Signal(str)  # kind

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._token: CancelToken | None = None
        self._kind: str = ""
        self._busy = False
        self._last_message = ""

    # ---- state ---------------------------------------------------------- #
    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def kind(self) -> str:
        return self._kind

    @property
    def token(self) -> CancelToken | None:
        return self._token

    @property
    def last_message(self) -> str:
        return self._last_message

    # ---- control -------------------------------------------------------- #
    def start(self, kind: str, fn: Callable, *args: Any, **kwargs: Any) -> CancelToken:
        """Run ``fn`` in a worker thread. Raises RuntimeError when a job is running."""
        if self._busy:
            raise RuntimeError("A job is already running. Wait for it or cancel it first.")
        self._kind = str(kind)
        self._busy = True
        self._last_message = ""
        self._token = CancelToken()
        self._thread = QThread()
        self._thread.setObjectName(f"etaluma-job-{self._kind}")
        self._worker = _Worker(fn, args, kwargs, self._token)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progressed.connect(self._on_progress, Qt.ConnectionType.QueuedConnection)
        self._worker.succeeded.connect(self._on_succeeded, Qt.ConnectionType.QueuedConnection)
        self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
        self._worker.cancelled.connect(self._on_cancelled, Qt.ConnectionType.QueuedConnection)
        log.debug("JobRunner.start(kind=%r, fn=%s)", self._kind, getattr(fn, "__name__", fn))
        self.started.emit(self._kind)
        self.percent.emit(-1)
        self._thread.start()
        return self._token

    def cancel(self) -> bool:
        """Ask the running job to stop. Returns False when nothing is running."""
        if not self._busy or self._token is None:
            return False
        log.info("Cancelling %s…", self._kind or "the job")
        self._token.cancel()
        return True

    def wait(self, msecs: int = 10000) -> bool:
        """Block until the worker thread has finished (tests and application shutdown only)."""
        thread = self._thread
        if thread is None:
            return True
        return bool(thread.wait(msecs))

    # ---- worker callbacks (GUI thread) ---------------------------------- #
    def _on_progress(self, message: str) -> None:
        from ..engine.jobs import PROGRESS_PREFIX  # noqa: WPS433 - the engine is imported lazily here

        if message.startswith(PROGRESS_PREFIX):  # progress bar and status line only; Debug in the log
            message = message[len(PROGRESS_PREFIX):]
            engine_log.debug("%s", message)
        else:
            engine_log.info("%s", message)
        self._last_message = message
        self.progress.emit(message)
        pct = parse_percent(message)
        if pct is not None:
            self.percent.emit(pct)

    def _teardown(self) -> None:
        thread, self._thread = self._thread, None
        worker, self._worker = self._worker, None
        if thread is not None:
            thread.quit()
            thread.wait(10000)
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()
        self._busy = False

    def _on_succeeded(self, result: object) -> None:
        kind = self._kind
        self._teardown()
        self.percent.emit(100)
        self.finished.emit(kind, result)

    def _on_failed(self, message: str, tb: str) -> None:
        kind = self._kind
        self._teardown()
        log.debug("Job %r failed:\n%s", kind, tb)
        self.failed.emit(kind, message)

    def _on_cancelled(self) -> None:
        kind = self._kind
        self._teardown()
        self.cancelled.emit(kind)
