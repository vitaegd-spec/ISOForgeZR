"""
archive_backend.py
------------------
All direct interaction with the ZIP / 7Z / RAR / SFX layers lives here.

This module is deliberately the mirror image of `iso_backend.py`: it
exposes the same public shape (`open_archive()`, `build_archive()`,
`extract_node()`, a source object with `populate()` / `extract_to_fp()` /
`close()`, and the same `BuildResult`), so `main_window.py` can treat a
ZIP project and an ISO project identically and the VFS tree in `vfs.py`
needs no format-specific knowledge at all.

Four formats, one interface
============================
Each format is handled by a small adapter class implementing the same
four operations (list entries, open one member for reading, report
whether a password is needed, accept a password). That keeps the
password/progress/VFS logic written exactly once instead of four times:

    ZIP -> `pyzipper` when available (adds AES-256 support), falling back
           to the stdlib `zipfile`, which handles only legacy ZipCrypto.
    7Z  -> `py7zr` (AES-256 both directions).
    RAR -> `rarfile` for reading; RARLAB's `Rar.exe` for writing.
    SFX -> whichever of the above matches the payload glued to the stub.

Every import is deferred to the moment a format is actually used, so a
missing optional dependency only ever affects that one format instead of
preventing the whole application from starting. When a library IS
missing, the error raised names the exact `pip install` needed.

RAR writing, and why it works differently
==========================================
The RAR *compression* algorithm is proprietary to RARLAB, so no
open-source library may implement a compressor. ISOForge therefore
creates RAR archives by driving RARLAB's own `Rar.exe` as an external
process -- supplied by the user via Tools > Settings, since it can't be
bundled for licensing reasons. See `rar_tools` for the full rationale.

That forces one structural difference from ZIP and 7Z. Those libraries
accept a stream per member, so members can be written straight out of
whatever source they came from. `Rar.exe` works on REAL FILES on disk,
while the VFS deliberately allows renaming and restructuring that
correspond to nothing on disk. So the RAR path first STAGES the virtual
tree into a temp directory laid out exactly as the archive should be,
then points Rar.exe at it (see `_write_rar`).

Staging costs temporary disk space roughly equal to the uncompressed
content, which is why it's used only for RAR and not as a general
strategy.

SFX writing -- a wrapper, not a format
=======================================
A self-extracting archive has no format of its own. It is a stub
executable with an ordinary archive concatenated onto its tail, and it
works because archive formats locate their index from the END of the
file (see `sfx_tools`' docstring). `_write_sfx` therefore builds a
perfectly normal payload archive using the writers above, then glues it
onto the stub -- which is precisely what 7-Zip and WinRAR do.

Password handling
==================
`ArchiveSource` opens an archive WITHOUT a password first and then
reports `needs_password`. The UI prompts, calls `try_password()` (which
verifies by actually attempting a real decrypt of the smallest available
member, so a wrong password is caught immediately rather than producing
a corrupt extraction later), and retries until it succeeds or the user
cancels.

On the way out, `build_archive(password=...)` encrypts with AES-256 for
ZIP and 7Z, and with `Rar.exe -hp` for RAR (which covers the header too,
hiding filenames as well as contents).

Data is read exactly once
==========================
As with `iso_backend`, any member carried over from a source archive is
decompressed exactly once into a spooled temp file and reused, so a
re-save never decompresses the same data twice.

Cancellation
=============
`build_archive()` and `extract_node()` accept an optional `cancel_token`
(see `fileops.CancelToken`), forwarded into the single `ProgressReporter`
shared across every write helper below. Since progress is reported very
frequently -- per file, per chunk -- that one wiring point is enough for
cancellation to be noticed promptly almost everywhere.

`fileops.OperationCancelled` is a BaseException specifically so it
passes straight through the many `except Exception:` per-item
resilience blocks in `_write_zip`/`_write_7z`/`_write_rar` (which exist
to skip one unreadable file and keep going) without being mistaken for
an ordinary per-item failure -- see that class's docstring for the full
rationale. The one path that needs explicit handling is RAR: Rar.exe
runs as an external subprocess, so `_write_rar` forwards the token into
`rar_tools.create_rar`, which checks it while streaming the tool's
output and kills the process if cancellation is requested -- an
in-process exception alone cannot stop a separate program.

`build_archive()` additionally re-checks the cancel token immediately
after the chosen `_write_*` helper returns successfully and BEFORE the
temp file is committed -- mirroring `iso_backend.build_iso()`'s same
safety net, for the same reason: a single large member (or the final
close of the output file) can spend a long time blocked inside the
operating system with no `write()` call of its own available to raise
`OperationCancelled` from until it returns. See `app/progress.py`'s
docstring for the full explanation of that class of stall.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Callable, Optional

from . import formats
from . import rar_tools
from . import sfx_tools
from .fileops import BuildResult, OperationCancelled, quiet_remove, replace_with_retry
from .progress import (
    ProgressReporter, SpoolProgressWriter, PROGRESS_SCALE,
)

ProgressCB = Optional[Callable[[str, int, int], None]]

_PER_ITEM_OVERHEAD = 64 * 1024  # matches progress._PER_ITEM_OVERHEAD_BYTES
_SPOOL_MAX = 64 * 1024 * 1024
_COPY_CHUNK = 4 * 1024 * 1024

# Share of an SFX build's progress budget reserved for gluing the stub
# and payload together. Compression dominates by a wide margin, but the
# concatenation still copies the whole payload once, so it deserves a
# visible slice rather than an instant jump to 100%.
_SFX_CONCAT_SHARE = 0.15


class ArchiveBackendError(RuntimeError):
    """Raised for any user-facing failure originating in this module."""


class PasswordRequired(ArchiveBackendError):
    """Raised when an operation needs a password that hasn't been
    supplied (or the supplied one was wrong)."""


# --------------------------------------------------------------------- #
# Dependency helpers
# --------------------------------------------------------------------- #
def _require(module_name: str, pip_name: str, purpose: str):
    """Imports an optional dependency, or raises a message that tells the
    user precisely what to install and why."""
    try:
        return __import__(module_name)
    except ImportError as exc:
        raise ArchiveBackendError(
            f"{purpose} requires the '{pip_name}' package, which isn't "
            f"installed.\n\nInstall it with:\n\n    pip install {pip_name}"
        ) from exc


def _zip_module(prefer_aes: bool = True):
    """Returns (module, supports_aes). `pyzipper` is a drop-in superset
    of `zipfile` that adds AES; the stdlib fallback can still read legacy
    ZipCrypto-encrypted entries and write unencrypted ones."""
    if prefer_aes:
        try:
            import pyzipper
            return pyzipper, True
        except ImportError:
            pass
    import zipfile
    return zipfile, False


def _normalise(member_path: str) -> str:
    """Archive members use forward slashes and may carry a leading './'
    or a trailing '/' for directories. Normalised to a leading-slash
    absolute-style path so it matches the VFS's own `VNode.path()`."""
    p = (member_path or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.strip("/")
    return "/" + p if p else "/"


# --------------------------------------------------------------------- #
# Per-format adapters
# --------------------------------------------------------------------- #
class _Entry:
    __slots__ = ("path", "is_dir", "size", "mtime", "encrypted")

    def __init__(self, path, is_dir, size, mtime, encrypted=False):
        self.path = path
        self.is_dir = is_dir
        self.size = int(size or 0)
        self.mtime = mtime or time.time()
        self.encrypted = encrypted


class _BaseAdapter:
    fmt = ""

    def __init__(self, path: str):
        self.path = path
        self._password: Optional[str] = None

    def set_password(self, password: Optional[str]) -> None:
        self._password = password

    def list_entries(self):          # pragma: no cover - interface
        raise NotImplementedError

    def open_member(self, member_path: str):  # pragma: no cover
        raise NotImplementedError

    @property
    def needs_password(self) -> bool:  # pragma: no cover
        return False

    def close(self) -> None:
        pass


class _ZipAdapter(_BaseAdapter):
    fmt = formats.ZIP

    def __init__(self, path: str):
        super().__init__(path)
        self._mod, self._aes = _zip_module()
        try:
            # IMPORTANT: pyzipper exposes TWO different classes -- plain
            # `ZipFile` (which behaves like the stdlib one and cannot
            # decrypt AES) and `AESZipFile` (which can). Opening an
            # AES-encrypted archive with the former silently succeeds at
            # listing, then fails every read with "Bad password" even when
            # the password is correct. Always prefer AESZipFile when it
            # exists; it handles unencrypted and legacy ZipCrypto archives
            # perfectly well too, so there's no downside to using it for
            # everything.
            opener = getattr(self._mod, "AESZipFile", None) or self._mod.ZipFile
            self._zf = opener(path, "r")
        except Exception as exc:
            raise ArchiveBackendError(
                f"Could not open '{os.path.basename(path)}' as a ZIP archive: {exc}"
            ) from exc
        self._infos = self._zf.infolist()

    @property
    def needs_password(self) -> bool:
        # Bit 0 of the general-purpose flag marks an encrypted entry.
        return any(
            (i.flag_bits & 0x1) and not i.is_dir()
            for i in self._infos
        )

    def set_password(self, password: Optional[str]) -> None:
        super().set_password(password)
        self._zf.setpassword(password.encode("utf-8") if password else None)

    def list_entries(self):
        out = []
        for info in self._infos:
            raw = info.filename or ""
            out.append(_Entry(
                _normalise(raw),
                info.is_dir() or raw.endswith("/"),
                info.file_size,
                _zip_mtime(info),
                bool(info.flag_bits & 0x1),
            ))
        return out

    def open_member(self, member_path: str):
        target = _normalise(member_path)
        for info in self._infos:
            if _normalise(info.filename) == target and not info.is_dir():
                return self._zf.open(info, "r")
        raise ArchiveBackendError(f"'{member_path}' was not found inside this ZIP archive.")

    def close(self) -> None:
        try:
            self._zf.close()
        except Exception:
            pass


def _zip_mtime(info) -> float:
    try:
        return time.mktime(tuple(info.date_time) + (0, 0, -1))
    except Exception:
        return time.time()


class _SevenZAdapter(_BaseAdapter):
    fmt = formats.SEVENZ

    def __init__(self, path: str):
        super().__init__(path)
        _require("py7zr", "py7zr", "Opening 7-Zip archives")
        import py7zr
        self._py7zr = py7zr
        self._extract_dir = None     # temp dir, filled lazily on first read
        try:
            with py7zr.SevenZipFile(path, mode="r") as zf:
                self._encrypted = bool(zf.needs_password())
                self._entries = [
                    _Entry(_normalise(f.filename),
                           bool(getattr(f, "is_directory", False))
                           or (f.filename or "").endswith("/"),
                           getattr(f, "uncompressed", 0) or 0,
                           getattr(f, "creationtime", None) or time.time(),
                           self._encrypted)
                    for f in zf.list()
                ]
        except Exception as exc:
            # A header-encrypted 7z refuses even to list without the
            # password; treat that as "needs password" rather than a
            # hard failure, so the UI can prompt.
            if "password" in str(exc).lower() or "encrypted" in str(exc).lower():
                self._encrypted = True
                self._entries = []
            else:
                raise ArchiveBackendError(
                    f"Could not open '{os.path.basename(path)}' as a 7-Zip archive: {exc}"
                ) from exc

    @property
    def needs_password(self) -> bool:
        return self._encrypted

    def set_password(self, password: Optional[str]) -> None:
        super().set_password(password)
        # A password change invalidates anything already extracted.
        if self._extract_dir:
            shutil.rmtree(self._extract_dir, ignore_errors=True)
            self._extract_dir = None
        if password and not self._entries:
            # Re-list now that the (header-encrypted) archive can be read.
            with self._py7zr.SevenZipFile(self.path, mode="r", password=password) as zf:
                self._entries = [
                    _Entry(_normalise(f.filename),
                           bool(getattr(f, "is_directory", False))
                           or (f.filename or "").endswith("/"),
                           getattr(f, "uncompressed", 0) or 0,
                           getattr(f, "creationtime", None) or time.time(), True)
                    for f in zf.list()
                ]

    def list_entries(self):
        return list(self._entries)

    def _ensure_cache(self):
        """Materialises the whole archive into a temp directory once.

        py7zr offers no per-member random-access reader: the supported
        API extracts to a directory. (Older versions had `readall()`
        returning in-memory buffers, but that was removed in py7zr 1.x
        and would have been a poor fit anyway -- it holds the entire
        decompressed archive in RAM.) Extracting once to disk and serving
        members from there is both version-proof and far kinder to memory
        on multi-GB archives, and means a re-save never decompresses the
        same data twice.
        """
        if self._extract_dir is not None:
            return
        target = tempfile.mkdtemp(prefix="isoforge7z_")
        try:
            with self._py7zr.SevenZipFile(self.path, mode="r",
                                          password=self._password) as zf:
                zf.extractall(path=target)
        except Exception as exc:
            shutil.rmtree(target, ignore_errors=True)
            msg = str(exc).lower()
            if "password" in msg or "encrypt" in msg or "crc" in msg:
                raise PasswordRequired(str(exc)) from exc
            raise ArchiveBackendError(
                f"Could not read the contents of this 7-Zip archive: {exc}"
            ) from exc
        self._extract_dir = target

    def open_member(self, member_path: str):
        self._ensure_cache()
        rel = _normalise(member_path).lstrip("/")
        candidate = os.path.join(self._extract_dir, *rel.split("/"))
        if os.path.isfile(candidate):
            return open(candidate, "rb")
        # Fall back to a case-insensitive / separator-tolerant sweep, for
        # archives whose stored names don't round-trip exactly through
        # the local filesystem's naming rules.
        for root, _dirs, files in os.walk(self._extract_dir):
            for fname in files:
                full = os.path.join(root, fname)
                got = os.path.relpath(full, self._extract_dir).replace(os.sep, "/")
                if got.lower() == rel.lower():
                    return open(full, "rb")
        raise ArchiveBackendError(
            f"'{member_path}' was not found inside this 7-Zip archive.")

    def close(self) -> None:
        if self._extract_dir:
            shutil.rmtree(self._extract_dir, ignore_errors=True)
            self._extract_dir = None


class _RarAdapter(_BaseAdapter):
    fmt = formats.RAR

    def __init__(self, path: str):
        super().__init__(path)
        _require("rarfile", "rarfile", "Opening RAR archives")
        import rarfile
        self._rarfile = rarfile
        # Make sure rarfile is pointed at whatever tool the user
        # configured before we ask it to do anything -- otherwise it
        # searches only PATH and would miss a copy sitting beside the app.
        rar_tools._sync_rarfile_tool()
        try:
            self._rf = rarfile.RarFile(path)
        except Exception as exc:
            raise ArchiveBackendError(
                f"Could not open '{os.path.basename(path)}' as a RAR archive: {exc}\n\n"
                "Reading RAR files needs RARLAB's UnRAR.exe (or Rar.exe), "
                "because the RAR format is proprietary and cannot be "
                "decompressed by Python alone.\n\n"
                "Set its location under Tools > Settings, or place it in "
                "the ISOForge folder."
            ) from exc
        self._infos = self._rf.infolist()

    @property
    def needs_password(self) -> bool:
        try:
            return bool(self._rf.needs_password())
        except Exception:
            return False

    def set_password(self, password: Optional[str]) -> None:
        super().set_password(password)
        try:
            self._rf.setpassword(password)
        except Exception:
            pass

    def list_entries(self):
        out = []
        for info in self._infos:
            is_dir = bool(getattr(info, "is_dir", lambda: False)()) \
                or (info.filename or "").endswith("/")
            mtime = time.time()
            dt = getattr(info, "date_time", None)
            if dt:
                try:
                    mtime = time.mktime(tuple(dt) + (0, 0, -1))
                except Exception:
                    pass
            out.append(_Entry(_normalise(info.filename), is_dir,
                              getattr(info, "file_size", 0) or 0, mtime,
                              self.needs_password))
        return out

    def open_member(self, member_path: str):
        target = _normalise(member_path)
        for info in self._infos:
            if _normalise(info.filename) == target:
                if getattr(info, "is_dir", lambda: False)():
                    continue
                return self._rf.open(info)
        raise ArchiveBackendError(f"'{member_path}' was not found inside this RAR archive.")

    def close(self) -> None:
        try:
            self._rf.close()
        except Exception:
            pass


# Signatures marking where a payload begins inside an SFX, used when a
# reader can't cope with the stub sitting in front of it.
#
# ZIP is deliberately absent. `zipfile` finds its index by scanning
# BACKWARDS from the end of the file, so a zip payload is readable in
# place and never needs carving -- and `PK\x03\x04` occurs often enough
# inside ordinary executables to make scanning for it actively harmful.
_PAYLOAD_SIGNATURES = (
    (b"7z\xBC\xAF\x27\x1C", formats.SEVENZ),
    (b"Rar!\x1A\x07\x01\x00", formats.RAR),   # RAR5
    (b"Rar!\x1A\x07\x00", formats.RAR),       # RAR4
)

# How far into an SFX to look for the payload. Stubs are tens to a few
# hundred KB, so this is wildly generous while still bounding the read
# on a multi-GB file.
_SFX_SCAN_LIMIT = 16 * 1024 * 1024

# A stub can coincidentally contain these bytes, so several candidate
# offsets are tried rather than trusting the first match.
_SFX_MAX_CANDIDATES = 8


def _find_payload_candidates(path: str):
    """Offsets inside `path` where an archive payload might begin."""
    try:
        with open(path, "rb") as fh:
            window = fh.read(_SFX_SCAN_LIMIT)
    except OSError:
        return []

    found = []
    for signature, kind in _PAYLOAD_SIGNATURES:
        start = 0
        while len(found) < _SFX_MAX_CANDIDATES:
            idx = window.find(signature, start)
            if idx < 0:
                break
            found.append((idx, kind))
            start = idx + 1
    found.sort(key=lambda pair: pair[0])
    return found


class _SfxAdapter(_BaseAdapter):
    """Reads a self-extracting archive by delegating to a real adapter.

    An SFX carries no marker saying which format its payload is, so the
    only reliable approach is to try each reader and keep whichever one
    succeeds.

    Two strategies, in order:

      1. READ IN PLACE. ZIP is tried first because `zipfile` locates the
         central directory by scanning backwards from the end of the
         file and is therefore completely untroubled by the stub in
         front of it. RAR is tried last, because it needs an external
         tool and attempting it early would turn "this isn't an archive"
         into a confusing complaint about UnRAR being missing.

      2. CARVE THE PAYLOAD OUT. 7z stores its signature at offset ZERO,
         so `py7zr` rejects any file with a stub glued to the front --
         which is every 7z-based SFX ever made, including the ones
         ISOForge itself produces with 7-Zip's `7z.sfx` module. For
         those, the payload is located by signature, copied to a temp
         file, and opened from there.

    Carving costs temporary disk space equal to the payload, which is
    why it is a fallback rather than the first thing tried. The temp
    file is removed by `close()`.
    """

    fmt = formats.SFX

    def __init__(self, path: str):
        super().__init__(path)
        self._inner: Optional[_BaseAdapter] = None
        self._carved: Optional[str] = None
        self.payload_label = ""
        attempts: list = []

        for adapter_cls, label in ((_ZipAdapter, "ZIP"),
                                   (_SevenZAdapter, "7z"),
                                   (_RarAdapter, "RAR")):
            try:
                self._inner = adapter_cls(path)
                self.payload_label = label
                break
            except Exception as exc:
                attempts.append(f"{label}: {exc}")

        if self._inner is None:
            self._inner = self._open_by_carving(attempts)

        if self._inner is None:
            raise ArchiveBackendError(
                f"'{os.path.basename(path)}' doesn't appear to be a "
                "self-extracting archive.\n\n"
                "ISOForge tried to read it as a ZIP, 7z and RAR payload "
                "and none of them matched, so it is most likely an "
                "ordinary program rather than an archive.\n\n"
                + "\n".join(attempts[:3])
            )

    # -- carving ------------------------------------------------------ #
    def _open_by_carving(self, attempts: list):
        """Locates a payload by signature and opens it from a temp copy.

        Every candidate offset is VERIFIED by actually opening what was
        carved, so a stub that happens to contain the signature bytes
        simply costs one wasted attempt instead of producing a confusing
        failure.
        """
        for offset, kind in _find_payload_candidates(self.path):
            if offset <= 0:
                # Offset zero means there's no stub at all, which the
                # in-place attempts above already covered.
                continue

            carved = self._carve_from(offset)
            if carved is None:
                continue

            adapter_cls = (_SevenZAdapter if kind == formats.SEVENZ
                           else _RarAdapter)
            try:
                inner = adapter_cls(carved)
            except Exception as exc:
                attempts.append(f"payload at offset {offset}: {exc}")
                quiet_remove(carved)
                continue

            self._carved = carved
            self.payload_label = formats.format_label(kind)
            return inner
        return None

    def _carve_from(self, offset: int) -> Optional[str]:
        """Copies everything from `offset` onwards into a temp file."""
        try:
            handle, carved = tempfile.mkstemp(prefix="isoforgesfx_",
                                              suffix=".payload")
            with os.fdopen(handle, "wb") as out, open(self.path, "rb") as src:
                src.seek(offset)
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
            return carved
        except OSError:
            return None

    # -- everything below simply forwards to whichever adapter matched  #
    @property
    def needs_password(self) -> bool:
        return self._inner.needs_password

    def set_password(self, password: Optional[str]) -> None:
        super().set_password(password)
        self._inner.set_password(password)

    def list_entries(self):
        return self._inner.list_entries()

    def open_member(self, member_path: str):
        return self._inner.open_member(member_path)

    def close(self) -> None:
        if self._inner is not None:
            self._inner.close()
            self._inner = None
        if self._carved:
            quiet_remove(self._carved)
            self._carved = None


_ADAPTERS = {
    formats.ZIP: _ZipAdapter,
    formats.SEVENZ: _SevenZAdapter,
    formats.RAR: _RarAdapter,
    formats.SFX: _SfxAdapter,
}


# --------------------------------------------------------------------- #
# The source object (mirrors iso_backend.IsoSource)
# --------------------------------------------------------------------- #
class ArchiveSource:
    """Read-only handle on an existing ZIP/7Z/RAR/SFX, kept alive so
    members that were never modified can still be extracted or
    re-saved."""

    def __init__(self, path: str, fmt: Optional[str] = None):
        self.archive_path = path
        self.fmt = fmt or formats.detect_format(path)
        if self.fmt not in _ADAPTERS:
            raise ArchiveBackendError(
                f"'{os.path.basename(path)}' is not a ZIP, 7Z, RAR or "
                "self-extracting archive."
            )
        self._adapter = _ADAPTERS[self.fmt](path)
        self._closed = False
        self._password: Optional[str] = None

    # -- password ----------------------------------------------------- #
    @property
    def needs_password(self) -> bool:
        return self._adapter.needs_password and self._password is None

    @property
    def password(self) -> Optional[str]:
        return self._password

    def try_password(self, password: str) -> bool:
        """Applies `password` and VERIFIES it by actually decrypting the
        smallest real member. Verifying eagerly matters: a wrong password
        on ZipCrypto in particular can otherwise sail through the initial
        call and only produce garbage bytes much later, during a save."""
        try:
            self._adapter.set_password(password)
        except Exception:
            return False

        smallest = None
        for entry in self._adapter.list_entries():
            if entry.is_dir or entry.size <= 0:
                continue
            if smallest is None or entry.size < smallest.size:
                smallest = entry

        if smallest is None:
            # Nothing decryptable to test against -- accept it.
            self._password = password
            return True

        try:
            fh = self._adapter.open_member(smallest.path)
            try:
                fh.read(min(4096, smallest.size))
            finally:
                try:
                    fh.close()
                except Exception:
                    pass
        except Exception:
            self._adapter.set_password(None)
            return False

        self._password = password
        return True

    # -- listing ------------------------------------------------------- #
    def populate(self, root_node) -> None:
        """Builds the VFS tree from the archive's member list.

        Archives store a flat list of paths rather than a real directory
        tree, and many omit explicit directory entries entirely, so
        intermediate folders are synthesised on demand from each member's
        path -- otherwise a zip containing only `a/b/c.txt` would appear
        completely empty.
        """
        root_node.children = []
        try:
            entries = self._adapter.list_entries()
        except Exception as exc:
            raise ArchiveBackendError(
                f"Could not read the contents of "
                f"'{os.path.basename(self.archive_path)}': {exc}"
            ) from exc

        # Work out every path that is genuinely a DIRECTORY before
        # creating anything. Two sources feed this:
        #   1. entries explicitly flagged as directories, and
        #   2. every intermediate parent path implied by any member.
        # Resolving this up front matters because some archives store a
        # zero-byte entry for a folder (e.g. "docs/") that would
        # otherwise be reconstructed as a FILE named "docs", colliding
        # with the real "docs" folder and producing a duplicate.
        dir_paths = set()
        for entry in entries:
            if entry.is_dir:
                dir_paths.add(entry.path)
            parent = entry.path.rpartition("/")[0]
            while parent and parent != "/":
                dir_paths.add(parent)
                parent = parent.rpartition("/")[0]

        dir_cache = {"/": root_node}

        def ensure_dir(dir_path: str):
            dir_path = _normalise(dir_path)
            existing = dir_cache.get(dir_path)
            if existing is not None:
                return existing
            parent_path, _, name = dir_path.rpartition("/")
            parent = ensure_dir(parent_path or "/")
            node = parent.find_child(name, is_dir=True)
            if node is None:
                node = parent.new_child(name, is_dir=True)
                node.source_kind = "archive"
            dir_cache[dir_path] = node
            return node

        for entry in sorted(entries, key=lambda e: e.path):
            if entry.path == "/":
                continue
            # Anything known to be a directory becomes one, even if the
            # archive listed it without a directory flag.
            if entry.is_dir or entry.path in dir_paths:
                ensure_dir(entry.path)
                continue
            parent_path, _, name = entry.path.rpartition("/")
            if not name:
                continue
            parent = ensure_dir(parent_path or "/")
            node = parent.find_child(name, is_dir=False)
            if node is None:
                node = parent.new_child(name, is_dir=False)
            node.source_kind = "archive"
            node.source_archive = self
            node.source_path = entry.path
            node.size = entry.size
            node.mtime = entry.mtime

    # -- reading ------------------------------------------------------- #
    def extract_to_fp(self, member_path: str, fp) -> None:
        if self._closed:
            raise ArchiveBackendError(
                f"'{member_path}' can no longer be read: this archive has "
                "already been closed."
            )
        try:
            src = self._adapter.open_member(member_path)
        except PasswordRequired:
            raise
        except ArchiveBackendError:
            raise
        except Exception as exc:
            msg = str(exc).lower()
            if "password" in msg or "encrypted" in msg or "bad password" in msg:
                raise PasswordRequired(
                    f"'{member_path}' is password protected."
                ) from exc
            raise ArchiveBackendError(
                f"Could not read '{member_path}' from the archive: {exc}"
            ) from exc

        try:
            while True:
                chunk = src.read(_COPY_CHUNK)
                if not chunk:
                    break
                fp.write(chunk)
        except Exception as exc:
            msg = str(exc).lower()
            if "password" in msg or "bad password" in msg or "crc" in msg:
                raise PasswordRequired(
                    f"Could not decrypt '{member_path}'. The password may be incorrect."
                ) from exc
            raise ArchiveBackendError(
                f"Could not read '{member_path}' from the archive: {exc}"
            ) from exc
        finally:
            try:
                src.close()
            except Exception:
                pass

    def extract_to_path(self, member_path: str, dest_file: str) -> None:
        os.makedirs(os.path.dirname(dest_file), exist_ok=True)
        with open(dest_file, "wb") as fp:
            self.extract_to_fp(member_path, fp)

    def close(self) -> None:
        if self._closed:
            return
        self._adapter.close()
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed

    # Naming parity with IsoSource so callers can stay generic.
    @property
    def iso_path(self) -> str:
        return self.archive_path


def open_archive(path: str, fmt: Optional[str] = None) -> ArchiveSource:
    try:
        return ArchiveSource(path, fmt)
    except ArchiveBackendError:
        raise
    except Exception as exc:  # pragma: no cover
        raise ArchiveBackendError(
            f"Failed to open '{os.path.basename(path)}': {exc}"
        ) from exc


# --------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------- #
def build_archive(
    root_node,
    out_path: str,
    fmt: str,
    password: Optional[str] = None,
    compression_level: int = 5,
    progress_cb: ProgressCB = None,
    source_to_close_before_replace=None,
    cancel_token=None,
) -> BuildResult:
    """Writes the VFS tree to a new archive at `out_path`.

    Returns the same `BuildResult` shape `iso_backend.build_iso()` does,
    so the UI's existing result-reporting path handles both identically.

    Writes to a temporary file first and only swaps it into place once
    complete, matching the ISO path -- an interrupted or failed save can
    therefore never leave a half-written archive where the user's
    original file used to be. `source_to_close_before_replace` is closed
    only after a successful write and immediately before that swap, since
    Windows refuses to replace a file that is still open.

    `cancel_token`, if supplied, is forwarded into the shared
    `ProgressReporter` (and, for RAR specifically, into
    `rar_tools.create_rar` so the external Rar.exe process can be killed
    promptly -- see this module's docstring). If cancellation is
    requested, `fileops.OperationCancelled` propagates out of whichever
    `_write_*` helper was running; the temp output file is removed before
    it is re-raised, exactly like any other build failure.

    A cancellation requested while a `_write_*` helper was genuinely
    blocked inside the operating system (a large single-member flush, the
    final close of the output file, antivirus scanning it on close) has
    no `write()` call of its own left to raise it from until that call
    returns -- see `app/progress.py`'s docstring for the full
    explanation. So the cancel token is checked once more here,
    immediately after the helper returns and BEFORE the temp file is
    committed, mirroring `iso_backend.build_iso()`'s identical safety
    net.
    """
    if not formats.can_write(fmt):
        raise ArchiveBackendError(_cannot_write_message(fmt))
    if password and not formats.supports_write_encryption(fmt):
        raise ArchiveBackendError(
            f"{formats.format_label(fmt)} does not support password protection."
        )

    files, dirs = _flatten(root_node)
    base_units = sum(max(0, f.size) + _PER_ITEM_OVERHEAD for f in files)
    base_units += len(dirs) * _PER_ITEM_OVERHEAD
    base_units = float(base_units or 1)

    # Work out the progress budget. Most formats compress in a single
    # pass, but two do extra work that the bar must account for or it
    # will stall at the end:
    #   RAR -- stages the tree to disk, then runs Rar.exe over it.
    #   SFX -- builds a payload archive, then copies stub+payload out.
    sfx_payload_fmt = None
    concat_units = 0.0
    if fmt == formats.SFX:
        sfx_payload_fmt = sfx_tools.payload_kind_for_current()
        concat_units = base_units * _SFX_CONCAT_SHARE

    doubles_work = (fmt == formats.RAR) or (sfx_payload_fmt == formats.RAR)
    payload_units = base_units * 2 if doubles_work else base_units
    total_units = payload_units + concat_units

    reporter = ProgressReporter(progress_cb, total_units or 1, cancel_token=cancel_token)

    tmp_out = f"{out_path}.tmp_building"
    quiet_remove(tmp_out)
    skipped: list = []

    try:
        if fmt == formats.ZIP:
            _write_zip(files, dirs, tmp_out, password, compression_level,
                       reporter, skipped)
        elif fmt == formats.SEVENZ:
            _write_7z(files, dirs, tmp_out, password, reporter, skipped)
        elif fmt == formats.RAR:
            _write_rar(files, dirs, tmp_out, password, compression_level,
                       reporter, skipped, base_units, cancel_token)
        elif fmt == formats.SFX:
            _write_sfx(files, dirs, tmp_out, password, compression_level,
                       reporter, skipped, sfx_payload_fmt,
                       base_units, payload_units, concat_units, cancel_token)
        else:  # pragma: no cover - guarded above
            raise ArchiveBackendError(f"Unsupported output format: {fmt}")
    except OperationCancelled:
        quiet_remove(tmp_out)
        raise
    except ArchiveBackendError:
        quiet_remove(tmp_out)
        raise
    except (rar_tools.RarToolError, sfx_tools.SfxToolError) as exc:
        quiet_remove(tmp_out)
        raise ArchiveBackendError(str(exc)) from exc
    except Exception as exc:
        quiet_remove(tmp_out)
        raise ArchiveBackendError(
            f"Could not create {formats.format_label(fmt)}: {exc}"
        ) from exc

    # The write helper above returned WITHOUT raising -- but it may have
    # spent a long time blocked inside a single OS-level flush/close with
    # no opportunity to raise OperationCancelled while it was stuck (see
    # app/progress.py). Check once more, right here, before treating this
    # as a success and committing it.
    if cancel_token is not None and cancel_token.cancelled:
        quiet_remove(tmp_out)
        raise OperationCancelled()

    if source_to_close_before_replace is not None:
        source_to_close_before_replace.close()
    replace_with_retry(tmp_out, out_path)
    reporter.finish("Done")

    return BuildResult(skipped=skipped,
                       format_label=_result_label(fmt, password, sfx_payload_fmt),
                       used_fallback_format=False)


def _cannot_write_message(fmt: str) -> str:
    """Explains why a format can't currently be produced, and how to fix
    it. Both cases are a missing external binary, but for different
    reasons and with different remedies."""
    if fmt == formats.SFX:
        return (
            "Self-extracting archives cannot be created yet.\n\n"
            "One is built by attaching your files to a small stub "
            "program, and ISOForge needs a copy of that stub. Install "
            "7-Zip (free) and ISOForge will find one automatically, or "
            "set its location under Tools > Settings."
        )
    if fmt == formats.RAR:
        return (
            "RAR archive files cannot be created.\n\n"
            "Creating RAR archives requires RARLAB's Rar.exe, which "
            "cannot be bundled with ISOForge for licensing reasons.\n\n"
            "Set its location under Tools > Settings, or save as ZIP or "
            "7Z instead."
        )
    return f"{formats.format_label(fmt)} files cannot be created."


def _result_label(fmt: str, password: Optional[str],
                  sfx_payload_fmt: Optional[str]) -> str:
    """The human-readable summary shown after a successful build.

    For SFX this names the payload too, since "self-extracting archive"
    alone hides a detail the user may care about when sharing the file --
    whether the recipient could also open it with 7-Zip, WinRAR, or
    Explorer's built-in ZIP support.
    """
    label = formats.format_label(fmt)
    if fmt == formats.SFX and sfx_payload_fmt:
        label += f" ({formats.format_label(sfx_payload_fmt)} payload)"
    if password:
        if fmt == formats.RAR:
            label += " (RAR encrypted)"
        else:
            label += " (AES-256 encrypted)"
    return label


def _flatten(root_node):
    """Returns (files, dirs) as flat lists of VNodes, each carrying the
    archive-relative path it should be stored under."""
    files, dirs = [], []

    def walk(node, prefix):
        for child in node.children:
            rel = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir:
                child._arc_name = rel
                dirs.append(child)
                walk(child, rel)
            else:
                child._arc_name = rel
                files.append(child)

    walk(root_node, "")
    return files, dirs


def _member_reader(node):
    """Yields a readable file object for a VNode regardless of whether it
    came from the local disk or another container, plus a flag saying
    whether the caller must close it."""
    if node.source_kind == "disk":
        return open(node.source_path, "rb"), True
    source = getattr(node, "source_archive", None)
    if node.source_kind == "archive" and source is not None:
        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX)
        source.extract_to_fp(node.source_path, spool)
        spool.seek(0)
        return spool, True
    iso_source = getattr(node, "source_iso", None)
    if node.source_kind == "iso" and iso_source is not None:
        # Supports converting an opened ISO straight into an archive.
        spool = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX)
        iso_source.extract_to_fp(node.source_path, spool)
        spool.seek(0)
        return spool, True
    raise ArchiveBackendError(f"'{node.name}' has no readable source.")


