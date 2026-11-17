"""Light and dark flat themes for ISOForge(ZR).

Note on fonts: the app's base font is set once in code via
main_window._apply_base_font(), deliberately WITHOUT touching point size.
Several rules below set only `font-weight` with no `font-size`; when Qt
merges such a partial rule against the default font it copies the
existing point size across, so an invalid default (e.g. -1, which is what
QFont.pointSize() returns once setPixelSize() has been used) would get
propagated and produce a noisy startup warning. Never call setPixelSize()
on the application-wide default font.

QCheckBox rules were added alongside archive support, since the password
and encryption dialogs are the first places in the app to use checkboxes
and would otherwise fall back to unstyled OS-native widgets that look
out of place against the rest of the UI.
"""

_SHARED_LIGHT = """
* { color: #2B303B; }
QMainWindow, QDialog { background: #F3F5F9; }
QToolBar { background: #FFFFFF; border: none; border-bottom: 1px solid #E3E7EF; padding: 6px 8px; spacing: 2px; }
QToolBar QToolButton { background: transparent; border: none; border-radius: 8px; padding: 6px 10px; margin: 1px; }
QToolBar QToolButton:hover { background: #EAF0FE; }
QToolBar QToolButton:pressed { background: #DCE6FD; }
QToolBar QToolButton:disabled { opacity: 0.35; }
QToolBar::separator { background: #E3E7EF; width: 1px; margin: 4px 6px; }
#PathBar { background: #FFFFFF; border: 1px solid #DDE2EC; border-radius: 8px; padding: 3px; }
#PathEdit { background: transparent; border: none; padding: 5px 8px; border-radius: 6px; }
#PathEdit:focus { background: #F0F3FA; }
QSplitter::handle { background: #E3E7EF; width: 2px; }
QTreeView, QTreeWidget { background: #FFFFFF; border: 1px solid #E3E7EF; border-radius: 10px; padding: 4px; outline: none; }
QTreeView::item, QTreeWidget::item { padding: 5px 4px; border-radius: 6px; }
QTreeView::item:selected, QTreeWidget::item:selected { background: #DCE6FD; color: #1D3E9C; }
QTreeView::item:hover:!selected, QTreeWidget::item:hover:!selected { background: #F0F4FE; }
QTreeView::branch { background: transparent; }
QTableWidget { background: #FFFFFF; border: 1px solid #E3E7EF; border-radius: 10px; gridline-color: #EEF1F7; outline: none; }
QTableWidget::item { padding: 6px 8px; }
QTableWidget::item:selected { background: #DCE6FD; color: #1D3E9C; }
QHeaderView::section { background: #F8F9FC; color: #6B7280; padding: 8px; border: none; border-bottom: 1px solid #E3E7EF; border-right: 1px solid #DDE2EC; font-weight: 600; }
QHeaderView::section:last { border-right: none; }
QStatusBar { background: #FFFFFF; border-top: 1px solid #E3E7EF; color: #6B7280; }
QMenu { background: #FFFFFF; border: 1px solid #E3E7EF; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #EAF0FE; color: #1D3E9C; }
QMenu::separator { height: 1px; background: #E3E7EF; margin: 6px 8px; }
QMenuBar { background: #FFFFFF; border-bottom: 1px solid #E3E7EF; padding: 2px 4px; }
QMenuBar::item { padding: 6px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: #EAF0FE; }
QPushButton { background: #4C7DF0; color: white; border: none; border-radius: 8px; padding: 8px 18px; font-weight: 600; }
QPushButton:hover { background: #3E6EE0; }
QPushButton:pressed { background: #2F5FD6; }
QPushButton:disabled { background: #C7D0E0; }
QPushButton[flat="true"] { background: transparent; color: #2B303B; border: 1px solid #DDE2EC; }
QPushButton[flat="true"]:hover { background: #F0F3FA; }
QLineEdit, QComboBox, QSpinBox { background: #FFFFFF; border: 1px solid #DDE2EC; border-radius: 8px; padding: 6px 8px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #4C7DF0; }
QLineEdit:disabled, QComboBox:disabled { background: #F1F3F7; color: #9AA3B2; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #FFFFFF; border: 1px solid #DDE2EC; border-radius: 8px; selection-background-color: #DCE6FD; selection-color: #1D3E9C; padding: 4px; }
QCheckBox { spacing: 8px; padding: 2px; }
QCheckBox:disabled { color: #9AA3B2; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #C7D0E0; border-radius: 4px; background: #FFFFFF; }
QCheckBox::indicator:hover { border: 1px solid #4C7DF0; }
QCheckBox::indicator:checked { background: #4C7DF0; border: 1px solid #4C7DF0; }
QCheckBox::indicator:disabled { background: #F1F3F7; border: 1px solid #DDE2EC; }
QProgressBar { background: #EEF1F7; border: none; border-radius: 7px; height: 14px; text-align: center; }
QProgressBar::chunk { background: #4C7DF0; border-radius: 7px; }
QLabel#SectionTitle { font-size: 15px; font-weight: 700; color: #1D2333; }
QLabel#Muted { color: #8A93A6; }
QFrame#Card { background: #FFFFFF; border: 1px solid #E3E7EF; border-radius: 12px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #D3DAE6; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #B7C1D6; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 2px; }
QScrollBar::handle:horizontal { background: #D3DAE6; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #B7C1D6; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
"""

