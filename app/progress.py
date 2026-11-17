"""
progress.py
-----------
Byte-accurate progress reporting for long ISO and archive operations.

Why this module exists
=======================
The progress bar originally counted FILES AND FOLDERS: one "tick" per
item added. That is badly misleading for anything large, because of how
pycdlib actually works:

    `iso.add_file()` / `iso.add_fp()` do NOT copy any file data. They only
    register where the data will come from, building an in-memory
    structure. EVERY byte of actual data movement happens inside the
    single `iso.write()` / `iso.write_fp()` call at the very end.

So building a 5 GB ISO looked like this:

    * ~1000 files registered almost instantly  -> 1000 ticks -> bar hits
      100% within a second
    * the write call then runs for a minute moving 5 GB  -> ONE call,
      zero progress updates -> bar sits frozen at 100%

The bar was measuring the cheap part and completely ignoring the
expensive part. Small ISOs hid the problem; large ones made it obvious.

Cancellation and the final backpatch pass -- a second, subtler stall
=====================================================================
After the main byte-progress fix above, a further real symptom was
reported: the bar reaching "X GB / ~X GB" -- i.e. every byte apparently
already written -- and then sitting there for a long time with no further
movement, and clicking Cancel appearing to do nothing at all during that
stretch.

Two independent things cause this, and both needed fixing:

1. **pycdlib patches the file backwards after writing it.** ISO9660 (and
   its Joliet/Rock Ridge/UDF extensions) can only finalise volume
   descriptors, path tables and certain checksums once the entire layout
   is known -- which isn't until every file has already been written.
   So after streaming out all the file data, `write_fp()` SEEKS BACKWARD
   and rewrites a number of earlier structures. Those are genuine
   `write()` calls, but the ORIGINAL `ProgressWriter.write()` only
   called `_report()` (and therefore only ever checked cancellation)
   when a write advanced the highest offset ever reached -- a backward
   patch write never does, so this entire pass was invisible to both the
   progress bar and to cancellation.

2. **The operating system can defer physically flushing buffered writes
   until the file is closed.** Every `write()` call handed several GB to
   the OS write cache extremely quickly (from Python's perspective, the
   bytes are "written" the instant the call returns), but the OS itself
   may not have actually pushed all of that to the physical disk yet --
   especially on a slow, external, or network drive, or when antivirus
   software scans a file synchronously as it closes. That flush then
   happens as ONE uninterruptible blocking operation when the file
   handle is finally closed, well after the last `write()` call this
   module can see, so there is no possible moment left at which to check
   cancellation.

The fixes below address each directly: `ProgressWriter.write()` now
reports (and checks cancellation) on EVERY call, not just forward-moving
ones, and periodically forces buffered data out to physical storage via
`os.fsync()` DURING the write loop rather than letting it all accumulate
for one blocking flush at the very end. Neither eliminates the
possibility of a slow final close() entirely -- a sufficiently slow
drive is a sufficiently slow drive -- but both substantially shrink the
window in which nothing is visible and nothing can be interrupted.
`iso_backend.py` and `archive_backend.py` additionally re-check the
cancel token immediately after such a blocking call returns and BEFORE
committing the result, so a cancellation that could not be delivered as
an exception while genuinely stuck inside the OS is still honoured the
moment control comes back to Python, rather than silently completing the
save regardless.

The model used instead
=======================
Progress is now measured in BYTES OF REAL WORK, split across the phases
that genuinely consume time:

  1. STAGING  - only files sourced from another container cost anything
                here, because those must be physically read out of the
                source into a temp spool. Files staged from local disk
                cost essentially nothing (pycdlib just records the path),
                and directories cost nothing at all.
  2. WRITING  - `iso.write_fp()` serializing the finished image. This is
                where nearly all the time goes for a folder->ISO build,
                and about half of it for an ISO->ISO re-save.

Each item also carries a small fixed overhead allowance
(_PER_ITEM_OVERHEAD_BYTES) so that a project made of thousands of tiny
files still shows smooth movement rather than appearing stalled.

That weighting makes the bar self-balancing without any special-casing:

    folder -> ISO   : staging ~0 bytes, writing ~5 GB
                      => bar spends ~all its time on the write. Correct.
    ISO -> ISO      : staging ~5 GB (read), writing ~5 GB
                      => bar splits roughly 50/50. Also correct.

The archive backend reuses ProgressReporter directly. ZIP and 7Z
compress as they write, so they simply charge bytes as each member is
processed. Two formats do extra work and get a larger budget split
across phases: RAR stages to disk and then runs Rar.exe, while a
self-extracting EXE builds a payload archive and then copies stub and
payload out to the final file.

Emitting on a fixed 0..1000 scale
==================================
Qt's QProgressBar stores its range as a C++ `int`. Handing it raw byte
counts would overflow on anything past ~2.1 GB, so every value leaving
this module is normalised to a fixed 0..1000 ("permille") scale. Callers
therefore always receive `(label, 0..1000, 1000)` regardless of the real
byte totals involved.

Cancellation
=============
`ProgressReporter` optionally accepts a `cancel_token` (see
`fileops.CancelToken`). Since every backend operation reports progress
extremely frequently -- per file, per chunk, per RAR output line -- this
is the one place a cancellation check needs adding for it to be picked
up promptly almost everywhere, without threading an explicit check
through every loop in both backends individually. `advance()` and
`set_phase_position()` both check the token first and raise
`fileops.OperationCancelled` (a BaseException -- see that class's
docstring for why) if it has been set. `raise_if_cancelled()` exposes the
same check to a caller holding only the reporter (not the raw token),
used to perform one extra check immediately after a long blocking call
returns -- see "Cancellation and the final backpatch pass" above.
"""
from __future__ import annotations

