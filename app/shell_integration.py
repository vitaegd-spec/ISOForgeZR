"""
shell_integration.py
--------------------
Registers ISOForge's Windows Explorer right-click menu entries.

What this produces
===================
Two cascading submenus (the same shape WinRAR and 7-Zip use) plus one
plain menu item:

    Right-click a .zip / .7z / .rar / .iso
        ISOForge  >  Open with ISOForge
                     Extract Here
                     Extract to subfolder

    Right-click any file(s) or folder(s)
        ISOForge  >  Compress with ISOForge...

    Right-click a .cue / .gdi / .toc / .iso
        Convert to CHD with ISOForge

The CHD item is a plain verb rather than part of a cascade, because it is
the only thing ISOForge adds for .cue/.gdi/.toc -- a flyout holding a single
entry would just be an extra click. It is attached to the same
`SystemFileAssociations` location as the archive menu (see below) under its
own key, so toggling it never disturbs the archive menu on .iso and vice
versa.

WHERE the entries go -- extensions are not like `*`
====================================================
This is the single most important detail in this module, and getting it
wrong produces a menu that is silently absent for archives while working
perfectly for everything else.

For the pseudo-classes `*` (any file) and `Directory`, Explorer reads
verbs DIRECTLY out of:

    HKCR\\*\\shell\\<verb>
    HKCR\\Directory\\shell\\<verb>

For a real file EXTENSION it does NOT do that. `HKCR\\.zip\\shell` is not
consulted at all. Explorer resolves the association first:

    1. read the (Default) value of HKCR\\.zip  -> a ProgID
       (e.g. "CompressedFolder", "WinRAR.ZIP", "7-Zip.zip")
    2. read verbs from HKCR\\<ThatProgID>\\shell
    3. read verbs from HKCR\\SystemFileAssociations\\.zip\\shell
    4. read verbs from HKCR\\*\\shell

ISOForge <= 2.3.0 wrote archive verbs to `HKCU\\Software\\Classes\\.zip\\
shell\\ISOForgeZR`, which appears in none of those steps -- so the
archive submenu never showed, while "Compress with ISOForge..." (attached
to `*` and `Directory`, both step-1 locations) worked fine. That exact
asymmetry is the signature of this bug.

Step 2 is deliberately NOT used here. The ProgID differs from machine to
machine, changes whenever the user picks a different default handler, and
belongs to whichever vendor owns the file type -- writing inside it would
be both fragile and invasive.

Step 3 is what `SystemFileAssociations` exists for: verbs that apply to
an extension regardless of which application owns it. That is what this
module now uses, and it is why `_verb_path()` builds a different path for
extensions than for `*` / `Directory`.

How the cascade is built -- and why NOT ExtendedSubCommandsKey
===============================================================
Windows offers two ways to hang a flyout off a verb key:

  1. `ExtendedSubCommandsKey` = an HKEY_CLASSES_ROOT-relative path to a
     SEPARATE key holding the items. One shared copy, referenced from
     every file type.
  2. `SubCommands` = "" (an empty REG_SZ) plus a `shell` subkey holding
     the items DIRECTLY beneath the verb key itself. One copy per
     attachment point, no cross-key reference at all.

ISOForge <= 2.2.0 used (1) and it failed in exactly the way that
indirection tends to: the cascade ARROW appeared, because `MUIVerb` plus
a cascade hint is enough for Explorer to draw one, but the flyout came up
EMPTY because the referenced key was never resolved.

Option (2) removes the failure mode rather than working around it: there
is nothing to resolve, because the items are already children of the key
Explorer is looking at. The cost is that the item set is duplicated
across the eight attachment points -- roughly fifty registry values,
which is nothing -- and that duplication is contained entirely within
`_attach_menu()`, so the item definitions still live in exactly one place
(`_ARCHIVE_ITEMS` / `_CREATE_ITEMS`).

Note that `SubCommands` must be written as an EMPTY STRING. It is the
PRESENCE of the value, not its content, that switches the verb into
cascade mode -- which is why `_set_values()` below deliberately writes
empty strings instead of skipping them.

Why pure registry rather than a shell-extension DLL
====================================================
A "real" shell extension is a COM in-process DLL implementing
IExplorerCommand/IContextMenu. That is the only way to get entries into
Windows 11's *primary* context menu, and the only way to receive a
multi-file selection as a single call. But it requires a compiled C++
DLL, registration of a CLSID, and (on Win11) packaging the whole thing
as a signed MSIX with an AppxManifest.

None of that is practical for a Python application, so ISOForge uses the
long-standing registry verb mechanism instead. The trade-offs are
honest and worth stating plainly:

  * On Windows 10 and earlier, entries appear directly in the menu.
  * On Windows 11 they appear under "Show more options" (Shift+F10),
    alongside every other non-MSIX application's entries.
  * Selecting several files invokes the command once PER FILE rather
    than once for the whole selection. ISOForge handles that by
    coalescing the separate launches back into one operation -- see
    `single_instance.py` for how.

Everything is written under HKEY_CURRENT_USER\\Software\\Classes, never
HKEY_LOCAL_MACHINE. That means:

  * no administrator rights are needed, so the toggle in Tools >
    Settings just works;
  * the entries belong to the user who installed them rather than to
    the whole machine;
  * nothing about existing file associations is touched. Adding a verb
    under SystemFileAssociations does NOT change which program owns the
    file type -- double-clicking a .zip still does whatever it did
    before.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Tuple

# Importing winreg on a non-Windows machine raises, and ISOForge should
# still run (and still show a sensible "not supported here" state), so
# the import is soft and every public function guards on it.
try:
    import winreg  # type: ignore
except Exception:  # pragma: no cover - non-Windows
    winreg = None  # type: ignore

# Root under which all of our class data is written.
_CLASSES = r"Software\Classes"

# Explorer's documented home for per-extension verbs that don't belong to
# any particular application -- see the module docstring.
_SFA = "SystemFileAssociations"

# The key name used for our entry inside each shell key.
_VERB_KEY = "ISOForgeZR"

# File types that get the archive submenu. `.iso` is included because
# ISOForge treats it as just another container, and the comic-book
# variants are simply renamed zip/rar files.
ARCHIVE_EXTENSIONS: Tuple[str, ...] = (
    ".zip", ".7z", ".rar", ".iso", ".cbz", ".cbr",
)

# Pseudo-classes for "any file" and "any folder". Unlike extensions,
# these ARE read directly from HKCR\<name>\shell.
_ALL_FILES = "*"
_DIRECTORY = "Directory"

MENU_LABEL = "ISOForge"

# --- CHD conversion -----------------------------------------------------
# Disc-image types that get the "Convert to CHD" verb. Kept in step with
# `chd_tools.SOURCE_EXTENSIONS` by hand rather than imported, so this module
# stays a dependency-free leaf (it is also run from the installer with no
# UI at all).
CHD_EXTENSIONS: Tuple[str, ...] = (".cue", ".gdi", ".toc", ".iso")

# A separate key name from `_VERB_KEY`, so the two can coexist on `.iso`.
_CHD_VERB_KEY = "ISOForgeZR_CHD"
CHD_MENU_LABEL = "Convert to CHD with ISOForge"
_CHD_ARGUMENTS = '--convert-chd "%1"'

# The flyout contents, as (sort_key, label, command arguments).
#
# The sort key becomes part of the registry key NAME because Explorer
# orders flyout items alphabetically by key name rather than by creation
# order -- so the numeric prefixes are what actually control the order
# the user sees.
_ARCHIVE_ITEMS: Tuple[Tuple[str, str, str], ...] = (
    ("10open", "Open with ISOForge", '--shell-open "%1"'),
    ("20here", "Extract Here", '--extract-here "%1"'),
    ("30into", "Extract to subfolder", '--extract-to "%1"'),
)
_CREATE_ITEMS: Tuple[Tuple[str, str, str], ...] = (
    ("10add", "Compress with ISOForge...", '--compress "%1"'),
)

# Shared-submenu containers written by ISOForge <= 2.2.0, which used
# ExtendedSubCommandsKey. Nothing creates these any more; they are only
# listed so register()/unregister() can clear them from an older install.
_LEGACY_MENU_KEYS: Tuple[str, ...] = (
    "ISOForgeZR.Menu.Archive",
    "ISOForgeZR.Menu.Create",
)


class ShellIntegrationError(RuntimeError):
    """Raised when registry entries can't be written or removed."""


