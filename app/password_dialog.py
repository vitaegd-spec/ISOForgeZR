"""
password_dialog.py
------------------
The two password prompts ISOForge(ZR) needs for encrypted archives:

  * `PasswordPromptDialog` -- "this archive is locked, what's the
    password?" Shown when opening or extracting an encrypted archive.
  * `EncryptionPromptDialog` -- "do you want to protect this archive?"
    Offered when SAVING any format that supports it.

Both follow ISOForge's existing dialog styling (SectionTitle / Muted
object names picked up by `theme.py`) so they match the rest of the app
in either light or dark mode.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QVBoxLayout,
)

from .dialog_base import ForgeDialog


class PasswordPromptDialog(ForgeDialog):
    """Asks for the password needed to READ an encrypted archive.

    `retry_message` lets the caller re-show this dialog after a rejected
    attempt with an explanation in place, instead of silently presenting
    an identical-looking empty box that gives the user no indication
    anything was wrong.
    """

    def __init__(self, parent, archive_name: str, retry_message: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Password Required")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("Password Required")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        sub = QLabel(
            f"<b>{archive_name}</b> is password protected. Enter its "
            "password to open and extract its contents."
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        sub.setTextFormat(Qt.RichText)
        layout.addWidget(sub)

        if retry_message:
            warn = QLabel(retry_message)
            warn.setWordWrap(True)
            # Inline styling rather than a theme rule: this label only
            # ever appears in this one transient error state, so a
            # dedicated stylesheet entry would be dead weight everywhere
            # else in the app.
            warn.setStyleSheet("color: #E5566D; font-weight: 600;")
            layout.addWidget(warn)

        form = QFormLayout()
        form.setSpacing(10)
        self.pw_edit = QLineEdit()
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.pw_edit.setPlaceholderText("Password")
        form.addRow("Password:", self.pw_edit)
        layout.addLayout(form)

        self.show_check = QCheckBox("Show password")
        self.show_check.toggled.connect(self._toggle_echo)
        layout.addWidget(self.show_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.pw_edit.returnPressed.connect(self.accept)
        self.pw_edit.setFocus()

    def _toggle_echo(self, shown: bool) -> None:
        self.pw_edit.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password)

    def password(self) -> str:
        return self.pw_edit.text()


class EncryptionPromptDialog(ForgeDialog):
    """Offers optional password protection when SAVING an archive.

    Encryption is off by default and the dialog is skipped entirely for
    formats that don't support it, so this never gets in the way of an
    ordinary save.
    """

    def __init__(self, parent, format_label: str,
                 initial_password: Optional[str] = None):
        super().__init__(parent)
        self.setWindowTitle("Protect Archive")
        self.setMinimumWidth(460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        title = QLabel("Protect Archive")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        sub = QLabel(
            f"Optionally protect this {format_label} with a password. "
            f"{self._mechanism_text(format_label)}"
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        self.enable_check = QCheckBox("Password protect this archive")
        self.enable_check.setChecked(bool(initial_password))
        self.enable_check.toggled.connect(self._sync_enabled)
        layout.addWidget(self.enable_check)

        form = QFormLayout()
        form.setSpacing(10)
        self.pw_edit = QLineEdit(initial_password or "")
        self.pw_edit.setEchoMode(QLineEdit.Password)
        self.confirm_edit = QLineEdit(initial_password or "")
        self.confirm_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Password:", self.pw_edit)
        form.addRow("Confirm:", self.confirm_edit)
        layout.addLayout(form)

        self.show_check = QCheckBox("Show password")
        self.show_check.toggled.connect(self._toggle_echo)
        layout.addWidget(self.show_check)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #E5566D; font-weight: 600;")
        self.error_label.hide()
        layout.addWidget(self.error_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync_enabled(self.enable_check.isChecked())

    @staticmethod
    def _mechanism_text(format_label: str) -> str:
        """Describes what protection actually means for this format.

        Stating it per-format rather than generically avoids
        over-claiming: ZIP and 7Z are encrypted by ISOForge itself and
        hide filenames as well as contents, RAR is encrypted by Rar.exe,
        and a self-extracting archive deliberately leaves its filenames
        readable -- see `archive_backend._write_sfx` for why that is
        necessary rather than an oversight.
        """
        label = format_label or ""
        if "Self-extracting" in label:
            return ("Contents are encrypted with AES-256. Filenames stay "
                    "visible so the program can ask for the password when "
                    "it runs.")
        if "RAR" in label:
            return "Contents and filenames are encrypted by Rar.exe."
        return "Contents are encrypted with AES-256."

    def _sync_enabled(self, enabled: bool) -> None:
        for w in (self.pw_edit, self.confirm_edit, self.show_check):
            w.setEnabled(enabled)
        if enabled:
            self.pw_edit.setFocus()

    def _toggle_echo(self, shown: bool) -> None:
        mode = QLineEdit.Normal if shown else QLineEdit.Password
        self.pw_edit.setEchoMode(mode)
        self.confirm_edit.setEchoMode(mode)

    def _on_accept(self) -> None:
        if not self.enable_check.isChecked():
            self.accept()
            return

        pw = self.pw_edit.text()
        if not pw:
            self._show_error("Please enter a password, or untick the box above.")
            return
        if pw != self.confirm_edit.text():
            self._show_error("The two passwords don't match.")
            return
        self.accept()

    def _show_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.show()

    def password(self) -> Optional[str]:
        """Returns the chosen password, or None if the user declined
        encryption."""
        if not self.enable_check.isChecked():
            return None
        return self.pw_edit.text() or None

