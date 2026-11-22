"""
chd_tools.py
------------
Locates and drives MAME's `chdman.exe` to convert disc images into CHD.

What CHD is, and why ISOForge drives chdman instead of doing it itself
=======================================================================
CHD ("Compressed Hunks of Data") is the lossless container used by MAME,
RetroArch, DuckStation, PCSX2, Dolphin-style front-ends and most other
emulators to store CD / DVD images in a fraction of the space. The format
is produced by one canonical tool, `chdman`, which ships with MAME (it is
open source and free to redistribute, but the build you want depends on
your platform and MAME version, so ISOForge asks you to point at your own
copy -- exactly like it does for Rar.exe and the SFX stub).

So CHD capability is DYNAMIC, like RAR and SFX:

    no chdman configured / found -> "Convert to CHD" explains how to fix it
    chdman configured / found    -> conversion works, in-app and from Explorer

What gets converted
====================
`chdman` has one sub-command per kind of source, and picking the wrong one
produces a CHD that emulators reject, so that decision is made here:

    .cue  .gdi  .toc   -> `createcd`   (these are always CD images)
    .iso               -> `createcd` for a CD-sized image,
                          `createdvd` for a DVD-sized one

An ISO carries no marker saying which kind of disc it came from, so size
is the only signal available: a CD tops out at roughly 700-800 MB, and
anything larger can only have come from a DVD. The in-app dialog lets the
user override that guess; the Explorer menu uses it as-is.

(A bare `.bin` is deliberately NOT accepted: it is one half of a
cue/bin pair and cannot be converted without its `.cue` sheet.)

Reading CHD files (the other direction)
=========================================
chdman is not only a compressor. `extractcd` unpacks a CD CHD back into a
.cue/.bin pair and `extractdvd` unpacks a DVD CHD back into a plain .iso, so
ISOForge can OPEN a CHD by letting chdman unpack it into a temporary image
and browsing that. `info` reports what kind of disc the CHD holds, which is
what picks between the two -- see `read_info()`. The unpacked data is turned
into something ISOForge can browse by `chd_backend`, not here: this module
only drives the tool.

Deliberately Qt-free
=====================
Imported by `main_window` and `settings_store`, but needs nothing from Qt
itself, so the module stays importable and testable without a GUI.
Configuration arrives through `configure()` (called by `settings_store` at
startup), the same split used by `rar_tools` and `sfx_tools`.

Console output is NOT UTF-8
============================
See `rar_tools`' docstring for the full explanation. In short: the only
things read out of chdman's output are the percentage (matched as raw
bytes) and a short on-screen label, so everything is decoded as ASCII with
non-ASCII dropped, instead of guessing an encoding.

Progress parsing
=================
chdman redraws one line in place using carriage returns:

    Compressing, 12.3% complete... (ratio=48.1%)

so it is read a byte at a time (line-buffered reading would show nothing
until the very end). Only the percentage directly followed by "complete" is
used -- the `ratio=` figure on the same line, and the `final ratio` printed
at the end, must never be mistaken for progress.

Cancellation
=============
chdman is a separate process, so an in-process exception cannot stop it.
The progress callback raises `fileops.OperationCancelled` (see
`progress.ProgressReporter`); the output pump catches it, kills the
process, and re-raises. The half-written temp file is always removed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from collections import namedtuple
from typing import Callable, List, Optional, Tuple

from .fileops import OperationCancelled, quiet_remove, replace_with_retry
from .progress import PROGRESS_SCALE, ProgressReporter

ProgressCB = Optional[Callable[[str, int, int], None]]


class ChdToolError(RuntimeError):
    """Raised for any user-facing failure driving chdman."""


# What a successful conversion hands back.
ChdResult = namedtuple(
    "ChdResult", ["source", "output", "kind", "input_size", "output_size"]
)

# --------------------------------------------------------------------- #
# Source types
# --------------------------------------------------------------------- #

# Always CD images; `createdvd` cannot read these.
CD_ONLY_EXTENSIONS: Tuple[str, ...] = (".cue", ".gdi", ".toc")

# Could be either a CD or a DVD image.
ISO_EXTENSION = ".iso"

# Every extension "Convert to CHD" accepts -- Explorer integration, the file
# dialog filter and `is_convertible()` all derive from this one tuple, so
# supporting another type later is a one-line change.
SOURCE_EXTENSIONS: Tuple[str, ...] = CD_ONLY_EXTENSIONS + (ISO_EXTENSION,)

KIND_AUTO = "auto"
KIND_CD = "cd"
KIND_DVD = "dvd"

# An ISO larger than this cannot have come from a CD. A standard 80-minute
# CD holds ~737 MB; the margin covers 90/99-minute media and over-burns.
CD_MAX_ISO_BYTES = 800 * 1000 * 1000

_SUBCOMMAND = {KIND_CD: "createcd", KIND_DVD: "createdvd"}
_KIND_LABEL = {KIND_CD: "CD", KIND_DVD: "DVD"}

# Explicit override supplied via Tools > Settings. `None` means "not
# configured -- fall back to auto-detection".
_configured_chdman: Optional[str] = None

_CHDMAN_NAMES = ("chdman.exe", "chdman")

# Folders commonly holding chdman, checked last so a deliberately-placed
# local copy always wins.
_PROGRAM_DIR_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")
_VENDOR_SUBDIRS = ("MAME",)
_FIXED_DIRS = (r"C:\MAME", r"C:\Tools\MAME")

# First percentage that is directly followed by the word "complete".
_PROGRESS_RE = re.compile(rb"(\d{1,3}(?:\.\d+)?)%\s*complete", re.IGNORECASE)

_TERMINATE_TIMEOUT_SECONDS = 5


# --------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------- #

def configure(chdman_path: Optional[str] = None) -> None:
    """Records an explicit chdman path. Empty/whitespace means 'not
    configured', so clearing the field in Settings cleanly reverts to
    auto-detection rather than pinning an invalid path."""
    global _configured_chdman
    _configured_chdman = (chdman_path or "").strip() or None


def configured_chdman() -> Optional[str]:
    return _configured_chdman


# --------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------- #

def _app_search_dirs() -> List[str]:
    """Folders to check for a user-dropped copy of chdman.exe.

    Mirrors `rar_tools._app_search_dirs` (running from source, or from a
    PyInstaller bundle) and additionally looks in a `tools` subfolder, since
    chdman is often kept tidily beside the app rather than loose in it.
    """
    dirs: List[str] = []
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)
    dirs += [here, root, os.path.join(root, "tools"), os.path.join(root, "chdman")]
    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs += [
            exe_dir,
            os.path.join(exe_dir, "app"),
            os.path.join(exe_dir, "tools"),
            os.path.join(exe_dir, "chdman"),
            os.path.join(exe_dir, "_internal"),
        ]
    except Exception:
        pass
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs += [meipass, os.path.join(meipass, "tools")]

    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def _vendor_dirs() -> List[str]:
    out = []
    for var in _PROGRAM_DIR_VARS:
        base = os.environ.get(var)
        if not base:
            continue
        for sub in _VENDOR_SUBDIRS:
            out.append(os.path.join(base, sub))
    out.extend(_FIXED_DIRS)
    return out


def _find() -> Optional[str]:
    for directory in _app_search_dirs():
        for name in _CHDMAN_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    for name in _CHDMAN_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for directory in _vendor_dirs():
        for name in _CHDMAN_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def resolve_chdman() -> Optional[str]:
    """Path to a usable chdman, or None.

    A configured path wins outright -- but only if it still exists, so a
    tool that has since been moved or uninstalled falls back to
    auto-detection rather than leaving CHD conversion permanently broken.
    """
    if _configured_chdman and os.path.isfile(_configured_chdman):
        return _configured_chdman
    return _find()


def can_convert() -> bool:
    return resolve_chdman() is not None


# --------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------- #

def _no_window_kwargs() -> dict:
    """Stops a console window flashing up on Windows (ISOForge is a
    windowed app, so every chdman launch would otherwise pop a console)."""
    if sys.platform != "win32":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0  # SW_HIDE
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


def _decode_console_output(data: bytes) -> str:
    """ASCII-only decode -- see the module docstring for why."""
    text = (data or b"").decode("ascii", errors="ignore")
    return "".join(ch for ch in text if ch.isprintable() or ch in "\r\n\t")


def probe_tool(path: str) -> Tuple[bool, str]:
    """Validates that `path` is really chdman by running it.

    Run with no arguments it prints a banner naming itself and its version,
    then its usage text, and exits with a non-zero code -- so the EXIT CODE
    is meaningless here and only the banner is trusted.
    Returns (ok, human-readable description).
    """
    if not path:
        return False, "No path set."
    if not os.path.isfile(path):
        return False, "File not found."
    try:
        proc = subprocess.run(
            [path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=15,
            **_no_window_kwargs(),
        )
    except PermissionError:
        return False, "Not executable (permission denied)."
    except OSError as exc:
        return False, f"Could not run this file: {exc}"
    except subprocess.TimeoutExpired:
        return False, "The program did not respond."

    text = _decode_console_output(proc.stdout)
    low = text.lower()
    if "chdman" not in low and "compressed hunks of data" not in low:
        return False, "This doesn't look like chdman."
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            first = line
            break
    return True, first[:110] or "chdman detected."


def describe_chdman() -> Tuple[bool, str]:
    path = resolve_chdman()
    if not path:
        return False, "Not found — CHD conversion is unavailable."
    return probe_tool(path)


def _terminate_process(proc) -> None:
    """Best-effort forceful shutdown of a running chdman, used when
    cancellation is requested mid-conversion."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
    except Exception:
        pass