def _write_zip(files, dirs, tmp_out, password, level, reporter, skipped):
    mod, has_aes = _zip_module(prefer_aes=bool(password))
    if password and not has_aes:
        raise ArchiveBackendError(
            "Creating password-protected ZIP files requires the 'pyzipper' "
            "package, which isn't installed.\n\nInstall it with:\n\n"
            "    pip install pyzipper\n\n"
            "(The standard library can read encrypted ZIPs but cannot "
            "create them.)"
        )

    if password:
        zf = mod.AESZipFile(tmp_out, "w", compression=mod.ZIP_DEFLATED,
                            encryption=mod.WZ_AES)
        zf.setpassword(password.encode("utf-8"))
    else:
        try:
            zf = mod.ZipFile(tmp_out, "w", compression=mod.ZIP_DEFLATED,
                             compresslevel=level)
        except TypeError:
            # compresslevel only exists on newer Pythons.
            zf = mod.ZipFile(tmp_out, "w", compression=mod.ZIP_DEFLATED)

    try:
        for node in dirs:
            try:
                zf.writestr(node._arc_name.rstrip("/") + "/", b"")
            except Exception as exc:
                skipped.append((node.path(), f"Could not add this folder: {exc}"))
            reporter.advance(_PER_ITEM_OVERHEAD, f"Adding folder {node.name}")

        for node in files:
            try:
                fh, must_close = _member_reader(node)
            except Exception as exc:
                skipped.append((node.path(), str(exc)))
                reporter.advance(max(0, node.size) + _PER_ITEM_OVERHEAD,
                                 f"Skipping {node.name}")
                continue
            try:
                with _open_zip_member_for_write(zf, node) as dest:
                    while True:
                        chunk = fh.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        dest.write(chunk)
                        reporter.advance(len(chunk), f"Compressing {node.name}")
            except Exception as exc:
                skipped.append((node.path(), f"Could not add this file: {exc}"))
            finally:
                if must_close:
                    try:
                        fh.close()
                    except Exception:
                        pass
            reporter.advance(_PER_ITEM_OVERHEAD, f"Added {node.name}")
    finally:
        zf.close()


