"""Background helpers that do not occupy the JobRunner.

The JobRunner serialises the user's jobs (scan, Quick video, Process). Preview loading,
histogram passes for the display panel and corner-crop reads are conveniences that must
never wait behind a two-hour export, so they run on Qt's global thread pool and report
back through a relay QObject that lives on the GUI thread. Callbacks therefore always run
on the GUI thread; the worker function itself must never touch widgets or AppContext.
"""
from __future__ import annotations

import inspect
import logging
import traceback
from typing import Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..engine.jobs import CancelToken, JobCancelled

log = logging.getLogger("etaluma.ui")

_LIVE: set["TaskRelay"] = set()


class TaskRelay(QObject):
    """Carries a worker's results to the GUI thread. Created on the GUI thread."""

    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    cancelled = Signal()

    def __init__(self, on_done=None, on_failed=None, on_progress=None, on_cancelled=None) -> None:
        super().__init__()
        self.token = CancelToken()
        self._on_done, self._on_failed = on_done, on_failed
        self._on_progress, self._on_cancelled = on_progress, on_cancelled
        # Receiver is this object (GUI thread), so emits from the pool are queued.
        self.done.connect(self._deliver_done)
        self.failed.connect(self._deliver_failed)
        self.progress.connect(self._deliver_progress)
        self.cancelled.connect(self._deliver_cancelled)

    def cancel(self) -> None:
        self.token.cancel()

    @Slot(object)
    def _deliver_done(self, result) -> None:
        try:
            if self._on_done is not None and not self.token.cancelled:
                self._on_done(result)
        finally:
            self._finish()

    @Slot(str)
    def _deliver_failed(self, message: str) -> None:
        try:
            if self._on_failed is not None:
                self._on_failed(message)
            else:
                log.warning("Background task failed: %s", message)
        finally:
            self._finish()

    @Slot(str)
    def _deliver_progress(self, message: str) -> None:
        if self._on_progress is not None and not self.token.cancelled:
            self._on_progress(message)

    @Slot()
    def _deliver_cancelled(self) -> None:
        try:
            if self._on_cancelled is not None:
                self._on_cancelled()
        finally:
            self._finish()

    def _finish(self) -> None:
        _LIVE.discard(self)
        self.deleteLater()


class _Task(QRunnable):
    def __init__(self, fn: Callable, relay: TaskRelay) -> None:
        super().__init__()
        self.fn = fn
        self.relay = relay
        self.setAutoDelete(True)

    def run(self) -> None:  # runs on a pool thread
        try:
            self._run()
        except RuntimeError as exc:  # the relay was deleted (window or app closed): nobody is listening
            log.debug("Background task result dropped: %s", exc)

    def _run(self) -> None:
        relay = self.relay
        try:
            params = inspect.signature(self.fn).parameters
            kwargs = {}
            if "progress" in params:
                kwargs["progress"] = relay.progress.emit
            if "cancel" in params:
                kwargs["cancel"] = relay.token
            result = self.fn(**kwargs)
        except JobCancelled:
            relay.cancelled.emit()
        except Exception as exc:  # reported on the GUI thread
            log.debug("Background task failed:\n%s", traceback.format_exc())
            relay.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            if relay.token.cancelled:
                relay.cancelled.emit()
            else:
                relay.done.emit(result)


def run_in_background(fn: Callable, on_done=None, on_failed=None, on_progress=None, on_cancelled=None) -> TaskRelay:
    """Run ``fn`` on the thread pool. ``fn`` may accept ``progress`` (str callable) and ``cancel`` (CancelToken).

    Returns the relay; call ``relay.cancel()`` to stop cooperatively. Callbacks run on the GUI thread.
    """
    relay = TaskRelay(on_done, on_failed, on_progress, on_cancelled)
    _LIVE.add(relay)
    QThreadPool.globalInstance().start(_Task(fn, relay))
    return relay


def wait_for_background(ms: int = 10000) -> bool:
    """Block until the pool is idle (tests and shutdown only)."""
    return QThreadPool.globalInstance().waitForDone(ms)
