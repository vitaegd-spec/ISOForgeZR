"""
sfx_tools.py
------------
Locates the stub executable used to build self-extracting archives.

What a self-extracting archive actually is
===========================================
There is no "SFX file format". A self-extracting archive is simply:

    <stub executable>  +  <ordinary archive bytes>

concatenated into one file and named `.exe`. Windows runs the stub, and
the stub locates the archive glued onto its own tail and unpacks it.

That works because archive formats locate their index from the END of
the file, not the start: a ZIP reader scans backwards for the End Of
Central Directory record and then treats every stored offset as relative
to wherever the archive actually begins. Prepending arbitrary bytes
therefore leaves the archive perfectly readable -- which is exactly why
7-Zip and WinRAR build their SFX files this way, and why a
self-extracting ZIP can still be opened by any normal ZIP tool.

Why the stub has to be supplied rather than generated
======================================================
The stub is a real Windows PE executable. ISOForge is a Python
application: it can concatenate bytes all day, but it cannot compile a
PE binary, and shipping a prebuilt one would mean redistributing
somebody else's compiled code.

So ISOForge takes the same approach it already takes for RAR (see
`rar_tools`): the user supplies the tool, ISOForge detects and drives
it. The practical difference is that suitable stubs are much easier to
come by than Rar.exe -- **7-Zip is free and ships them**, and if 7-Zip
is installed ISOForge will normally find one with no configuration at
all.

Which stub implies which payload
=================================
A stub only understands ONE archive format -- the one it was compiled to
unpack. 7-Zip's modules expect a 7z payload; WinRAR's `Zip.SFX` expects
a ZIP payload; its `Default.SFX` expects RAR. Feeding a stub the wrong
kind produces a file that launches and then fails to extract, which is a
miserable way to discover the mistake.

`payload_kind()` therefore infers the required payload from the stub's
filename and `archive_backend` builds that format accordingly. The user
picks "self-extracting archive"; ISOForge works out the rest.

Deliberately Qt-free
=====================
Imported by `archive_backend`, which must stay importable without a GUI,
so configuration arrives via `configure()` from `settings_store` rather
than being read from QSettings here -- the same split used by
`rar_tools`.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

# Payload formats a stub can require. These deliberately match the
# canonical ids in `formats.py` so callers can pass them straight
# through without translation.
PAYLOAD_7Z = "7z"
PAYLOAD_ZIP = "zip"
PAYLOAD_RAR = "rar"


class SfxToolError(RuntimeError):
    """Raised for any user-facing failure involving the SFX stub."""


# Explicit override supplied via Tools > Settings. `None` means "not
# configured -- fall back to auto-detection".
_configured_stub: Optional[str] = None

# Known stub filenames, mapped to the payload each one unpacks.
#
# 7-Zip ships several variants differing only in UI: `7z.sfx` shows a
# GUI extraction dialog, `7zCon.sfx` is console-based, and the `7zS*`
# family are the smaller installer-style modules. All consume a 7z
# payload, so ISOForge treats them identically and simply prefers the
# GUI one, which is what a user double-clicking an .exe expects.
_STUB_PAYLOADS = {
    # 7-Zip (free, LGPL) -- by far the most likely to be present.
    "7z.sfx": PAYLOAD_7Z,
    "7zcon.sfx": PAYLOAD_7Z,
    "7zs.sfx": PAYLOAD_7Z,
    "7zsd.sfx": PAYLOAD_7Z,
    "7zs2.sfx": PAYLOAD_7Z,
    "7zs2con.sfx": PAYLOAD_7Z,
    "7zsd_all.sfx": PAYLOAD_7Z,
    # WinRAR.
    "zip.sfx": PAYLOAD_ZIP,
    "zip64.sfx": PAYLOAD_ZIP,
    "default.sfx": PAYLOAD_RAR,
    "wincon.sfx": PAYLOAD_RAR,
}

# Search order when no path is configured. GUI 7-Zip module first: it
# gives the friendliest experience for a double-clicked .exe.
_PREFERRED_NAMES: Tuple[str, ...] = (
    "7z.sfx", "7zS2.sfx", "7zSD.sfx", "7zS.sfx", "7zCon.sfx",
    "Zip.SFX", "Zip64.SFX", "Default.SFX", "WinCon.SFX",
)

# Vendor install folders checked last, after the app folder, so a
# deliberately-placed local copy always wins.
_VENDOR_SUBDIRS = ("7-Zip", "WinRAR")
_PROGRAM_DIR_VARS = ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")

# Minimum bytes needed to read the DOS header and reach e_lfanew.
_DOS_HEADER_BYTES = 0x40


# --------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------- #
def configure(stub_path: Optional[str] = None) -> None:
    """Records an explicit stub path. Empty/whitespace is treated as
    'not configured' so clearing the field in Settings cleanly reverts
    to auto-detection rather than pinning an unusable path."""
    global _configured_stub
    _configured_stub = (stub_path or "").strip() or None


def configured_stub() -> Optional[str]:
    return _configured_stub


# --------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------- #
def _app_search_dirs() -> List[str]:
    """Folders to check for a user-dropped stub.

    Mirrors `rar_tools._app_search_dirs`: covers running from source
    (`app/` plus the project root) and from a PyInstaller bundle (the
    folder holding the .exe plus its `_internal`/`app` subfolders).
    """
    dirs: List[str] = []

    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(here)                    # .../ISOForge/app
    dirs.append(os.path.dirname(here))   # .../ISOForge
    dirs.append(os.path.join(os.path.dirname(here), "sfx"))

    try:
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        dirs.append(exe_dir)
        dirs.append(os.path.join(exe_dir, "app"))
        dirs.append(os.path.join(exe_dir, "sfx"))
        dirs.append(os.path.join(exe_dir, "_internal"))
    except Exception:
        pass

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(meipass)
        dirs.append(os.path.join(meipass, "sfx"))

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
    return out


def resolve_stub() -> Optional[str]:
    """Path to a usable SFX stub, or None.

    A configured path wins outright -- but only if it still exists, so a
    tool that has since been moved or uninstalled falls back to
    auto-detection rather than leaving SFX permanently broken.
    """
    if _configured_stub and os.path.isfile(_configured_stub):
        return _configured_stub

    for directory in _app_search_dirs() + _vendor_dirs():
        for name in _PREFERRED_NAMES:
            candidate = os.path.join(directory, name)
            if os.path.isfile(candidate):
                return candidate
    return None


def can_create_sfx() -> bool:
    return resolve_stub() is not None


# --------------------------------------------------------------------- #
# Stub inspection
# --------------------------------------------------------------------- #
def payload_kind(stub_path: Optional[str]) -> str:
    """Which archive format this stub expects glued onto its tail.

    Inferred from the filename, because there is no reliable marker
    inside the binary itself. Anything unrecognised is assumed to be a
    7z module: that is what the overwhelming majority of stubs in
    circulation are, and it means a renamed or third-party 7-Zip module
    still works rather than being rejected outright.
    """
    if not stub_path:
        return PAYLOAD_7Z
    return _STUB_PAYLOADS.get(os.path.basename(stub_path).lower(), PAYLOAD_7Z)


def payload_kind_for_current() -> str:
    return payload_kind(resolve_stub())


def probe_stub(path: str) -> Tuple[bool, str]:
    """Validates that `path` is a real Windows executable.

    Checks the two structural markers every PE file carries: the `MZ`
    DOS signature at offset 0, and the `PE\\0\\0` signature at the offset
    stored in `e_lfanew`. That catches the realistic mistakes -- picking
    a `.dll`-less text file, a shortcut, or the wrong file entirely --
    without pretending to be a full PE validator.

    Deliberately does NOT execute the file, unlike `rar_tools.probe_tool`
    which runs Rar.exe to read its version banner. Running a stub would
    do nothing useful (it has no archive attached yet) and running an
    unverified binary just to inspect it would be poor behaviour.
    """
    if not path:
        return False, "No path set."
    if not os.path.isfile(path):
        return False, "File not found."

    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            dos = fh.read(_DOS_HEADER_BYTES)
            if len(dos) < _DOS_HEADER_BYTES or dos[:2] != b"MZ":
                return False, "Not a Windows program (missing MZ header)."
            pe_offset = int.from_bytes(dos[0x3C:0x40], "little")
            if pe_offset <= 0 or pe_offset >= size:
                return False, "Not a Windows program (bad PE offset)."
            fh.seek(pe_offset)
            if fh.read(4) != b"PE\x00\x00":
                return False, "Not a Windows program (missing PE signature)."
    except OSError as exc:
        return False, f"Could not read this file: {exc}"

    kind = payload_kind(path)
    label = {
        PAYLOAD_7Z: "7z payload",
        PAYLOAD_ZIP: "ZIP payload",
        PAYLOAD_RAR: "RAR payload",
    }[kind]
    return True, f"Windows program, {size / 1024:.0f} KB — expects a {label}."


def describe_stub() -> Tuple[bool, str]:
    path = resolve_stub()
    if not path:
        return False, "Not found — self-extracting archives are unavailable."
    return probe_stub(path)


def read_stub_bytes(stub_path: Optional[str] = None) -> bytes:
    """Loads the stub for concatenation.

    Stubs are small (tens to a few hundred KB), so reading the whole
    thing is simpler than streaming and costs nothing measurable.
    """
    path = stub_path or resolve_stub()
    if not path:
        raise SfxToolError(
            "Creating a self-extracting archive requires an SFX stub "
            "module.\n\n"
            "Install 7-Zip (free) and ISOForge will find one "
            "automatically, or set its location under Tools > Settings."
        )
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise SfxToolError(f"Could not read the SFX stub: {exc}") from exc

    if not data.startswith(b"MZ"):
        raise SfxToolError(
            f"'{os.path.basename(path)}' is not a Windows program, so it "
            "cannot be used as a self-extracting stub.\n\n"
            "Choose a module such as 7-Zip's 7z.sfx under Tools > Settings."
        )
    return data