def _is_progress_line(line: str) -> bool:
    return bool(re.search(r"\d%\s*complete", line, re.IGNORECASE))


def _tail(text: str, lines: int = 8) -> str:
    """Last few meaningful output lines, for error messages. The in-place
    progress redraws are dropped -- they would otherwise flood the tail."""
    rows = [r.strip() for r in re.split(r"[\r\n]+", text or "") if r.strip()]
    rows = [r for r in rows if not _is_progress_line(r)]
    if not rows:
        return "(no output)"
    return "\n".join(rows[-lines:])


# --------------------------------------------------------------------- #
# Source inspection
# --------------------------------------------------------------------- #

def is_convertible(path: str) -> bool:
    return os.path.splitext(path or "")[1].lower() in SOURCE_EXTENSIONS


def source_dialog_filter() -> str:
    """Qt filter string for choosing a source image."""
    patterns = " ".join(f"*{ext}" for ext in SOURCE_EXTENSIONS)
    return f"Disc images ({patterns});;All files (*)"


def default_output_path(source_path: str) -> str:
    """Same folder, same name, `.chd` extension."""
    return os.path.splitext(source_path)[0] + ".chd"


def unique_path(path: str, reserved=()) -> str:
    """Returns `path`, or `path (2)` / `path (3)` ... if it is taken.

    `reserved` lists paths a batch has already promised to other files
    but not yet created -- without it, `game.cue` and `game.iso` in one
    folder would both be told to write `game.chd`.
    """
    taken = {os.path.normcase(os.path.abspath(p)) for p in reserved}

    def free(candidate: str) -> bool:
        return (not os.path.exists(candidate)
                and os.path.normcase(os.path.abspath(candidate)) not in taken)

    if free(path):
        return path
    stem, ext = os.path.splitext(path)
    counter = 2
    while True:
        candidate = f"{stem} ({counter}){ext}"
        if free(candidate):
            return candidate
        counter += 1