# zipfile's classic (non-ZIP64) limit. Mirrors zipfile.ZIP64_LIMIT; kept
# local so this module never depends on a private-ish stdlib constant.
_ZIP64_LIMIT = (1 << 31) - 1


def _open_zip_member_for_write(zf, node):
    """Opens a streaming write handle for one ZIP member, enabling ZIP64
    up front for anything that could exceed the classic 2 GB limit.

    `ZipFile.open(name, "w")` cannot know how big the stream will be, so
    it decides whether to write a ZIP64 local header BEFORE any data
    arrives -- and without `force_zip64=True` it assumes "small". Writing
    a member larger than ~2 GB then fails at close() with "File size too
    large, try using force_zip64", which the per-item resilience block in
    `_write_zip` would report as a skipped file. The VFS already knows
    each node's size, so ZIP64 is requested exactly when it's needed and
    small members keep the slightly more compact classic header.

    The TypeError fallback covers any zipfile/pyzipper build whose
    `open()` predates the `force_zip64` keyword.
    """
    needs_zip64 = int(getattr(node, "size", 0) or 0) * 1.05 > _ZIP64_LIMIT
    try:
        return zf.open(node._arc_name, "w", force_zip64=needs_zip64)
    except TypeError:
        return zf.open(node._arc_name, "w")


