"""
rar_tools.py
------------
Locates and drives RARLAB's `Rar.exe` / `UnRAR.exe` command-line tools.

Why this module exists
=======================
The RAR *compression* algorithm is proprietary to RARLAB. No open-source
library is licensed to implement a compressor, which is why ISOForge
originally treated RAR as read-only: it cannot legally BUNDLE RARLAB's
tools.

That restriction is about REDISTRIBUTION, not capability. If the user
supplies their own licensed copies of `Rar.exe` (creates + extracts) and
`UnRAR.exe` (extracts only), ISOForge can drive them as external
processes and gain full RAR read AND write support. That is exactly what
Tools > Settings configures, and what this module implements.

So RAR capability is now DYNAMIC rather than fixed:

    no tools configured   -> RAR is read-only via the `rarfile` library
                             (which itself still needs an unrar binary)
    UnRAR.exe configured  -> reading is reliable and fast
    Rar.exe configured    -> reading AND writing, including encryption

`formats.can_write()` consults `can_create_rar()` below, so the whole UI
(Save As filters, the Convert dialog's format list, the "save as ZIP/7Z
instead?" prompt) adapts automatically the moment a valid tool path is
saved. Nothing else needs to know how RAR support is provided.

Deliberately Qt-free
=====================
This module is imported by `archive_backend`, which is deliberately free
of any Qt dependency. Configuration therefore arrives through
`configure()` -- called once at startup by `settings_store` (which IS
Qt-aware) -- rather than being read from QSettings here. That keeps the
backend importable and testable without a GUI.

Auto-detection
===============
When no explicit path is configured, `resolve_rar()` / `resolve_unrar()`
search, in order:

    1. next to the running application (including `app/`, which is where
       a user most naturally drops the two .exe files),
    2. anywhere on PATH,
    3. the standard WinRAR install locations.

That means dropping `Rar.exe` and `UnRAR.exe` into the ISOForge folder is
enough to enable RAR writing with no configuration at all -- Settings is
there for when they live somewhere else.

Console output is NOT UTF-8 -- read this before touching output parsing
=========================================================================
Rar.exe writes its progress meter for a native Windows console using
that console's OEM code page (commonly CP437 or CP866 depending on
locale), not UTF-8 -- and the meter itself redraws using extended/
box-drawing byte values purely for cosmetic effect. Decoding those bytes
as UTF-8, even with `errors="replace"`, does NOT reliably fall back to a
visible replacement character: sequences of high-bit-set bytes can
coincidentally form perfectly VALID multi-byte UTF-8 sequences that
decode into unrelated, unrecognisable characters from a completely
different Unicode block (this is what produced a real reported bug --
readable-looking but nonsensical characters appearing in the progress
label, not an obvious mojibake placeholder).

There is no single encoding that can be assumed correct across every
RAR build, Windows locale and console code page, and the only
information genuinely needed out of this output is a short, human
-readable label and the percentage (already extracted separately by
`_last_percent` via a byte-level regex that never needs decoding at
all). `_decode_console_output()` therefore decodes as ASCII and DROPS
anything outside it, rather than guessing an encoding. A filename made
of ASCII characters (the overwhelming common case) still displays
correctly; one containing other characters loses them from this
cosmetic progress label only -- the archive itself, and the file names
actually stored inside it, are entirely unaffected, since this label is
never used for anything but on-screen display.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

from .fileops import OperationCancelled

ProgressCB = Optional[Callable[[str, int, int], None]]


class RarToolError(RuntimeError):
    """Raised for any user-facing failure driving Rar.exe / UnRAR.exe."""


# Explicit overrides supplied via Tools > Settings. `None` means "not
# configured -- fall back to auto-detection".
_configured_rar: Optional[str] = None
_configured_unrar: Optional[str] = None

# Candidate executable names, in preference order, per role. Casing
# varies between RARLAB's Windows builds ("Rar.exe", "UnRAR.exe") and
# Unix builds ("rar", "unrar"); Windows filesystems are case-insensitive
# so listing both costs nothing and helps on Linux/macOS.
_RAR_NAMES = ("Rar.exe", "rar.exe", "rar")
_UNRAR_NAMES = ("UnRAR.exe", "unrar.exe", "unrar")

# Where WinRAR normally installs. Checked last, after the app folder and
# PATH, so a deliberately-placed local copy always wins.
_WINRAR_DIR_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

# Matches the running percentage RAR prints while compressing. Matched
# directly against the raw bytes, since it never needs decoding at all --
# see the module docstring for why the rest of the line does.
_PERCENT_RE = re.compile(rb"(\d{1,3})%")

# How long to wait for a killed Rar.exe process to actually exit before
# giving up on it. Generous, since Windows process teardown can lag, but
# bounded so a truly stuck process can never hang cancellation forever.
_TERMINATE_TIMEOUT_SECONDS = 5


# --------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------- #
def configure(rar_path: Optional[str] = None,
              unrar_path: Optional[str] = None) -> None:
    """Records explicit tool paths. Empty/whitespace values are treated
    as 'not configured' so clearing a field in Settings cleanly reverts
    to auto-detection rather than pinning an invalid path."""
    global _configured_rar, _configured_unrar
    _configured_rar = (rar_path or "").strip() or None
    _configured_unrar = (unrar_path or "").strip() or None
    _sync_rarfile_tool()


def configured_rar() -> Optional[str]:
    return _configured_rar


def configured_unrar() -> Optional[str]:
    return _configured_unrar


# --------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------- #
def _app_search_dirs() -> List[str]:
    """Folders to check for a user-dropped copy of the tools.

    Covers both running from source (`app/` alongside this file, plus the
    project root) and running as a PyInstaller bundle (the folder holding
    the .exe, plus its `_internal`/`app` subfolders).
    """
    dirs: List[str] = []

    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(here)                                   # .../ISOForge/app
    dirs.append(os.path.dirname(here))                  # .../ISOForge

    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs.append(exe_dir)
        dirs.append(os.path.join(exe_dir, "app"))
        dirs.append(os.path.join(exe_dir, "_internal"))
    except Exception:
        pass

    # PyInstaller one-file builds unpack to a temp dir exposed as _MEIPASS.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
        dirs.append(os.path.join(meipass, "app"))

    seen, out = set(), []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key not in seen and os.path.isdir(d):
            seen.add(key)
            out.append(d)
    return out


def _winrar_dirs() -> List[str]:
    out = []
    for var in _WINRAR_DIR_VARS:
        base = os.environ.get(var)
        if base:
            out.append(os.path.join(base, "WinRAR"))
    return out


def _find(names: Tuple[str, ...]) -> Optional[str]:
    for directory in _app_search_dirs():
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate

    for name in names:
        found = shutil.which(name)
        if found:
            return found

    for directory in _winrar_dirs():
        for name in names:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate

    return None


def resolve_rar() -> Optional[str]:
    """Path to a tool that can CREATE RAR archives, or None."""
    if _configured_rar and os.path.isfile(_configured_rar):
        return _configured_rar
    return _find(_RAR_NAMES)


def resolve_unrar() -> Optional[str]:
    """Path to a tool that can EXTRACT RAR archives, or None.

    Falls back to `Rar.exe`, which extracts perfectly well too -- so a
    user who supplies only Rar.exe still gets both directions.
    """
    if _configured_unrar and os.path.isfile(_configured_unrar):
        return _configured_unrar
    found = _find(_UNRAR_NAMES)
    if found:
        return found
    return resolve_rar()


def can_create_rar() -> bool:
    return resolve_rar() is not None


def can_extract_rar() -> bool:
    return resolve_unrar() is not None


# --------------------------------------------------------------------- #
# rarfile integration
# --------------------------------------------------------------------- #
def _sync_rarfile_tool() -> None:
    """Points the `rarfile` library at our resolved UnRAR binary.

    RAR *reading* already works through `rarfile`, which shells out to an
    external unrar for the actual decompression and by default only looks
    on PATH. Telling it about a user-supplied copy means the existing
    read path (and therefore the whole VFS/password/progress pipeline)
    benefits from the Settings entry without being rewritten.

    Failures are swallowed deliberately: rarfile is an optional
    dependency, and a missing/odd version must never prevent the app from
    starting or break ZIP/7Z/ISO support.
    """
    tool = resolve_unrar()
    if not tool:
        return
    try:
        import rarfile
    except Exception:
        return
    try:
        rarfile.UNRAR_TOOL = tool
        # Newer rarfile caches which backend it settled on; force it to
        # re-evaluate now that the tool path has changed.
        setup = getattr(rarfile, "tool_setup", None)
        if callable(setup):
            try:
                setup(force=True)
            except TypeError:
                setup()
    except Exception:
        pass


# --------------------------------------------------------------------- #
# Process helpers
# --------------------------------------------------------------------- #
def _no_window_kwargs() -> dict:
    """Stops a console window flashing up on Windows.

    ISOForge is a windowed (console-less) app, so without this every
    Rar.exe invocation would briefly pop a black console -- visually
    jarring during a long save that runs the tool once.
    """
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
    """Safely converts RAR's raw console output bytes into displayable
    text. See the module docstring ("Console output is NOT UTF-8") for
    why this deliberately decodes as ASCII and drops everything else
    rather than guessing an encoding."""
    text = (data or b"").decode("ascii", errors="ignore")
    return "".join(ch for ch in text if ch.isprintable() or ch in "\r\n\t")


def probe_tool(path: str) -> Tuple[bool, str]:
    """Validates that `path` is really a working RAR/UnRAR binary.

    Run with no arguments, both tools print a banner containing their
    name and version, then exit with a non-zero "no files specified"
    style code -- so the EXIT CODE is meaningless here and only the
    banner text is trusted. Returns (ok, human-readable description).
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
    first = ""
    for line in text.splitlines():
        line = line.strip()
        if line:
            first = line
            break

    low = text.lower()
    if "rar" not in low:
        return False, "This doesn't look like a RAR tool."
    return True, first or "RAR tool detected."