def _read_text(path: str) -> str:
    """Reads a small sheet file. Cue/gdi/toc files are plain text in
    whatever encoding the ripping tool used, so UTF-8 is tried first and
    Latin-1 (which cannot fail) is the fallback."""
    with open(path, "rb") as fh:
        raw = fh.read(2 * 1024 * 1024)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


_CUE_TOC_FILE_RE = re.compile(
    r'^\s*(?:DATA)?FILE\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE | re.MULTILINE)
_TOKEN_RE = re.compile(r'"[^"]*"|\S+')


def referenced_files(sheet_path: str) -> List[str]:
    """Absolute paths of the data files a .cue / .gdi / .toc points at.

    Returns an empty list for anything else, or if the sheet can't be
    parsed -- callers treat "nothing found" as "nothing to check", never as
    an error, so an unusual sheet is simply handed to chdman unvalidated.
    """
    ext = os.path.splitext(sheet_path)[1].lower()
    base = os.path.dirname(os.path.abspath(sheet_path))
    names: List[str] = []
    try:
        text = _read_text(sheet_path)
        if ext in (".cue", ".toc"):
            for quoted, bare in _CUE_TOC_FILE_RE.findall(text):
                names.append(quoted or bare)
        elif ext == ".gdi":
            lines = [ln for ln in text.splitlines() if ln.strip()]
            # First line is the track count; each later line is
            #   <track> <lba> <type> <sector size> <file> <offset>
            for line in lines[1:]:
                tokens = _TOKEN_RE.findall(line)
                if len(tokens) >= 5:
                    names.append(tokens[4].strip('"'))
    except OSError:
        return []

    out, seen = [], set()
    for name in names:
        full = os.path.normpath(os.path.join(base, name))
        if full.lower() not in seen:
            seen.add(full.lower())
            out.append(full)
    return out


def source_size(path: str) -> int:
    """Total bytes of the source: the image itself for an .iso, or the
    sum of every data file a .cue/.gdi/.toc references (the sheet alone is
    a few hundred bytes and would make any size comparison meaningless)."""
    try:
        if os.path.splitext(path)[1].lower() in CD_ONLY_EXTENSIONS:
            total = 0
            found = False
            for ref in referenced_files(path):
                if os.path.isfile(ref):
                    total += os.path.getsize(ref)
                    found = True
            if found:
                return total
        return os.path.getsize(path)
    except OSError:
        return 0


def resolve_kind(path: str, preference: str = KIND_AUTO) -> Tuple[str, str]:
    """Decides which chdman sub-command to use.

    Returns (KIND_CD | KIND_DVD, short human-readable reason).

    `preference` can force an ISO either way. It is ignored for
    .cue/.gdi/.toc, which `createdvd` cannot read.
    """
    ext = os.path.splitext(path or "")[1].lower()
    if ext in CD_ONLY_EXTENSIONS:
        return KIND_CD, f"{ext} sheets are always CD images"
    if preference == KIND_CD:
        return KIND_CD, "chosen manually"
    if preference == KIND_DVD:
        return KIND_DVD, "chosen manually"
    try:
        size = os.path.getsize(path)
    except OSError:
        return KIND_CD, "size unknown, assuming a CD"
    mb = size / (1000 * 1000)
    if size > CD_MAX_ISO_BYTES:
        return KIND_DVD, f"{mb:,.0f} MB is too large for a CD, so it is treated as a DVD"
    return KIND_CD, f"{mb:,.0f} MB fits on a CD"


def kind_label(kind: str) -> str:
    return _KIND_LABEL.get(kind, kind)


def validate_source(path: str) -> None:
    """Raises ChdToolError, with a plain-English reason, if `path` can't
    possibly be converted. Cheap checks only -- chdman does the rest."""
    if not path or not os.path.isfile(path):
        raise ChdToolError(f"'{os.path.basename(path or '')}' was not found.")
    if not is_convertible(path):
        accepted = ", ".join(SOURCE_EXTENSIONS)
        raise ChdToolError(
            f"'{os.path.basename(path)}' isn't a type that can be converted "
            f"to CHD.\n\nSupported: {accepted}\n\n"
            "A bare .bin can't be converted on its own — use its .cue file."
        )
    if os.path.splitext(path)[1].lower() in CD_ONLY_EXTENSIONS:
        refs = referenced_files(path)
        missing = [r for r in refs if not os.path.isfile(r)]
        if missing:
            shown = "\n".join(f"• {os.path.basename(m)}" for m in missing[:5])
            more = len(missing) - 5
            extra = f"\n...and {more} more." if more > 0 else ""
            raise ChdToolError(
                f"'{os.path.basename(path)}' refers to file(s) that can't be "
                f"found next to it:\n\n{shown}{extra}\n\n"
                "Keep the sheet and its data files together in one folder."
            )


# --------------------------------------------------------------------- #
# Converting
# --------------------------------------------------------------------- #

def _pump_output(proc, on_percent, cancel_token=None) -> str:
    """Streams chdman's output, reporting progress as it arrives.

    One byte at a time on purpose: chdman redraws its progress meter with
    carriage returns, so line-buffered reading would show nothing until the
    conversion had finished. Total output is tiny, so this costs nothing.
    See the module docstring for the cancellation design.
    """
    collected = bytearray()
    line = bytearray()
    last_emit = 0.0
    last_pct = -1.0
    stream = proc.stdout
    if stream is None:
        return ""

    def emit(chunk: bytes) -> None:
        nonlocal last_emit, last_pct
        match = _PROGRESS_RE.search(chunk)
        if not match:
            return
        try:
            pct = max(0.0, min(100.0, float(match.group(1))))
        except ValueError:
            return
        now = time.monotonic()
        if pct != last_pct or now - last_emit > 0.1:
            last_pct = pct
            last_emit = now
            try:
                on_percent(pct / 100.0)
            except OperationCancelled:
                _terminate_process(proc)
                raise

    while True:
        if cancel_token is not None and cancel_token.cancelled:
            _terminate_process(proc)
            break
        try:
            byte = stream.read(1)
        except Exception:
            break
        if not byte:
            break
        collected.extend(byte)
        if byte in (b"\r", b"\n"):
            chunk = bytes(line)
            line.clear()
            emit(chunk)
        else:
            line.extend(byte)
    if line:
        emit(bytes(line))
    return _decode_console_output(bytes(collected))


def convert_to_chd(
    source_path: str,
    out_path: str,
    preference: str = KIND_AUTO,
    progress_cb: ProgressCB = None,
    cancel_token=None,
) -> ChdResult:
    """Converts `source_path` into a CHD at `out_path`.

    `progress_cb(label, done, total)` receives the usual `(label, 0..1000,
    1000)` -- the same shape every other long operation in ISOForge emits,
    so the shared progress dialog needs no special case.

    Written to a temporary file first and only moved into place once chdman
    has finished successfully, so a failed or cancelled conversion can never
    leave a half-written (and therefore corrupt) CHD where one used to be.
    Any existing file at `out_path` is replaced -- asking whether that is
    acceptable is the caller's job.
    """
    chdman = resolve_chdman()
    if not chdman:
        raise ChdToolError(
            "Converting to CHD requires chdman.exe, which ships with MAME.\n\n"
            "Set its location under Tools > Settings, or place chdman.exe "
            "in the ISOForge folder."
        )
    validate_source(source_path)
    kind, _reason = resolve_kind(source_path, preference)
    name = os.path.basename(source_path)
    input_size = source_size(source_path)

    out_dir = os.path.dirname(os.path.abspath(out_path))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise ChdToolError(f"Could not create the output folder: {exc}") from exc

    tmp_out = os.path.splitext(out_path)[0] + ".tmp_building.chd"
    quiet_remove(tmp_out)

    reporter = ProgressReporter(progress_cb, PROGRESS_SCALE, cancel_token=cancel_token)
    reporter.advance(0, f"Starting conversion of {name}...")

    def on_percent(fraction: float) -> None:
        reporter.set_phase_position(
            0, fraction * PROGRESS_SCALE,
            f"Compressing {name} ({kind_label(kind)}) — {fraction * 100:.1f}%",
        )

    cmd = [chdman, _SUBCOMMAND[kind], "-i", source_path, "-o", tmp_out]

    committed = False
    try:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                **_no_window_kwargs(),
            )
        except OSError as exc:
            raise ChdToolError(f"Could not start chdman: {exc}") from exc

        output = _pump_output(proc, on_percent, cancel_token)

        if cancel_token is not None and cancel_token.cancelled:
            # _pump_output already killed the process before returning.
            raise OperationCancelled()

        code = proc.wait()
        if code != 0:
            raise ChdToolError(
                f"chdman failed (exit code {code}) while converting "
                f"'{name}'.\n\n{_tail(output)}"
            )
        if not os.path.exists(tmp_out):
            raise ChdToolError(
                "chdman reported success but produced no file.\n\n"
                f"{_tail(output)}"
            )

        # Belt and braces, mirroring the backends: a cancel that arrived
        # while the process was finishing is honoured before committing.
        if cancel_token is not None and cancel_token.cancelled:
            raise OperationCancelled()

        replace_with_retry(tmp_out, out_path)
        committed = True
    finally:
        if not committed:
            quiet_remove(tmp_out)

    reporter.finish("Done")
    try:
        output_size = os.path.getsize(out_path)
    except OSError:
        output_size = 0
    return ChdResult(source_path, out_path, kind, input_size, output_size)


