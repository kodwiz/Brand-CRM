"""Design tokens and the application stylesheet.

Neutral surfaces carry the interface. The rose accent is reserved for the selected navigation item,
primary actions and links. Green / amber / red appear only as meaningful status, always with a text label.
"""

# --- colour ---------------------------------------------------------------------------------
WORKSPACE = "#F8F7F4"       # surrounding background
SURFACE = "#FFFFFF"         # main working surface
SECTION = "#F2EFEC"         # soft neutral section / table header background
TEXT = "#302B2A"
TEXT_MUTED = "#766D69"
BORDER = "#E3DCDA"
BORDER_STRONG = "#CFC6C3"
ACCENT = "#987080"          # brand rose: primary action, links, selection marker
ACCENT_HOVER = "#85606F"
ACCENT_PRESSED = "#744F5E"
ACCENT_TINT = "#F0E6EA"     # selected navigation / row selection

OK = "#2F6B4F"
OK_BG = "#E6F1EB"
WARN = "#8A5A00"
WARN_BG = "#FBF0D9"
DANGER = "#A33A2F"
DANGER_BG = "#F8E4E1"
NEUTRAL_BG = "#EEEAE7"

# --- type -----------------------------------------------------------------------------------
FONT_FAMILY = '"Segoe UI", "Inter", "Helvetica Neue", "Noto Sans", "DejaVu Sans", Arial, sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "DejaVu Sans Mono", monospace'
SIZE_BASE = 13
SIZE_SMALL = 12
SIZE_TITLE = 20
SIZE_SECTION = 14

# --- layout ---------------------------------------------------------------------------------
SIDEBAR_WIDTH = 208
HEADER_HEIGHT = 52
ROW_HEIGHT = 34
CONTROL_HEIGHT = 32
RADIUS = 4
SPACE = 8


