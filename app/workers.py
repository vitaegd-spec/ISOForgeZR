"""Background worker threads so long-running build/extract/open
operations never freeze the UI.

Cancellation
=============
Every `TaskWorker` owns its own `CancelToken` (see `fileops.CancelToken`)
and injects it into the wrapped function's keyword arguments -- but ONLY
if that function actually declares a `cancel_token` parameter, checked
via `_accepts_cancel_token()`. That guard matters: several callers passed
to `TaskWorker` (e.g. opening a file, which is normally fast enough that
cancelling it isn't worth supporting yet) don't accept a `cancel_token`
kwarg at all, and unconditionally injecting one would raise a plain
`TypeError` for every such call. Requiring the target function to
declare the parameter -- exactly the same convention already used for
`progress_cb` -- means cancellability is opt-in per operation, and
`main_window.ProgressDialog` uses the same check to decide whether to
even show a Cancel button, so the button is never shown for an operation
that can't actually honour it.
"""
from __future__ import annotations

import inspect

from PySide6.QtCore import QThread, Signal

from .fileops import CancelToken, OperationCancelled


def _accepts_cancel_token(fn) -> bool:
    """True if `fn` declares a `cancel_token` parameter (by name --
    covers both a plain keyword parameter and `**kwargs`-style catch-alls
    that explicitly list it)."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return "cancel_token" in params


class TaskWorker(QThread):
    progress = Signal(str, int, int)
    finished_ok = Signal(object)
    failed = Signal(str)
    # Emitted instead of `failed` when the operation stopped because the
    # user clicked Cancel, rather than because something went wrong --
    # kept as a separate signal so the UI can respond without showing an
    # error dialog for what was a deliberate, successful cancellation.
    cancelled = Signal()

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.cancel_token = CancelToken()

    def run(self):
        try:
            def progress_cb(label, done, total):
                self.progress.emit(label, done, total)

            if "progress_cb" not in self._kwargs:
                self._kwargs["progress_cb"] = progress_cb
            if "cancel_token" not in self._kwargs and _accepts_cancel_token(self._fn):
                self._kwargs["cancel_token"] = self.cancel_token
            result = self._fn(*self._args, **self._kwargs)
            self.finished_ok.emit(result)
        except OperationCancelled:
            self.cancelled.emit()
        except Exception as exc:  # pragma: no cover
            self.failed.emit(str(exc))