# --------------------------------------------------------------------- #
# Reading CHD files
# --------------------------------------------------------------------- #

CHD_EXTENSION = ".chd"

# What a CHD holds, as far as ISOForge is concerned. Only CD and DVD can be
# opened: the others have no ISO 9660 disc to browse.
MEDIA_CD = "cd"
MEDIA_DVD = "dvd"
MEDIA_GDROM = "gdrom"
MEDIA_HARDDISK = "harddisk"
MEDIA_LASERDISC = "laserdisc"
MEDIA_UNKNOWN = "unknown"

# Four-character metadata tags chdman writes, which is how the media type is
# recognised. (The DVD tag really is "DVD " with a trailing space.)
_CD_TAGS = frozenset({"CHT2", "CHTR", "CHCD"})
_GDROM_TAGS = frozenset({"CHGD", "CHGT"})
_DVD_TAGS = frozenset({"DVD"})
_HARDDISK_TAGS = frozenset({"GDDD"})
_LASERDISC_TAGS = frozenset({"AVLD", "AVAV"})

_TAG_RE = re.compile(r"Tag='([^']*)'")
_TRACK_RE = re.compile(r"TRACK:(\d+)\s+TYPE:(\S+)")

ChdInfo = namedtuple("ChdInfo", ["media", "tags", "tracks", "text"])