def describe_rar() -> Tuple[bool, str]:
    path = resolve_rar()
    if not path:
        return False, "Not found — RAR creation is unavailable."
    ok, desc = probe_tool(path)
    return ok, desc


def describe_unrar() -> Tuple[bool, str]:
    path = resolve_unrar()
    if not path:
        return False, "Not found — RAR extraction is unavailable."
    ok, desc = probe_tool(path)
    return ok, desc


def _terminate_process(proc) -> None:
    """Best-effort, forceful shutdown of a running Rar.exe, used when
    cancellation is requested mid-compression.

    `kill()` rather than `terminate()`: Rar.exe is not known to install
    any graceful-shutdown signal handling, and a build that's being
    cancelled should stop as promptly as possible rather than waiting on
    a signal it may simply ignore. The temp staging directory (and
    whatever partial archive Rar.exe was writing to) is cleaned up by
    the caller regardless of how the process actually ended.
    """
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=_TERMINATE_TIMEOUT_SECONDS)
    except Exception:
        pass


# --------------------------------------------------------------------- #
# Creating archives
# --------------------------------------------------------------------- #
def create_rar(
    staging_dir: str,
    out_path: str,
    password: Optional[str] = None,
    compression: int = 3,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    cancel_token=None,
) -> None:
    """Compresses everything inside `staging_dir` into `out_path`.

    The caller stages the project's virtual tree into a real directory
    first (see `archive_backend._write_rar`). That indirection is
    unavoidable: Rar.exe works on real files, while the VFS allows
    renames and restructuring that don't correspond to anything on disk.

    `progress_cb(fraction, label)` receives 0.0-1.0 progress parsed from
    RAR's own output.

    `cancel_token`, if supplied, is checked as RAR's output streams in
    (see `_pump_output`); if cancellation is requested mid-compression,
    the running Rar.exe process is killed and `fileops.OperationCancelled`
    is raised. Because Rar.exe only reports progress through its console
    output, cancellation can only be noticed between output lines --
    in the worst case (a single very large file compressing silently for
    a while) there can be a short delay before the process is actually
    killed, which is an inherent limitation of driving an external tool
    rather than something ISOForge can fully control.

    Note on passwords: `-hp` encrypts file DATA and the archive HEADER
    (so filenames are hidden too, matching what ISOForge does for 7Z).
    The password is necessarily passed as a command-line argument, which
    on most systems is briefly visible to other processes on the same
    machine via the process list -- an inherent limitation of driving an
    external tool, and the same approach other GUI frontends take.
    """
    rar = resolve_rar()
    if not rar:
        raise RarToolError(
            "Creating RAR archives requires RARLAB's Rar.exe.\n\n"
            "Set its location under Tools > Settings, or place Rar.exe "
            "in the ISOForge folder.\n\n"
            "Rar.exe is part of WinRAR and cannot be bundled with "
            "ISOForge for licensing reasons."
        )

    level = max(0, min(5, int(compression)))
    cmd = [
        rar,
        "a",            # add to archive
        "-r",           # recurse into subfolders
        "-ep1",         # store names relative to the staging folder
        "-y",           # assume Yes to any prompt (never block on stdin)
        "-idc",         # suppress the copyright banner (keeps % output)
        f"-m{level}",   # compression level, 0 (store) .. 5 (best)
    ]
    if password:
        cmd.append(f"-hp{password}")
    cmd.append(out_path)
    cmd.append("*")     # RAR expands this itself; cwd anchors it

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=staging_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            bufsize=0,
            **_no_window_kwargs(),
        )
    except OSError as exc:
        raise RarToolError(f"Could not start Rar.exe: {exc}") from exc

    output = _pump_output(proc, progress_cb, cancel_token)

    if cancel_token is not None and cancel_token.cancelled:
        # _pump_output already killed the process before returning here
        # (either via the top-of-loop check or from within the
        # progress_cb exception handler) -- this just turns that into
        # the exception build_archive()'s caller is watching for.
        raise OperationCancelled()

    code = proc.wait()

    # RAR exit codes: 0 = success, 1 = warning (non-fatal, e.g. a file
    # was locked and skipped). Anything higher is a real failure.
    if code not in (0, 1):
        raise RarToolError(
            f"Rar.exe failed (exit code {code}).\n\n{_tail(output)}"
        )
    if not os.path.exists(out_path):
        raise RarToolError(
            "Rar.exe reported success but produced no archive.\n\n"
            f"{_tail(output)}"
        )


