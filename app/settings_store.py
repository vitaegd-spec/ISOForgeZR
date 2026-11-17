"""
settings_store.py
-----------------
Persistence for the external-tool paths configured in Tools > Settings.

Why this is separate from `rar_tools` / `sfx_tools`
====================================================
Those modules are deliberately Qt-free so that `archive_backend` (which
imports them) stays importable and testable without a GUI. This module
is the Qt-aware other half: it owns the QSettings keys, and pushes the
saved values into them at startup via `apply_to_runtime()`.

That one-way flow (settings -> runtime) keeps the dependency pointing in
a single direction and means the backends never have to know that a GUI,
a settings store, or a user preference exists at all.

Explorer integration is deliberately NOT stored here: its state lives in
the Windows registry itself (see `shell_integration`), which is the one
place that can't drift out of sync with what Explorer actually shows.
"""
from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QSettings

from . import rar_tools
from . import sfx_tools

# QSettings keys. Grouped under "tools/" so they're visually distinct
# from window geometry and view preferences, and so the allow-list in
# main_window can permit them explicitly.
KEY_RAR = "tools/rarPath"
KEY_UNRAR = "tools/unrarPath"
KEY_SFX = "tools/sfxStub"

SETTINGS_KEYS = (KEY_RAR, KEY_UNRAR, KEY_SFX)


def _settings(org: str, app: str) -> QSettings:
    return QSettings(org, app)


def load(org: str, app: str) -> Tuple[str, str, str]:
    """Returns the saved (rar_path, unrar_path, sfx_stub); empty strings
    where a path has never been set."""
    s = _settings(org, app)
    rar = s.value(KEY_RAR, "") or ""
    unrar = s.value(KEY_UNRAR, "") or ""
    sfx = s.value(KEY_SFX, "") or ""
    return str(rar), str(unrar), str(sfx)


def save(org: str, app: str,
         rar_path: Optional[str], unrar_path: Optional[str],
         sfx_stub: Optional[str] = None) -> None:
    """Persists the paths AND applies them immediately, so a newly-saved
    tool takes effect without restarting -- which matters because the
    Save As / Convert menus change the moment RAR or SFX becomes
    writable."""
    s = _settings(org, app)
    s.setValue(KEY_RAR, (rar_path or "").strip())
    s.setValue(KEY_UNRAR, (unrar_path or "").strip())
    s.setValue(KEY_SFX, (sfx_stub or "").strip())
    rar_tools.configure(rar_path, unrar_path)
    sfx_tools.configure(sfx_stub)


def apply_to_runtime(org: str, app: str) -> None:
    """Pushes saved paths into the tool modules. Call once during
    startup, before the main window is constructed, so format
    capabilities are already correct when the UI first builds its
    menus."""
    rar, unrar, sfx = load(org, app)
    rar_tools.configure(rar, unrar)
    sfx_tools.configure(sfx)