def parse_info(text: str) -> ChdInfo:
    """Works out the media type from the text `chdman info` printed.

    Separate from `read_info()` so it can be tested without running a tool.
    A GD-ROM is checked before a CD because its tracks are described by
    their own tags, and a disc must never be misread as a plain CD and then
    fail halfway through extraction.
    """
    tags = {t.strip().upper() for t in _TAG_RE.findall(text or "")}
    tracks = [(int(n), kind) for n, kind in _TRACK_RE.findall(text or "")]
    if tags & _GDROM_TAGS:
        media = MEDIA_GDROM
    elif tags & _CD_TAGS:
        media = MEDIA_CD
    elif tags & _DVD_TAGS:
        media = MEDIA_DVD
    elif tags & _HARDDISK_TAGS:
        media = MEDIA_HARDDISK
    elif tags & _LASERDISC_TAGS:
        media = MEDIA_LASERDISC
    else:
        media = MEDIA_UNKNOWN
    return ChdInfo(media, sorted(tags), tracks, text or "")


def _require_chdman() -> str:
    chdman = resolve_chdman()
    if not chdman:
        raise ChdToolError(
            "Opening and saving CHD files requires chdman.exe, which ships "
            "with MAME.\n\n"
            "Set its location under Tools > Settings, or place chdman.exe "
            "in the ISOForge folder."
        )
    return chdman