import os
import time
from typing import Callable, Optional

from .fileops import OperationCancelled

# Values are emitted on a fixed 0..PROGRESS_SCALE range -- see the module
# docstring for why raw byte counts can't be handed to Qt directly.
PROGRESS_SCALE = 1000

# Notional cost charged per file/folder on top of its real byte size, so
# that a project of thousands of tiny files still animates smoothly
# instead of looking stalled. Deliberately small enough that it stays
# negligible next to genuinely large payloads.
_PER_ITEM_OVERHEAD_BYTES = 64 * 1024

# An ISO9660 logical sector. File data is laid out sector-aligned, so
# size estimates round up to this.
_SECTOR = 2048

# Rough per-item metadata cost inside the finished image (directory
# records duplicated across the ISO9660 / Joliet / Rock Ridge / UDF
# views), plus a flat allowance for volume descriptors, path tables and
# terminator padding. These only need to be in the right ballpark -- see
# ProgressWriter, which self-corrects if the real image outgrows the
# estimate.
_METADATA_SECTORS_PER_ITEM = 4
_FIXED_OVERHEAD_SECTORS = 512

# How many bytes to let accumulate in the OS write buffer before forcing
# them out to physical storage via os.fsync(), rather than letting the
# entire multi-GB image queue up for one uninterruptible flush when the
# file is finally closed. See the module docstring ("Cancellation and
# the final backpatch pass") for the full rationale -- this is the
# direct fix for the progress bar appearing frozen at "100%" for a long
# time on a slow, external, or network drive, with Cancel appearing to
# do nothing because no write() call (and therefore no cancellation
# check) occurred during that stretch.
#
# 64 MB is a deliberate middle ground: frequent enough that the final
# close() never has more than a fraction of the image left to flush in
# one go, while infrequent enough that the fsync overhead itself stays
# negligible next to the total write time on any reasonably fast drive.
_PERIODIC_SYNC_BYTES = 64 * 1024 * 1024


