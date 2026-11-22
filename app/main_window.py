"""main_window.py — the ISOForge(ZR) main application window.

Format-agnostic UI
===================
Every action here (Open / Save / Save As / Extract / Convert) dispatches
through `formats.py` to either `iso_backend` or `archive_backend`. The
two backends deliberately expose the same shape, so this file contains
one code path per *action* rather than one per action-and-format
combination.

Where the formats genuinely differ, the difference is handled explicitly
and visibly rather than being papered over:

  * ZIP/7Z/RAR/SFX support passwords; ISO has no such concept.
  * ISO has a volume label; archives don't.
  * RAR and SFX can only be CREATED once the binary each needs has been
    located (see Tools > Settings). Because `formats.can_write()` answers
    that question dynamically, every menu and filter below adapts on its
    own -- there is no separate "RAR mode" or "SFX mode" branch to keep
    in sync.

Explorer integration
=====================
`run()` also serves the right-click menu verbs registered by
`shell_integration`. Those arrive as ordinary command-line arguments,
one process per selected file, and are folded back into a single action
by `single_instance` before reaching `handle_shell_request()`.

Extract verbs and Convert-to-CHD run WITHOUT showing this window -- the
user asked for files on disk, not an editor -- while Open and Compress
show it, since the next step is theirs to take.

CHD conversion
===============
"Convert Folder" (the original Convert action, renamed so the two can't be
confused) builds an ISO or archive from a folder. "Convert to CHD" is a
separate, single-purpose action that compresses a disc image (.cue, .gdi,
.toc, .iso) into a CHD by driving MAME's chdman.exe -- see `chd_tools`.
The toolbar/menu button and the Explorer verb share one code path
(`_execute_chd_jobs`), so they behave identically.

A CHD can also be OPENED and SAVED like any other container. It is handled as
a disc image: opening unpacks it to a temporary ISO (see `chd_backend`), and
saving builds an ISO and compresses it. A project opened from a CHD therefore
behaves like an ISO project everywhere -- it has a volume label, the
long-path advisory applies -- which is what `formats.is_disc_image()` is for.

Cancellation
=============
`ProgressDialog` shows a Cancel button only for operations that actually
support it (`_run_task` checks this via `workers._accepts_cancel_token`),
and `_run_task` wires the button straight to the worker's own
`CancelToken`. See `fileops.OperationCancelled` for why cancellation is a
BaseException rather than an ordinary one, and `workers.TaskWorker` for
how a cancelled run is told apart from a genuinely failed one.

A real reported symptom -- the progress dialog appearing to hang at
"100%" for a long time on a large ISO, with Cancel seemingly doing
nothing -- turned out to have a genuine cause on the backend side (see
`app/progress.py`'s docstring for the full explanation: pycdlib patches
the file backwards after writing it, and the OS can defer flushing
several GB to physical disk until the file closes, neither of which
previously produced any visible progress or any opportunity to notice a
cancellation request). `ProgressDialog` now also plays its part: if
Cancel hasn't resolved within a few seconds, the dialog explains what is
most likely still happening rather than leaving an unchanging message on
screen that reads as frozen -- see `_show_cancel_hint` below.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from PySide6.QtCore import Qt, QSettings, QSize, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence, QIcon, QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QToolBar, QWidget, QVBoxLayout, QHBoxLayout,
    QSplitter, QTreeWidget, QTreeWidgetItem, QTableWidget, QTableWidgetItem,
    QLineEdit, QToolButton, QLabel, QStatusBar, QFileDialog, QMessageBox,
    QInputDialog, QProgressBar, QDialog, QDialogButtonBox, QFormLayout,
    QHeaderView, QAbstractItemView, QMenu, QFrame, QSizePolicy, QPushButton,
    QComboBox, QScrollArea,
)

from . import archive_backend
from . import chd_backend
from . import chd_tools
from . import formats
from . import icons
from . import iso_backend
from . import rar_tools
from . import settings_store
from . import sfx_tools
from . import shell_commands
from . import shell_integration
from . import single_instance
from . import theme
from .dialog_base import ForgeDialog
from .password_dialog import EncryptionPromptDialog, PasswordPromptDialog
from .progress import PROGRESS_SCALE
from .settings_dialog import SettingsDialog
from .theme_toggle import ThemeToggle
from .vfs import ForgeProject, VNode, find_long_paths
from .workers import TaskWorker, _accepts_cancel_token

APP_NAME = "ISOForge(ZR)"
ORG_NAME = "ViresITConsultants"

# Everything ISOForge is allowed to persist. Anything else found in the
# store is stripped at startup, so stale keys from older versions can't
# quietly linger. Tool paths are included explicitly via settings_store.
_ALLOWED_SETTINGS_KEYS = {
    "geometry", "recentFiles", "darkMode", "columnWidths",
} | set(settings_store.SETTINGS_KEYS)

_RESIZABLE_COLUMNS = (0, 1, 2)
_DEFAULT_COLUMN_WIDTHS = {0: 340, 1: 90, 2: 130}
LONG_PATH_WARN_THRESHOLD = 950

# How long a headless Explorer verb (extract, CHD conversion) keeps listening
# for more of the same selection after finishing, before the process exits.
_HEADLESS_LINGER_MS = 2000


def human_size(num: int) -> str:
    if num is None:
        return "-"
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step:
            return f"{num:3.1f} {unit}" if unit != "B" else f"{int(num)} {unit}"
        num /= step
    return f"{num:.1f} PB"


def human_time(ts: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return ""


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if value is None:
        return default
    return bool(value)


def _same_file(path_a: str, path_b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(path_a)) == os.path.normcase(os.path.abspath(path_b))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
class ProgressDialog(QDialog):
    """The modal "building/extracting" dialog.

    Shows a Cancel button only when `cancellable=True` is passed in --
    see `_run_task`, which decides that by checking whether the target
    function declares a `cancel_token` parameter at all. Showing a
    Cancel button that quietly does nothing would be worse than not
    showing one; this way, a button only ever appears where clicking it
    genuinely stops the operation.

    Once Cancel is clicked, the "Cancelling..." message is deliberately
    NOT the only thing ever shown. On a large ISO or archive, the worker
    thread can genuinely be stuck for a while inside a single blocking
    OS-level call (a multi-GB write finally being flushed to a slow,
    external, or network drive; antivirus scanning the finished file as
    it closes) that cannot safely be interrupted out of -- see
    `app/progress.py`'s docstring for the full explanation, and
    `_show_cancel_hint` below for how that's communicated rather than
    left to look like a frozen application.
    """

    cancel_requested = Signal()

    # How long to wait after Cancel is clicked, with no resolution,
    # before explaining that a slow disk operation -- not a genuine
    # hang -- is the likely reason nothing has visibly changed yet.
    _CANCEL_HINT_DELAY_MS = 4000

    def __init__(self, parent, title: str, cancellable: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(460)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)

        self._cancellable = cancellable
        self._cancelling = False
        self._cancel_hint_timer = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        self.label = QLabel(title)
        self.label.setObjectName("SectionTitle")
        layout.addWidget(self.label)
        self.detail = QLabel("Preparing...")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setMinimum(0)
        self.bar.setMaximum(100)
        layout.addWidget(self.bar)

        if cancellable:
            button_row = QHBoxLayout()
            button_row.addStretch(1)
            self.cancel_btn = QPushButton("Cancel")
            self.cancel_btn.clicked.connect(self._on_cancel_clicked)
            button_row.addWidget(self.cancel_btn)
            layout.addLayout(button_row)
        else:
            self.cancel_btn = None

    def _on_cancel_clicked(self) -> None:
        if self._cancelling:
            return
        self._cancelling = True
        if self.cancel_btn is not None:
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling...")
        self.detail.setText(
            "Cancelling — waiting for the current step to stop safely...")
        self.cancel_requested.emit()

        # If nothing has resolved this within a few seconds, the most
        # likely explanation is that the worker thread is genuinely
        # blocked inside a single OS-level write/flush/close call it
        # cannot safely be interrupted out of -- see app/progress.py's
        # docstring for exactly why that happens on a large build.
        # Nothing ISOForge does can speed that up, so the honest and
        # most useful thing to do is explain what is probably still
        # happening rather than leave an unchanging message on screen
        # indefinitely, which reads as a frozen application.
        self._cancel_hint_timer = QTimer(self)
        self._cancel_hint_timer.setSingleShot(True)
        self._cancel_hint_timer.timeout.connect(self._show_cancel_hint)
        self._cancel_hint_timer.start(self._CANCEL_HINT_DELAY_MS)

    def _show_cancel_hint(self) -> None:
        if not self._cancelling:
            return
        self.detail.setText(
            "Still cancelling — this can take a while if a lot of data "
            "was just written to a slow, external, or network drive. "
            "Windows may still be flushing it to disk, and antivirus "
            "software commonly scans a file right as it closes; "
            "ISOForge cannot safely interrupt either of those partway "
            "through. It isn't frozen — it will stop as soon as that "
            "finishes."
        )

    def closeEvent(self, event) -> None:
        # Treat the window's own [X] the same as the Cancel button --
        # without this, closing the dialog that way would abandon the
        # worker thread still running in the background instead of
        # asking it to stop, exactly the same problem Cancel exists to
        # solve.
        if self._cancellable and not self._cancelling:
            self._on_cancel_clicked()
            event.ignore()
        else:
            super().closeEvent(event)

    def update_progress(self, text: str, done: int, total: int) -> None:
        if self._cancelling:
            # Once cancellation has been requested, keep showing the
            # cancelling-related message (either the initial one, or
            # _show_cancel_hint's fuller explanation) rather than
            # overwriting it with whatever step happened to be in
            # flight -- a progress update arriving just after the click
            # would otherwise visually contradict the button the user
            # just pressed.
            return
        self.detail.setText(text)
        if total > 0:
            self.bar.setMaximum(total)
            self.bar.setValue(done)
            self.bar.setFormat(f"{(done / total) * 100:.0f}%")
        else:
            self.bar.setMaximum(0)


# --------------------------------------------------------------------------- #
class ConvertDialog(ForgeDialog):
    """'Convert Folder' -- create a container from a folder.

    (Shown to the user as "Convert Folder" so it isn't confused with the
    separate "Convert to CHD" action, which works on disc images.)

    Format-aware: the output type drives which extension is suggested,
    whether the volume-label field is relevant (ISO only), and whether
    password protection is offered. The list of output formats is built
    from `formats.writable_formats()`, so RAR and self-extracting EXE
    appear here automatically once their tools are configured.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert Folder")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        title = QLabel("Convert Folder")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        extensions = ", ".join(
            formats.primary_extension(f) for f in formats.writable_formats())
        sub = QLabel(
            "Pick a source folder and ISOForge will build a ready-to-use "
            f"file from its contents ({extensions})."
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(10)

        src_row = QHBoxLayout()
        self.src_edit = QLineEdit()
        src_btn = QPushButton("Browse...")
        src_btn.clicked.connect(self._pick_source)
        src_row.addWidget(self.src_edit)
        src_row.addWidget(src_btn)
        form.addRow("Source folder:", src_row)

        self.fmt_combo = QComboBox()
        # Only formats that can actually be produced right now are shown,
        # so the user is never offered an output the app can't deliver.
        for fmt in formats.writable_formats():
            self.fmt_combo.addItem(
                f"{formats.format_label(fmt)}  ({formats.primary_extension(fmt)})",
                fmt,
            )
        self.fmt_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("Output format:", self.fmt_combo)

        out_row = QHBoxLayout()
        self.out_edit = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(self._pick_output)
        out_row.addWidget(self.out_edit)
        out_row.addWidget(out_btn)
        form.addRow("Output file:", out_row)

        self.label_edit = QLineEdit()
        self.label_row_label = QLabel("Volume label:")
        form.addRow(self.label_row_label, self.label_edit)

        layout.addLayout(form)

        self.pw_note = QLabel("")
        self.pw_note.setObjectName("Muted")
        self.pw_note.setWordWrap(True)
        layout.addWidget(self.pw_note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons

        self._on_format_changed()

    def selected_format(self) -> str:
        return self.fmt_combo.currentData() or formats.ISO

    def _on_format_changed(self, *_):
        fmt = self.selected_format()
        is_iso = formats.is_disc_image(fmt)
        self.label_edit.setVisible(is_iso)
        self.label_row_label.setVisible(is_iso)

        if formats.supports_write_encryption(fmt):
            self.pw_note.setText(
                "You'll be asked whether to password protect this archive "
                "after clicking OK."
            )
            self.pw_note.show()
        else:
            self.pw_note.hide()

        current = self.out_edit.text().strip()
        if current:
            stem = os.path.splitext(current)[0]
            self.out_edit.setText(stem + formats.primary_extension(fmt))

    def _pick_source(self):
        path = QFileDialog.getExistingDirectory(self, "Select source folder")
        if not path:
            return
        self.src_edit.setText(path)
        base = os.path.basename(os.path.normpath(path))
        if not self.label_edit.text():
            self.label_edit.setText(base[:32].upper())
        if not self.out_edit.text():
            ext = formats.primary_extension(self.selected_format())
            self.out_edit.setText(os.path.join(os.path.dirname(path), base + ext))

    def _pick_output(self):
        fmt = self.selected_format()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save as",
            self.out_edit.text().strip(),
            filter=f"{formats.format_label(fmt)} (*{formats.primary_extension(fmt)})",
        )
        if path:
            self.out_edit.setText(formats.ensure_extension(path, fmt))

    def _on_accept(self):
        if not self.src_edit.text().strip():
            QMessageBox.warning(self, APP_NAME, "Please choose a source folder.")
            return
        if not self.out_edit.text().strip():
            QMessageBox.warning(self, APP_NAME, "Please choose where to save the file.")
            return
        self.accept()

    def values(self):
        fmt = self.selected_format()
        out = formats.ensure_extension(self.out_edit.text().strip(), fmt)
        return (self.src_edit.text().strip(), out,
                self.label_edit.text().strip(), fmt)


# --------------------------------------------------------------------------- #
class ChdConvertDialog(ForgeDialog):
    """'Convert to CHD' -- compress one disc image into a CHD file.

    Deliberately small: a source image, an output name, and (only where it
    genuinely matters) a disc type. A .cue/.gdi/.toc is always a CD image,
    so the disc-type choice is disabled for those rather than offering an
    option `chdman createdvd` can't honour. For an .iso it defaults to
    "Automatic", which guesses CD vs DVD from the image's size -- see
    `chd_tools.resolve_kind` -- and exists as a dropdown because that guess
    can be wrong for a small DVD image (a PSP game, for instance).

    Overwriting an existing CHD is confirmed HERE, so the code that does the
    conversion never has to ask.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Convert to CHD")
        self.setMinimumWidth(560)
        self._suggested_out = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        title = QLabel("Convert to CHD")
        title.setObjectName("SectionTitle")
        layout.addWidget(title)

        extensions = ", ".join(chd_tools.SOURCE_EXTENSIONS)
        sub = QLabel(
            f"Compress a disc image ({extensions}) into a CHD — the lossless, "
            "compressed format used by MAME and most emulators. The original "
            "files are left untouched."
        )
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        form = QFormLayout()
        form.setSpacing(10)

        src_row = QHBoxLayout()
        self.src_edit = QLineEdit()
        self.src_edit.setPlaceholderText("Choose a .cue, .gdi, .toc or .iso file")
        src_btn = QPushButton("Browse...")
        src_btn.clicked.connect(self._pick_source)
        src_row.addWidget(self.src_edit)
        src_row.addWidget(src_btn)
        form.addRow("Source image:", src_row)

        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Automatic (recommended)", chd_tools.KIND_AUTO)
        self.kind_combo.addItem("CD image", chd_tools.KIND_CD)
        self.kind_combo.addItem("DVD image", chd_tools.KIND_DVD)
        form.addRow("Disc type:", self.kind_combo)

        out_row = QHBoxLayout()
        self.out_edit = QLineEdit()
        out_btn = QPushButton("Browse...")
        out_btn.clicked.connect(self._pick_output)
        out_row.addWidget(self.out_edit)
        out_row.addWidget(out_btn)
        form.addRow("Output file:", out_row)
        layout.addLayout(form)

        self.kind_hint = QLabel("")
        self.kind_hint.setObjectName("Muted")
        self.kind_hint.setWordWrap(True)
        layout.addWidget(self.kind_hint)

        tool = chd_tools.resolve_chdman()
        self.tool_note = QLabel(
            f"Using chdman: {tool}" if tool else
            "chdman.exe was not found — set it under Tools > Settings.")
        self.tool_note.setObjectName("Muted")
        self.tool_note.setWordWrap(True)
        layout.addWidget(self.tool_note)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Convert")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.src_edit.textChanged.connect(self._on_source_changed)
        self.kind_combo.currentIndexChanged.connect(self._update_kind_hint)
        self._update_kind_hint()

    # -- helpers ----------------------------------------------------------- #
    def selected_kind(self) -> str:
        return self.kind_combo.currentData() or chd_tools.KIND_AUTO

    def _source_text(self) -> str:
        return self.src_edit.text().strip().strip('"')

    def _on_source_changed(self, *_):
        text = self._source_text()
        if text and chd_tools.is_convertible(text):
            suggested = chd_tools.default_output_path(text)
            current = self.out_edit.text().strip()
            # Only overwrite what the user hasn't typed themselves: an
            # untouched (or previously auto-filled) field follows the
            # source, a hand-edited one is left alone.
            if not current or current == self._suggested_out:
                self.out_edit.setText(suggested)
            self._suggested_out = suggested
        self._update_kind_hint()

    def _update_kind_hint(self, *_):
        text = self._source_text()
        if not text or not os.path.isfile(text) or not chd_tools.is_convertible(text):
            self.kind_combo.setEnabled(True)
            self.kind_hint.setText("")
            return
        cd_only = os.path.splitext(text)[1].lower() in chd_tools.CD_ONLY_EXTENSIONS
        self.kind_combo.setEnabled(not cd_only)
        kind, reason = chd_tools.resolve_kind(text, self.selected_kind())
        self.kind_hint.setText(
            f"Will be converted as a {chd_tools.kind_label(kind)} image ({reason}).")

    def _pick_source(self):
        start = self._source_text()
        start_dir = os.path.dirname(start) if start else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select disc image", start_dir, chd_tools.source_dialog_filter())
        if path:
            self.src_edit.setText(path)

    def _pick_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save CHD as", self.out_edit.text().strip(),
            filter="CHD files (*.chd)")
        if path:
            self.out_edit.setText(self._with_chd_extension(path))

    @staticmethod
    def _with_chd_extension(path: str) -> str:
        if os.path.splitext(path)[1].lower() == ".chd":
            return path
        return path + ".chd"

    def _on_accept(self):
        src = self._source_text()
        if not src:
            QMessageBox.warning(self, APP_NAME, "Please choose a disc image to convert.")
            return
        try:
            chd_tools.validate_source(src)
        except chd_tools.ChdToolError as exc:
            QMessageBox.warning(self, APP_NAME, str(exc))
            return
        out = self.out_edit.text().strip()
        if not out:
            QMessageBox.warning(self, APP_NAME, "Please choose where to save the CHD.")
            return
        out = self._with_chd_extension(out)
        if _same_file(out, src):
            QMessageBox.warning(
                self, APP_NAME, "The output file can't be the same as the source.")
            return
        if os.path.exists(out):
            resp = QMessageBox.question(
                self, APP_NAME,
                f"'{os.path.basename(out)}' already exists. Replace it?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
        self.out_edit.setText(out)
        self.accept()

    def values(self):
        """(source, output, disc-type preference)."""
        out = self._with_chd_extension(self.out_edit.text().strip())
        return self._source_text(), out, self.selected_kind()


# --------------------------------------------------------------------------- #
class PropertiesDialog(ForgeDialog):
    def __init__(self, parent, title: str, rows: list):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)
        head = QLabel(title)
        head.setObjectName("SectionTitle")
        layout.addWidget(head)
        form = QFormLayout()
        for k, v in rows:
            lbl = QLabel(str(v))
            lbl.setWordWrap(True)
            form.addRow(k + ":", lbl)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


# --------------------------------------------------------------------------- #
class HelpDialog(ForgeDialog):
    """Displays the About/Help content in a scrollable, properly-sized
    dialog.

    Why not QMessageBox (used previously)
    =======================================
    QMessageBox has two limitations that make it a poor fit for content
    this long: it cannot scroll at all, and every platform clamps or
    caps its size well below what's needed to show everything -- which
    is exactly what produced the cramped, unscrollable dialog reported
    (some text visibly cut off at the bottom with no way to reach it).

    A plain QDialog sidesteps both. Its content sits inside a
    QScrollArea -- the same pattern used by `SettingsDialog`, including
    the same transparent-background treatment so the scroll viewport
    doesn't paint an un-themed background over ISOForge's own dialog
    background (see `settings_dialog.py`'s docstring for the full
    rationale) -- so the dialog can be sized generously AND still
    scroll if the content ever exceeds even that.
    """

    def __init__(self, parent, title: str, html: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        self.resize(720, 760)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        # See settings_dialog.py's docstring for why both the stylesheet
        # rule AND the direct setAutoFillBackground(False) calls are
        # needed: QScrollArea's viewport otherwise paints its own
        # opaque, un-themed background regardless of this dialog's
        # stylesheet, producing dark content on a light backdrop (or
        # vice versa) independent of ISOForge's actual theme setting.
        scroll.setStyleSheet("QScrollArea { background: transparent; }")
        scroll.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        scroll.viewport().setStyleSheet("background: transparent;")

        content = QWidget()
        content.setAutoFillBackground(False)
        content.setStyleSheet("background: transparent;")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 22, 24, 18)
        content_layout.setSpacing(14)

        icon_label = QLabel()
        icon_label.setPixmap(icons.icon_disc_logo(64).pixmap(64, 64))
        content_layout.addWidget(icon_label)

        text_label = QLabel(html)
        text_label.setTextFormat(Qt.RichText)
        text_label.setWordWrap(True)
        text_label.setOpenExternalLinks(True)
        content_layout.addWidget(text_label)
        content_layout.addStretch(1)

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # -- fixed footer, always visible ----------------------------- #
        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(24, 12, 24, 18)
        footer_layout.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        footer_layout.addWidget(close_btn)
        outer.addWidget(footer, 0)


# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowIcon(icons.icon_disc_logo())
        self.setMinimumSize(1000, 680)
        self.resize(1180, 760)

        self.settings = QSettings(ORG_NAME, APP_NAME)
        self._sanitize_settings()

        self.dark_mode = _coerce_bool(self.settings.value("darkMode", False))
        icons.set_theme(self.dark_mode)

        self.project = ForgeProject()
        self._active_worker = None
        self._warned_long_paths: set = set()

        # Filename Save As should suggest for a project that came from
        # Explorer's "Compress with ISOForge...", which has no save path
        # of its own yet.
        self._suggested_name: str = ""
        # Paths already staged from Explorer into the CURRENT unsaved
        # project. Lets a selection that arrives in more than one batch end
        # up in one session -- see `stage_for_compression`.
        self._shell_staged_paths: list = []

        self._build_ui()
        self._build_actions()
        self._build_toolbar()
        self._build_menu()
        self._restore_geometry()
        self._refresh_all()
        self._update_title()

    def _sanitize_settings(self) -> None:
        for key in list(self.settings.allKeys()):
            if key not in _ALLOWED_SETTINGS_KEYS:
                self.settings.remove(key)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 8, 10, 10)
        root_layout.setSpacing(8)

        path_bar = QFrame()
        path_bar.setObjectName("PathBar")
        path_layout = QHBoxLayout(path_bar)
        path_layout.setContentsMargins(4, 2, 4, 2)
        path_layout.setSpacing(2)

        self.back_btn = QToolButton()
        self.back_btn.setIcon(icons.icon_nav("left"))
        self.back_btn.setToolTip("Back")
        self.forward_btn = QToolButton()
        self.forward_btn.setIcon(icons.icon_nav("right"))
        self.forward_btn.setToolTip("Forward")
        self.up_btn = QToolButton()
        self.up_btn.setIcon(icons.icon_nav("up"))
        self.up_btn.setToolTip("Up one level")
        self.refresh_btn = QToolButton()
        self.refresh_btn.setIcon(icons.icon_refresh())
        self.refresh_btn.setToolTip("Refresh")

        for b in (self.back_btn, self.forward_btn, self.up_btn, self.refresh_btn):
            b.setIconSize(QSize(18, 18))
            path_layout.addWidget(b)

        self.path_edit = QLineEdit()
        self.path_edit.setObjectName("PathEdit")
        self.path_edit.setPlaceholderText("/")
        path_layout.addWidget(self.path_edit, 1)

        root_layout.addWidget(path_bar)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(230)
        self.tree.setIconSize(QSize(18, 18))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Type", "Modified"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        header.setSectionResizeMode(1, QHeaderView.Interactive)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self._restore_column_widths()
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.setAcceptDrops(True)
        self.table.setDragDropMode(QAbstractItemView.DropOnly)

        splitter.addWidget(self.tree)
        splitter.addWidget(self.table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 800])

        root_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

        self.status = QStatusBar()
        self.setStatusBar(self.status)
        self.status_left = QLabel("")
        self.status_right = QLabel("")
        self.status.addWidget(self.status_left, 1)
        self.status.addPermanentWidget(self.status_right)

        self.back_btn.clicked.connect(self._go_back)
        self.forward_btn.clicked.connect(self._go_forward)
        self.up_btn.clicked.connect(self._go_up)
        self.refresh_btn.clicked.connect(self._refresh_all)
        self.path_edit.returnPressed.connect(self._on_path_entered)
        self.tree.itemClicked.connect(self._on_tree_clicked)
        self.tree.customContextMenuRequested.connect(self._on_tree_context_menu)
        self.table.itemDoubleClicked.connect(self._on_table_double_clicked)
        self.table.itemSelectionChanged.connect(self._update_status)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)

        self.table.dragEnterEvent = self._table_drag_enter
        self.table.dragMoveEvent = self._table_drag_move
        self.table.dropEvent = self._table_drop

    def _build_actions(self):
        def act(text, icon, slot, shortcut=None, tip=None):
            a = QAction(icon, text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            a.setToolTip(tip or text)
            return a

        self.act_new = act("New", icons.icon_new_disc(), self.on_new, QKeySequence.New)
        self.act_open = act("Open", icons.icon_open_folder(), self.on_open, QKeySequence.Open,
                            tip="Open an ISO, CHD, ZIP, 7Z, RAR or self-extracting archive")
        self.act_save = act("Save", icons.icon_save(), self.on_save, QKeySequence.Save)
        self.act_save_as = act("Save As", icons.icon_save_as(), self.on_save_as, "Ctrl+Shift+S")
        self.act_add_files = act("Add Files", icons.icon_add_files(), self.on_add_files,
                                 tip="Add files from your computer")
        self.act_add_folder = act("Add Folder", icons.icon_add_folder(), self.on_add_folder,
                                  tip="Add a folder from your computer")
        self.act_new_folder = act("New Folder", icons.icon_new_folder(), self.on_new_folder,
                                  tip="Create an empty folder")
        self.act_extract = act("Extract", icons.icon_extract(), self.on_extract,
                               tip="Extract selected items to disk")
        self.act_delete = act("Delete", icons.icon_delete(), self.on_delete, QKeySequence.Delete)
        self.act_rename = act("Rename", icons.icon_rename(), self.on_rename, "F2",
                              tip="Rename the selected item")
        self.act_properties = act("Properties", icons.icon_properties(), self.on_properties,
                                  tip="Show size and item counts")
        self.act_convert = act("Convert Folder", icons.icon_convert(), self.on_convert,
                               tip="Create an ISO or archive directly from a folder")
        self.act_convert_chd = act("Convert to CHD", icons.icon_convert_chd(),
                                   self.on_convert_chd,
                                   tip="Compress a disc image (.cue, .gdi, .toc, .iso) to CHD")
        self.act_settings = act("Settings", icons.icon_settings(), self.on_settings,
                                "Ctrl+,",
                                tip="External tools and Explorer integration")
        self.act_help = act("Help", icons.icon_help(), self.on_help, QKeySequence.HelpContents)

    def _build_toolbar(self):
        tb = QToolBar("Main")
        tb.setMovable(False)
        tb.setIconSize(QSize(26, 26))
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.addToolBar(tb)

        for a in (self.act_new, self.act_open, self.act_save, self.act_save_as):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_add_files, self.act_add_folder, self.act_new_folder):
            tb.addAction(a)
        tb.addSeparator()
        for a in (self.act_extract, self.act_delete, self.act_rename):
            tb.addAction(a)
        tb.addSeparator()
        tb.addAction(self.act_properties)
        tb.addAction(self.act_convert)
        tb.addAction(self.act_convert_chd)
        tb.addSeparator()
        tb.addAction(self.act_settings)
        tb.addAction(self.act_help)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.theme_toggle = ThemeToggle(checked=self.dark_mode)
        self.theme_toggle.toggled.connect(self.on_toggle_dark_mode)
        tb.addWidget(self.theme_toggle)

    def _build_menu(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")
        file_menu.addAction(self.act_new)
        file_menu.addAction(self.act_open)
        self.recent_menu = file_menu.addMenu("Open Recent")
        self._populate_recent_menu()
        file_menu.addSeparator()
        file_menu.addAction(self.act_save)
        file_menu.addAction(self.act_save_as)
        file_menu.addSeparator()
        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        edit_menu = menubar.addMenu("&Edit")
        edit_menu.addAction(self.act_add_files)
        edit_menu.addAction(self.act_add_folder)
        edit_menu.addAction(self.act_new_folder)
        edit_menu.addSeparator()
        edit_menu.addAction(self.act_rename)
        edit_menu.addAction(self.act_delete)
        edit_menu.addSeparator()
        self.rename_volume_act = QAction("Rename Disc...", self)
        self.rename_volume_act.triggered.connect(self._rename_volume_flow)
        edit_menu.addAction(self.rename_volume_act)

        tools_menu = menubar.addMenu("&Tools")
        tools_menu.addAction(self.act_extract)
        tools_menu.addAction(self.act_properties)
        tools_menu.addSeparator()
        tools_menu.addAction(self.act_convert)
        tools_menu.addAction(self.act_convert_chd)
        tools_menu.addSeparator()
        tools_menu.addAction(self.act_settings)

        help_menu = menubar.addMenu("&Help")
        help_menu.addAction(self.act_help)

    def _restore_geometry(self):
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)

    def _restore_column_widths(self) -> None:
        saved = self.settings.value("columnWidths")
        for col in _RESIZABLE_COLUMNS:
            width = _DEFAULT_COLUMN_WIDTHS[col]
            if isinstance(saved, dict):
                raw = saved.get(col, saved.get(str(col)))
                try:
                    candidate = int(raw)
                    if candidate > 0:
                        width = candidate
                except (TypeError, ValueError):
                    pass
            self.table.setColumnWidth(col, width)

    def _save_column_widths(self) -> None:
        widths = {col: self.table.columnWidth(col) for col in _RESIZABLE_COLUMNS}
        self.settings.setValue("columnWidths", widths)

    def closeEvent(self, event):
        if self.project.dirty:
            resp = QMessageBox.question(
                self, APP_NAME,
                "You have unsaved changes. Save before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if resp == QMessageBox.Cancel:
                event.ignore()
                return
            if resp == QMessageBox.Save:
                self.on_save()
                if self.project.dirty:
                    event.ignore()
                    return
        self.settings.setValue("geometry", self.saveGeometry())
        self._save_column_widths()

        # Release the open container before quitting. This matters most
        # for 7Z, whose reader materialises the whole archive into a temp
        # directory that is only removed by close() -- without this,
        # quitting with a large 7Z open would strand gigabytes in TEMP
        # until the OS eventually cleaned it up. The same applies to a
        # self-extracting archive whose payload had to be carved out.
        self.project.close_source()

        super().closeEvent(event)

    def _update_title(self):
        if self.project.save_path:
            name = os.path.basename(self.project.save_path)
        elif self._suggested_name:
            name = self._suggested_name
        else:
            name = "Untitled"
        star = "*" if self.project.dirty else ""
        lock = " 🔒" if self.project.password else ""
        self.setWindowTitle(f"{name}{star}{lock} — {APP_NAME}")
        # "Rename Disc" only makes sense for ISO: archives have no
        # volume-label concept, so the action is hidden rather than
        # offered-then-ignored.
        is_iso = (self.project.fmt is None
                  or formats.is_disc_image(self.project.fmt))
        self.rename_volume_act.setVisible(is_iso)

    def _recent_list(self):
        return self.settings.value("recentFiles", []) or []

    def _add_recent(self, path):
        items = [p for p in self._recent_list() if p != path]
        items.insert(0, path)
        self.settings.setValue("recentFiles", items[:8])
        self._populate_recent_menu()

    def _populate_recent_menu(self):
        self.recent_menu.clear()
        items = self._recent_list()
        if not items:
            empty = QAction("(No recent files)", self)
            empty.setEnabled(False)
            self.recent_menu.addAction(empty)
            return
        for p in items:
            a = QAction(p, self)
            a.triggered.connect(lambda checked=False, path=p: self._open_path(path))
            self.recent_menu.addAction(a)

    def _confirm_discard_if_dirty(self) -> bool:
        if not self.project.dirty:
            return True
        resp = QMessageBox.question(
            self, APP_NAME,
            "You have unsaved changes. Discard them?",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        return resp == QMessageBox.Discard

    def _run_task(self, title, fn, *args, on_success=None, on_error=None,
                  on_cancelled=None, **kwargs):
        """Runs `fn` on a background `TaskWorker`, showing a modal
        progress dialog until it finishes.

        A Cancel button is shown ONLY if `fn` declares a `cancel_token`
        parameter (checked via `workers._accepts_cancel_token`) -- see
        `ProgressDialog`'s docstring for why. When shown, clicking it (or
        closing the dialog) sets the worker's own `CancelToken`, and the
        dialog stays open in a "Cancelling..." state until the worker
        thread actually unwinds and emits `cancelled`.
        """
        cancellable = _accepts_cancel_token(fn)
        dlg = ProgressDialog(self, title, cancellable=cancellable)
        worker = TaskWorker(fn, *args, **kwargs)
        self._active_worker = worker

        def release_worker():
            # The worker reports its result from INSIDE run(), so the thread
            # is still finishing when these slots execute. Dropping the last
            # reference to a QThread that has not exited yet destroys it
            # mid-flight, which crashes the application (reproducible when
            # two tasks run back to back). wait() returns as soon as run()
            # has returned -- effectively immediately -- and makes releasing
            # it safe.
            worker.wait()
            self._active_worker = None

        def handle_progress(text, done, total):
            dlg.update_progress(text, done, total)

        def handle_success(result):
            dlg.accept()
            release_worker()
            if on_success:
                on_success(result)

        def handle_failure(message):
            dlg.reject()
            release_worker()
            if on_error:
                on_error(message)
            else:
                QMessageBox.critical(self, APP_NAME, f"Something went wrong:\n\n{message}")

        def handle_cancelled():
            dlg.reject()
            release_worker()
            if on_cancelled:
                on_cancelled()
            else:
                # No error dialog: the user asked for this, so a quiet
                # status-bar note is enough -- an error box here would
                # misrepresent a deliberate cancellation as something
                # having gone wrong.
                self.status_left.setText("Cancelled.")

        worker.progress.connect(handle_progress)
        worker.finished_ok.connect(handle_success)
        worker.failed.connect(handle_failure)
        worker.cancelled.connect(handle_cancelled)
        if cancellable:
            dlg.cancel_requested.connect(worker.cancel_token.cancel)
        worker.start()
        dlg.exec()

    def _check_long_paths(self, scan_roots=None, reset_seen: bool = False) -> None:
        # Path-length ceilings are an ISO9660/UDF concern; the archive
        # formats handle long paths without complaint, so the advisory is
        # simply not relevant for archive projects.
        if self.project.is_archive:
            return
        if reset_seen:
            self._warned_long_paths.clear()

        roots = list(scan_roots) if scan_roots is not None else [self.project.root]
        offenders, seen_ids = [], set()
        for root in roots:
            for node, length in find_long_paths(root, LONG_PATH_WARN_THRESHOLD):
                if node.id in seen_ids:
                    continue
                seen_ids.add(node.id)
                offenders.append((node, length))

        new_offenders = [(n, l) for n, l in offenders if n.id not in self._warned_long_paths]
        if not new_offenders:
            return

        new_offenders.sort(key=lambda pair: -pair[1])
        for n, _ in new_offenders:
            self._warned_long_paths.add(n.id)

        max_shown = 5
        lines = [f"• {node.path()}\n   ({length} characters)"
                 for node, length in new_offenders[:max_shown]]
        more = len(new_offenders) - max_shown
        extra = f"\n\n...and {more} more item(s)." if more > 0 else ""

        QMessageBox.warning(
            self, APP_NAME,
            "Heads up — one or more paths in this project are getting long:\n\n"
            + "\n\n".join(lines) + extra +
            "\n\nThis is just an advisory notice. Rock Ridge and UDF support "
            "paths up to roughly 1023-1024 bytes, and the ISO will still "
            "build normally regardless.",
        )

    # --------------------------------------------------------- tree/table
    def _refresh_all(self):
        self._refresh_tree()
        self._refresh_table()
        self._update_status()
        self._update_title()

    def _node_icon(self, node: VNode) -> QIcon:
        return icons.icon_open_folder() if node.is_dir else QIcon(icons.file_pixmap())

    def _container_icon(self, size: int = 64) -> QIcon:
        return icons.container_logo(self.project.fmt, size,
                                    locked=bool(self.project.password))

    def _refresh_tree(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        root_item = QTreeWidgetItem([self._display_root_name()])
        root_item.setIcon(0, self._container_icon())
        root_item.setData(0, Qt.UserRole, self.project.root)
        self.tree.addTopLevelItem(root_item)

        def add_children(parent_item, node: VNode):
            for child in sorted(node.children, key=lambda c: c.name.lower()):
                if not child.is_dir:
                    continue
                item = QTreeWidgetItem([child.name])
                item.setIcon(0, icons.icon_open_folder())
                item.setData(0, Qt.UserRole, child)
                parent_item.addChild(item)
                add_children(item, child)

        add_children(root_item, self.project.root)
        root_item.setExpanded(True)
        self._select_tree_node(self.project.current_dir)
        self.tree.blockSignals(False)

    def _display_root_name(self) -> str:
        if self.project.volume_label:
            return self.project.volume_label
        if self.project.save_path:
            return os.path.basename(self.project.save_path)
        if self._suggested_name:
            return self._suggested_name
        return "Untitled"

    def _select_tree_node(self, node: VNode):
        def find(item):
            if item.data(0, Qt.UserRole) is node:
                return item
            for i in range(item.childCount()):
                found = find(item.child(i))
                if found:
                    return found
            return None

        root_item = self.tree.topLevelItem(0)
        if root_item is None:
            return
        found = find(root_item)
        if found:
            self.tree.setCurrentItem(found)

    def _refresh_table(self):
        self.table.setRowCount(0)
        children = self.project.current_dir.sorted_children()
        self.table.setRowCount(len(children))
        for row, node in enumerate(children):
            name_item = QTableWidgetItem(node.name)
            name_item.setIcon(self._node_icon(node))
            name_item.setData(Qt.UserRole, node)
            size_item = QTableWidgetItem("" if node.is_dir else human_size(node.size))
            size_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            type_item = QTableWidgetItem(
                "File folder" if node.is_dir else self._file_type_label(node.name))
            mod_item = QTableWidgetItem(human_time(node.mtime))
            self.table.setItem(row, 0, name_item)
            self.table.setItem(row, 1, size_item)
            self.table.setItem(row, 2, type_item)
            self.table.setItem(row, 3, mod_item)
        self.path_edit.setText(self.project.current_dir.path())
        self.back_btn.setEnabled(self.project.can_go_back())
        self.forward_btn.setEnabled(self.project.can_go_forward())
        self.up_btn.setEnabled(self.project.current_dir.parent is not None)

    @staticmethod
    def _file_type_label(name: str) -> str:
        ext = os.path.splitext(name)[1].lstrip(".").upper()
        return f"{ext} File" if ext else "File"

    def _selected_nodes(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()})
        nodes = []
        for r in rows:
            item = self.table.item(r, 0)
            if item is not None:
                nodes.append(item.data(Qt.UserRole))
        return nodes

    def _update_status(self):
        total_objs = self.project.total_objects()
        total_size = human_size(self.project.root.total_size())
        kind = formats.format_label(self.project.fmt) if self.project.fmt else ""
        prefix = f"{kind}  —  " if kind else ""
        self.status_left.setText(
            f"{prefix}Total {total_objs} object(s), {total_size}")
        sel = self._selected_nodes()
        if sel:
            sel_size = human_size(sum(n.total_size() for n in sel))
            self.status_right.setText(f"Selected {len(sel)} object(s), {sel_size}")
        else:
            self.status_right.setText("")

    # ------------------------------------------------------------ nav
    def _navigate(self, node: VNode):
        self.project.navigate_to(node)
        self._refresh_table()
        self._select_tree_node(node)
        self._update_status()

    def _go_back(self):
        if self.project.go_back():
            self._refresh_table()
            self._select_tree_node(self.project.current_dir)

    def _go_forward(self):
        if self.project.go_forward():
            self._refresh_table()
            self._select_tree_node(self.project.current_dir)

    def _go_up(self):
        if self.project.go_up():
            self._refresh_table()
            self._select_tree_node(self.project.current_dir)

    def _on_path_entered(self):
        text = self.path_edit.text().strip()
        node = self._resolve_path(text)
        if node is None:
            QMessageBox.warning(self, APP_NAME, f"Path not found: {text}")
            self.path_edit.setText(self.project.current_dir.path())
            return
        self._navigate(node)

    def _resolve_path(self, text: str):
        parts = [p for p in text.replace("\\", "/").split("/") if p]
        node = self.project.root
        for part in parts:
            nxt = node.find_child(part, is_dir=True)
            if nxt is None:
                return None
            node = nxt
        return node

    def _on_tree_clicked(self, item, _col):
        node = item.data(0, Qt.UserRole)
        if node is not None:
            self._navigate(node)

    def _on_table_double_clicked(self, item):
        row_item = self.table.item(item.row(), 0)
        node = row_item.data(Qt.UserRole)
        if node.is_dir:
            self._navigate(node)

    # ------------------------------------------------------------- D&D
    def _table_drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _table_drag_move(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _table_drop(self, event):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.toLocalFile()]
        event.acceptProposedAction()
        self._add_local_paths(paths)

    def _add_local_paths(self, paths):
        added, new_nodes = 0, []
        for path in paths:
            try:
                if os.path.isdir(path):
                    name = self.project.current_dir.unique_name(
                        os.path.basename(os.path.normpath(path)))
                    child = self.project.current_dir.new_child(name, is_dir=True)
                    iso_backend.populate_from_disk(child, path)
                    new_nodes.append(child)
                elif os.path.isfile(path):
                    name = self.project.current_dir.unique_name(os.path.basename(path))
                    child = self.project.current_dir.new_child(name, is_dir=False)
                    child.source_kind = "disk"
                    child.source_path = path
                    child.size = os.path.getsize(path)
                    new_nodes.append(child)
                else:
                    continue
                added += 1
            except Exception as exc:
                QMessageBox.warning(self, APP_NAME, str(exc))
        if added:
            self.project.mark_dirty()
            self._refresh_all()
            self._check_long_paths(scan_roots=new_nodes)
        return added

    # ------------------------------------------------------ context menus
    def _on_tree_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        self.tree.setCurrentItem(item)
        node = item.data(0, Qt.UserRole)

        menu = QMenu(self)
        menu.addAction(self.act_add_files)
        menu.addAction(self.act_add_folder)
        menu.addAction(self.act_new_folder)
        menu.addSeparator()
        menu.addAction(self.act_extract)

        if node is self.project.root:
            if self.project.fmt is None or formats.is_disc_image(self.project.fmt):
                rename_act = QAction(icons.icon_rename(), "Rename Disc...", self)
                rename_act.triggered.connect(self._rename_volume_flow)
                menu.addAction(rename_act)
        else:
            rename_act = QAction(icons.icon_rename(), "Rename", self)
            rename_act.triggered.connect(lambda checked=False, n=node: self._rename_node_flow(n))
            menu.addAction(rename_act)
            delete_act = QAction(icons.icon_delete(), "Delete", self)
            delete_act.triggered.connect(lambda checked=False, n=node: self._delete_nodes_flow([n]))
            menu.addAction(delete_act)

        menu.addSeparator()
        menu.addAction(self.act_properties)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_table_context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction(self.act_add_files)
        menu.addAction(self.act_add_folder)
        menu.addAction(self.act_new_folder)
        if self._selected_nodes():
            menu.addSeparator()
            menu.addAction(self.act_extract)
            menu.addAction(self.act_rename)
            menu.addAction(self.act_delete)
            menu.addSeparator()
            menu.addAction(self.act_properties)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # =========================================================== ACTIONS
    def on_new(self):
        if not self._confirm_discard_if_dirty():
            return
        self.project.close_source()
        self.project = ForgeProject()
        self._suggested_name = ""
        self._warned_long_paths.clear()
        self._refresh_all()

    def on_open(self):
        if not self._confirm_discard_if_dirty():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Disc Image or Archive", filter=formats.open_dialog_filter())
        if path:
            self._open_path(path)

    def _open_path(self, path: str):
        if not os.path.exists(path):
            QMessageBox.warning(self, APP_NAME, f"File not found:\n{path}")
            return

        fmt = formats.detect_format(path)
        if fmt is None:
            QMessageBox.warning(
                self, APP_NAME,
                f"'{os.path.basename(path)}' isn't a file type ISOForge "
                "recognises.\n\nSupported: .iso, .chd, .zip, .7z, .rar, .exe")
            return

        if formats.is_archive(fmt):
            self._open_archive_path(path, fmt)
        elif fmt == formats.CHD:
            self._open_chd_path(path)
        else:
            self._open_iso_path(path)

    def _open_iso_path(self, path: str):
        def do_open(progress_cb=None):
            if progress_cb:
                progress_cb("Reading directory structure...", 0, 1)
            source = iso_backend.open_iso(path)
            project = ForgeProject()
            project.source = source
            project.fmt = formats.ISO
            project.save_path = path
            project.volume_label = os.path.splitext(os.path.basename(path))[0].upper()[:32]
            source.populate(project.root)
            project.current_dir = project.root
            if progress_cb:
                progress_cb("Done", 1, 1)
            return project

        self._run_task(f"Opening {os.path.basename(path)}", do_open,
                       on_success=self._adopt_project(path))

    def _open_chd_path(self, path: str):
        """Opens a CHD by unpacking it to a temporary ISO (see `chd_backend`).

        Unpacking a disc can take a while, so it runs behind the progress
        dialog with a working Cancel -- `do_open` declares `cancel_token`,
        which is what makes `_run_task` offer the button. If anything goes
        wrong after the temporary image exists, it is released here so a
        failed open never leaves gigabytes in the temp folder.
        """
        if not self._ensure_chdman("Opening a CHD file"):
            return

        def do_open(progress_cb=None, cancel_token=None):
            source = chd_backend.open_chd(
                path, progress_cb=progress_cb, cancel_token=cancel_token)
            try:
                project = ForgeProject()
                project.source = source
                project.fmt = formats.CHD
                project.save_path = path
                project.volume_label = os.path.splitext(
                    os.path.basename(path))[0].upper()[:32]
                source.populate(project.root)
                project.current_dir = project.root
            except BaseException:
                source.close()
                raise
            return project

        self._run_task(f"Opening {os.path.basename(path)}", do_open,
                       on_success=self._adopt_project(path))

    def _open_archive_path(self, path: str, fmt: str):
        """Opening an archive happens in two stages.

        The handle is created and any password is negotiated HERE, on the
        UI thread, because password prompting is inherently interactive
        and cannot happen inside a worker thread. Only the potentially
        slow part -- walking the member list and building the VFS tree --
        is then handed off to the worker.
        """
        try:
            source = archive_backend.open_archive(path, fmt)
        except archive_backend.ArchiveBackendError as exc:
            self._report_archive_open_failure(fmt, exc)
            return

        if source.needs_password:
            if not self._prompt_until_unlocked(source, os.path.basename(path)):
                source.close()
                return

        def do_open(progress_cb=None):
            if progress_cb:
                progress_cb("Reading archive contents...", 0, 1)
            project = ForgeProject()
            project.source = source
            project.fmt = fmt
            project.save_path = path
            project.password = source.password
            source.populate(project.root)
            project.current_dir = project.root
            if progress_cb:
                progress_cb("Done", 1, 1)
            return project

        self._run_task(f"Opening {os.path.basename(path)}", do_open,
                       on_success=self._adopt_project(path))

    def _report_archive_open_failure(self, fmt: str, exc: Exception) -> None:
        """Shows an open failure, offering Settings when a missing RAR
        tool is the likely cause -- the one failure here the user can fix
        themselves in two clicks."""
        message = str(exc)
        if fmt == formats.RAR and not rar_tools.can_extract_rar():
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(APP_NAME)
            box.setText(message)
            settings_btn = box.addButton("Open Settings...", QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            if box.clickedButton() is settings_btn:
                self.on_settings()
            return
        QMessageBox.critical(self, APP_NAME, message)

    def _adopt_project(self, path: str):
        """Returns the on_success handler shared by both open paths."""
        def handler(project):
            self.project.close_source()
            self.project = project
            self._suggested_name = ""
            self._warned_long_paths.clear()
            self._add_recent(path)
            self._refresh_all()
            self.setWindowIcon(self._container_icon())
        return handler

    def _prompt_until_unlocked(self, source, display_name: str) -> bool:
        """Loops the password prompt until it's accepted or cancelled.

        `try_password()` verifies by really decrypting a member, so a
        wrong entry is caught here rather than producing a corrupt
        extraction much later.
        """
        message = ""
        while True:
            dlg = PasswordPromptDialog(self, display_name, message)
            if dlg.exec() != QDialog.Accepted:
                return False
            pw = dlg.password()
            if not pw:
                message = "Please enter a password."
                continue
            if source.try_password(pw):
                return True
            message = "That password didn't work. Please try again."

    # ------------------------------------------------------------- save
    def on_save(self):
        if not self.project.save_path:
            self.on_save_as()
            return

        fmt = self.project.fmt or formats.detect_format(self.project.save_path) or formats.ISO
        if not formats.can_write(fmt):
            # Only RAR (no Rar.exe) or SFX (no stub) can land here.
            # Offer the fix first, then the fallback -- steering the user
            # to a working save in the format they asked for is better
            # than quietly changing it.
            if not self._offer_tool_setup(fmt):
                return
            if not formats.can_write(fmt):
                suggested = formats.suggested_write_format(fmt)
                resp = QMessageBox.question(
                    self, APP_NAME,
                    f"Save this as a {formats.format_label(suggested)} instead?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
                )
                if resp != QMessageBox.Yes:
                    return
                stem = os.path.splitext(self.project.save_path)[0]
                self._save_as_with_format(
                    stem + formats.primary_extension(suggested), suggested)
                return

        self._save_to(self.project.save_path, fmt, reuse_password=True)

    def _offer_tool_setup(self, fmt: str) -> bool:
        """Explains why a format can't be written and offers to fix it.

        Returns True if the user visited Settings or chose to change
        format (so the caller should re-check capability), False if they
        declined outright.

        Both cases are a missing external binary, but the remedies differ
        enough to deserve their own wording: RAR needs a specific
        proprietary tool the user may not own, whereas an SFX stub comes
        free with 7-Zip.
        """
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(APP_NAME)

        if fmt == formats.SFX:
            box.setText(
                "Self-extracting archives can't be created yet.\n\n"
                "One is built by attaching your files to a small stub "
                "program, and ISOForge needs a copy of that stub. The "
                "easiest source is 7-Zip, which is free and includes "
                "suitable modules.\n\n"
                "Install 7-Zip and ISOForge will find one automatically, "
                "or point it at a module yourself."
            )
        elif fmt == formats.CHD:
            box.setText(
                "CHD files can't be created yet.\n\n"
                "Saving as CHD uses MAME's chdman.exe, which is free but "
                "can't be bundled with ISOForge.\n\n"
                "Point ISOForge at chdman.exe to enable saving as CHD."
            )
        else:
            box.setText(
                f"{formats.format_label(fmt)} files can't be created yet.\n\n"
                "The RAR compression algorithm is proprietary to RARLAB, so "
                "ISOForge can't create RAR archives on its own — but it can "
                "use RARLAB's Rar.exe if you have it (it ships with WinRAR).\n\n"
                "Point ISOForge at Rar.exe to enable saving as RAR."
            )

        settings_btn = box.addButton("Open Settings...", QMessageBox.AcceptRole)
        other_btn = box.addButton("Save as Another Format", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()

        clicked = box.clickedButton()
        if clicked is settings_btn:
            self.on_settings()
            return True
        if clicked is other_btn:
            return True
        return False

    def on_save_as(self):
        current_fmt = self.project.fmt or formats.ISO
        target_fmt = formats.suggested_write_format(current_fmt)

        if self.project.save_path:
            default = self.project.save_path
            if not formats.can_write(current_fmt):
                default = os.path.splitext(self.project.save_path)[0] + \
                    formats.primary_extension(target_fmt)
        elif self._suggested_name:
            # Came from Explorer's "Compress with ISOForge..." -- default
            # to the selection's own name next to where it came from.
            default = self._suggested_name + formats.primary_extension(target_fmt)
        else:
            default = formats.default_name_for(target_fmt)

        path, selected_filter = QFileDialog.getSaveFileName(
            self, "Save As", default, filter=formats.save_dialog_filter())
        if not path:
            return

        fmt = formats.detect_format(path) or \
            formats.format_from_save_filter(selected_filter) or target_fmt

        # Typing "x.rar" or "x.exe" by hand is possible even when that
        # format isn't writable, so re-check rather than trusting the
        # filter to have excluded it.
        if not formats.can_write(fmt):
            if not self._offer_tool_setup(fmt):
                return
            if not formats.can_write(fmt):
                fmt = formats.suggested_write_format(fmt)
                path = os.path.splitext(path)[0] + formats.primary_extension(fmt)

        self._save_as_with_format(formats.ensure_extension(path, fmt), fmt)

    def _save_as_with_format(self, path: str, fmt: str):
        self._save_to(path, fmt, reuse_password=False)

    def _save_to(self, path: str, fmt: str, reuse_password: bool):
        self._check_long_paths(reset_seen=True)

        password = None
        if formats.supports_write_encryption(fmt):
            if reuse_password and self.project.password:
                # Re-saving an already-encrypted archive keeps its
                # password silently rather than re-asking every time.
                password = self.project.password
            else:
                dlg = EncryptionPromptDialog(
                    self, formats.format_label(fmt),
                    initial_password=self.project.password)
                if dlg.exec() != QDialog.Accepted:
                    return
                password = dlg.password()

        source = self.project.source
        overwriting_open_source = (
            source is not None
            and not source.is_closed
            and _same_file(path, getattr(source, "iso_path", "") or "")
        )
        source_to_release = source if overwriting_open_source else None

        if formats.is_archive(fmt):
            def do_save(progress_cb=None, cancel_token=None):
                result = archive_backend.build_archive(
                    self.project.root, path, fmt,
                    password=password,
                    progress_cb=progress_cb,
                    source_to_close_before_replace=source_to_release,
                    cancel_token=cancel_token,
                )
                return path, fmt, password, result
        elif fmt == formats.CHD:
            def do_save(progress_cb=None, cancel_token=None):
                # The ISO is built from the project's own sources, never
                # from the CHD on disk, so overwriting the CHD that is
                # currently open is safe -- nothing needs releasing first.
                result = chd_backend.build_chd(
                    self.project.root, path,
                    volume_label=self.project.volume_label,
                    progress_cb=progress_cb,
                    cancel_token=cancel_token,
                )
                return path, fmt, None, result
        else:
            def do_save(progress_cb=None, cancel_token=None):
                result = iso_backend.build_iso(
                    self.project.root, path,
                    volume_label=self.project.volume_label,
                    progress_cb=progress_cb,
                    source_to_close_before_replace=source_to_release,
                    cancel_token=cancel_token,
                )
                return path, fmt, None, result

        def on_success(payload):
            result_path, saved_fmt, used_pw, result = payload

            if overwriting_open_source:
                # The bytes under the old handle have just been replaced,
                # so re-open from the file we actually wrote.
                self.project.source = None
                try:
                    self._reload_source_after_save(result_path, saved_fmt, used_pw)
                except Exception:
                    # A failed reload must never undo an otherwise
                    # successful save -- the file on disk is correct
                    # either way.
                    pass

            self.project.save_path = result_path
            self.project.fmt = saved_fmt
            self.project.password = used_pw
            self.project.dirty = False
            self._suggested_name = ""
            self._add_recent(result_path)
            self._refresh_all()
            self.setWindowIcon(self._container_icon())

            note = ""
            if getattr(result, "used_fallback_format", False):
                note = f"  —  format: {result.format_label}"
            elif saved_fmt in (formats.SFX, formats.CHD) or used_pw:
                # Worth stating outright for SFX: the label names the
                # payload format, which decides who else can open it.
                note = f"  —  {result.format_label}"
            self.status_left.setText(f"Saved to {result_path}{note}")
            self._report_build_result(result)

        def on_cancelled():
            # A cancelled Save leaves the ORIGINAL file untouched -- both
            # backends build into a temp file first and only replace the
            # real target on success, so there's nothing to undo here
            # beyond letting the user know nothing changed.
            self.status_left.setText("Save cancelled — the original file was not changed.")

        self._run_task(f"Building {os.path.basename(path)}", do_save,
                       on_success=on_success, on_cancelled=on_cancelled)

    def _reload_source_after_save(self, path: str, fmt: str, password) -> None:
        if formats.is_archive(fmt):
            fresh = archive_backend.open_archive(path, fmt)
            if password:
                fresh.try_password(password)
        else:
            fresh = iso_backend.open_iso(path)
        reloaded = ForgeProject()
        fresh.populate(reloaded.root)
        self.project.root = reloaded.root
        self.project.current_dir = self.project.root
        self.project.source = fresh

    def _report_build_result(self, result) -> None:
        """Shows a dialog ONLY when content had to be left out.

        A format fallback is deliberately NOT reported here -- it means
        the adaptive-format system worked as designed and produced a
        complete, valid file, so it's surfaced quietly in the status bar
        instead of interrupting a successful save.
        """
        skipped = getattr(result, "skipped", None) or []
        if not skipped:
            return
        max_shown = 8
        lines = [f"• {display_path}" for display_path, _reason in skipped[:max_shown]]
        more = len(skipped) - max_shown
        extra = f"\n\n...and {more} more item(s)." if more > 0 else ""
        QMessageBox.warning(
            self, APP_NAME,
            "The file was saved, but the following item(s) could not be "
            "read from the original source and were left out:\n\n"
            + "\n".join(lines) + extra +
            "\n\nEverything else was copied successfully.",
        )

    # =================================================== Explorer verbs
    def handle_shell_request(self, request) -> bool:
        """Performs one (already merged) context-menu action.

        Returns True when the caller should quit afterwards -- i.e. this
        was a headless extract or CHD conversion with nothing left for the
        user to do.
        """
        if request is None:
            return False
        if request.action == shell_commands.COMPRESS:
            self.stage_for_compression(request.paths)
            return False
        if request.action == shell_commands.OPEN:
            self.show()
            self.raise_()
            self._open_path(request.paths[0])
            return False
        if request.action == shell_commands.CONVERT_CHD:
            return self.run_shell_chd_convert(request)
        return self.run_shell_extract(request)

    def stage_for_compression(self, paths) -> None:
        """Loads Explorer's selection into a fresh project, ready to save.

        Deliberately does NOT force a Save As dialog: the user still has
        to choose a format, and possibly a password, and may want to
        rearrange things first. Everything is staged and the next move is
        theirs.
        """
        self.show()
        self.raise_()

        existing = [p for p in paths if os.path.exists(p)]
        missing = [p for p in paths if not os.path.exists(p)]

        if not existing:
            QMessageBox.warning(
                self, APP_NAME,
                "None of the selected items could be found.")
            return

        # If this window is ALREADY holding an unsaved selection that came
        # from Explorer, these files belong to it: they are the rest of the
        # same selection, arriving a moment after the first batch was acted
        # on (a slow-starting process, a busy machine). Adding them here is
        # what keeps one selection in one session -- and avoids the worse
        # alternative of asking the user to "discard" the files they have
        # only just selected. `_suggested_name` is only ever set in exactly
        # this state: it is cleared by New, Open and a successful Save.
        joining = (bool(self._suggested_name)
                   and self.project.save_path is None
                   and self.project.source is None)

        if joining:
            known = {os.path.normcase(os.path.abspath(q))
                     for q in self._shell_staged_paths}
            existing = [q for q in existing
                        if os.path.normcase(os.path.abspath(q)) not in known]
            if not existing:
                return          # nothing new -- a duplicate delivery
        else:
            if self.project.dirty or self.project.source is not None:
                # An already-busy window shouldn't silently discard whatever
                # is open, so the new selection goes into a clean project
                # only once the current one is safe to replace.
                if not self._confirm_discard_if_dirty():
                    return
                self.project.close_source()
                self.project = ForgeProject()
                self._warned_long_paths.clear()
            self._shell_staged_paths = []

        self._shell_staged_paths.extend(existing)
        self._suggested_name = shell_commands.suggested_archive_name(
            self._shell_staged_paths)
        added = self._add_local_paths(existing)
        self._refresh_all()

        if missing:
            QMessageBox.warning(
                self, APP_NAME,
                f"{len(missing)} selected item(s) could not be found and "
                "were skipped.")

        total = len(self._shell_staged_paths)
        self.status_left.setText(
            f"Added {added} item(s) — choose Save As to pick a format."
            if total == added else
            f"Added {added} more item(s), {total} in all — choose Save As "
            "to pick a format.")

    def run_shell_extract(self, request) -> bool:
        """Extracts one or more archives without showing the editor.

        Each archive is handled in turn: opened, unlocked if needed, and
        unpacked with the usual progress dialog. A single summary at the
        end reports what happened, so extracting ten archives doesn't
        mean dismissing ten dialogs.

        Returns True so the caller quits -- this window was never shown
        and has nothing further to offer.
        """
        succeeded, failed = [], []

        for archive_path in request.paths:
            try:
                destination = self._extract_one(request.action, archive_path)
                if destination:
                    succeeded.append(destination)
            except Exception as exc:
                failed.append((os.path.basename(archive_path), str(exc)))

        self._report_extract_outcome(succeeded, failed)
        return True

    def _extract_one(self, action: str, archive_path: str):
        """Extracts a single archive. Returns the destination, or None if
        the user cancelled at the password prompt OR clicked Cancel on
        the progress dialog partway through."""
        if not os.path.exists(archive_path):
            raise RuntimeError("File not found.")

        fmt = formats.detect_format(archive_path)
        if fmt is None:
            raise RuntimeError("Not a file type ISOForge recognises.")

        if formats.is_archive(fmt):
            source = archive_backend.open_archive(archive_path, fmt)
            if source.needs_password:
                if not self._prompt_until_unlocked(
                        source, os.path.basename(archive_path)):
                    source.close()
                    return None
        else:
            source = iso_backend.open_iso(archive_path)

        destination = shell_commands.destination_for(action, archive_path)
        if action == shell_commands.EXTRACT_TO:
            # Never merge two archives into one folder by accident.
            destination = shell_commands.unique_directory(destination)

        try:
            root = VNode(name="", is_dir=True)
            source.populate(root)
            nodes = list(root.children)
            if not nodes:
                raise RuntimeError("The archive is empty.")

            os.makedirs(destination, exist_ok=True)
            use_archive = formats.is_archive(fmt)
            outcome = {}

            def do_extract(progress_cb=None, cancel_token=None):
                for node in nodes:
                    if use_archive:
                        archive_backend.extract_node(
                            node, destination, progress_cb=progress_cb,
                            cancel_token=cancel_token)
                    else:
                        iso_backend.extract_node(
                            node, destination, progress_cb=progress_cb,
                            cancel_token=cancel_token)
                return destination

            self._run_task(
                f"Extracting {os.path.basename(archive_path)}",
                do_extract,
                on_success=lambda _r: None,
                # Captured rather than shown: the caller collects every
                # failure and reports them together at the end.
                on_error=lambda msg: outcome.setdefault("message", msg),
                on_cancelled=lambda: outcome.setdefault("cancelled", True),
            )

            if outcome.get("cancelled"):
                # Treated the same way as declining the password prompt:
                # nothing to report as successfully extracted. Anything
                # already written to `destination` before the click stays
                # on disk, which is an inherent, unavoidable consequence
                # of stopping a file-by-file extraction partway through.
                return None
            if "message" in outcome:
                raise RuntimeError(outcome["message"])
            return destination
        finally:
            try:
                source.close()
            except Exception:
                pass

    def _report_extract_outcome(self, succeeded, failed) -> None:
        if succeeded and not failed:
            if len(succeeded) == 1:
                QMessageBox.information(
                    self, APP_NAME, f"Extracted to:\n{succeeded[0]}")
            else:
                QMessageBox.information(
                    self, APP_NAME,
                    f"Extracted {len(succeeded)} archives.\n\n"
                    + "\n".join(succeeded[:8]))
            return

        if failed and not succeeded:
            lines = [f"• {name}: {reason}" for name, reason in failed[:6]]
            QMessageBox.critical(
                self, APP_NAME,
                "Nothing could be extracted:\n\n" + "\n".join(lines))
            return

        if failed:
            lines = [f"• {name}: {reason}" for name, reason in failed[:6]]
            QMessageBox.warning(
                self, APP_NAME,
                f"Extracted {len(succeeded)} archive(s), but "
                f"{len(failed)} failed:\n\n" + "\n".join(lines))

    # ================================================== CHD conversion
    def _ensure_chdman(self, purpose: str = "Converting to CHD") -> bool:
        """True if chdman.exe can be found; otherwise explains what's
        needed and offers Settings, then re-checks.

        Returns False when the user declines (or Settings still doesn't
        yield a working path), so every caller simply stops. Shared by the
        toolbar action and the Explorer verb -- the second can run with the
        main window hidden, which is fine: the message box and the Settings
        dialog both work with a hidden parent, exactly as the extract
        verbs' password prompts already do.
        """
        if chd_tools.can_convert():
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle(APP_NAME)
        box.setText(
            f"{purpose} needs chdman.exe.\n\n"
            "It ships with MAME (free). Point ISOForge at your copy under "
            "Tools > Settings and CHD support is enabled straight away, or "
            "drop chdman.exe into the ISOForge folder."
        )
        settings_btn = box.addButton("Open Settings...", QMessageBox.AcceptRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is settings_btn:
            self.on_settings()
            return chd_tools.can_convert()
        return False

    def on_convert_chd(self):
        """Toolbar / Tools menu: convert one disc image to CHD."""
        if not self._ensure_chdman():
            return
        dlg = ChdConvertDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        src, out, preference = dlg.values()
        succeeded, failed, cancelled = self._execute_chd_jobs(
            [(src, out, preference)])
        if cancelled:
            # Nothing was written: convert_to_chd builds into a temp file
            # and removes it on cancel.
            self.status_left.setText("CHD conversion cancelled — nothing was written.")
            return
        self._report_chd_outcome(succeeded, failed)
        if succeeded:
            self.status_left.setText(f"Created {succeeded[0].output}")

    def _execute_chd_jobs(self, jobs):
        """Converts every (source, output, preference) in `jobs`, one after
        another, behind a single progress dialog.

        Returns (succeeded, failed, cancelled):
          * succeeded -- a list of `chd_tools.ChdResult`;
          * failed    -- a list of (file name, reason);
          * cancelled -- True if the user clicked Cancel.

        One file failing never stops the rest: each failure is recorded and
        the loop moves on, which is what makes a multi-file Explorer
        selection behave sensibly. Cancellation is different -- it ends the
        whole batch. (`OperationCancelled` is a BaseException, so it passes
        straight through the `except ChdToolError` below untouched.)
        """
        total_files = len(jobs)

        def do_convert(progress_cb=None, cancel_token=None):
            succeeded, failed = [], []
            for index, (src, out, preference) in enumerate(jobs):
                def report(label, done, total, index=index):
                    if progress_cb is None:
                        return
                    prefix = f"[{index + 1}/{total_files}] " if total_files > 1 else ""
                    overall = int((index * PROGRESS_SCALE + done) / total_files)
                    progress_cb(prefix + label, overall, PROGRESS_SCALE)
                try:
                    succeeded.append(chd_tools.convert_to_chd(
                        src, out, preference,
                        progress_cb=report, cancel_token=cancel_token))
                except chd_tools.ChdToolError as exc:
                    failed.append((os.path.basename(src), str(exc)))
            return succeeded, failed

        outcome = {}
        self._run_task(
            "Converting to CHD", do_convert,
            on_success=lambda result: outcome.update(result=result),
            # Captured rather than shown: the caller reports everything
            # together in one place.
            on_error=lambda message: outcome.update(error=message),
            on_cancelled=lambda: outcome.update(cancelled=True),
        )
        if outcome.get("cancelled"):
            return [], [], True
        if "error" in outcome:
            return [], [("CHD conversion", outcome["error"])], False
        succeeded, failed = outcome.get("result", ([], []))
        return succeeded, failed, False

    @staticmethod
    def _chd_result_line(result) -> str:
        line = os.path.basename(result.output)
        if result.input_size > 0 and result.output_size > 0:
            percent = result.output_size / result.input_size * 100
            line += (f":  {human_size(result.input_size)} → "
                     f"{human_size(result.output_size)}  ({percent:.0f}% of original)")
        return line

    def _report_chd_outcome(self, succeeded, failed) -> None:
        """One summary for the whole batch, so converting ten images doesn't
        mean dismissing ten dialogs."""
        if succeeded and not failed:
            if len(succeeded) == 1:
                QMessageBox.information(
                    self, APP_NAME,
                    f"Created:\n{succeeded[0].output}\n\n"
                    f"{self._chd_result_line(succeeded[0])}")
            else:
                shown = [f"• {self._chd_result_line(r)}" for r in succeeded[:8]]
                more = len(succeeded) - 8
                extra = f"\n...and {more} more." if more > 0 else ""
                QMessageBox.information(
                    self, APP_NAME,
                    f"Converted {len(succeeded)} disc images to CHD:\n\n"
                    + "\n".join(shown) + extra)
            return

        if failed and not succeeded:
            if len(failed) == 1:
                QMessageBox.critical(self, APP_NAME, failed[0][1])
            else:
                lines = [f"• {name}: {reason.splitlines()[0] if reason else ''}"
                         for name, reason in failed[:6]]
                QMessageBox.critical(
                    self, APP_NAME,
                    "Nothing could be converted:\n\n" + "\n".join(lines))
            return

        if failed:
            lines = [f"• {name}: {reason.splitlines()[0] if reason else ''}"
                     for name, reason in failed[:6]]
            QMessageBox.warning(
                self, APP_NAME,
                f"Converted {len(succeeded)} disc image(s), but "
                f"{len(failed)} failed:\n\n" + "\n".join(lines))

    def run_shell_chd_convert(self, request) -> bool:
        """Explorer's 'Convert to CHD with ISOForge' verb.

        Each CHD is written NEXT TO its source, named after it. An existing
        file is never overwritten -- a headless action has nobody to ask --
        so a clash picks `name (2).chd` instead. The disc type is
        automatic; use Tools > Convert to CHD when a DVD image needs to be
        forced one way or the other.

        Returns True so the caller quits: this window was never shown and
        has nothing further to offer.
        """
        if not self._ensure_chdman():
            return True

        jobs, failed, reserved = [], [], []
        for path in request.paths:
            try:
                chd_tools.validate_source(path)
            except chd_tools.ChdToolError as exc:
                failed.append((os.path.basename(path), str(exc)))
                continue
            out = chd_tools.unique_path(
                chd_tools.default_output_path(path), reserved)
            reserved.append(out)
            jobs.append((path, out, chd_tools.KIND_AUTO))

        succeeded = []
        if jobs:
            done, bad, cancelled = self._execute_chd_jobs(jobs)
            if cancelled:
                return True
            succeeded = done
            failed.extend(bad)

        self._report_chd_outcome(succeeded, failed)
        return True

    # ------------------------------------------------------------ misc
    def on_settings(self):
        """Tools > Settings — external tools and Explorer integration.

        Re-applying capabilities afterwards matters: enabling RAR or SFX
        changes the Save As filter list and the Convert Folder dialog's
        format options, and the menus should reflect that immediately
        rather than after a restart.
        """
        rar_path, unrar_path, sfx_path = settings_store.load(ORG_NAME, APP_NAME)
        chdman_path = settings_store.load_chdman(ORG_NAME, APP_NAME)
        dlg = SettingsDialog(self, rar_path, unrar_path, sfx_path, chdman_path)
        if dlg.exec() != QDialog.Accepted:
            return

        new_rar, new_unrar, new_sfx = dlg.values()
        before = {fmt: formats.can_write(fmt)
                  for fmt in (formats.RAR, formats.SFX)}
        settings_store.save(ORG_NAME, APP_NAME, new_rar, new_unrar, new_sfx)
        settings_store.save_chdman(ORG_NAME, APP_NAME, dlg.chdman_value())
        after = {fmt: formats.can_write(fmt)
                 for fmt in (formats.RAR, formats.SFX)}

        self._apply_shell_settings(dlg.shell_values())
        self._refresh_all()
        self._report_capability_changes(before, after)

    def _report_capability_changes(self, before: dict, after: dict) -> None:
        """Tells the user what just became possible -- or stopped being.

        Worth saying out loud: these changes alter the Save As list, and
        a silently-appearing menu entry is easy to miss.
        """
        gained = [f for f in after if after[f] and not before[f]]
        lost = [f for f in after if before[f] and not after[f]]

        messages = {
            formats.RAR: (
                "RAR support is now fully enabled — you can create and "
                "extract RAR archives, with optional password protection.",
                "Rar.exe is no longer available, so RAR archives can be "
                "opened but not created.",
            ),
            formats.SFX: (
                "Self-extracting archives are now available — choose "
                "\"Self-extracting archive (*.exe)\" in Save As.",
                "The SFX module is no longer available, so self-extracting "
                "archives can't be created.",
            ),
        }

        for fmt in gained:
            QMessageBox.information(self, APP_NAME, messages[fmt][0])
        for fmt in lost:
            QMessageBox.warning(self, APP_NAME, messages[fmt][1])

    def _apply_shell_settings(self, values) -> None:
        """Writes the Explorer menu choices, reporting any failure.

        A registry write can fail on a locked-down machine, and that must
        not be silent -- otherwise the checkbox appears to have worked
        while nothing changed.
        """
        if values is None:
            return
        archives, create, chd = values
        try:
            if archives or create or chd:
                shell_integration.register(
                    archives=archives, create=create, chd=chd)
            else:
                shell_integration.unregister()
        except shell_integration.ShellIntegrationError as exc:
            QMessageBox.warning(
                self, APP_NAME,
                f"The Explorer menu could not be updated:\n\n{exc}")

    def on_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Files")
        if paths:
            self._add_local_paths(paths)

    def on_add_folder(self):
        path = QFileDialog.getExistingDirectory(self, "Add Folder")
        if path:
            self._add_local_paths([path])

    def on_new_folder(self):
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        if ok and name.strip():
            self.project.new_folder(self.project.current_dir, name.strip())
            self._refresh_all()

    def on_extract(self):
        nodes = self._selected_nodes()
        if not nodes:
            resp = QMessageBox.question(
                self, APP_NAME,
                "No items selected. Extract everything?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp != QMessageBox.Yes:
                return
            nodes = list(self.project.root.children)
        if not nodes:
            QMessageBox.information(self, APP_NAME, "There's nothing to extract.")
            return

        # An encrypted archive that was opened but never unlocked (e.g.
        # restored from Open Recent) must be unlocked before extraction,
        # otherwise every member would fail one by one.
        source = self.project.source_archive
        if source is not None and source.needs_password:
            if not self._prompt_until_unlocked(
                    source, os.path.basename(self.project.save_path or "archive")):
                return
            self.project.password = source.password

        dest = QFileDialog.getExistingDirectory(self, "Extract To")
        if not dest:
            return

        use_archive = self.project.is_archive

        def do_extract(progress_cb=None, cancel_token=None):
            for n in nodes:
                if use_archive:
                    archive_backend.extract_node(n, dest, progress_cb=progress_cb,
                                                 cancel_token=cancel_token)
                else:
                    iso_backend.extract_node(n, dest, progress_cb=progress_cb,
                                             cancel_token=cancel_token)
            return dest

        def on_success(dest_path):
            QMessageBox.information(self, APP_NAME, f"Extracted to:\n{dest_path}")

        self._run_task("Extracting...", do_extract, on_success=on_success)

    def _delete_nodes_flow(self, nodes):
        if not nodes:
            return
        names = ", ".join(n.name for n in nodes[:5]) + (", ..." if len(nodes) > 5 else "")
        resp = QMessageBox.question(
            self, APP_NAME,
            f"Delete {len(nodes)} item(s) from this project?\n\n{names}\n\n"
            "This only affects the project in ISOForge; nothing on your "
            "computer or in the original file is changed until you Save.",
            QMessageBox.Yes | QMessageBox.No,
        )
        if resp == QMessageBox.Yes:
            self.project.delete_nodes(nodes)
            self._refresh_all()

    def on_delete(self):
        self._delete_nodes_flow(self._selected_nodes())

    def _rename_node_flow(self, node: VNode):
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=node.name)
        if ok and new_name.strip() and new_name.strip() != node.name:
            if not self.project.rename_node(node, new_name.strip()):
                QMessageBox.warning(self, APP_NAME, "An item with that name already exists here.")
            else:
                self._refresh_all()
                self._check_long_paths(scan_roots=[node])

    def _rename_volume_flow(self):
        current = self._display_root_name()
        new_label, ok = QInputDialog.getText(self, "Rename Disc", "Disc name:", text=current)
        if ok and new_label.strip() and new_label.strip() != current:
            self.project.rename_volume(new_label.strip())
            self._refresh_all()

    def on_rename(self):
        nodes = self._selected_nodes()
        if len(nodes) > 1:
            QMessageBox.information(self, APP_NAME, "Please select a single item to rename.")
            return
        if len(nodes) == 1:
            self._rename_node_flow(nodes[0])
            return

        item = self.tree.currentItem()
        node = item.data(0, Qt.UserRole) if item else None
        if node is None:
            return
        if node is self.project.root:
            if self.project.fmt is None or formats.is_disc_image(self.project.fmt):
                self._rename_volume_flow()
        else:
            self._rename_node_flow(node)

    def on_properties(self):
        nodes = self._selected_nodes()
        if not nodes:
            root = self.project.root
            rows = [
                ("Name", self._display_root_name()),
                ("Format", formats.format_label(self.project.fmt)
                    if self.project.fmt else "New (unsaved)"),
                ("Encrypted", "Yes" if self.project.password else "No"),
                ("Folders", root.folder_count()),
                ("Files", root.file_count()),
                ("Total size", human_size(root.total_size())),
                ("Save location", self.project.save_path or "(not saved yet)"),
            ]
            if self.project.fmt == formats.RAR and not formats.can_write(formats.RAR):
                rows.append(
                    ("Note", "Read-only — set Rar.exe under Tools > Settings "
                             "to enable saving as RAR."))
            if self.project.fmt == formats.SFX and not formats.can_write(formats.SFX):
                rows.append(
                    ("Note", "Read-only — install 7-Zip, or set an SFX module "
                             "under Tools > Settings, to save as .exe."))
            if self.project.fmt == formats.CHD and not formats.can_write(formats.CHD):
                rows.append(
                    ("Note", "Read-only — set chdman.exe under Tools > Settings "
                             "to enable saving as CHD."))
            PropertiesDialog(self, "Properties", rows).exec()
            return

        total_size = sum(n.total_size() for n in nodes)
        files = sum(n.file_count() for n in nodes)
        folders = sum((1 + n.folder_count()) if n.is_dir else 0 for n in nodes)
        PropertiesDialog(self, "Properties", [
            ("Selected items", len(nodes)),
            ("Folders", folders),
            ("Files", files),
            ("Total size", human_size(total_size)),
        ]).exec()

    def on_convert(self):
        dlg = ConvertDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        src, out, label, fmt = dlg.values()
        if not os.path.isdir(src):
            QMessageBox.warning(self, APP_NAME, "The selected source folder does not exist.")
            return

        password = None
        if formats.supports_write_encryption(fmt):
            pw_dlg = EncryptionPromptDialog(self, formats.format_label(fmt))
            if pw_dlg.exec() != QDialog.Accepted:
                return
            password = pw_dlg.password()

        if formats.is_archive(fmt):
            def do_convert(progress_cb=None, cancel_token=None):
                result = archive_backend.quick_folder_to_archive(
                    src, out, fmt, password=password, progress_cb=progress_cb,
                    cancel_token=cancel_token)
                return out, result
        elif fmt == formats.CHD:
            def do_convert(progress_cb=None, cancel_token=None):
                result = chd_backend.quick_folder_to_chd(
                    src, out, volume_label=label, progress_cb=progress_cb,
                    cancel_token=cancel_token)
                return out, result
        else:
            def do_convert(progress_cb=None, cancel_token=None):
                result = iso_backend.quick_folder_to_iso(
                    src, out, volume_label=label, progress_cb=progress_cb,
                    cancel_token=cancel_token)
                return out, result

        def on_success(payload):
            out_path, result = payload
            QMessageBox.information(self, APP_NAME, f"Created:\n{out_path}")
            self._report_build_result(result)
            resp = QMessageBox.question(
                self, APP_NAME, "Open it in ISOForge now?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if resp == QMessageBox.Yes:
                self._open_path(out_path)

        self._run_task(f"Building {os.path.basename(out)}", do_convert,
                       on_success=on_success)

    def on_toggle_dark_mode(self, checked: bool) -> None:
        self.dark_mode = checked
        self.settings.setValue("darkMode", checked)
        icons.set_theme(checked)
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(theme.get_stylesheet(checked))
        self._rebuild_action_icons()
        self._refresh_all()

    def _rebuild_action_icons(self) -> None:
        self.act_new.setIcon(icons.icon_new_disc())
        self.act_open.setIcon(icons.icon_open_folder())
        self.act_save.setIcon(icons.icon_save())
        self.act_save_as.setIcon(icons.icon_save_as())
        self.act_add_files.setIcon(icons.icon_add_files())
        self.act_add_folder.setIcon(icons.icon_add_folder())
        self.act_new_folder.setIcon(icons.icon_new_folder())
        self.act_extract.setIcon(icons.icon_extract())
        self.act_delete.setIcon(icons.icon_delete())
        self.act_rename.setIcon(icons.icon_rename())
        self.act_properties.setIcon(icons.icon_properties())
        self.act_convert.setIcon(icons.icon_convert())
        self.act_convert_chd.setIcon(icons.icon_convert_chd())
        self.act_settings.setIcon(icons.icon_settings())
        self.act_help.setIcon(icons.icon_help())
        self.back_btn.setIcon(icons.icon_nav("left"))
        self.forward_btn.setIcon(icons.icon_nav("right"))
        self.up_btn.setIcon(icons.icon_nav("up"))
        self.refresh_btn.setIcon(icons.icon_refresh())

    def on_help(self):
        rar_state = (
            "<b>RAR support is fully enabled</b> — archives can be created "
            "and extracted."
            if formats.can_write(formats.RAR) else
            "<b>RAR is currently read-only.</b> Set <i>Rar.exe</i> under "
            "Tools &gt; Settings to enable creating them."
        )
        sfx_state = (
            "<b>Self-extracting archives are available</b> — choose "
            "<i>Self-extracting archive (*.exe)</i> in Save As."
            if formats.can_write(formats.SFX) else
            "<b>Self-extracting archives aren't available yet.</b> Install "
            "7-Zip (free), or set an SFX module under Tools &gt; Settings."
        )
        chd_state = (
            "<b>CHD conversion is available</b> — use <i>Convert to CHD</i> "
            "on the toolbar, or right-click a disc image in Explorer."
            if chd_tools.can_convert() else
            "<b>CHD conversion isn't available yet.</b> Set <i>chdman.exe</i> "
            "(it ships with MAME) under Tools &gt; Settings."
        )
        shell_state = ""
        if shell_integration.is_supported():
            shell_state = (
                "<h4>Explorer right-click menu</h4>"
                "<p>Under <b>Tools &gt; Settings</b> you can add ISOForge to "
                "Windows Explorer's context menu: right-click an archive for "
                "<i>Open / Extract Here / Extract to subfolder</i>, or "
                "right-click any files or folders for <i>Compress with "
                "ISOForge…</i>. Selecting several items groups them into one "
                "archive. A third option adds <i>Convert to CHD with "
                "ISOForge</i> to .cue, .gdi, .toc and .iso files.</p>"
                f"<p>{shell_integration.status_summary()}</p>"
            )
        text = (
            "<h3>ISOForge(ZR)</h3>"
            "<p>A modern, lightweight editor for <b>ISO</b> disc images and "
            "<b>ZIP</b>, <b>7Z</b>, <b>RAR</b> and <b>self-extracting</b> "
            "archives — all with the same open / create / edit / extract "
            "workflow.</p>"
            "<p><b>New</b> starts an empty project. <b>Open</b> loads an existing "
            "file. <b>Add Files/Add Folder</b> stage local content (you can also "
            "drag &amp; drop straight into the file list). <b>Extract</b> copies "
            "selected items back out. <b>Save/Save As</b> builds the final "
            "file.</p>"
            "<p>Deleting or renaming only changes the in-memory project — nothing "
            "is written until you choose Save.</p>"
            "<h4>Cancelling a long build or extract</h4>"
            "<p>Building or extracting a large archive shows a progress dialog "
            "with a <b>Cancel</b> button. Clicking it (or closing the dialog) "
            "stops the operation as soon as it safely can — any partial output "
            "file is removed, and the original file (when saving over one "
            "that already existed) is left completely untouched. On a very "
            "large build, cancelling can itself take a little while if a lot "
            "of data was just written to a slow, external, or network drive "
            "and Windows or antivirus software is still busy with it -- "
            "ISOForge explains this if it happens rather than looking "
            "frozen.</p>"
            "<h4>Passwords</h4>"
            "<p>Opening a password-protected archive prompts for its password, "
            "which is verified immediately rather than failing later. When "
            "saving, you're offered optional password protection — "
            "<b>AES-256</b> for ZIP, 7Z and self-extracting archives, and RAR's "
            "own encryption (which hides filenames too). Re-saving an "
            "already-encrypted archive keeps its password without asking "
            "again.</p>"
            "<h4>Self-extracting archives</h4>"
            f"<p>{sfx_state}</p>"
            "<p>A self-extracting archive is a small program with your files "
            "attached, so the recipient can unpack them by double-clicking "
            "without needing an archiver. It stays a perfectly ordinary archive "
            "too — ISOForge, 7-Zip and WinRAR can all still open it, and so can "
            "Explorer itself when the contents are ZIP.</p>"
            "<p>ISOForge attaches your files to a small <i>stub</i> program, "
            "which it can't compile itself. 7-Zip is free and includes suitable "
            "modules, and ISOForge finds them automatically once it's "
            "installed.</p>"
            "<h4>About RAR</h4>"
            f"<p>{rar_state}</p>"
            "<p>The RAR compression algorithm is proprietary to RARLAB, so "
            "ISOForge can't create RAR archives by itself and can't bundle the "
            "tools that can. Instead, point it at your own <code>Rar.exe</code> "
            "and <code>UnRAR.exe</code> (both ship with WinRAR) under "
            "<b>Tools &gt; Settings</b>. Dropping them into the ISOForge folder "
            "works too — they're detected automatically.</p>"
            + shell_state +
            "<h4>Convert Folder</h4>"
            "<p><b>Convert Folder</b> builds an ISO or archive straight from "
            "a folder on disk, without creating a project first.</p>"
            "<h4>Converting to CHD</h4>"
            f"<p>{chd_state}</p>"
            "<p>CHD is the lossless, compressed disc-image format used by "
            "MAME and most emulators. <b>Convert to CHD</b> accepts "
            "<code>.cue</code>, <code>.gdi</code>, <code>.toc</code> and "
            "<code>.iso</code> files and writes a <code>.chd</code> beside "
            "the original, which is left untouched. A .cue/.gdi/.toc is "
            "always a CD image. An .iso is treated as a CD or a DVD "
            "depending on its size (anything over about 800 MB is a DVD); "
            "the toolbar dialog lets you override that, which the Explorer "
            "menu cannot. A bare .bin can't be converted on its own — use "
            "its .cue file.</p>"
            "<p>CHD files can also be <b>opened</b>, browsed and edited like "
            "an ISO, and <b>saved</b> as CHD (or any other format). Opening "
            "unpacks the disc into a temporary image, so it needs free disk "
            "space about the size of the uncompressed disc, and it is removed "
            "again when you close the file. CD and DVD CHDs work; Dreamcast "
            "GD-ROM and hard-disk CHDs can't be opened. A CHD saved from "
            "ISOForge holds the disc's files exactly, but isn't a bit-for-bit "
            "copy of a raw console CD.</p>"
            "<h4>ISO specifics</h4>"
            "<p>ISOs are built with UDF alongside ISO9660/Joliet/Rock Ridge, "
            "removing the classic 4GB single-file limit. If a disc's content "
            "can't be written that way, a simpler format is used automatically "
            "and noted in the status bar.</p>"
            "<p>Use the switch at the top-right to flip between light and dark "
            "mode; your choice is remembered.</p>"
            "<p>Every launch starts from a completely blank project — ISOForge "
            "never reopens whatever you were last working on.</p>"
        )
        dlg = HelpDialog(self, f"About {APP_NAME}", text)
        dlg.exec()


def _set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{ORG_NAME}.{APP_NAME}.1")
    except Exception:
        pass


def _apply_base_font(app: QApplication) -> None:
    """Nudges the default font towards Segoe UI WITHOUT touching point
    size -- see theme.py's docstring for why that matters."""
    font = app.font()
    preferred = QFont("Segoe UI")
    if preferred.exactMatch():
        font.setFamily("Segoe UI")
    app.setFont(font)


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument(
        "--open", "-o", dest="open_path", default=None, metavar="PATH",
        help="Explicitly open this .iso/.zip/.7z/.rar/.exe on startup. "
             "Without this flag, ISOForge always starts a fresh, empty "
             "project.",
    )

    # Verbs invoked by the Explorer context menu (--shell-open,
    # --extract-here, --extract-to, --compress and --convert-chd). Each
    # receives exactly one path because Windows invokes a registry verb
    # once per selected item; `single_instance` folds those launches back
    # together.
    parser.add_argument("--shell-open", dest="shell_open", default=None,
                        metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--extract-here", dest="extract_here", default=None,
                        metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--extract-to", dest="extract_to", default=None,
                        metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--compress", dest="compress", nargs="+", default=None,
                        metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--convert-chd", dest="convert_chd", default=None,
                        metavar="PATH", help=argparse.SUPPRESS)

    # Maintenance verbs used by the installer/uninstaller.
    parser.add_argument("--register-shell", dest="register_shell",
                        action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--unregister-shell", dest="unregister_shell",
                        action="store_true", help=argparse.SUPPRESS)

    ns, _unknown = parser.parse_known_args(argv)
    return ns