def read_info(chd_path: str) -> ChdInfo:
    """Asks chdman what a CHD contains (`chdman info`).

    Also the cheapest possible check that the file really is a CHD: a file
    with the wrong contents, or a truncated download, fails here with
    chdman's own explanation rather than part-way through a long extraction.
    """
    chdman = _require_chdman()
    name = os.path.basename(chd_path)
    try:
        proc = subprocess.run(
            [chdman, "info", "-i", chd_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            timeout=60,
            **_no_window_kwargs(),
        )
    except OSError as exc:
        raise ChdToolError(f"Could not start chdman: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ChdToolError(
            f"chdman did not respond while reading '{name}'.") from exc
    text = _decode_console_output(proc.stdout)
    if proc.returncode != 0:
        raise ChdToolError(
            f"chdman could not read '{name}'. It may be damaged, or not a "
            f"CHD file at all.\n\n{_tail(text)}")
    return parse_info(text)


def _run_extraction(cmd, name: str, on_percent, cancel_token) -> None:
    """Runs one extract command to completion, with progress and cancel.

    Same design as `convert_to_chd` (see the module docstring): the output
    is read a byte at a time so the in-place percentage is seen as it
    happens, and cancellation kills the process.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            **_no_window_kwargs(),
        )
    except OSError as exc:
        raise ChdToolError(f"Could not start chdman: {exc}") from exc

    output = _pump_output(proc, on_percent or (lambda _fraction: None),
                          cancel_token)
    if cancel_token is not None and cancel_token.cancelled:
        # _pump_output has already killed the process.
        raise OperationCancelled()
    code = proc.wait()
    if code != 0:
        raise ChdToolError(
            f"chdman failed (exit code {code}) while unpacking '{name}'.\n\n"
            f"{_tail(output)}")


def extract_cd(chd_path: str, cue_path: str, bin_path: str,
               on_percent=None, cancel_token=None) -> None:
    """Unpacks a CD CHD into `cue_path` plus ONE `bin_path` holding every
    track back to back.

    The destination files must not already exist (chdman refuses to
    overwrite without `-f`); callers always unpack into a fresh folder.
    """
    chdman = _require_chdman()
    _run_extraction(
        [chdman, "extractcd", "-i", chd_path, "-o", cue_path, "-ob", bin_path],
        os.path.basename(chd_path), on_percent, cancel_token)
    if not os.path.isfile(cue_path) or not os.path.isfile(bin_path):
        raise ChdToolError(
            "chdman reported success but did not produce the expected files.")


def extract_dvd(chd_path: str, iso_path: str,
                on_percent=None, cancel_token=None) -> None:
    """Unpacks a DVD CHD into a plain .iso at `iso_path`."""
    chdman = _require_chdman()
    _run_extraction(
        [chdman, "extractdvd", "-i", chd_path, "-o", iso_path],
        os.path.basename(chd_path), on_percent, cancel_token)
    if not os.path.isfile(iso_path):
        raise ChdToolError(
            "chdman reported success but did not produce the expected file.")