# --------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------- #
def is_supported() -> bool:
    """Whether Explorer integration can be configured on this machine."""
    return sys.platform == "win32" and winreg is not None


def _frozen() -> bool:
    """True when running from a PyInstaller build rather than source."""
    return bool(getattr(sys, "frozen", False))


def launch_command() -> str:
    """The quoted command prefix Explorer should run.

    Packaged builds point straight at the .exe. Running from source needs
    the interpreter plus the script, and deliberately prefers
    `pythonw.exe` so no console window flashes up each time a menu entry
    is used.
    """
    if _frozen():
        return f'"{os.path.abspath(sys.executable)}"'

    interpreter = os.path.abspath(sys.executable)
    pythonw = os.path.join(os.path.dirname(interpreter), "pythonw.exe")
    if os.path.isfile(pythonw):
        interpreter = pythonw

    # main.py lives one level above this package.
    script = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "main.py")
    )
    return f'"{interpreter}" "{script}"'


def _icon_source() -> str:
    """Icon reference for the menu entries.

    A packaged build carries its own icon as resource 0. From source
    there is no .exe to point at, so the generated .ico is used if it's
    present and the icon is simply omitted otherwise -- a missing icon
    degrades gracefully, an invalid one shows a broken placeholder.
    """
    if _frozen():
        return f"{os.path.abspath(sys.executable)},0"
    ico = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     os.pardir, "build", "isoforge.ico")
    )
    return ico if os.path.isfile(ico) else ""


