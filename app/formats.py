"""
formats.py
----------
Central registry describing every container format ISOForge(ZR) can work
with, and what each one is actually capable of.

Why this module exists
=======================
ISOForge originally only ever handled one container type (.iso), so
"which backend do I call?" was never a question worth asking. Adding ZIP,
7Z, RAR and self-extracting EXE makes it a question asked in a dozen
places -- Open, Save, Save As, Extract, Convert, the file-dialog
filters, the window title, and so on. Rather than scattering
`if path.endswith(".zip")` checks across the UI, every one of those
decisions is answered here.

Capabilities are not all static
================================
ISO / ZIP / 7Z can always be both read and written -- those are handled
by pure-Python libraries that ship with the app.

RAR, SFX and CHD are different, and deliberately DYNAMIC, for the same
underlying reason: each needs a binary ISOForge cannot legally or
practically ship.

  * RAR -- the compression algorithm is proprietary to RARLAB and no
    open-source library may implement a compressor, so ISOForge drives
    RARLAB's own `Rar.exe` when the user supplies it (see `rar_tools`).
  * SFX -- a self-extracting archive is a stub EXECUTABLE with an
    archive concatenated onto it. ISOForge can do the concatenating, but
    cannot compile a Windows PE binary, so it needs a stub module -- most
    easily one of the free ones 7-Zip ships (see `sfx_tools`).
  * CHD -- a compressed disc image, produced and unpacked by MAME's
    `chdman.exe`. ISOForge cannot read or write the format itself, so it
    drives the tool, as for RAR (see `chd_tools` and `chd_backend`). Unlike
    the archive formats a CHD holds a DISC IMAGE, so a project opened from
    one behaves like an ISO project (it has a volume label, for instance):
    see `is_disc_image()`.

So `can_write()` asks those modules at call time rather than answering
from a fixed table:

    tool absent  -> the format is unavailable; Save offers an alternative
    tool present -> fully writable, encryption included

Every part of the UI that asks a capability question here therefore
adapts the instant a tool path is saved -- the Save As filter list, the
Convert dialog's output formats, and the "save as something else?"
prompt all update with no extra wiring.

Encryption support likewise varies:

  * ZIP -- AES-256 on write (via `pyzipper`); reads both AES and the
           older/weaker ZipCrypto.
  * 7Z  -- AES-256 both directions (via `py7zr`).
  * RAR -- decryption always (given a tool); encryption on write once
           Rar.exe is configured, using its `-hp` mode so filenames are
           hidden as well as contents.
  * SFX -- whatever its payload format supports, which in practice means
           AES-256. Filenames stay visible so the stub can prompt for
           the password -- see `archive_backend._write_sfx`.
  * ISO -- no encryption concept at all in the format.
"""
from __future__ import annotations

import os
from typing import Optional

# Canonical format identifiers used throughout the app.
ISO = "iso"
ZIP = "zip"
SEVENZ = "7z"
RAR = "rar"
SFX = "sfx"
CHD = "chd"

ARCHIVE_FORMATS = (ZIP, SEVENZ, RAR, SFX)
ALL_FORMATS = (ISO, CHD) + ARCHIVE_FORMATS

_EXT_TO_FORMAT = {
    ".iso": ISO,
    ".chd": CHD,
    ".zip": ZIP,
    ".7z": SEVENZ,
    ".rar": RAR,
    # A self-extracting archive is an ordinary executable by extension --
    # what makes it an archive is the payload glued onto its tail.
    ".exe": SFX,
    # Common multi-part RAR naming. These still open exactly like a plain
    # .rar; rarfile transparently follows the volume chain when the
    # sibling parts sit alongside the first one.
    ".r00": RAR,
    ".cbz": ZIP,   # comic archives are just renamed zips/rars, and users
    ".cbr": RAR,   # reasonably expect them to open here too
}

_FORMAT_LABELS = {
    ISO: "ISO disc image",
    CHD: "CHD disc image",
    ZIP: "ZIP archive",
    SEVENZ: "7-Zip archive",
    RAR: "RAR archive",
    SFX: "Self-extracting archive",
}

_PRIMARY_EXT = {
    ISO: ".iso",
    CHD: ".chd",
    ZIP: ".zip",
    SEVENZ: ".7z",
    RAR: ".rar",
    SFX: ".exe",
}

# Formats that can ALWAYS be created. RAR and SFX are deliberately absent
# and handled dynamically in `can_write()` -- see the module docstring.
_ALWAYS_WRITABLE = {ISO, ZIP, SEVENZ}

# Formats that support password protection of their contents.
_ENCRYPTABLE_WRITE = {ZIP, SEVENZ, RAR, SFX}
_DECRYPTABLE_READ = {ZIP, SEVENZ, RAR, SFX}


def detect_format(path: str) -> Optional[str]:
    """Returns the canonical format id for `path`, or None if the
    extension isn't one ISOForge handles."""
    ext = os.path.splitext(path or "")[1].lower()
    return _EXT_TO_FORMAT.get(ext)