def _pump_output(proc, progress_cb, cancel_token=None) -> str:
    """Streams the tool's output, reporting progress as it arrives.

    Read one byte at a time on purpose. RAR draws its progress meter with
    carriage returns rather than newlines, so line-buffered reading would
    show nothing until the whole operation finished -- reintroducing
    exactly the "frozen progress bar" problem the byte-accurate progress
    work set out to remove. The total volume of output is tiny (a few KB
    of progress redraws), so per-byte reads cost nothing measurable.

    Cancellation is checked at the top of every iteration AND after every
    progress callback: the callback (`progress_cb`, ultimately
    `ProgressReporter.set_phase_position`) raises
    `fileops.OperationCancelled` itself once the token is set, and
    catching that here -- rather than only relying on the top-of-loop
    check -- kills the process the instant a new progress line arrives,
    rather than waiting for the next raw byte read to notice the flag on
    its own.
    """
    collected = bytearray()
    line = bytearray()
    last_emit = 0.0
    last_pct = -1.0

    stream = proc.stdout
    if stream is None:
        return ""

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
            if progress_cb:
                pct = _last_percent(chunk)
                now = time.monotonic()
                # Throttle: RAR can redraw its meter very rapidly, and
                # every callback here crosses a thread boundary.
                if pct is not None and (pct != last_pct or now - last_emit > 0.1):
                    last_pct = pct
                    last_emit = now
                    try:
                        progress_cb(pct / 100.0, _clean_label(chunk))
                    except OperationCancelled:
                        _terminate_process(proc)
                        raise
        else:
            line.extend(byte)

    if line and progress_cb:
        pct = _last_percent(bytes(line))
        if pct is not None:
            try:
                progress_cb(pct / 100.0, _clean_label(bytes(line)))
            except OperationCancelled:
                _terminate_process(proc)
                raise

    return _decode_console_output(bytes(collected))