# --------------------------------------------------------------------- #
# Registry paths
# --------------------------------------------------------------------- #
def _verb_path(class_name: str) -> str:
    """Where ISOForge's cascade lives for one class.

    Extensions are routed through `SystemFileAssociations`, because
    Explorer never reads verbs from a bare `HKCR\\.ext\\shell` -- see the
    module docstring. `*` and `Directory` are read directly and so are
    used as-is.
    """
    if class_name.startswith("."):
        return f"{_CLASSES}\\{_SFA}\\{class_name}\\shell\\{_VERB_KEY}"
    return f"{_CLASSES}\\{class_name}\\shell\\{_VERB_KEY}"


def _chd_verb_path(extension: str) -> str:
    """Where the Convert-to-CHD verb lives for one extension. Always under
    `SystemFileAssociations`, for the reason given in the module docstring."""
    return f"{_CLASSES}\\{_SFA}\\{extension}\\shell\\{_CHD_VERB_KEY}"


def _legacy_ext_verb_path(extension: str) -> str:
    """The location ISOForge 2.3.0 wrongly used for extensions.

    Explorer ignored these entirely, so they are dead weight rather than
    a live menu -- but they still need removing so the registry doesn't
    accumulate keys that do nothing.
    """
    return f"{_CLASSES}\\{extension}\\shell\\{_VERB_KEY}"


def _all_attachment_classes() -> Tuple[str, ...]:
    return ARCHIVE_EXTENSIONS + (_ALL_FILES, _DIRECTORY)


# --------------------------------------------------------------------- #
# Registry helpers
# --------------------------------------------------------------------- #
def _require() -> None:
    if not is_supported():
        raise ShellIntegrationError(
            "Explorer integration is only available on Windows."
        )


