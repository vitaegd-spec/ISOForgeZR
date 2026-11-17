"""
iso_backend.py
--------------
All direct interaction with the `pycdlib` library lives in this module.

Design philosophy
==================
ISO9660 (and its Joliet / Rock Ridge / UDF extensions) is fundamentally an
"append-mostly" on-disk format. Editing an existing image in place is messy
and error prone. Instead, ISOForge keeps its own lightweight in-memory tree
(see `vfs.py`) that represents *what the final disc should contain*, then
builds a brand-new image from scratch on Save.

Defensive READING of foreign/third-party discs
================================================
`pycdlib.PyCdlib` is a STATEFUL object wrapping one open file, and it is
not safely reusable after it throws an unexpected internal exception
(most commonly the cryptic `TypeError: '>' not supported between
instances of 'NoneType' and 'int'`). ISOForge therefore:

1. Never reuses a single PyCdlib instance across `populate()`'s
   multiple path-flavor (Joliet/UDF/Rock Ridge/ISO9660) attempts.
2. Wraps every data read in a self-healing retry ladder: fresh instance,
   then each other path flavor, before giving up on that one file.

Defensive WRITING -- the adaptive output-format ladder
=======================================================
The same class of pycdlib internal bug can also strike while BUILDING the
new image, including inside `iso.write()` / `iso.write_fp()` itself.
That distinction matters enormously. `add_*()` merely accumulates
in-memory structures; the write call is where pycdlib actually serializes
every accumulated directory record out to disk, and therefore where a
subtly unusual value inherited from a third-party source disc finally
gets compared/encoded. A failure there is NOT attributable to any single
file, so per-item skip-and-warn protection can never catch it.

So ISOForge treats the output format as ADAPTIVE. It attempts the whole
build against a ladder of progressively simpler format profiles (see
_BUILD_PROFILES below), richest first, and accepts the first one whose
write completes successfully:

    ISO9660+Joliet+RockRidge+UDF  ->  ISO9660+Joliet+UDF
      ->  ISO9660+UDF  ->  ISO9660+Joliet+RockRidge
      ->  ISO9660+Joliet  ->  ISO9660 only

UDF-bearing profiles are deliberately ordered first, since UDF is what
lifts the classic 4GB-per-file ceiling.

Cross-format sources
=====================
`_add_file_node` accepts nodes sourced from an opened ARCHIVE as well as
from an opened ISO, which is what makes "open a ZIP, save it as an ISO"
work without any special-case conversion path.

Byte-accurate progress
=======================
The write is streamed through `write_fp()` with a progress-tracking file
wrapper -- see `app/progress.py` for the full rationale, including why
that wrapper now reports and checks cancellation on EVERY write (not
only ones that advance the file further) and periodically forces
buffered data to physical disk during the write loop.

Cancellation
=============
`build_iso()` and `extract_node()` accept an optional `cancel_token` (see
`fileops.CancelToken`), forwarded straight into the shared
`ProgressReporter`. Since progress is reported extremely frequently
throughout both functions, that one wiring point is enough for
cancellation to be noticed promptly almost everywhere, without touching
every individual loop.

A cancellation requested WHILE `iso.write_fp()` / `iso.write()` is
genuinely blocked inside the operating system (a large flush, antivirus
scanning the file on close, a slow or network drive) has no `write()`
call available to raise it from until that blocking call returns -- see
`app/progress.py` for the full explanation. So `_build_once` and
`build_iso` both re-check the cancel token immediately after such a call
returns, and BEFORE the result is treated as a success and committed to
disk, so a cancellation that could not be delivered as an exception
while genuinely stuck is still honoured the moment control comes back to
Python, rather than silently completing the save regardless.

`fileops.OperationCancelled` is a BaseException specifically so that it
passes straight through the many `except Exception:` per-item
resilience blocks below (e.g. in `_add_file_node`, which deliberately
catches broadly so one unreadable file doesn't abort an otherwise good
build) without being mistaken for an ordinary per-item failure. The one
place that DOES need to react to it explicitly is the adaptive
format-profile loop in `build_iso()`, which must stop trying further
profiles and clean up rather than treating cancellation as "this
profile failed, try the next one".
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Callable, Optional

import pycdlib

from .fileops import BuildResult, OperationCancelled, quiet_remove, replace_with_retry
from .diskscan import populate_from_disk
from .progress import (
    ProgressReporter, ProgressWriter, SpoolProgressWriter,
    estimate_work, PROGRESS_SCALE,
)

PATH_TYPES = ("joliet", "udf", "rockridge", "iso9660")

_PATH_KW = {
    "joliet": "joliet_path",
    "udf": "udf_path",
    "rockridge": "rr_path",
    "iso9660": "iso_path",
}

_VERSION_RE = re.compile(r";\d+$")


class IsoBackendError(RuntimeError):
    """Raised for any user-facing failure originating in this module."""


# Re-exported so existing callers can keep using
# `iso_backend.populate_from_disk(...)` unchanged even though the
# implementation now lives in the shared `diskscan` module.
__all__ = [
    "IsoBackendError", "IsoSource", "BuildResult", "open_iso",
    "build_iso", "extract_node", "populate_from_disk",
    "quick_folder_to_iso", "make_iso9660_dir_name",
    "make_iso9660_file_name", "make_joliet_name", "make_rr_name",
    "make_udf_name", "make_safe_vol_ident",
]


# --------------------------------------------------------------------------- #
# Name sanitizing helpers
# --------------------------------------------------------------------------- #
def _clean_iso9660_chars(s: str) -> str:
    s = s.upper()
    return re.sub(r"[^A-Z0-9_]", "_", s) or "_"


def make_iso9660_dir_name(name: str, used: set) -> str:
    base = _clean_iso9660_chars(name)[:30] or "DIR"
    candidate = base
    counter = 1
    while candidate in used:
        suffix = f"_{counter}"
        candidate = (base[: max(1, 30 - len(suffix))] + suffix)
        counter += 1
    used.add(candidate)
    return candidate


def make_iso9660_file_name(name: str, used: set) -> str:
    stem, ext = os.path.splitext(name)
    stem_clean = _clean_iso9660_chars(stem)[:26] or "FILE"
    ext_clean = _clean_iso9660_chars(ext[1:])[:3] if ext else ""

    def build(stem_part: str) -> str:
        if ext_clean:
            return f"{stem_part}.{ext_clean};1"
        return f"{stem_part}.;1"

    candidate = build(stem_clean)
    counter = 1
    while candidate in used:
        suffix = f"_{counter}"
        trimmed = stem_clean[: max(1, 26 - len(suffix))] + suffix
        candidate = build(trimmed)
        counter += 1
    used.add(candidate)
    return candidate


def make_joliet_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|;]', "_", name)
    return cleaned[:64] if len(cleaned) > 64 else cleaned


def make_rr_name(name: str) -> str:
    cleaned = name.replace("/", "_").replace("\x00", "_")
    return cleaned[:180] if len(cleaned) > 180 else cleaned


def make_udf_name(name: str) -> str:
    return make_rr_name(name)


def make_safe_vol_ident(label: str) -> str:
    """Builds a Primary Volume Descriptor Volume Identifier guaranteed to
    be ASCII and <=32 characters (therefore <=32 bytes). Non-ASCII
    characters are stripped BEFORE truncating, since a label that looks
    safely short by character count can still exceed 32 BYTES once
    encoded."""
    base = (label or "ISOFORGE").upper()
    ascii_only = base.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Z0-9 _.-]", "_", ascii_only).strip() or "ISOFORGE"
    return cleaned[:32] or "ISOFORGE"


# --------------------------------------------------------------------------- #
# Reading an existing ISO
# --------------------------------------------------------------------------- #
class IsoSource:
    """Wraps a read-only `pycdlib.PyCdlib` against an existing .iso,
    kept alive so unmodified files can still be extracted/re-saved."""

    def __init__(self, iso_path: str):
        self.iso_path = iso_path
        probe = pycdlib.PyCdlib()
        try:
            probe.open(iso_path)
        except Exception as exc:
            raise IsoBackendError(
                f"Could not open '{os.path.basename(iso_path)}' as an ISO image: {exc}"
            ) from exc
        finally:
            try:
                probe.close()
            except Exception:
                pass

        self.path_type: Optional[str] = None
        self._pycdlib: Optional["pycdlib.PyCdlib"] = None
        self._closed = False

    def _open_fresh_instance(self) -> "pycdlib.PyCdlib":
        inst = pycdlib.PyCdlib()
        inst.open(self.iso_path)
        return inst

    def _reopen_fresh(self) -> None:
        if self._pycdlib is not None:
            try:
                self._pycdlib.close()
            except Exception:
                pass
        self._pycdlib = self._open_fresh_instance()

    def populate(self, root_node) -> None:
        """Tries a FULL walk with each path flavor in turn, each against
        its own brand-new PyCdlib instance, falling back automatically."""
        last_exc: Optional[Exception] = None
        for pt in PATH_TYPES:
            root_node.children = []
            try:
                candidate = self._open_fresh_instance()
            except Exception as exc:
                last_exc = exc
                continue
            try:
                self._populate_with(candidate, root_node, pt)
            except Exception as exc:
                last_exc = exc
                try:
                    candidate.close()
                except Exception:
                    pass
                continue

            if self._pycdlib is not None:
                try:
                    self._pycdlib.close()
                except Exception:
                    pass
            self._pycdlib = candidate
            self.path_type = pt
            return

        root_node.children = []
        raise IsoBackendError(
            "This ISO's directory structure could not be fully read using "
            "any supported method (Joliet, UDF, Rock Ridge, or plain "
            "ISO9660). The disc may use an unusual, malformed, or only "
            "partially-standard structure that this version of ISOForge's "
            f"underlying ISO library cannot parse.\n\nUnderlying error: {last_exc}"
        )

    def _populate_with(self, pycdlib_instance, root_node, path_type: str) -> None:
        kw_root = {_PATH_KW[path_type]: "/"}
        path_map = {"/": root_node}
        for dirname, dirlist, filelist in pycdlib_instance.walk(**kw_root):
            parent = path_map.get(dirname)
            if parent is None:
                continue
            base = "" if dirname == "/" else dirname
            for d in sorted(dirlist):
                child = parent.new_child(_VERSION_RE.sub("", d), is_dir=True)
                child.source_kind = "iso"
                path_map[f"{base}/{d}"] = child
            for f in sorted(filelist):
                node = parent.new_child(_VERSION_RE.sub("", f), is_dir=False)
                node.source_kind = "iso"
                node.source_iso = self
                node.source_path = f"{base}/{f}"
                node.size = self._safe_size(pycdlib_instance, node.source_path, path_type)

    @staticmethod
    def _safe_size(pycdlib_instance, path: str, path_type: str) -> int:
        try:
            rec = pycdlib_instance.get_record(**{_PATH_KW[path_type]: path})
            get_len = getattr(rec, "get_data_length", None)
            if callable(get_len):
                return int(get_len())
            return int(getattr(rec, "data_length", 0) or 0)
        except Exception:
            return 0

    def extract_to_fp(self, iso_internal_path: str, fp) -> None:
        """Reads a file's bytes into `fp`, with a three-layer self-healing
        retry ladder (as-is -> fresh instance -> every other path flavor)."""
        if self._closed:
            raise IsoBackendError(
                f"'{iso_internal_path}' can no longer be read: this ISO's "
                "original source file has been closed."
            )
        if self._pycdlib is None or self.path_type is None:
            raise IsoBackendError(
                f"'{iso_internal_path}' cannot be read: this ISO source was "
                "never successfully opened."
            )

        last_exc: Optional[Exception] = None
        try:
            self._pycdlib.get_file_from_iso_fp(fp, **{_PATH_KW[self.path_type]: iso_internal_path})
            return
        except Exception as exc:
            last_exc = exc

        try:
            self._reopen_fresh()
            self._reset_fp(fp)
            self._pycdlib.get_file_from_iso_fp(fp, **{_PATH_KW[self.path_type]: iso_internal_path})
            return
        except Exception as exc:
            last_exc = exc

        for alt_pt in PATH_TYPES:
            if alt_pt == self.path_type:
                continue
            try:
                self._reopen_fresh()
                self._reset_fp(fp)
                self._pycdlib.get_file_from_iso_fp(fp, **{_PATH_KW[alt_pt]: iso_internal_path})
                self.path_type = alt_pt
                return
            except Exception as exc:
                last_exc = exc
                continue

        raise IsoBackendError(
            f"Could not read '{iso_internal_path}' from the original disc "
            "using any supported method. This specific file's record on "
            "the source disc may be unusual or corrupt.\n\n"
            f"Underlying error: {last_exc}"
        )

    @staticmethod
    def _reset_fp(fp) -> None:
        try:
            fp.seek(0)
        except Exception:
            pass
        try:
            fp.truncate()
        except Exception:
            pass

    def extract_to_path(self, iso_internal_path: str, dest_file: str) -> None:
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        with open(dest_file, "wb") as fp:
            self.extract_to_fp(iso_internal_path, fp)

    def close(self) -> None:
        if self._closed:
            return
        if self._pycdlib is not None:
            try:
                self._pycdlib.close()
            except Exception:
                pass
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


def open_iso(path: str) -> IsoSource:
    try:
        return IsoSource(path)
    except IsoBackendError:
        raise
    except Exception as exc:  # pragma: no cover
        raise IsoBackendError(f"Failed to open '{os.path.basename(path)}': {exc}") from exc


# --------------------------------------------------------------------------- #
# Building / writing a brand new ISO
# --------------------------------------------------------------------------- #
ProgressCB = Optional[Callable[[str, int, int], None]]

_RETRY_MAX_ATTEMPTS = 6
_RETRY_SHRINK_CHARS = 24
_RETRY_MIN_LENGTH = 4
_PER_ITEM_OVERHEAD = 64 * 1024  # must match progress._PER_ITEM_OVERHEAD_BYTES

_KWARG_MISMATCH_PHRASES = (
    "unexpected keyword argument",
    "positional argument",
    "positional arguments",
    "missing 1 required",
    "missing required",
)

# The adaptive output-format ladder -- see this module's docstring for the
# full rationale. Richest first; UDF-bearing profiles deliberately ranked
# above non-UDF ones so large-file support is only sacrificed if a disc's
# content genuinely cannot be written with UDF at all.
_BUILD_PROFILES = (
    ("ISO9660 Level 3 + Joliet + Rock Ridge + UDF 2.60",
     {"interchange_level": 3, "joliet": 3, "rock_ridge": "1.09", "udf": "2.60"}),
    ("ISO9660 Level 3 + Joliet + UDF 2.60",
     {"interchange_level": 3, "joliet": 3, "udf": "2.60"}),
    ("ISO9660 Level 3 + UDF 2.60",
     {"interchange_level": 3, "udf": "2.60"}),
    ("ISO9660 Level 3 + Joliet + Rock Ridge",
     {"interchange_level": 3, "joliet": 3, "rock_ridge": "1.09"}),
    ("ISO9660 Level 3 + Joliet",
     {"interchange_level": 3, "joliet": 3}),
    ("ISO9660 Level 3 only",
     {"interchange_level": 3}),
)


def _is_signature_mismatch_type_error(exc: TypeError) -> bool:
    """Distinguishes a genuine "wrong arguments were passed" TypeError
    (e.g. `new() got an unexpected keyword argument 'udf'`, raised when a
    pycdlib version's signature lacks a parameter entirely) from
    pycdlib's own INTERNAL bugs, which also surface as bare TypeErrors
    (notably `'>' not supported between instances of 'NoneType' and
    'int'`). Only the former should short-circuit retry logic."""
    msg = str(exc)
    return any(phrase in msg for phrase in _KWARG_MISMATCH_PHRASES)


def _retry_on_invalid_input(fn, kwargs: dict, shrink_keys) -> None:
    """Calls fn(**kwargs), progressively shortening the string parameters
    named in `shrink_keys` and retrying if pycdlib rejects a value. Only a
    genuine signature-mismatch TypeError propagates immediately."""
    current = dict(kwargs)
    last_exc: Optional[Exception] = None
    for _attempt in range(_RETRY_MAX_ATTEMPTS):
        try:
            fn(**current)
            return
        except TypeError as exc:
            if _is_signature_mismatch_type_error(exc):
                raise
            last_exc = exc
        except Exception as exc:
            last_exc = exc

        shortened_any = False
        candidates = [
            k for k in shrink_keys
            if isinstance(current.get(k), str) and len(current[k]) > _RETRY_MIN_LENGTH
        ]
        candidates.sort(key=lambda k: -len(current[k]))
        for key in candidates[:1] or candidates:
            new_len = max(_RETRY_MIN_LENGTH, len(current[key]) - _RETRY_SHRINK_CHARS)
            if new_len < len(current[key]):
                current[key] = current[key][:new_len]
                shortened_any = True
            break
        if not shortened_any:
            break
    if last_exc is not None:
        raise last_exc
    fn(**kwargs)  # pragma: no cover


def _count_nodes(node) -> int:
    total = 1
    if node.is_dir:
        for c in node.children:
            total += _count_nodes(c)
    return total


def build_iso(
    root_node,
    out_path: str,
    volume_label: str = "",
    progress_cb: ProgressCB = None,
    source_to_close_before_replace=None,
    cancel_token=None,
) -> BuildResult:
    """Builds a new ISO at `out_path` from `root_node`, returning a
    BuildResult describing anything skipped and which format profile was
    used.

    File data read out of a source container is extracted exactly once
    and cached across all profile attempts, so retrying a different
    format never re-reads (or re-fails on) the source.

    `source_to_close_before_replace` is closed only AFTER a successful
    write and immediately before the final atomic replace -- closing any
    earlier would leave the project unable to retry if the write failed.

    `cancel_token`, if supplied, is forwarded into the shared
    `ProgressReporter` so every progress tick throughout this build
    checks it. If cancellation is requested mid-build, the CURRENT
    format-profile attempt's partial temp file is removed, the cache of
    already-extracted spool files is closed, and
    `fileops.OperationCancelled` propagates to the caller -- deliberately
    WITHOUT falling through to try the next, simpler format profile,
    which is what would happen if this were treated like an ordinary
    per-profile failure.

    A cancellation requested while `_build_once` was genuinely blocked
    inside the operating system (see `app/progress.py`) has no `write()`
    call left to raise it from until that blocking call returns, so the
    cancel token is checked ONE MORE TIME here, immediately after
    `_build_once` returns successfully and BEFORE the result is committed
    -- closing the narrow window in which a cancellation requested during
    that stretch could otherwise be silently missed and the save
    completed anyway.
    """
    vol_ident = make_safe_vol_ident(volume_label)

    # Progress is measured in BYTES OF REAL WORK, not item counts -- see
    # app/progress.py's docstring for the full rationale.
    staging_units, write_units, estimated_iso_bytes = estimate_work(root_node)
    reporter = ProgressReporter(progress_cb, staging_units + write_units,
                                cancel_token=cancel_token)

    # node.id -> (spool, size); populated lazily on first use, reused by
    # every subsequent profile attempt.
    extracted_cache: dict = {}
    last_error: Optional[Exception] = None
    try:
        for profile_index, (format_label, new_kwargs) in enumerate(_BUILD_PROFILES):
            attempt_tmp = f"{out_path}.tmp_building{profile_index}"
            try:
                skipped = _build_once(
                    root_node=root_node,
                    tmp_out=attempt_tmp,
                    vol_ident=vol_ident,
                    new_kwargs=new_kwargs,
                    format_label=format_label,
                    is_first_attempt=(profile_index == 0),
                    progress_cb=progress_cb,
                    extracted_cache=extracted_cache,
                    reporter=reporter,
                    staging_units=staging_units,
                    estimated_iso_bytes=estimated_iso_bytes,
                )
            except OperationCancelled:
                # Deliberately NOT "try the next profile" -- the user
                # asked to stop, not for a different format. Clean up
                # this attempt's partial temp file before propagating;
                # the outer `finally` below handles the extraction
                # cache.
                quiet_remove(attempt_tmp)
                raise
            except Exception as exc:
                last_error = exc
                quiet_remove(attempt_tmp)
                continue

            # This profile's write returned WITHOUT raising -- but that
            # write may have spent a long time blocked inside a single
            # OS-level flush/close with no opportunity to raise
            # OperationCancelled while it was stuck (see
            # app/progress.py). Check once more, right here, before
            # treating this as a success and committing it: this is the
            # belt-and-braces half of the check already performed inside
            # _build_once itself, closing the narrow window between that
            # check and this line.
            if cancel_token is not None and cancel_token.cancelled:
                quiet_remove(attempt_tmp)
                raise OperationCancelled()

            # This profile's write succeeded -- commit it.
            if source_to_close_before_replace is not None:
                source_to_close_before_replace.close()
            replace_with_retry(attempt_tmp, out_path)
            reporter.finish("Done")
            return BuildResult(
                skipped=skipped,
                format_label=format_label,
                used_fallback_format=(profile_index > 0),
            )
    finally:
        for spool, _size in extracted_cache.values():
            try:
                spool.close()
            except Exception:
                pass

    raise IsoBackendError(
        "This ISO could not be written in any supported disc format "
        "(UDF, Joliet, Rock Ridge, or plain ISO9660 were all tried).\n\n"
        "This usually means the source contains metadata that the "
        "underlying ISO library cannot re-encode in any form.\n\n"
        f"Underlying error: {last_error}"
    )


def _build_once(
    root_node,
    tmp_out: str,
    vol_ident: str,
    new_kwargs: dict,
    format_label: str,
    is_first_attempt: bool,
    progress_cb: ProgressCB,
    extracted_cache: dict,
    reporter,
    staging_units: float,
    estimated_iso_bytes: int,
) -> list:
    """Performs ONE complete build attempt using a single format profile.

    Raises if `iso.new()` or the write fails (signalling the caller to try
    the next profile). Individual files/folders that can't be added are
    skipped and reported rather than aborting this attempt.

    `fileops.OperationCancelled`, raised from within `reporter` (a
    BaseException, not an ordinary Exception -- see its docstring),
    passes straight through every `except Exception:` block below
    untouched, so cancellation is never mistaken for "this one file
    failed, skip it and continue".
    """
    joliet_on = "joliet" in new_kwargs
    rr_on = "rock_ridge" in new_kwargs
    udf_on = "udf" in new_kwargs

    iso = pycdlib.PyCdlib()
    _retry_on_invalid_input(
        iso.new, {"vol_ident": vol_ident, **new_kwargs}, shrink_keys=("vol_ident",)
    )

    skipped_files: list = []
    prefix = "" if is_first_attempt else f"[{format_label}] "

    def tick(label: str, units: float = _PER_ITEM_OVERHEAD) -> None:
        """Charges a small fixed overhead per item handled. Real file
        bytes are charged separately -- during extraction for
        container-sourced files, and during the write phase for
        everything else (see app/progress.py)."""
        reporter.advance(units, f"{prefix}{label}")

    def _display(node) -> str:
        return node.path() if hasattr(node, "path") else node.name

    def _skip_subtree(node, reason: str, report: bool) -> None:
        if report:
            skipped_files.append((_display(node), reason))
        tick(f"Skipping {node.name}")
        if node.is_dir:
            for child in node.children:
                _skip_subtree(child, reason, report=False)

    def _optional_kwargs(name: str, joliet_path, udf_path) -> dict:
        kw = {}
        if rr_on:
            kw["rr_name"] = make_rr_name(name)
        if joliet_on:
            kw["joliet_path"] = joliet_path
        if udf_on:
            kw["udf_path"] = udf_path
        return kw

    def _source_reader(node):
        """Returns the object able to stream this node's bytes, whether
        it came from an ISO or an archive. Supporting both is what makes
        'open a ZIP, save it as an ISO' work with no separate conversion
        path."""
        if node.source_kind == "iso":
            return getattr(node, "source_iso", None)
        if node.source_kind == "archive":
            return getattr(node, "source_archive", None)
        return None

    def _get_cached_data(node):
        """Extracts this node's bytes from its source container exactly
        once, caching the spooled result for reuse by any later profile
        attempt."""
        cached = extracted_cache.get(node.id)
        if cached is not None:
            spool, size = cached
            spool.seek(0)
            return spool, size

        reader = _source_reader(node)
        if reader is None:
            raise IsoBackendError(f"'{node.name}' has no readable source.")

        spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        try:
            # Wrapped so multi-GB reads out of a source animate the bar
            # too, rather than jumping in one lump per file.
            counting = SpoolProgressWriter(
                spool, reporter, f"{prefix}Reading {node.name}"
            )
            reader.extract_to_fp(node.source_path, counting)
        except Exception:
            try:
                spool.close()
            except Exception:
                pass
            raise
        size = spool.tell()
        spool.seek(0)
        extracted_cache[node.id] = (spool, size)
        return spool, size

    def add_dir_recursive(node, iso_path: str, joliet_path: str, udf_path: str) -> None:
        iso_used = set()
        for child in node.children:
            if not child.is_dir:
                continue
            iso_name = make_iso9660_dir_name(child.name, iso_used)
            child_iso_path = f"{iso_path}/{iso_name}".replace("//", "/")
            child_joliet_path = f"{joliet_path}/{make_joliet_name(child.name)}".replace("//", "/")
            child_udf_path = f"{udf_path}/{make_udf_name(child.name)}".replace("//", "/")
            try:
                _retry_on_invalid_input(
                    iso.add_directory,
                    {"iso_path": child_iso_path,
                     **_optional_kwargs(child.name, child_joliet_path, child_udf_path)},
                    shrink_keys=("rr_name", "joliet_path", "udf_path"),
                )
            except Exception as exc:
                _skip_subtree(
                    child,
                    f"Could not add this folder to the new image, so its "
                    f"contents were also left out: {exc}",
                    report=True,
                )
                continue
            tick(f"Adding folder {child.name}")
            add_dir_recursive(child, child_iso_path, child_joliet_path, child_udf_path)

        for child in node.children:
            if child.is_dir:
                continue
            iso_name = make_iso9660_file_name(child.name, iso_used)
            child_iso_path = f"{iso_path}/{iso_name}".replace("//", "/")
            child_joliet_path = f"{joliet_path}/{make_joliet_name(child.name)}".replace("//", "/")
            child_udf_path = f"{udf_path}/{make_udf_name(child.name)}".replace("//", "/")
            _add_file_node(child, child_iso_path, child_joliet_path, child_udf_path)
            tick(f"Adding {child.name}")

    def _add_file_node(node, iso_path, joliet_path, udf_path) -> None:
        opt = _optional_kwargs(node.name, joliet_path, udf_path)
        display_path = _display(node)

        if node.source_kind == "disk":
            try:
                _retry_on_invalid_input(
                    iso.add_file,
                    {"filename": node.source_path, "iso_path": iso_path, **opt},
                    shrink_keys=("rr_name", "joliet_path", "udf_path"),
                )
            except Exception as exc:
                skipped_files.append((display_path, f"Could not add this file to the new image: {exc}"))
            return

        if node.source_kind in ("iso", "archive") and _source_reader(node) is not None:
            try:
                spool, size = _get_cached_data(node)
            except Exception as exc:
                skipped_files.append((display_path, str(exc)))
                return
            try:
                _retry_on_invalid_input(
                    iso.add_fp,
                    {"fp": spool, "length": size, "iso_path": iso_path, **opt},
                    shrink_keys=("rr_name", "joliet_path", "udf_path"),
                )
            except Exception as exc:
                skipped_files.append((display_path, f"Could not add this file to the new image: {exc}"))
            return

        skipped_files.append((display_path, "This item has no readable source."))

    try:
        add_dir_recursive(root_node, "", "", "")
        # Write via write_fp() with a progress-tracking file wrapper
        # instead of write(path). This is where essentially all the time
        # goes on a large build -- a single opaque call that emitted no
        # progress whatsoever. Streaming it makes it observable.
        #
        # Old pycdlib versions may either lack write_fp() entirely OR
        # expose it while raising AttributeError internally, so the
        # fallback is driven by actually ATTEMPTING it and catching that
        # failure -- a plain hasattr()/callable() probe is not sufficient
        # to tell the two situations apart.
        wrote_via_fp = False
        write_fp = getattr(iso, "write_fp", None)
        if callable(write_fp):
            try:
                with open(tmp_out, "wb") as raw_fp:
                    writer = ProgressWriter(
                        raw_fp,
                        expected_total_bytes=estimated_iso_bytes,
                        reporter=reporter,
                        base_units=staging_units,
                        label=f"{prefix}Writing image to disk",
                    )
                    write_fp(writer)
                wrote_via_fp = True
            except AttributeError:
                # Genuinely unsupported on this pycdlib build -- fall
                # through to the legacy path below. Any OTHER exception
                # (notably pycdlib's internal NoneType/int bug) must keep
                # propagating so build_iso() can try the next format
                # profile, which is why only AttributeError is caught.
                wrote_via_fp = False

        if not wrote_via_fp:
            reporter.set_phase_position(
                staging_units, 0.0, f"{prefix}Writing image to disk..."
            )
            iso.write(tmp_out)
    finally:
        try:
            iso.close()
        except Exception:
            pass

    # The write above may have spent a long time blocked inside a single
    # OS-level flush/close call -- writing several GB to a slow,
    # external, or network drive, or antivirus scanning the finished
    # file on close, are both common causes, and NEITHER offers any
    # opportunity for OperationCancelled to be raised while blocked
    # (ProgressWriter can only check cancellation from inside a write()
    # call, and none occur during a bare close()). So a cancellation
    # requested during that blocking tail is checked HERE too, the
    # moment control returns to Python, rather than relying solely on
    # checks made during the write itself.
    reporter.raise_if_cancelled()

    return skipped_files


# --------------------------------------------------------------------------- #
# Extracting nodes out to the local filesystem
# --------------------------------------------------------------------------- #
def extract_node(node, dest_dir: str, progress_cb: ProgressCB = None,
                 cancel_token=None) -> None:
    """Extracts `node` (and everything beneath it) to `dest_dir`.

    Progress is byte-weighted here too, for the same reason as the build
    path: counting items makes the bar race ahead on a folder full of
    tiny files and then stall on a single large one.

    `cancel_token`, if supplied, is forwarded into the shared
    `ProgressReporter`; since nothing in the recursive `_extract` helper
    below catches exceptions broadly, `fileops.OperationCancelled`
    propagates straight out of this function with no special handling
    needed here.
    """
    total_units = 0.0

    def measure(n) -> None:
        nonlocal total_units
        total_units += _PER_ITEM_OVERHEAD
        if n.is_dir:
            for c in n.children:
                measure(c)
        else:
            total_units += max(0, int(getattr(n, "size", 0) or 0))

    measure(node)
    reporter = ProgressReporter(progress_cb, total_units, cancel_token=cancel_token)

    def _extract(n, dest_path):
        if n.is_dir:
            os.makedirs(dest_path, exist_ok=True)
            reporter.advance(_PER_ITEM_OVERHEAD, f"Extracting {n.name}")
            for c in n.children:
                _extract(c, os.path.join(dest_path, c.name))
            return

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        if n.source_kind == "disk":
            _copy_with_progress(n.source_path, dest_path, reporter, n.name)
        elif n.source_kind == "iso" and getattr(n, "source_iso", None) is not None:
            with open(dest_path, "wb") as fp:
                counting = SpoolProgressWriter(fp, reporter, f"Extracting {n.name}")
                n.source_iso.extract_to_fp(n.source_path, counting)
        elif n.source_kind == "archive" and getattr(n, "source_archive", None) is not None:
            with open(dest_path, "wb") as fp:
                counting = SpoolProgressWriter(fp, reporter, f"Extracting {n.name}")
                n.source_archive.extract_to_fp(n.source_path, counting)
        else:
            raise IsoBackendError(f"'{n.name}' has no readable source.")
        reporter.advance(_PER_ITEM_OVERHEAD, f"Extracting {n.name}")

    _extract(node, os.path.join(dest_dir, node.name) if node.name else dest_dir)
    reporter.finish("Done")


def _copy_with_progress(src: str, dst: str, reporter, label: str,
                        chunk_size: int = 4 * 1024 * 1024) -> None:
    """Chunked copy so a single large local file still animates the bar,
    rather than jumping only once it has fully completed."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(chunk_size)
            if not chunk:
                break
            fout.write(chunk)
            reporter.advance(len(chunk), f"Extracting {label}")


def quick_folder_to_iso(folder_path: str, out_path: str, volume_label: str = "",
                        progress_cb: ProgressCB = None, cancel_token=None) -> BuildResult:
    """Convenience helper used by the 'Convert folder to ISO' wizard."""
    from .vfs import VNode

    root = VNode(name="", is_dir=True)
    root.source_kind = "new"
    populate_from_disk(root, folder_path)
    label = volume_label or os.path.basename(os.path.normpath(folder_path)) or "ISOFORGE"
    return build_iso(root, out_path, volume_label=label, progress_cb=progress_cb,
                     cancel_token=cancel_token)