_SHARED_DARK = """
* { color: #E4E7EC; }
QToolTip { background: #2E323C; color: #F1F3F7; border: 1px solid #454B59; padding: 4px 8px; border-radius: 6px; }
QMainWindow, QDialog { background: #1B1E24; }
QToolBar { background: #21252D; border: none; border-bottom: 1px solid #2E323C; padding: 6px 8px; spacing: 2px; }
QToolBar QToolButton { background: transparent; border: none; border-radius: 8px; padding: 6px 10px; margin: 1px; }
QToolBar QToolButton:hover { background: #2B3A5C; }
QToolBar QToolButton:pressed { background: #33456E; }
QToolBar QToolButton:disabled { opacity: 0.35; }
QToolBar::separator { background: #2E323C; width: 1px; margin: 4px 6px; }
#PathBar { background: #21252D; border: 1px solid #333846; border-radius: 8px; padding: 3px; }
#PathEdit { background: transparent; border: none; padding: 5px 8px; border-radius: 6px; color: #E4E7EC; }
#PathEdit:focus { background: #2A2F3A; }
QSplitter::handle { background: #2E323C; width: 2px; }
QTreeView, QTreeWidget { background: #21252D; border: 1px solid #2E323C; border-radius: 10px; padding: 4px; outline: none; }
QTreeView::item, QTreeWidget::item { padding: 5px 4px; border-radius: 6px; }
QTreeView::item:selected, QTreeWidget::item:selected { background: #2A3B66; color: #BFD1FF; }
QTreeView::item:hover:!selected, QTreeWidget::item:hover:!selected { background: #262B36; }
QTreeView::branch { background: transparent; }
QTableWidget { background: #21252D; border: 1px solid #2E323C; border-radius: 10px; gridline-color: #2A2F3A; outline: none; }
QTableWidget::item { padding: 6px 8px; }
QTableWidget::item:selected { background: #2A3B66; color: #BFD1FF; }
QHeaderView::section { background: #262B34; color: #9AA3B2; padding: 8px; border: none; border-bottom: 1px solid #2E323C; border-right: 1px solid #3A4050; font-weight: 600; }
QHeaderView::section:last { border-right: none; }
QStatusBar { background: #21252D; border-top: 1px solid #2E323C; color: #9AA3B2; }
QMenu { background: #23272F; border: 1px solid #333846; border-radius: 8px; padding: 6px; }
QMenu::item { padding: 6px 22px 6px 14px; border-radius: 6px; }
QMenu::item:selected { background: #2B3A5C; color: #BFD1FF; }
QMenu::separator { height: 1px; background: #333846; margin: 6px 8px; }
QMenuBar { background: #21252D; border-bottom: 1px solid #2E323C; padding: 2px 4px; }
QMenuBar::item { padding: 6px 12px; border-radius: 6px; background: transparent; }
QMenuBar::item:selected { background: #2B3A5C; }
QPushButton { background: #4C7DF0; color: white; border: none; border-radius: 8px; padding: 8px 18px; font-weight: 600; }
QPushButton:hover { background: #5C8BFA; }
QPushButton:pressed { background: #3E6EE0; }
QPushButton:disabled { background: #3A4050; color: #6B7280; }
QPushButton[flat="true"] { background: transparent; color: #E4E7EC; border: 1px solid #333846; }
QPushButton[flat="true"]:hover { background: #2A2F3A; }
QLineEdit, QComboBox, QSpinBox { background: #23272F; border: 1px solid #333846; border-radius: 8px; padding: 6px 8px; color: #E4E7EC; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #4C7DF0; }
QLineEdit:disabled, QComboBox:disabled { background: #1F2229; color: #6B7280; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView { background: #23272F; border: 1px solid #333846; border-radius: 8px; selection-background-color: #2A3B66; selection-color: #BFD1FF; padding: 4px; }
QCheckBox { spacing: 8px; padding: 2px; }
QCheckBox:disabled { color: #6B7280; }
QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #454B59; border-radius: 4px; background: #23272F; }
QCheckBox::indicator:hover { border: 1px solid #4C7DF0; }
QCheckBox::indicator:checked { background: #4C7DF0; border: 1px solid #4C7DF0; }
QCheckBox::indicator:disabled { background: #1F2229; border: 1px solid #333846; }
QProgressBar { background: #2A2F3A; border: none; border-radius: 7px; height: 14px; text-align: center; color: #E4E7EC; }
QProgressBar::chunk { background: #4C7DF0; border-radius: 7px; }
QLabel#SectionTitle { font-size: 15px; font-weight: 700; color: #F1F3F7; }
QLabel#Muted { color: #9AA3B2; }
QFrame#Card { background: #21252D; border: 1px solid #2E323C; border-radius: 12px; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 12px; margin: 2px; }
QScrollBar::handle:vertical { background: #3A4050; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4A5164; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar:horizontal { background: transparent; height: 12px; margin: 2px; }
QScrollBar::handle:horizontal { background: #3A4050; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: #4A5164; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0px; }
"""

LIGHT_STYLESHEET = _SHARED_LIGHT
DARK_STYLESHEET = _SHARED_DARK
STYLESHEET = LIGHT_STYLESHEET


def get_stylesheet(dark: bool) -> str:
    return DARK_STYLESHEET if dark else LIGHT_STYLESHEET