def _write_7z(files, dirs, tmp_out, password, reporter, skipped,
              header_encryption: bool = True):
    """Writes a 7z archive.

    `header_encryption` hides the FILE NAMES as well as the contents,
    matching 7-Zip's own `-mhe=on`, and is what users expect "password
    protected" to mean -- so it defaults on. `_write_sfx` deliberately
    turns it off for an SFX payload; see that function for why.
    """
    _require("py7zr", "py7zr", "Creating 7-Zip archives")
    import py7zr

    kwargs = {"mode": "w"}
    if password:
        kwargs["password"] = password
        kwargs["header_encryption"] = bool(header_encryption)

    with py7zr.SevenZipFile(tmp_out, **kwargs) as zf:
        # Deliberately NOT writing explicit directory entries here.
        # py7zr creates parent folders implicitly from each member's
        # path, and an explicit "name/" entry round-trips back as a
        # zero-byte FILE called "name" -- which would then collide with
        # the real folder of the same name when the archive is reopened.
        for node in dirs:
            reporter.advance(_PER_ITEM_OVERHEAD, f"Adding folder {node.name}")

        for node in files:
            try:
                fh, must_close = _member_reader(node)
            except Exception as exc:
                skipped.append((node.path(), str(exc)))
                reporter.advance(max(0, node.size) + _PER_ITEM_OVERHEAD,
                                 f"Skipping {node.name}")
                continue
            try:
                # Streamed rather than read into memory: the previous
                # `fh.read()` + `writestr()` held each member's ENTIRE
                # contents in RAM, so a multi-GB file could exhaust memory.
                # Local files are handed to py7zr by path (which also keeps
                # their timestamps); everything else is passed as a seekable
                # stream via `writef()`. `writestr()` remains only as a
                # fallback for very old py7zr builds without `writef()`.
                if node.source_kind == "disk":
                    zf.write(node.source_path, node._arc_name)
                elif hasattr(zf, "writef"):
                    zf.writef(fh, node._arc_name)
                else:
                    zf.writestr(fh.read(), node._arc_name)
                reporter.advance(max(0, int(node.size or 0)) + _PER_ITEM_OVERHEAD,
                                 f"Compressing {node.name}")
            except Exception as exc:
                skipped.append((node.path(), f"Could not add this file: {exc}"))
                reporter.advance(_PER_ITEM_OVERHEAD, f"Skipping {node.name}")
            finally:
                if must_close:
                    try:
                        fh.close()
                    except Exception:
                        pass