def stylesheet() -> str:
    return f"""
* {{ font-family: {FONT_FAMILY}; font-size: {SIZE_BASE}px; color: {TEXT}; }}
QMainWindow, QDialog {{ background: {WORKSPACE}; }}
QWidget#workspace {{ background: {WORKSPACE}; }}
QWidget#surface {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; }}
QToolTip {{ background: {TEXT}; color: {SURFACE}; border: none; padding: 4px 6px; }}

/* ---- sidebar ---- */
QWidget#sidebar {{ background: {SURFACE}; border-right: 1px solid {BORDER}; }}
QLabel#brand {{ font-size: 15px; font-weight: 600; letter-spacing: 0.5px; padding: 0 16px; }}
QLabel#brandSub {{ color: {TEXT_MUTED}; font-size: {SIZE_SMALL}px; padding: 0 16px; }}
QLabel#navGroup {{ color: {TEXT_MUTED}; font-size: 11px; font-weight: 600; padding: 8px 16px 2px 16px; }}
QPushButton#nav {{
    text-align: left; border: none; border-left: 3px solid transparent; border-radius: 0;
    padding: 0 16px 0 13px; height: 28px; background: transparent; color: {TEXT};
}}
QPushButton#nav:hover {{ background: {WORKSPACE}; }}
QPushButton#nav:checked {{ background: {ACCENT_TINT}; border-left: 3px solid {ACCENT}; font-weight: 600; }}
QPushButton#nav:disabled {{ color: {BORDER_STRONG}; }}
QLabel#userName {{ font-weight: 600; padding: 0 16px; }}
QLabel#userRole {{ color: {TEXT_MUTED}; font-size: {SIZE_SMALL}px; padding: 0 16px; }}
QPushButton#linkButton {{ border: none; background: transparent; color: {ACCENT}; text-align: left; padding: 4px 16px; }}
QPushButton#linkButton:hover {{ text-decoration: underline; color: {ACCENT_HOVER}; }}

/* ---- header ---- */
QWidget#header {{ background: {SURFACE}; border-bottom: 1px solid {BORDER}; }}
QLabel#headerTitle {{ font-size: {SIZE_SECTION}px; font-weight: 600; }}
QLabel#envBadge {{ background: {WARN_BG}; color: {WARN}; border-radius: 3px; padding: 2px 8px; font-weight: 600; font-size: 11px; }}

/* ---- typography ---- */
QLabel#pageTitle {{ font-size: {SIZE_TITLE}px; font-weight: 600; }}
QLabel#sectionTitle {{ font-size: {SIZE_SECTION}px; font-weight: 600; }}
QLabel#muted {{ color: {TEXT_MUTED}; }}
QLabel#fieldLabel {{ color: {TEXT_MUTED}; font-size: {SIZE_SMALL}px; }}
QLabel#kpiValue {{ font-size: 22px; font-weight: 600; }}
QLabel#error {{ color: {DANGER}; font-size: {SIZE_SMALL}px; }}

/* ---- inputs ---- */
QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS}px;
    padding: 0 8px; min-height: {CONTROL_HEIGHT - 2}px; selection-background-color: {ACCENT_TINT}; selection-color: {TEXT};
}}
QPlainTextEdit {{ padding: 6px 8px; }}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QPlainTextEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit[invalid="true"] {{ border: 1px solid {DANGER}; }}
QLineEdit:disabled, QComboBox:disabled {{ background: {SECTION}; color: {TEXT_MUTED}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER_STRONG}; selection-background-color: {ACCENT_TINT}; selection-color: {TEXT}; outline: 0; }}
QCheckBox {{ spacing: 8px; }}

/* ---- buttons ---- */
QPushButton {{
    background: {SURFACE}; border: 1px solid {BORDER_STRONG}; border-radius: {RADIUS}px;
    padding: 0 14px; min-height: {CONTROL_HEIGHT - 2}px;
}}
QPushButton:hover {{ background: {WORKSPACE}; }}
QPushButton:disabled {{ color: {BORDER_STRONG}; background: {SECTION}; border-color: {BORDER}; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}
QPushButton#primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; color: {SURFACE}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#primary:disabled {{ background: {BORDER_STRONG}; border-color: {BORDER_STRONG}; color: {SURFACE}; }}
QPushButton#danger {{ background: {SURFACE}; border: 1px solid {DANGER}; color: {DANGER}; }}
QPushButton#danger:hover {{ background: {DANGER_BG}; }}

/* ---- tables ---- */
QTableView, QTableWidget {{
    background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; gridline-color: {BORDER};
    selection-background-color: {ACCENT_TINT}; selection-color: {TEXT}; outline: 0; alternate-background-color: {SURFACE};
}}
QTableView::item {{ padding: 0 8px; border-bottom: 1px solid {BORDER}; }}
QTableView::item:selected {{ background: {ACCENT_TINT}; color: {TEXT}; }}
QHeaderView::section {{
    background: {SECTION}; color: {TEXT_MUTED}; border: none; border-bottom: 1px solid {BORDER};
    padding: 0 8px; height: 30px; font-weight: 600; font-size: {SIZE_SMALL}px;
}}
QTableCornerButton::section {{ background: {SECTION}; border: none; }}

/* ---- lists ---- */
QListWidget {{ background: {SURFACE}; border: 1px solid {BORDER}; border-radius: {RADIUS}px; outline: 0; }}
QListWidget::item {{ padding: 6px 10px; border-bottom: 1px solid {BORDER}; }}
QListWidget::item:selected {{ background: {ACCENT_TINT}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

/* ---- tabs ---- */
QTabWidget::pane {{ border: none; border-top: 1px solid {BORDER}; top: -1px; }}
QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent; padding: 8px 14px; margin-right: 4px; color: {TEXT_MUTED}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; font-weight: 600; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; }}

/* ---- misc ---- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG}; border-radius: 4px; min-height: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG}; border-radius: 4px; min-width: 28px; }}
QFrame#hr {{ background: {BORDER}; max-height: 1px; min-height: 1px; border: none; }}
QLabel#banner {{ background: {WARN_BG}; color: {WARN}; border-radius: {RADIUS}px; padding: 8px 12px; }}
QLabel#bannerError {{ background: {DANGER_BG}; color: {DANGER}; border-radius: {RADIUS}px; padding: 8px 12px; }}
QLabel#bannerOk {{ background: {OK_BG}; color: {OK}; border-radius: {RADIUS}px; padding: 8px 12px; }}
"""


STATUS_STYLES = {
    "ok": (OK, OK_BG),
    "warn": (WARN, WARN_BG),
    "danger": (DANGER, DANGER_BG),
    "neutral": (TEXT_MUTED, NEUTRAL_BG),
}