def _round_to_sector(n: int) -> int:
    if n <= 0:
        return 0
    return ((n + _SECTOR - 1) // _SECTOR) * _SECTOR


class ProgressReporter:
    """Accumulates work in byte-units and emits throttled, monotonic
    updates on the 0..PROGRESS_SCALE range.

    `progress_cb` keeps the existing `(label, done, total)` signature so
    nothing downstream needs to change.
    """

    def __init__(
        self,
        progress_cb: Optional[Callable[[str, int, int], None]],
        total_units: float,
        min_interval: float = 0.05,
        min_delta: int = 1,
        cancel_token=None,
    ):
        self._cb = progress_cb
        self._total = max(1.0, float(total_units))
        self._done = 0.0
        self._min_interval = min_interval
        self._min_delta = min_delta
        self._last_emit_time = 0.0
        self._last_emit_value = -1
        self._label = ""
        self._cancel_token = cancel_token
        # Progress is held just below the top until finish() is called,
        # so a slightly-off size estimate can never leave the bar
        # visually "complete" while work is still happening -- which was
        # the exact symptom being fixed here.
        self._ceiling = PROGRESS_SCALE - 1

    # -- cancellation --------------------------------------------------- #
    def _check_cancel(self) -> None:
        if self._cancel_token is not None:
            self._cancel_token.raise_if_cancelled()

    def raise_if_cancelled(self) -> None:
        """Lets a caller holding only the reporter (not the raw
        `CancelToken`) perform an explicit, one-off cancellation check --
        used immediately after a long blocking call returns (a write/
        flush/close that may have spent a long time stuck inside the OS
        with no `write()` calls of its own to check during), so a
        cancellation requested during that stretch is still honoured the
        moment control returns to Python, rather than being missed
        entirely because no in-progress check ever had the chance to
        fire. See this module's docstring for the full rationale.
        """
        self._check_cancel()

    # -- work accounting --------------------------------------------- #
    def advance(self, units: float, label: Optional[str] = None) -> None:
        """Adds `units` of completed work."""
        self._check_cancel()
        if units > 0:
            self._done += units
        if label is not None:
            self._label = label
        self._maybe_emit()

    def set_phase_position(self, base_units: float, phase_done: float,
                           label: Optional[str] = None) -> None:
        """Sets an ABSOLUTE position: everything before this phase
        (`base_units`) plus `phase_done` within it. Used by the write
        phase, where the natural progress signal is the output file's
        current offset rather than an increment."""
        self._check_cancel()
        self._done = base_units + max(0.0, phase_done)
        if label is not None:
            self._label = label
        self._maybe_emit()

    def finish(self, label: Optional[str] = None) -> None:
        """Forces a final 100% emit, bypassing throttling and the
        sub-maximum ceiling."""
        self._done = self._total
        if label is not None:
            self._label = label
        if self._cb:
            self._cb(self._label, PROGRESS_SCALE, PROGRESS_SCALE)
        self._last_emit_value = PROGRESS_SCALE
        self._last_emit_time = time.monotonic()

    # -- emission ----------------------------------------------------- #
    def _maybe_emit(self) -> None:
        if not self._cb:
            return

        value = int((self._done / self._total) * PROGRESS_SCALE)
        value = max(0, min(self._ceiling, value))

        # Never move backwards -- a wobbling bar reads as a bug even when
        # the underlying numbers are merely being refined.
        if value < self._last_emit_value:
            value = self._last_emit_value

        now = time.monotonic()
        moved_enough = (value - self._last_emit_value) >= self._min_delta
        waited_enough = (now - self._last_emit_time) >= self._min_interval

        # Throttle so a multi-GB write can't emit millions of
        # cross-thread signals and flood the Qt event loop.
        #
        # The two conditions are deliberately OR'd, not AND'd. AND-ing
        # them starves fast operations: a small/quick write completes in
        # well under the minimum interval, so almost every update gets
        # suppressed and the bar jumps in a few coarse leaps. OR-ing
        # means an update is emitted as soon as EITHER enough visible
        # movement has accumulated OR enough time has passed -- which
        # keeps fast writes smooth while still capping the signal rate on
        # slow ones (a 0..1000 scale caps total emissions at ~1000
        # anyway, since _min_delta of 1 permille is the finest step the
        # bar can actually render).
        if self._last_emit_value >= 0 and not (moved_enough or waited_enough):
            return

        self._last_emit_value = value
        self._last_emit_time = now
        self._cb(self._label, value, PROGRESS_SCALE)


class ProgressWriter:
    """A seekable file-like wrapper that reports progress as an ISO image
    is serialized to disk.

    pycdlib exposes `write_fp(outfp)` alongside `write(filename)`; by
    handing it one of these instead of a plain file, the otherwise-opaque
    minutes spent inside a single write call become continuously
    observable.

    Progress is tracked by the highest file offset reached rather than by
    cumulative bytes written, because pycdlib seeks backwards while
    patching volume descriptors -- counting raw writes would overshoot.

    Every write() call reports and checks cancellation -- not only ones
    that advance the highest offset reached -- and every
    `_PERIODIC_SYNC_BYTES` worth of data written triggers an explicit
    flush to physical storage. See this module's docstring
    ("Cancellation and the final backpatch pass") for why both of those
    are necessary, not just cosmetic.
    """

    def __init__(self, fp, expected_total_bytes: int, reporter: ProgressReporter,
                 base_units: float, label: str = "Writing image to disk"):
        self._fp = fp
        self._expected = max(1, int(expected_total_bytes))
        self._reporter = reporter
        self._base_units = base_units
        self._label = label
        self._max_pos = 0
        self._bytes_since_sync = 0

    # -- the file-like surface pycdlib needs -------------------------- #
    def write(self, data):
        written = self._fp.write(data)
        n = written if isinstance(written, int) else len(data)
        pos = self._fp.tell()
        if pos > self._max_pos:
            self._max_pos = pos

        # Always report -- and therefore always check cancellation --
        # regardless of whether THIS write advances the highest offset
        # ever reached. Gating this behind "pos > previous max" was the
        # root cause of the reported "stuck at 100%, Cancel does
        # nothing" symptom: pycdlib performs a final pass BACKWARD over
        # the file once the whole layout is known (patching volume
        # descriptors, path tables and checksums), and every one of
        # those patch writes is both a legitimate progress signal and a
        # legitimate opportunity to notice a cancellation request --
        # neither of which happened when this call was skipped for
        # non-advancing writes.
        self._report(pos)

        self._bytes_since_sync += max(0, n)
        if self._bytes_since_sync >= _PERIODIC_SYNC_BYTES:
            self._sync_to_disk()

        return written

    def seek(self, offset, whence=0):
        return self._fp.seek(offset, whence)

    def tell(self):
        return self._fp.tell()

    def flush(self):
        return self._fp.flush()

    def truncate(self, size=None):
        if size is None:
            return self._fp.truncate()
        return self._fp.truncate(size)

    def writable(self):
        return True

    def seekable(self):
        return True

    def readable(self):
        return False

    @property
    def closed(self):
        return getattr(self._fp, "closed", False)

    def close(self):
        # Deliberately NOT closing the wrapped file here: ownership stays
        # with the caller's `with open(...)` block, and pycdlib may call
        # close() on what it was handed.
        try:
            self._fp.flush()
        except Exception:
            pass

    # -- progress ------------------------------------------------------ #
    def _report(self, current_pos: int) -> None:
        # If the real image outgrows the estimate, grow the estimate
        # rather than pinning at 100% -- keeps the bar honest and
        # monotonic even when the guess was low.
        if self._max_pos > self._expected:
            self._expected = int(self._max_pos * 1.02) + _SECTOR

        fraction = self._max_pos / self._expected
        if current_pos < self._max_pos:
            # A write below the highest offset ever reached means
            # pycdlib is patching earlier structures now that the full
            # layout is known, not writing new data -- worth saying so
            # explicitly, since the byte count itself won't move during
            # this pass and would otherwise look identical to a frozen
            # bar.
            label = f"{self._label} — finalizing (patching image structures)..."
        else:
            label = f"{self._label}  ({_human(self._max_pos)} / ~{_human(self._expected)})"

        self._reporter.set_phase_position(
            self._base_units, fraction * self._write_units(), label,
        )

    def _sync_to_disk(self) -> None:
        """Forces buffered writes out to physical storage now, rather
        than letting them accumulate for the OS to flush all at once
        when the file is eventually closed.

        This is what actually shrinks the "stuck at the end" window on a
        slow, external, or network drive: instead of several GB of
        dirty pages piling up invisibly and then all being forced out in
        one uninterruptible blocking call at close() time, the cost is
        paid in smaller, periodic instalments spread across the visible
        write phase -- each one small enough to complete quickly even on
        a slow drive.

        `os.fsync()` (Windows: `FlushFileBuffers`) is what genuinely
        forces this, as opposed to `flush()` alone, which only empties
        Python's own internal buffer into the OS -- it says nothing
        about whether the OS itself has pushed that data to the physical
        device yet. Any failure here (an unusual file-like object with
        no real file descriptor, a filesystem that doesn't support fsync,
        etc) is swallowed deliberately: this is a best-effort mitigation,
        and it must never be able to fail an otherwise-successful build.
        """
        self._reporter.advance(
            0, f"{self._label} — syncing to disk "
               f"({_human(self._max_pos)} written so far)...",
        )
        try:
            self._fp.flush()
            os.fsync(self._fp.fileno())
        except Exception:
            pass
        finally:
            self._bytes_since_sync = 0

    def _write_units(self) -> float:
        return float(self._expected)

    @property
    def bytes_written(self) -> int:
        return self._max_pos


class SpoolProgressWriter:
    """Counts bytes while a file is being extracted out of a source
    container into a temp spool (or out to disk during Extract), so those
    phases animate smoothly too -- they can themselves be multi-GB."""

    def __init__(self, fp, reporter: ProgressReporter, label: str):
        self._fp = fp
        self._reporter = reporter
        self._label = label
        self._counted = 0

    def write(self, data):
        written = self._fp.write(data)
        n = written if isinstance(written, int) else len(data)
        self._counted += n
        self._reporter.advance(n, self._label)
        return written

    def seek(self, offset, whence=0):
        return self._fp.seek(offset, whence)

    def tell(self):
        return self._fp.tell()

    def truncate(self, size=None):
        if size is None:
            return self._fp.truncate()
        return self._fp.truncate(size)

    def flush(self):
        return self._fp.flush()

    def writable(self):
        return True

    def seekable(self):
        return True

    @property
    def closed(self):
        return getattr(self._fp, "closed", False)

    @property
    def counted(self) -> int:
        return self._counted


def estimate_work(root_node) -> tuple:
    """Returns (staging_units, write_units, estimated_iso_bytes).

    See the module docstring for the weighting rationale. In short:
    staging only charges for bytes that must physically be READ (i.e.
    files still living inside a previously-opened container), while the
    write phase charges for the whole finished image.
    """
    staging_units = 0.0
    payload_bytes = 0
    item_count = 0

    def walk(node):
        nonlocal staging_units, payload_bytes, item_count
        for child in node.children:
            item_count += 1
            if child.is_dir:
                staging_units += _PER_ITEM_OVERHEAD_BYTES
                walk(child)
                continue

            size = max(0, int(getattr(child, "size", 0) or 0))
            payload_bytes += _round_to_sector(size)

            if getattr(child, "source_kind", "") in ("iso", "archive"):
                # Must be read out of the source container during staging.
                staging_units += size + _PER_ITEM_OVERHEAD_BYTES
            else:
                # Local disk files are only *referenced* during staging;
                # their bytes are moved later, during the write phase.
                staging_units += _PER_ITEM_OVERHEAD_BYTES

    walk(root_node)

    estimated_iso_bytes = (
        payload_bytes
        + (item_count * _METADATA_SECTORS_PER_ITEM * _SECTOR)
        + (_FIXED_OVERHEAD_SECTORS * _SECTOR)
    )
    return staging_units, float(estimated_iso_bytes), estimated_iso_bytes


def _human(num: float) -> str:
    step = 1024.0
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < step:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} PB"
