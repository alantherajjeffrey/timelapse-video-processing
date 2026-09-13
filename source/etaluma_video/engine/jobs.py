"""Progress and cooperative cancellation protocol shared by the engine, the CLI and the Qt job runner.

The engine never touches threads or Qt. Long operations accept ``progress`` (a callable taking one
string) and ``cancel`` (a CancelToken, or any zero-argument callable returning True when cancelled,
which is what Codex 0.3 used) and call ``check_cancel(cancel)`` between units of work.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

ProgressFn = Callable[[str], None]

#: Messages starting with this prefix only move the progress bar and the status line; the UI
#: logs them at DEBUG and the run folder's processing_log.txt leaves them out.
PROGRESS_PREFIX = "progress: "

log = logging.getLogger("etaluma.engine")


class JobCancelled(Exception):
    """Raised inside the engine when the caller asked to stop."""


class CancelToken:
    """Thread-safe cancellation flag. Callable so it also satisfies the Codex ``cancel()`` protocol."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def __call__(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise JobCancelled()


def check_cancel(cancel: Callable[[], bool] | CancelToken | None) -> None:
    """Raise JobCancelled if the token/callable reports cancellation."""
    if cancel is not None and cancel():
        raise JobCancelled()


def log_progress(message: str) -> None:
    """Default progress sink: the engine logger (the UI attaches a handler to it)."""
    if message.startswith(PROGRESS_PREFIX):
        log.debug(message[len(PROGRESS_PREFIX):])
    else:
        log.info(message)
