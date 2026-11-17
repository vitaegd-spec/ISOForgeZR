"""
fileops.py
----------
Small pieces shared by BOTH container backends, kept here rather than in
either one of them.

Why this module exists
=======================
`archive_backend` originally imported `BuildResult` and the atomic-replace
helper straight from `iso_backend`. That worked, but it quietly coupled
ZIP/7Z/RAR support to `pycdlib`: importing the archive backend dragged in
the ISO backend, which imports `pycdlib` at module level, so a user with
`py7zr` installed but not `pycdlib` couldn't open a ZIP at all.

Putting the genuinely shared pieces here breaks that dependency. Each
backend now imports only what it actually needs, and neither one depends
on the other.

Cancellation
=============
`OperationCancelled` / `CancelToken` support the Cancel button on the
build/extract progress dialog. See `OperationCancelled`'s docstring for
why it is deliberately a BaseException rather than an ordinary Exception
-- that single design choice is what lets both backends' many existing
`except Exception:` per-item resilience blocks (skip one bad file, keep
going) continue working completely unmodified, while still never
mistaking "the user asked to stop" for "this one file failed".
"""
from __future__ import annotations

import os
import threading
import time
from collections import namedtuple
from typing import Optional

# What a build hands back, regardless of which backend produced it:
# which items (if any) had to be left out, which format profile actually
# succeeded, and whether that meant falling back from the preferred one.
BuildResult = namedtuple(
    "BuildResult", ["skipped", "format_label", "used_fallback_format"]
)

_REPLACE_MAX_ATTEMPTS = 10
_REPLACE_RETRY_BASE_DELAY = 0.2


def quiet_remove(path: str) -> None:
    """Deletes `path` if present, ignoring any failure. Used for cleaning
    up temporary build files, where a failed delete must never mask the
    real error that prompted the cleanup."""
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def replace_with_retry(src: str, dst: str) -> None:
    """`os.replace()` with a short bounded retry.

    A freshly-written file can be transiently locked on Windows by
    antivirus real-time scanning, Search indexing, or a cloud-sync client
    such as OneDrive -- none of which ISOForge controls, and all of which
    release within moments. By the time this runs, every handle ISOForge
    itself held has already been closed, so a WinError 5/32 here reflects
    an external lock rather than anything left open locally.

    Only those two specific Windows error codes are retried; any other
    OSError (a missing destination folder, say) propagates immediately
    since retrying would never help.
    """
    last_exc: Optional[OSError] = None
    for attempt in range(_REPLACE_MAX_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32):
                last_exc = exc
            else:
                raise
        time.sleep(_REPLACE_RETRY_BASE_DELAY * (attempt + 1))
    if last_exc is not None:
        raise last_exc


# --------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------- #
class OperationCancelled(BaseException):
    """Raised to unwind a long-running build/extract when the user
    clicks Cancel.

    Deliberately a BaseException, not an ordinary Exception -- the same
    reasoning that makes KeyboardInterrupt and SystemExit BaseExceptions
    rather than Exceptions applies here just as directly. Both backends
    are full of `except Exception:` blocks that exist purely for
    per-item resilience: "this one file couldn't be added/read, skip it
    and keep going" (see e.g. `iso_backend._add_file_node` or
    `archive_backend._write_zip`'s per-member try/except). If this were
    an ordinary Exception, EVERY one of those blocks would need an
    explicit `except OperationCancelled: raise` added ahead of it, or
    clicking Cancel would silently get reinterpreted as "one file
    failed" and the build would carry on regardless.

    Making it a BaseException sidesteps that entirely: `except
    Exception:` never matches it, so it passes straight through every
    such block untouched, while `finally` clauses and context managers
    (which run for any exception type, matched or not) still perform
    their cleanup correctly. Only the small number of places that must
    react SPECIFICALLY to cancellation (the format-fallback loop in
    `iso_backend.build_iso`, the profile dispatch in
    `archive_backend.build_archive`, and `workers.TaskWorker.run`) catch
    it explicitly.
    """


class CancelToken:
    """A simple, thread-safe flag a long-running operation can be asked
    to check periodically.

    Deliberately minimal: no `wait()`/blocking method is exposed, since
    every caller here polls cooperatively between units of work rather
    than blocking on cancellation -- exposing a blocking wait would only
    invite a callsite to use it wrongly.

    `cancel()` is called from the GUI thread (when the user clicks
    Cancel); `raise_if_cancelled()` is called from the worker thread
    doing the actual build/extract. `threading.Event` is safe to use
    this way across threads without any extra locking.
    """

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise OperationCancelled()
