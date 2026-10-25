# Dark + teal/green theme, matching the MaxVoice logo (sampled from
# app/resources/logo.png — dominant colors cluster around #006e5a..#14a082).
# Applied once, app-wide, via QApplication.setStyleSheet() in app/main.py.

BACKGROUND = "#101512"
PANEL = "#19211d"
PANEL_HOVER = "#223028"
BORDER = "#2b3832"
ACCENT = "#17a673"
ACCENT_HOVER = "#22c98c"
TEXT = "#f1f1f1"
TEXT_MUTED = "#a3b0a9"
DANGER = "#e5484d"
SUCCESS = "#17a673"
WARN = "#d6a419"

STYLESHEET = f"""
* {{
    font-size: 14px;
}}

QWidget {{
    background-color: {BACKGROUND};
    color: {TEXT};
}}

QMainWindow, QDialog {{
    background-color: {BACKGROUND};
}}

QLabel {{
    color: {TEXT};
    background: transparent;
}}

QToolTip {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {ACCENT};
    padding: 4px 8px;
}}

QPushButton {{
    background-color: #202a25;
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 9px 16px;
}}
QPushButton:hover {{
    background-color: #2a3831;
    border-color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: #182019;
}}
QPushButton:disabled {{
    color: #5c655f;
    border-color: #202a25;
    background-color: #161c19;
}}

QPushButton[accent="true"] {{
    background-color: {ACCENT};
    color: #ffffff;
    font-weight: 600;
    border: none;
}}
QPushButton[accent="true"]:hover {{
    background-color: {ACCENT_HOVER};
}}
QPushButton[accent="true"]:disabled {{
    background-color: #1f4a3a;
    color: #7f9c91;
}}

QPushButton[danger="true"] {{
    border-color: {DANGER};
    color: {DANGER};
}}
QPushButton[danger="true"]:hover {{
    background-color: {DANGER};
    color: white;
}}

QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QListWidget {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 6px 8px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{
    border: 1px solid {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
}}
QComboBox QAbstractItemView {{
    background-color: {PANEL};
    color: {TEXT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}

QListWidget {{
    padding: 4px;
}}
QListWidget::item {{
    border-radius: 7px;
    margin: 2px;
}}
QListWidget::item:selected {{
    background-color: #163d30;
    border: 1px solid {ACCENT};
}}
QListWidget::item:hover {{
    background-color: {PANEL_HOVER};
}}

QScrollArea {{
    border: none;
    background: transparent;
}}

QFrame#segmentCard {{
    background-color: {PANEL};
    border: 1px solid {BORDER};
    border-left: 4px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QFrame#segmentCard[status="failed"], QFrame#segmentCard[status="tts_failed"] {{
    border-left: 4px solid {DANGER};
}}
QFrame#segmentCard[status="transcribed"] {{
    border-left: 4px solid {SUCCESS};
}}
QFrame#segmentCard[status="tts_done"] {{
    border-left: 4px solid {ACCENT};
}}
QFrame#segmentCard[status="transcribing"], QFrame#segmentCard[status="extracting_audio"],
QFrame#segmentCard[status="tts_processing"], QFrame#segmentCard[status="tts_queued"] {{
    border-left: 4px solid {WARN};
}}

QSplitter::handle {{
    background-color: {BORDER};
    width: 4px;
}}

QScrollBar:vertical {{
    background: {BACKGROUND};
    width: 12px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: #37453e;
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}

QDialogButtonBox QPushButton {{
    min-width: 90px;
}}
"""