def _run_maintenance(args) -> bool:
    """Handles --register-shell / --unregister-shell and reports whether
    the process should exit immediately.

    These run without any UI so the installer and uninstaller can call
    them silently.
    """
    if not (args.register_shell or args.unregister_shell):
        return False
    try:
        if args.register_shell:
            shell_integration.register(archives=True, create=True)
        else:
            shell_integration.unregister()
    except Exception:
        # Never let a failed registry tweak fail an install or uninstall.
        pass
    return True


def run():
    _set_windows_app_user_model_id()

    args = _parse_args(sys.argv[1:])
    if _run_maintenance(args):
        return

    request = shell_commands.build_request(args)
    payload = request.to_payload() if request is not None else None

    # 1. A context-menu verb goes to an already-running instance when there
    #    is one, which is what turns Explorer's one-process-per-file into a
    #    single grouped action.
    if request is not None:
        if single_instance.send_to_running_instance(payload):
            return

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    # 2. CLAIM THE PRIMARY ROLE NOW -- before settings, theme, icons or the
    #    main window exist. Explorer starts one process per selected file,
    #    all within a few milliseconds of each other, and building the window
    #    takes long enough that if listening only began afterwards, every one
    #    of those processes would still find nobody listening and open a
    #    session of its own. Claiming first makes the winner unambiguous: the
    #    claim is atomic, so exactly one process gets it. See the module
    #    docstring of `single_instance` for the full explanation.
    batcher = single_instance.RequestBatcher()
    is_primary = batcher.listen()

    # 3. Every process that LOST the race hands its file to the winner and
    #    quits without ever building a window. The winner exists (it holds
    #    the name) but may still be starting up, so a few short retries are
    #    allowed before giving up and handling the request alone.
    if request is not None and not is_primary:
        if single_instance.send_to_running_instance(
                payload, attempts=8, retry_delay_ms=250):
            return
        # The primary went away before we could reach it (a headless one
        # finishes and exits). Try to take over, so any launches still to
        # come have somewhere to go.
        is_primary = batcher.listen()

    app.setWindowIcon(icons.icon_disc_logo())
    _apply_base_font(app)

    # Push saved tool paths into the runtime BEFORE the window exists, so
    # format capabilities (and therefore the menus built from them) are
    # already correct on the very first paint.
    settings_store.apply_to_runtime(ORG_NAME, APP_NAME)

    pre_settings = QSettings(ORG_NAME, APP_NAME)
    dark = _coerce_bool(pre_settings.value("darkMode", False))
    icons.set_theme(dark)
    app.setStyleSheet(theme.get_stylesheet(dark))

    window = MainWindow()

    # Headless only while serving an extract / CHD verb: the window stays
    # hidden, and the process exits once the work is done.
    headless = request is not None and not request.wants_window

    # A headless process does not quit the instant it finishes: the rest of
    # the same selection may still be on its way (see `single_instance`), and
    # quitting would turn those launches away. It lingers briefly, and the
    # clock restarts whenever another batch arrives.
    idle_quit = QTimer()
    idle_quit.setSingleShot(True)
    idle_quit.timeout.connect(app.quit)

    def handle_batch(payloads):
        idle_quit.stop()
        requests = [shell_commands.request_from_payload(p) for p in payloads]
        merged = shell_commands.merge_requests([r for r in requests if r])
        if merged is None:
            if headless:
                idle_quit.start(_HEADLESS_LINGER_MS)
            return
        should_quit = window.handle_shell_request(merged)
        if should_quit and headless:
            idle_quit.start(_HEADLESS_LINGER_MS)

    batcher.set_handler(handle_batch)

    if request is not None:
        # Queued last, so the debounce timer starts just before the event
        # loop does -- connections that arrived while the window was being
        # built are read first and extend the wait, rather than racing it.
        batcher.add_local(payload)
        if request.wants_window:
            window.show()
    else:
        window.show()
        if args.open_path:
            if os.path.exists(args.open_path):
                window._open_path(args.open_path)
            else:
                QMessageBox.warning(
                    window, APP_NAME, f"File not found:\n{args.open_path}")

    exit_code = app.exec()
    batcher.close()
    sys.exit(exit_code)