def _last_percent(chunk: bytes) -> Optional[float]:
    matches = _PERCENT_RE.findall(chunk or b"")
    if not matches:
        return None
    try:
        return max(0.0, min(100.0, float(matches[-1])))
    except ValueError:
        return None


def _clean_label(chunk: bytes) -> str:
    """Extracts a short, safely-displayable fragment of one line of
    Rar.exe's console output (with the percentage already stripped by
    the caller). See the module docstring ("Console output is NOT
    UTF-8") for why this decodes as ASCII and drops anything else,
    rather than attempting to guess an encoding.
    """
    stripped = _PERCENT_RE.sub(b"", chunk)
    text = _decode_console_output(stripped).strip()
    text = re.sub(r"\s{2,}", " ", text)
    return text[:120]


def _tail(text: str, lines: int = 8) -> str:
    """Last few meaningful output lines, for error messages."""
    rows = [r.strip() for r in re.split(r"[\r\n]+", text or "") if r.strip()]
    if not rows:
        return "(no output)"
    return "\n".join(rows[-lines:])


# --------------------------------------------------------------------- #
# Extracting archives
# --------------------------------------------------------------------- #
def extract_rar(archive_path: str, dest_dir: str,
                password: Optional[str] = None) -> None:
    """Extracts a whole RAR into `dest_dir` using the configured tool.

    Used as a fallback when the `rarfile` library isn't installed. The
    normal reading path still goes through `rarfile` (see
    `_sync_rarfile_tool`), because that integrates with the VFS,
    per-member streaming and password verification.
    """
    tool = resolve_unrar()
    if not tool:
        raise RarToolError(
            "Extracting RAR archives requires UnRAR.exe (or Rar.exe).\n\n"
            "Set its location under Tools > Settings, or place it in the "
            "ISOForge folder."
        )

    os.makedirs(dest_dir, exist_ok=True)
    cmd = [tool, "x", "-y", "-idc"]
    # `-p-` explicitly means "no password"; without it an encrypted
    # archive would block waiting for console input that never comes.
    cmd.append(f"-p{password}" if password else "-p-")
    cmd.append(archive_path)
    cmd.append(dest_dir + os.sep)

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **_no_window_kwargs(),
        )
    except OSError as exc:
        raise RarToolError(f"Could not start {os.path.basename(tool)}: {exc}") from exc

    if proc.returncode not in (0, 1):
        text = _decode_console_output(proc.stdout)
        if proc.returncode == 11 or "password" in text.lower():
            raise RarToolError("The password appears to be incorrect.")
        raise RarToolError(
            f"Extraction failed (exit code {proc.returncode}).\n\n{_tail(text)}"
        )


# Resolve once at import so a tool sitting beside the app is picked up
# even if Settings has never been opened.
_sync_rarfile_tool()
