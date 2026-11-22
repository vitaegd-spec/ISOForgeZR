"""
dialog_base.py
--------------
`ForgeDialog` -- the common base class for ISOForge(ZR)'s pop-up dialogs.

Why this exists
================
Three dialogs (Settings, Convert Folder and Convert to CHD) were reported
with a title-bar [X] that did nothing when clicked, while every other dialog
closed normally.

Inside Qt there is nothing wrong with those three: the close request is
handled identically for all of them (a QCloseEvent reaches QDialog, which
rejects and hides the dialog). The window flags are identical too. So the
failure sits BELOW Qt, in how Windows hands the click to the window -- which
is also why it differs from dialog to dialog on one machine, and why it
cannot be reproduced away from a real Windows desktop.

Rather than guess at one cause, this base class makes closing from the title
bar independent of every layer that could be swallowing it:

  1. EXPLICIT WINDOW FLAGS. The dialog states outright that it has a title
     bar, a system menu and a close button, instead of inheriting whatever
     the platform default happens to be.

  2. THE CLOSE COMMAND IS FORCE-ENABLED. On Windows the [X] is the "Close"
     entry of the window's system menu. If that entry is greyed, the button
     is dead. It is re-enabled every time the dialog is shown.

  3. THE CLOSE MESSAGE IS HANDLED DIRECTLY. `nativeEvent` catches the
     Windows close commands (WM_SYSCOMMAND/SC_CLOSE and WM_CLOSE) before Qt's
     own event routing sees them -- including its "this window is blocked by
     another modal window, ignore the close" rule -- and turns them into an
     ordinary `close()`. That still runs `closeEvent()`, so subclasses that
     intercept closing (see `ProgressDialog`) keep working.

  4. `closeEvent` ACCEPTS. It rejects the dialog and always accepts the
     event, so a dialog can never be left on screen having "refused" to
     close.

All of the Windows-specific code is guarded and silent on failure: on any
other platform, or if a call is unavailable, the dialog simply behaves like
a normal QDialog.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QDialog

# Kept as a module-level flag (rather than testing sys.platform inline) so
# the message-handling logic can be exercised off Windows.
_IS_WINDOWS = sys.platform == "win32"

# Win32 message ids / commands used below.
_WM_CLOSE = 0x0010
_WM_SYSCOMMAND = 0x0112
_SC_CLOSE = 0xF060
# The low four bits of a WM_SYSCOMMAND wParam are used internally by Windows
# and must be masked off before comparing.
_SYSCOMMAND_MASK = 0xFFF0
_MF_BYCOMMAND_ENABLED = 0x00000000  # MF_BYCOMMAND | MF_ENABLED

_NATIVE_EVENT_TYPES = (b"windows_generic_MSG", b"windows_dispatcher_MSG")


class ForgeDialog(QDialog):
    """A QDialog whose title-bar [X] is guaranteed to close it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        # Spelled out in full. No Help ("?") button, and no minimise /
        # maximise: dialogs here are never meant to have them.
        self.setWindowFlags(
            Qt.Dialog
            | Qt.CustomizeWindowHint
            | Qt.WindowTitleHint
            | Qt.WindowSystemMenuHint
            | Qt.WindowCloseButtonHint
        )

    # ------------------------------------------------------------------ #
    # Closing
    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:
        """Rejects and always accepts, so the dialog cannot stay open."""
        event.accept()
        if self.isVisible():
            self.reject()

    # ------------------------------------------------------------------ #
    # Windows specifics
    # ------------------------------------------------------------------ #
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._enable_close_command()

    def _enable_close_command(self) -> None:
        """Makes sure the system menu's Close entry -- which IS the [X] --
        is enabled. A no-op away from Windows."""
        if not _IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetSystemMenu.argtypes = [wintypes.HWND, wintypes.BOOL]
            user32.GetSystemMenu.restype = wintypes.HMENU
            user32.EnableMenuItem.argtypes = [
                wintypes.HMENU, wintypes.UINT, wintypes.UINT]
            user32.EnableMenuItem.restype = wintypes.BOOL
            user32.DrawMenuBar.argtypes = [wintypes.HWND]

            hwnd = int(self.winId())
            menu = user32.GetSystemMenu(hwnd, False)
            if menu:
                user32.EnableMenuItem(menu, _SC_CLOSE, _MF_BYCOMMAND_ENABLED)
                user32.DrawMenuBar(hwnd)
        except Exception:
            pass

    def nativeEvent(self, event_type, message):
        """Turns Windows' close commands into a plain `close()`.

        `close()` is queued with a zero-length timer instead of being called
        from inside the message handler: the dialog may be running its own
        modal loop at this moment, and tearing it down re-entrantly from
        within a window procedure is exactly the kind of thing to avoid.
        """
        if _IS_WINDOWS and self._is_close_message(event_type, message):
            QTimer.singleShot(0, self.close)
            return True, 0
        # "Not handled": Qt carries on with its normal processing. (This is
        # exactly what QWidget.nativeEvent itself returns, and avoids a
        # super() call that PySide6 can reject for some argument types.)
        return False, 0

    @staticmethod
    def _is_close_message(event_type, message) -> bool:
        try:
            if bytes(event_type) not in _NATIVE_EVENT_TYPES:
                return False
            from ctypes import wintypes

            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_CLOSE:
                return True
            return (msg.message == _WM_SYSCOMMAND
                    and (msg.wParam & _SYSCOMMAND_MASK) == _SC_CLOSE)
        except Exception:
            return False