def _set_values(path: str, values: dict) -> None:
    """Writes REG_SZ values, creating the key if needed.

    EMPTY STRINGS ARE WRITTEN, not skipped -- `SubCommands` must exist as
    an empty value for Explorer to treat the verb as a cascade, and an
    earlier version of this helper silently dropped it. Only `None` is
    treated as "omit this value", which is what optional entries like
    `Icon` use.
    """
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_WRITE) as key:
        for name, value in values.items():
            if value is None:
                continue
            # A name of "" writes the key's (Default) value.
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _delete_tree(path: str) -> None:
    """Recursively removes a key, tolerating one that isn't there.

    `winreg.DeleteKey` refuses to remove a key that still has children,
    so subkeys are enumerated and deleted depth-first. Enumeration is
    done by repeatedly reading index 0 rather than walking upwards,
    because deleting while iterating shifts every later index.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0,
                            winreg.KEY_READ | winreg.KEY_WRITE) as key:
            while True:
                try:
                    child = winreg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(f"{path}\\{child}")
    except FileNotFoundError:
        return
    except OSError:
        return

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        pass
    except OSError as exc:  # pragma: no cover - permissions etc.
        raise ShellIntegrationError(
            f"Could not remove registry key '{path}': {exc}") from exc


def _key_exists(path: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ):
            return True
    except OSError:
        return False


def _read_value(path: str, name: str = "") -> Optional[str]:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return str(value)
    except OSError:
        return None


# --------------------------------------------------------------------- #
# Menu construction
# --------------------------------------------------------------------- #
def _attach_menu(class_name: str, items: Tuple[Tuple[str, str, str], ...]) -> None:
    """Writes the whole cascade -- header AND items -- for one class.

    The key is deleted first so nothing from a previous layout can
    survive. That matters specifically because ISOForge <= 2.2.0 wrote an
    `ExtendedSubCommandsKey` value here; leaving that behind alongside the
    new `SubCommands` would give Explorer two contradictory instructions
    about where the flyout contents are.
    """
    path = _verb_path(class_name)
    _delete_tree(path)

    header = {
        "MUIVerb": MENU_LABEL,
        # Empty on purpose -- presence is the cascade switch. See
        # _set_values' docstring.
        "SubCommands": "",
    }
    icon = _icon_source()
    if icon:
        header["Icon"] = icon
    _set_values(path, header)

    command = launch_command()
    for sort_key, label, arguments in items:
        item = f"{path}\\shell\\{sort_key}"
        _set_values(item, {"MUIVerb": label})
        _set_values(f"{item}\\command", {"": f"{command} {arguments}"})


def _detach_menu(class_name: str) -> None:
    _delete_tree(_verb_path(class_name))


def _attach_chd(extension: str) -> None:
    """Writes the single Convert-to-CHD verb for one extension.

    A plain verb, not a cascade: just a label, an icon and a command. The
    key is deleted first so a stale command from a moved install can't
    survive alongside the new one.
    """
    path = _chd_verb_path(extension)
    _delete_tree(path)
    values = {"MUIVerb": CHD_MENU_LABEL}
    icon = _icon_source()
    if icon:
        values["Icon"] = icon
    _set_values(path, values)
    _set_values(f"{path}\\command", {"": f"{launch_command()} {_CHD_ARGUMENTS}"})


def _detach_chd(extension: str) -> None:
    _delete_tree(_chd_verb_path(extension))


def _clear_legacy() -> None:
    """Removes every layout ISOForge used before the current one.

    Two generations need clearing:
      * <= 2.2.0 -- shared `ISOForgeZR.Menu.*` submenu containers.
      * 2.3.0    -- per-extension verbs written to `HKCU\\Software\\
                    Classes\\.ext\\shell`, a location Explorer never
                    reads.
    """
    for name in _LEGACY_MENU_KEYS:
        _delete_tree(f"{_CLASSES}\\{name}")
    for ext in ARCHIVE_EXTENSIONS:
        _delete_tree(_legacy_ext_verb_path(ext))


def _notify_explorer() -> None:
    """Asks Explorer to re-read association data.

    Without this the new entries typically appear anyway, but only after
    a delay or an Explorer restart -- which reads as "the toggle didn't
    work". Failure is ignored: it's a cosmetic refresh, never a reason
    to report the registration itself as failed.
    """
    try:
        import ctypes
        SHCNE_ASSOCCHANGED = 0x08000000
        SHCNF_IDLIST = 0x0000
        ctypes.windll.shell32.SHChangeNotify(
            SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)
    except Exception:
        pass


# --------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------- #
def register(archives: bool = True, create: bool = True,
             chd: Optional[bool] = None) -> None:
    """Installs the requested menus, removing any that were turned off.

    `archives` and `create` are applied every time so the function can be
    used as a straight "make the registry match these checkboxes" call,
    rather than the caller having to work out what changed.

    `chd` is tri-state: True installs the Convert-to-CHD verbs, False
    removes them, and None (the default) leaves them exactly as they are.
    None matters because the installer's silent `--register-shell` calls
    this with only the two original flags; treating "unspecified" as "off"
    would quietly delete the CHD menu the user had enabled every time the
    app was upgraded.
    """
    _require()

    if archives:
        for ext in ARCHIVE_EXTENSIONS:
            _attach_menu(ext, _ARCHIVE_ITEMS)
    else:
        for ext in ARCHIVE_EXTENSIONS:
            _detach_menu(ext)

    if create:
        _attach_menu(_ALL_FILES, _CREATE_ITEMS)
        _attach_menu(_DIRECTORY, _CREATE_ITEMS)
    else:
        _detach_menu(_ALL_FILES)
        _detach_menu(_DIRECTORY)

    if chd is True:
        for ext in CHD_EXTENSIONS:
            _attach_chd(ext)
    elif chd is False:
        for ext in CHD_EXTENSIONS:
            _detach_chd(ext)

    # Nothing references the older layouts any more; clear them out of
    # installations registered by an earlier build.
    _clear_legacy()

    _notify_explorer()


def unregister() -> None:
    """Removes every entry ISOForge has ever created.

    Deliberately unconditional -- it clears every attachment point and
    both legacy layouts regardless of what is currently installed, so it
    doubles as a repair for a partially-written state.
    """
    _require()
    for cls in _all_attachment_classes():
        _detach_menu(cls)
    for ext in CHD_EXTENSIONS:
        _detach_chd(ext)
    _clear_legacy()
    _notify_explorer()


def archives_registered() -> bool:
    """True when at least one archive type carries a populated cascade.

    Checks for the `shell` subkey rather than just the verb key, so a
    half-written entry (header present, items missing -- the exact state
    that produced an empty flyout) is correctly reported as NOT
    registered.
    """
    if not is_supported():
        return False
    return any(_key_exists(f"{_verb_path(ext)}\\shell")
               for ext in ARCHIVE_EXTENSIONS)


def create_registered() -> bool:
    if not is_supported():
        return False
    return any(_key_exists(f"{_verb_path(cls)}\\shell")
               for cls in (_ALL_FILES, _DIRECTORY))


def chd_registered() -> bool:
    """True when at least one disc-image type carries the CHD verb.

    Checks for the `command` subkey rather than just the verb key, for the
    same reason as `archives_registered`: a half-written entry must not
    count as registered.
    """
    if not is_supported():
        return False
    return any(_key_exists(f"{_chd_verb_path(ext)}\\command")
               for ext in CHD_EXTENSIONS)


def registered_chd_command() -> Optional[str]:
    """The Convert-to-CHD command currently registered, or None."""
    if not is_supported():
        return None
    for ext in CHD_EXTENSIONS:
        value = _read_value(f"{_chd_verb_path(ext)}\\command", "")
        if value:
            return value
    return None


def registered_command() -> Optional[str]:
    """The command currently registered, or None.

    Used to detect a STALE registration -- one pointing at an executable
    that has since moved, been reinstalled elsewhere, or (commonly during
    development) a source checkout that later became a packaged build.
    """
    if not is_supported():
        return None
    for ext in ARCHIVE_EXTENSIONS:
        value = _read_value(f"{_verb_path(ext)}\\shell\\10open\\command", "")
        if value:
            return value
    for cls in (_ALL_FILES, _DIRECTORY):
        value = _read_value(f"{_verb_path(cls)}\\shell\\10add\\command", "")
        if value:
            return value
    return None


def _uses_legacy_layout() -> bool:
    """True when entries were written by a build using an older layout.

    Covers both known generations:
      * an `ExtendedSubCommandsKey` value or a shared `ISOForgeZR.Menu.*`
        container (<= 2.2.0) -- produced an arrow with an empty flyout;
      * a per-extension verb under `Classes\\.ext\\shell` (2.3.0) --
        produced no archive menu at all.

    Either way the registration needs re-applying, even if the command
    path it points at is perfectly correct.
    """
    if not is_supported():
        return False

    for cls in _all_attachment_classes():
        if _read_value(_verb_path(cls), "ExtendedSubCommandsKey") is not None:
            return True
    if any(_key_exists(f"{_CLASSES}\\{name}") for name in _LEGACY_MENU_KEYS):
        return True
    return any(_key_exists(_legacy_ext_verb_path(ext))
               for ext in ARCHIVE_EXTENSIONS)


def needs_refresh() -> bool:
    """True when entries exist but need re-applying."""
    if not is_supported():
        return False
    if _uses_legacy_layout():
        return True
    prefix = launch_command()
    for current in (registered_command(), registered_chd_command()):
        if current and not current.startswith(prefix):
            return True
    return False


def status_summary() -> str:
    """One-line description for the Settings dialog."""
    if not is_supported():
        return "Explorer integration is only available on Windows."

    if _uses_legacy_layout():
        return ("Added by an older version — re-apply to restore the "
                "archive menu.")

    archives = archives_registered()
    create = create_registered()
    chd = chd_registered()
    if not archives and not create and not chd:
        return "Not currently added to the Explorer menu."
    if needs_refresh():
        return ("Added, but pointing at a different copy of ISOForge — "
                "re-apply to update it.")

    parts = []
    if archives:
        parts.append("archives")
    if create:
        parts.append("files and folders")
    if chd:
        parts.append("disc images (Convert to CHD)")
    return "Added to the Explorer menu for " + " and ".join(parts) + "."

