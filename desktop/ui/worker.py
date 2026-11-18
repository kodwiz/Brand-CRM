"""Run blocking API calls off the UI thread so the window never freezes."""
from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal

from desktop.api.client import ApiError


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)  # ApiError


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except ApiError as e:
            self.signals.failed.emit(e)
        except Exception as e:  # never let a worker crash silently
            self.signals.failed.emit(ApiError(f"Unexpected error: {e}"))
        else:
            self.signals.done.emit(result)


_live: set[_Task] = set()


def run_async(fn: Callable[[], Any], on_done: Callable[[Any], None], on_error: Callable[[ApiError], None]) -> None:
    task = _Task(fn)
    _live.add(task)

    def _finish(cb):
        def inner(arg):
            _live.discard(task)
            cb(arg)
        return inner

    # QueuedConnection: the signals object lives on the UI thread, so callbacks always run there
    # (a plain callable connection would execute inside the worker thread).
    task.signals.done.connect(_finish(on_done), Qt.ConnectionType.QueuedConnection)
    task.signals.failed.connect(_finish(on_error), Qt.ConnectionType.QueuedConnection)
    QThreadPool.globalInstance().start(task)
