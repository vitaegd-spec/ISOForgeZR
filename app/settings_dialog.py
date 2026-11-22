"""
settings_dialog.py
------------------
The Tools > Settings dialog.

Four sections:

  * **External Tools** -- locating RARLAB's `Rar.exe` / `UnRAR.exe`,
    which is what unlocks full RAR support (see `rar_tools`' docstring
    for why those tools can't simply be bundled).
  * **Self-extracting archives** -- locating the stub module used to
    build `.exe` archives (see `sfx_tools`).
  * **CHD conversion** -- locating MAME's `chdman.exe`, which is what
    "Convert to CHD" drives (see `chd_tools`).
  * **Windows Explorer** -- the right-click menu integration (see
    `shell_integration`), including the Convert-to-CHD item.

Design notes
=============
Every path field carries a LIVE status line that re-validates as you
type or browse. Validating eagerly matters here: pointing at the wrong
file is easy, and the alternative -- discovering it only when a save
fails minutes later -- is exactly the kind of late failure the rest of
ISOForge works hard to avoid.

The two validations differ in kind, deliberately. A RAR tool is checked
by RUNNING it and reading its version banner, because that is the only
way to be sure it works. An SFX stub is checked by INSPECTING its
headers instead: running it would achieve nothing (it has no archive
attached yet), and executing an unverified binary merely to describe it
would be poor behaviour.

The Explorer section is hidden entirely on non-Windows platforms rather
than shown disabled, since there is nothing there a Linux or macOS user
could ever act on.

Scrolling
==========
Four full sections -- each with its own heading, description, input
row(s) and status/hint text -- comfortably exceed a laptop's available
screen height once fit-to-content sizing is applied, and Qt does not
shrink a dialog's content to fit the screen on its own. Without a scroll
container the OK/Cancel buttons at the bottom simply become unreachable
on any display shorter than the fully-expanded content, which is exactly
what the fixed-height QVBoxLayout that used to hold everything directly
produced.

The fix is the standard one: the four sections go inside a QScrollArea,
and only the button row sits outside it in the dialog's own layout. That
keeps OK/Cancel permanently visible and reachable regardless of screen
height, while the sections above scroll as needed. `setWidgetResizable`
is required so the inner container widget tracks the scroll area's width
rather than keeping its own fixed size -- without it, word-wrapped labels
and line edits would not resize with the dialog at all.
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout,
    QWidget,
)

from . import chd_tools
from . import rar_tools
from . import sfx_tools
from . import shell_integration
from .dialog_base import ForgeDialog

_OK_COLOR = "#39B37E"
_BAD_COLOR = "#E5566D"
_IDLE_COLOR = "#8A93A6"


class SettingsDialog(ForgeDialog):
    """Point ISOForge at external helper tools and configure Explorer."""

    def __init__(self, parent, rar_path: str = "", unrar_path: str = "",
                 sfx_path: str = "", chdman_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(660)
        # A generous default height so the common case shows everything
        # at once without scrolling, while still leaving headroom on a
        # smaller screen -- resize() sets the STARTING size, not a cap,
        # so the window remains freely resizable (and remembers a
        # larger size if the user drags it) while the scroll area below
        # guarantees the buttons stay reachable however small it gets.
        self.resize(660, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # -- scrollable content -------------------------------------- #
        # All four sections go here, NOT directly in the dialog's own
        # layout -- see the module docstring for why that's what keeps
        # OK/Cancel reachable on a short screen.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # QScrollArea's viewport paints its OWN opaque background using
        # the native OS palette, entirely independent of this dialog's
        # stylesheet -- our theme only targets `QDialog { background }`,
        # which that viewport never inherits. Left alone, this produces
        # exactly the bug reported: the viewport shows whatever the
        # Windows theme's native window colour is, while the explicitly
        # object-named `QFrame#Card` boxes sitting on top of it still
        # pick up ISOForge's real (possibly dark) card colour -- dark
        # cards on a light, un-themed backdrop.
        #
        # Both a stylesheet rule AND a direct setAutoFillBackground(False)
        # call are used together, since some native Qt styles have been
        # known to keep auto-filling a widget's background from its
        # QPalette even when a stylesheet says "transparent" -- the
        # explicit call is what guarantees the dialog's own themed
        # background paints through underneath, regardless of platform.
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setStyleSheet("background: transparent;")

        content = QWidget()
        content.setAutoFillBackground(False)
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(22, 20, 22, 18)
        content_layout.setSpacing(14)

        self._build_tools_section(content_layout, rar_path, unrar_path)
        self._build_sfx_section(content_layout, sfx_path)
        self._build_chd_section(content_layout, chdman_path)
        self._build_shell_section(content_layout)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # -- fixed footer, always visible ----------------------------- #
        footer = QWidget()
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(22, 12, 22, 18)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        footer_layout.addWidget(buttons)
        outer.addWidget(footer, 0)

        self.rar_edit.textChanged.connect(self._refresh_status)
        self.unrar_edit.textChanged.connect(self._refresh_status)
        self.sfx_edit.textChanged.connect(self._refresh_status)
        self.chd_edit.textChanged.connect(self._refresh_status)
        self._refresh_status()

    # ------------------------------------------------------- tools
    def _build_tools_section(self, layout, rar_path: str, unrar_path: str) -> None:
        title = QLabel("RAR Tools")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "ISOForge can create and extract RAR archives by using RARLAB's "
            "own command-line tools. These can't be bundled with ISOForge "
            "for licensing reasons, so point to your own copies below "
            "(they ship with WinRAR)."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)

        self.rar_edit, rar_row = self._path_row(self._browse_rar)
        self.rar_edit.setText(rar_path or "")
        form.addRow("Rar.exe:", rar_row)
        self.rar_status = self._status_label()
        form.addRow("", self.rar_status)

        self.unrar_edit, unrar_row = self._path_row(self._browse_unrar)
        self.unrar_edit.setText(unrar_path or "")
        form.addRow("UnRAR.exe:", unrar_row)
        self.unrar_status = self._status_label()
        form.addRow("", self.unrar_status)

        card_layout.addLayout(form)

        note = QLabel(
            "<b>Rar.exe</b> enables creating RAR archives (and can extract "
            "them too). <b>UnRAR.exe</b> only extracts, and is optional if "
            "Rar.exe is set.<br>"
            "Leave a field empty to search automatically — ISOForge looks "
            "beside the application, on your PATH, and in the standard "
            "WinRAR folder."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        card_layout.addWidget(note)

        detect_row = QHBoxLayout()
        detect_btn = QPushButton("Detect Automatically")
        detect_btn.setProperty("flat", True)
        detect_btn.clicked.connect(self._detect)
        detect_row.addWidget(detect_btn)
        detect_row.addStretch(1)
        card_layout.addLayout(detect_row)

        layout.addWidget(card)

    # --------------------------------------------------------- sfx
    def _build_sfx_section(self, layout, sfx_path: str) -> None:
        title = QLabel("Self-Extracting Archives")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "A self-extracting archive is a small program with your files "
            "attached to it, so the recipient can unpack them by "
            "double-clicking — no archiver needed. ISOForge supplies the "
            "files; it needs a copy of the stub program to attach them to."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        self.sfx_edit, sfx_row = self._path_row(self._browse_sfx)
        self.sfx_edit.setText(sfx_path or "")
        form.addRow("SFX module:", sfx_row)
        self.sfx_status = self._status_label()
        form.addRow("", self.sfx_status)
        card_layout.addLayout(form)

        note = QLabel(
            "The easiest source is <b>7-Zip</b>, which is free and ships "
            "suitable modules (<code>7z.sfx</code>). If 7-Zip is installed, "
            "ISOForge normally finds one with no configuration at all.<br>"
            "The module decides the archive format inside the .exe, and "
            "ISOForge matches it automatically."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        card_layout.addWidget(note)

        layout.addWidget(card)

    # --------------------------------------------------------- chd
    def _build_chd_section(self, layout, chdman_path: str) -> None:
        title = QLabel("CHD Conversion")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "CHD is the compressed, lossless disc-image format used by MAME "
            "and most emulators. ISOForge converts .cue, .gdi, .toc and .iso "
            "images to CHD by driving MAME's own chdman.exe, so point to "
            "your copy below."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(10)
        self.chd_edit, chd_row = self._path_row(self._browse_chdman)
        self.chd_edit.setText(chdman_path or "")
        form.addRow("chdman.exe:", chd_row)
        self.chd_status = self._status_label()
        form.addRow("", self.chd_status)
        card_layout.addLayout(form)

        note = QLabel(
            "<b>chdman.exe</b> ships with MAME (free). Leave the field empty "
            "to search automatically — ISOForge looks beside the "
            "application (or in a <code>tools</code> folder there), on your "
            "PATH, and in a <code>MAME</code> folder under Program Files."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setTextFormat(Qt.RichText)
        card_layout.addWidget(note)

        detect_row = QHBoxLayout()
        detect_btn = QPushButton("Detect Automatically")
        detect_btn.setProperty("flat", True)
        detect_btn.clicked.connect(self._detect_chdman)
        detect_row.addWidget(detect_btn)
        detect_row.addStretch(1)
        card_layout.addLayout(detect_row)

        layout.addWidget(card)

    # ------------------------------------------------------- shell
    def _build_shell_section(self, layout) -> None:
        """Explorer integration controls.

        `self._shell_supported` gates everything: on a non-Windows build
        the section isn't created at all, and `shell_values()` reports
        None so the caller knows to skip applying it.
        """
        self._shell_supported = shell_integration.is_supported()
        if not self._shell_supported:
            self.shell_archive_check = None
            self.shell_create_check = None
            self.shell_chd_check = None
            self.shell_status = None
            return

        title = QLabel("Windows Explorer")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Add ISOForge to the right-click menu, so archives can be "
            "opened or extracted, files compressed, and disc images "
            "converted to CHD — straight from Explorer."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        card = QFrame()
        card.setObjectName("Card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        card_layout.setSpacing(8)

        self.shell_archive_check = QCheckBox(
            "Archives  —  Open with ISOForge, Extract Here, Extract to subfolder")
        self.shell_archive_check.setChecked(shell_integration.archives_registered())
        card_layout.addWidget(self.shell_archive_check)

        self.shell_create_check = QCheckBox(
            "Files and folders  —  Compress with ISOForge...")
        self.shell_create_check.setChecked(shell_integration.create_registered())
        card_layout.addWidget(self.shell_create_check)

        self.shell_chd_check = QCheckBox(
            "Disc images (.cue, .gdi, .toc, .iso)  —  Convert to CHD")
        self.shell_chd_check.setChecked(shell_integration.chd_registered())
        card_layout.addWidget(self.shell_chd_check)

        self.shell_status = QLabel(shell_integration.status_summary())
        self.shell_status.setObjectName("Muted")
        self.shell_status.setWordWrap(True)
        card_layout.addWidget(self.shell_status)

        hint = QLabel(
            "Applies to your account only — no administrator rights needed, "
            "and your existing file associations are left alone.<br>"
            "The CHD item needs chdman.exe (above) when you use it.<br>"
            "On Windows 11 these appear under <b>Show more options</b>."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        hint.setTextFormat(Qt.RichText)
        card_layout.addWidget(hint)

        layout.addWidget(card)

    # -- construction helpers ----------------------------------------- #
    def _path_row(self, browse_slot):
        row = QHBoxLayout()
        edit = QLineEdit()
        edit.setPlaceholderText("Leave empty to detect automatically")
        browse = QPushButton("Browse...")
        browse.clicked.connect(browse_slot)
        row.addWidget(edit, 1)
        row.addWidget(browse)
        return edit, row

    @staticmethod
    def _status_label() -> QLabel:
        lbl = QLabel("")
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        return lbl

    # -- browsing ------------------------------------------------------ #
    def _executable_filter(self) -> str:
        # Windows users are picking a .exe; everything else may have no
        # extension at all, so both options are always offered.
        return "Programs (*.exe);;All files (*)"

    def _browse_rar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate Rar.exe", self._start_dir(self.rar_edit),
            self._executable_filter())
        if path:
            self.rar_edit.setText(path)

    def _browse_unrar(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate UnRAR.exe", self._start_dir(self.unrar_edit),
            self._executable_filter())
        if path:
            self.unrar_edit.setText(path)

    def _browse_sfx(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate an SFX module", self._start_dir(self.sfx_edit),
            "SFX modules (*.sfx);;Programs (*.exe);;All files (*)")
        if path:
            self.sfx_edit.setText(path)

    def _browse_chdman(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate chdman.exe", self._start_dir(self.chd_edit),
            self._executable_filter())
        if path:
            self.chd_edit.setText(path)

    @staticmethod
    def _start_dir(edit: QLineEdit) -> str:
        current = (edit.text() or "").strip()
        if current and os.path.isfile(current):
            return os.path.dirname(current)
        if current and os.path.isdir(current):
            return current
        return ""

    # -- detection / validation ---------------------------------------- #
    def _detect(self) -> None:
        """Clears the RAR fields and lets auto-detection speak for itself.

        Clearing first is deliberate: the resolver only falls back to
        searching when a field is empty, so wiping them is what actually
        triggers a fresh search rather than re-reporting whatever was
        already typed in.
        """
        previous = (rar_tools.configured_rar(), rar_tools.configured_unrar())
        rar_tools.configure(None, None)
        try:
            found_rar = rar_tools.resolve_rar() or ""
            found_unrar = rar_tools.resolve_unrar() or ""
        finally:
            rar_tools.configure(*previous)

        self.rar_edit.setText(found_rar)
        # Only fill UnRAR if a DEDICATED unrar was found. resolve_unrar()
        # falls back to Rar.exe, and echoing that same path into both
        # boxes would imply two tools were found when only one was.
        self.unrar_edit.setText(
            found_unrar if found_unrar and found_unrar != found_rar else "")
        self._refresh_status()

    def _detect_chdman(self) -> None:
        """Clears the chdman field and lets auto-detection speak for itself
        (same approach as `_detect` above: the resolver only searches when
        nothing is configured)."""
        previous = chd_tools.configured_chdman()
        chd_tools.configure(None)
        try:
            found = chd_tools.resolve_chdman() or ""
        finally:
            chd_tools.configure(previous)
        self.chd_edit.setText(found)
        self._refresh_status()

    def _refresh_status(self, *_args) -> None:
        # Accepts (and ignores) any payload, because this is wired to
        # QLineEdit.textChanged, which emits the new text. Matching the
        # `*_` pattern used by ConvertDialog._on_format_changed keeps the
        # slot usable from both a signal and a direct call.
        self._apply_status(
            self.rar_status, self.rar_edit.text(),
            rar_tools.resolve_rar, rar_tools.probe_tool,
            rar_tools.configure, (rar_tools.configured_rar,
                                  rar_tools.configured_unrar),
            enabled_text="RAR archives can be created and extracted.",
            missing_text=("Not found — RAR archives can be opened but not "
                          "created."),
        )
        self._apply_status(
            self.unrar_status, self.unrar_edit.text(),
            rar_tools.resolve_unrar, rar_tools.probe_tool,
            rar_tools.configure, (rar_tools.configured_rar,
                                  rar_tools.configured_unrar),
            enabled_text="RAR archives can be extracted.",
            missing_text="Not found — RAR archives cannot be extracted.",
        )
        self._apply_status(
            self.sfx_status, self.sfx_edit.text(),
            sfx_tools.resolve_stub, sfx_tools.probe_stub,
            sfx_tools.configure, (sfx_tools.configured_stub,),
            enabled_text="Self-extracting archives can be created.",
            missing_text=("Not found — install 7-Zip, or choose a module "
                          "such as 7z.sfx."),
        )
        self._apply_status(
            self.chd_status, self.chd_edit.text(),
            chd_tools.resolve_chdman, chd_tools.probe_tool,
            chd_tools.configure, (chd_tools.configured_chdman,),
            enabled_text="Disc images can be converted to CHD.",
            missing_text=("Not found — set chdman.exe (it ships with MAME) "
                          "to enable Convert to CHD."),
        )

    def _apply_status(self, label: QLabel, typed: str, resolver, prober,
                      configurer, savers, enabled_text: str,
                      missing_text: str) -> None:
        typed = (typed or "").strip()

        if typed:
            ok, description = prober(typed)
            if ok:
                self._set_status(label, _OK_COLOR,
                                 f"{description} — {enabled_text}")
            else:
                self._set_status(label, _BAD_COLOR, description)
            return

        # Empty field: report what auto-detection would find, and make
        # clear the path shown was discovered rather than entered. The
        # configured values are stashed and restored around the probe so
        # asking "what WOULD be found?" can't disturb the live settings.
        previous = tuple(saver() for saver in savers)
        configurer(*([None] * len(savers)))
        try:
            found = resolver()
        finally:
            configurer(*previous)

        if not found:
            self._set_status(label, _IDLE_COLOR, missing_text)
            return

        ok, description = prober(found)
        if ok:
            self._set_status(
                label, _OK_COLOR,
                f"Found automatically: {found}<br>{description} — {enabled_text}")
        else:
            self._set_status(label, _BAD_COLOR,
                             f"Found {found}, but it isn't usable: {description}")

    @staticmethod
    def _set_status(label: QLabel, color: str, text: str) -> None:
        label.setStyleSheet(f"color: {color};")
        label.setText(text)

    # -- results -------------------------------------------------------- #
    def values(self) -> Tuple[str, str, str]:
        """(rar_path, unrar_path, sfx_stub), all trimmed."""
        return (self.rar_edit.text().strip(),
                self.unrar_edit.text().strip(),
                self.sfx_edit.text().strip())

    def chdman_value(self) -> str:
        """The chdman.exe path, trimmed (empty means auto-detect)."""
        return self.chd_edit.text().strip()

    def shell_values(self) -> Optional[Tuple[bool, bool, bool]]:
        """(archives, files_and_folders, chd), or None where unsupported."""
        if not self._shell_supported:
            return None
        return (self.shell_archive_check.isChecked(),
                self.shell_create_check.isChecked(),
                self.shell_chd_check.isChecked())