def _write_rar(files, dirs, tmp_out, password, level, reporter, skipped,
               staging_units, cancel_token=None):
    """Builds a RAR by staging the tree to disk, then running Rar.exe.

    See this module's docstring for why RAR alone needs a staging pass:
    Rar.exe compresses real files, while the VFS describes a layout that
    may exist nowhere on disk.

    The staging directory is always removed afterwards, including when
    the compression step fails -- a half-staged copy of the user's data
    left behind in TEMP would be both confusing and potentially large.
    This applies equally to cancellation: the `finally` below runs
    whether `_stage_tree` / `rar_tools.create_rar` complete normally,
    fail, or unwind via `fileops.OperationCancelled`.
    """
    if not rar_tools.can_create_rar():
        raise ArchiveBackendError(
            "Creating RAR archives requires RARLAB's Rar.exe.\n\n"
            "Set its location under Tools > Settings, or place Rar.exe "
            "in the ISOForge folder.\n\n"
            "It ships with WinRAR and cannot be bundled with ISOForge "
            "for licensing reasons."
        )

    staging = tempfile.mkdtemp(prefix="isoforgerar_")
    try:
        # -- phase 1: stage ------------------------------------------- #
        for node in dirs:
            target = os.path.join(staging, *node._arc_name.split("/"))
            try:
                os.makedirs(target, exist_ok=True)
            except Exception as exc:
                skipped.append((node.path(), f"Could not create this folder: {exc}"))
            reporter.advance(_PER_ITEM_OVERHEAD, f"Preparing {node.name}")

        for node in files:
            target = os.path.join(staging, *node._arc_name.split("/"))
            try:
                os.makedirs(os.path.dirname(target) or staging, exist_ok=True)
            except Exception as exc:
                skipped.append((node.path(), f"Could not prepare this file: {exc}"))
                reporter.advance(max(0, node.size) + _PER_ITEM_OVERHEAD,
                                 f"Skipping {node.name}")
                continue

            try:
                fh, must_close = _member_reader(node)
            except Exception as exc:
                skipped.append((node.path(), str(exc)))
                reporter.advance(max(0, node.size) + _PER_ITEM_OVERHEAD,
                                 f"Skipping {node.name}")
                continue

            try:
                with open(target, "wb") as dest:
                    while True:
                        chunk = fh.read(_COPY_CHUNK)
                        if not chunk:
                            break
                        dest.write(chunk)
                        reporter.advance(len(chunk), f"Preparing {node.name}")
            except Exception as exc:
                skipped.append((node.path(), f"Could not add this file: {exc}"))
            finally:
                if must_close:
                    try:
                        fh.close()
                    except Exception:
                        pass
            reporter.advance(_PER_ITEM_OVERHEAD, f"Prepared {node.name}")

        if not os.listdir(staging):
            raise ArchiveBackendError(
                "There is nothing to put in this archive — every item "
                "was skipped or the project is empty."
            )

        # -- phase 2: compress ---------------------------------------- #
        def on_rar_progress(fraction: float, label: str) -> None:
            # Rar.exe reports its own 0-100%, which maps onto the second
            # half of the budget reserved above.
            reporter.set_phase_position(
                staging_units,
                max(0.0, min(1.0, fraction)) * staging_units,
                f"Compressing{(' ' + label) if label else '...'}",
            )

        rar_tools.create_rar(
            staging_dir=staging,
            out_path=os.path.abspath(tmp_out),
            password=password,
            # ISOForge's generic 0-9 "level" maps onto RAR's 0-5 scale.
            compression=min(5, max(0, round(level * 5 / 9))) if level else 3,
            progress_cb=on_rar_progress,
            cancel_token=cancel_token,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_sfx(files, dirs, tmp_out, password, level, reporter, skipped,
               payload_fmt, base_units, payload_units, concat_units,
               cancel_token=None):
    """Builds a self-extracting archive: stub executable + payload.

    Two steps, in order:

      1. Build an ordinary archive in the format this particular stub
         knows how to unpack (`payload_fmt`, inferred by `sfx_tools`).
      2. Write the stub out, then append the payload byte-for-byte.

    Step 2 really is just concatenation -- see `sfx_tools`' docstring for
    why that produces a file which is simultaneously a valid program and
    a valid archive. It is exactly what 7-Zip and WinRAR do.

    A note on header encryption: when the payload is 7z, this
    deliberately passes `header_encryption=False`. Encrypting the header
    hides the filenames, but it also means nothing can read the archive's
    structure without the password first -- and a stub has to be able to
    open the archive in order to know it should prompt at all. Leaving
    the header readable keeps the file contents encrypted with AES-256
    while guaranteeing the extractor can actually ask for the password.
    """
    stub = sfx_tools.read_stub_bytes()

    payload_tmp = f"{tmp_out}.payload"
    quiet_remove(payload_tmp)

    try:
        # -- step 1: the payload archive ------------------------------ #
        if payload_fmt == formats.ZIP:
            _write_zip(files, dirs, payload_tmp, password, level,
                       reporter, skipped)
        elif payload_fmt == formats.RAR:
            _write_rar(files, dirs, payload_tmp, password, level,
                       reporter, skipped, base_units, cancel_token)
        else:
            _write_7z(files, dirs, payload_tmp, password, reporter, skipped,
                      header_encryption=False)

        if not os.path.exists(payload_tmp):
            raise ArchiveBackendError(
                "The archive payload could not be created, so there is "
                "nothing to attach to the self-extracting stub."
            )

        # -- step 2: stub + payload ----------------------------------- #
        payload_size = os.path.getsize(payload_tmp)
        copied = 0

        with open(tmp_out, "wb") as out:
            out.write(stub)
            with open(payload_tmp, "rb") as src:
                while True:
                    chunk = src.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    copied += len(chunk)
                    fraction = copied / payload_size if payload_size else 1.0
                    reporter.set_phase_position(
                        payload_units,
                        min(1.0, fraction) * concat_units,
                        "Building self-extracting program",
                    )
    finally:
        quiet_remove(payload_tmp)


def quick_folder_to_archive(folder_path: str, out_path: str, fmt: str,
                            password: Optional[str] = None,
                            progress_cb: ProgressCB = None,
                            cancel_token=None) -> BuildResult:
    """Convenience helper backing the 'Convert folder to archive' wizard."""
    from .vfs import VNode
    from .diskscan import populate_from_disk

    root = VNode(name="", is_dir=True)
    root.source_kind = "new"
    populate_from_disk(root, folder_path)
    return build_archive(root, out_path, fmt, password=password,
                         progress_cb=progress_cb, cancel_token=cancel_token)


# --------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------- #
def extract_node(node, dest_dir: str, progress_cb: ProgressCB = None,
                 cancel_token=None) -> None:
    """Extracts `node` and everything beneath it to `dest_dir`, with the
    same byte-weighted progress model used elsewhere.

    `cancel_token`, if supplied, is forwarded into the shared
    `ProgressReporter`; since `_extract` below never catches exceptions
    broadly, `fileops.OperationCancelled` propagates straight out with no
    special handling needed here.
    """
    total_units = 0.0

    def measure(n):
        nonlocal total_units
        total_units += _PER_ITEM_OVERHEAD
        if n.is_dir:
            for c in n.children:
                measure(c)
        else:
            total_units += max(0, int(getattr(n, "size", 0) or 0))

    measure(node)
    reporter = ProgressReporter(progress_cb, total_units or 1, cancel_token=cancel_token)

    def _extract(n, dest_path):
        if n.is_dir:
            os.makedirs(dest_path, exist_ok=True)
            reporter.advance(_PER_ITEM_OVERHEAD, f"Extracting {n.name}")
            for c in n.children:
                _extract(c, os.path.join(dest_path, c.name))
            return

        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        if n.source_kind == "disk":
            with open(n.source_path, "rb") as fin, open(dest_path, "wb") as fout:
                while True:
                    chunk = fin.read(_COPY_CHUNK)
                    if not chunk:
                        break
                    fout.write(chunk)
                    reporter.advance(len(chunk), f"Extracting {n.name}")
        elif n.source_kind == "archive" and getattr(n, "source_archive", None):
            with open(dest_path, "wb") as fp:
                counting = SpoolProgressWriter(fp, reporter, f"Extracting {n.name}")
                n.source_archive.extract_to_fp(n.source_path, counting)
        elif n.source_kind == "iso" and getattr(n, "source_iso", None):
            with open(dest_path, "wb") as fp:
                counting = SpoolProgressWriter(fp, reporter, f"Extracting {n.name}")
                n.source_iso.extract_to_fp(n.source_path, counting)
        else:
            raise ArchiveBackendError(f"'{n.name}' has no readable source.")
        reporter.advance(_PER_ITEM_OVERHEAD, f"Extracting {n.name}")

    _extract(node, os.path.join(dest_dir, node.name) if node.name else dest_dir)
    reporter.finish("Done")