def is_archive(fmt: Optional[str]) -> bool:
    return fmt in ARCHIVE_FORMATS


def is_iso(fmt: Optional[str]) -> bool:
    return fmt == ISO


def is_disc_image(fmt: Optional[str]) -> bool:
    """True for formats that hold a DISC IMAGE rather than a plain list of
    files: ISO, and CHD (which wraps one). Such projects have a volume label
    and are subject to the ISO path-length advisory; archives are not."""
    return fmt in (ISO, CHD)


def can_write(fmt: Optional[str]) -> bool:
    """Whether ISOForge can currently CREATE this format.

    The tool modules are imported lazily so `formats` stays a
    dependency-free leaf module and can't form an import cycle with
    them.
    """
    if fmt in _ALWAYS_WRITABLE:
        return True
    if fmt == RAR:
        from . import rar_tools
        return rar_tools.can_create_rar()
    if fmt == SFX:
        from . import sfx_tools
        return sfx_tools.can_create_sfx()
    if fmt == CHD:
        from . import chd_tools
        return chd_tools.can_convert()
    return False


def writable_formats() -> tuple:
    """Every format that can be created right now, in menu order.

    ISO/ZIP/7Z lead because they are always available; the
    tool-dependent formats follow so the list stays stable as tools come
    and go rather than reshuffling around them.
    """
    out = [ISO, ZIP, SEVENZ]
    if can_write(RAR):
        out.append(RAR)
    if can_write(SFX):
        out.append(SFX)
    if can_write(CHD):
        out.append(CHD)
    return tuple(out)


def supports_write_encryption(fmt: Optional[str]) -> bool:
    """Whether a password can be applied when creating this format.

    RAR and SFX qualify only when they can be written at all -- there's
    no point offering encryption for a format the app can't currently
    produce.
    """
    if fmt in (RAR, SFX):
        return can_write(fmt)
    return fmt in _ENCRYPTABLE_WRITE


def supports_read_encryption(fmt: Optional[str]) -> bool:
    return fmt in _DECRYPTABLE_READ


def format_label(fmt: Optional[str]) -> str:
    return _FORMAT_LABELS.get(fmt, "Unknown format")


def primary_extension(fmt: Optional[str]) -> str:
    return _PRIMARY_EXT.get(fmt, "")


def suggested_write_format(fmt: Optional[str]) -> str:
    """Given the format a project was OPENED from, returns the format it
    should be SAVED as.

    Identity for anything writable -- including RAR, SFX and CHD once their
    tools are configured, so a file opened and edited saves straight back
    in its own format. Only when that isn't possible does this fall back:
    to ISO for a CHD (the same disc image, just uncompressed), and to 7Z for
    the rest, the closest generally-available equivalent (solid compression
    and AES-256, like RAR, and the payload SFX usually carries anyway).
    """
    if can_write(fmt):
        return fmt
    if fmt == CHD:
        return ISO
    return SEVENZ


def default_name_for(fmt: Optional[str], stem: str = "Untitled") -> str:
    return f"{stem}{primary_extension(fmt) or '.iso'}"


# --------------------------------------------------------------------- #
# File-dialog filter strings
# --------------------------------------------------------------------- #
def open_dialog_filter() -> str:
    """Qt filter for the Open dialog, covering every readable format.

    `.exe` is included because a self-extracting archive really is an
    archive -- ISOForge can open one and show what's inside it.
    """
    return (
        "All supported files (*.iso *.chd *.zip *.7z *.rar *.exe *.cbz *.cbr);;"
        "ISO disc images (*.iso);;"
        "CHD disc images (*.chd);;"
        "ZIP archives (*.zip *.cbz);;"
        "7-Zip archives (*.7z);;"
        "RAR archives (*.rar *.cbr);;"
        "Self-extracting archives (*.exe);;"
        "All files (*)"
    )


def save_dialog_filter() -> str:
    """Qt filter for Save As, listing only formats that can be created
    right now -- so RAR and SFX appear only once their tools are
    available, and the user is never offered an output format the app
    can't produce."""
    parts = [
        f"{format_label(fmt)} (*{primary_extension(fmt)})"
        for fmt in writable_formats()
    ]
    return ";;".join(parts)


def format_from_save_filter(selected_filter: str) -> Optional[str]:
    """Maps the filter string Qt reports back from a Save dialog to a
    format id, so an extension-less filename still gets the right one."""
    low = (selected_filter or "").lower()
    if "*.iso" in low:
        return ISO
    if "*.chd" in low:
        return CHD
    if "*.zip" in low:
        return ZIP
    if "*.7z" in low:
        return SEVENZ
    if "*.rar" in low:
        return RAR
    if "*.exe" in low:
        return SFX
    return None


def ensure_extension(path: str, fmt: Optional[str]) -> str:
    """Appends the format's primary extension if `path` doesn't already
    carry a recognised one for that format."""
    if not path:
        return path
    ext = _PRIMARY_EXT.get(fmt)
    if not ext:
        return path
    if os.path.splitext(path)[1].lower() == ext:
        return path
    return path + ext

