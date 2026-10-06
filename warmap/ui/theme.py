"""warmap's chrome theme: one `Theme` dataclass per mode, looked up through
`current()` at paint/stylesheet time rather than baked into a widget at
construction time.

The accent is a cyan-teal, the color of a radar sweep.

The encryption-bucket colors used on the map itself (Open=red, WEP=orange,
WPA*=green, Unknown=gray) live in `warmap.models.ENC_COLORS` instead of
here, those are data-encoding colors, fixed regardless of theme, not
chrome.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    surface: str
    elevated: str
    raised: str
    hairline: str
    baseline: str
    border: str
    text_primary: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_on_fill: str


DARK = Theme(
    name="dark",
    bg="#0d0d0d",
    surface="#1a1a19",
    elevated="#242422",
    raised="#2e2e2c",
    hairline="#2c2c2a",
    baseline="#383835",
    border="rgba(255, 255, 255, 0.10)",
    text_primary="#ffffff",
    text_secondary="#c3c2b7",
    text_muted="#898781",
    accent="#1fb6c9",
    # #1fb6c9's WCAG relative luminance is ~0.42, so near-black text sits at
    # a comfortable contrast on it; white would wash out.
    accent_on_fill="#0d0d0d",
)

LIGHT = Theme(
    name="light",
    bg="#f9f9f7",
    surface="#fcfcfb",
    elevated="#f0efec",
    raised="#e6e4dd",
    hairline="#e1e0d9",
    baseline="#c9c7bb",
    border="rgba(11, 11, 11, 0.10)",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    text_muted="#898781",
    accent="#0e7a8a",
    accent_on_fill="#ffffff",
)

THEMES: Dict[str, Theme] = {DARK.name: DARK, LIGHT.name: LIGHT}
DEFAULT_THEME_NAME = "dark"  # a map reads better dark

_current: Theme = THEMES[DEFAULT_THEME_NAME]
_stylesheet_cache: Dict[str, str] = {}


def current() -> Theme:
    return _current


def set_theme(name: Optional[str]) -> Theme:
    global _current
    _current = THEMES.get(name, THEMES[DEFAULT_THEME_NAME])
    return _current


def stylesheet_for(theme: Theme) -> str:
    cached = _stylesheet_cache.get(theme.name)
    if cached is None:
        cached = _build_stylesheet(theme)
        _stylesheet_cache[theme.name] = cached
    return cached


def stylesheet() -> str:
    return stylesheet_for(current())


def _build_stylesheet(t: Theme) -> str:
    return f"""
    QWidget {{
        background: {t.bg};
        color: {t.text_primary};
        font-size: 13px;
    }}
    QMainWindow {{ background: {t.bg}; }}
    QToolBar {{
        background: {t.surface};
        border: none;
        border-bottom: 1px solid {t.hairline};
        spacing: 4px;
        padding: 4px;
    }}
    QToolButton {{
        background: transparent;
        color: {t.text_primary};
        border: 1px solid transparent;
        border-radius: 6px;
        padding: 4px 8px;
    }}
    QToolButton:hover {{ background: {t.elevated}; border: 1px solid {t.border}; }}
    QToolButton:checked {{ background: {t.accent}; color: {t.accent_on_fill}; }}

    QDockWidget {{
        color: {t.text_secondary};
        font-weight: 600;
    }}
    QDockWidget::title {{
        background: {t.surface};
        padding: 6px 8px;
        border-bottom: 1px solid {t.hairline};
    }}

    QGroupBox {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 8px;
        margin-top: 14px;
        padding: 10px 8px 8px 8px;
        font-weight: 600;
        color: {t.text_secondary};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 8px;
        padding: 0 4px;
    }}

    QLabel {{ color: {t.text_primary}; background: transparent; }}
    QLabel[muted="true"] {{ color: {t.text_muted}; }}
    QLabel[cssClass="sectionHeader"] {{
        color: {t.text_secondary};
        font-size: 12px;
        font-weight: 600;
        letter-spacing: 0.5px;
    }}
    /* Dialog chrome for the Send-to-phone dialog's title and its "this is on
       your network" caveat. The caveat is deliberately quiet rather than a
       red warning box: it is a fact to read once, not an alarm. */
    QLabel[cssClass="dialogHeading"] {{
        color: {t.text_primary};
        font-size: 16px;
        font-weight: 600;
    }}
    QLabel[cssClass="dialogNote"] {{
        color: {t.text_muted};
        font-size: 11px;
    }}

    QLineEdit, QSpinBox, QComboBox {{
        background: {t.surface};
        color: {t.text_primary};
        border: 1px solid {t.baseline};
        border-radius: 6px;
        padding: 4px 8px;
        selection-background-color: {t.accent};
        selection-color: {t.accent_on_fill};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border: 1px solid {t.accent}; }}

    QCheckBox {{ spacing: 6px; }}
    QSlider::groove:horizontal {{
        height: 4px;
        background: {t.baseline};
        border-radius: 2px;
    }}
    QSlider::handle:horizontal {{
        background: {t.accent};
        width: 14px;
        margin: -6px 0;
        border-radius: 7px;
    }}

    QListWidget {{
        background: {t.surface};
        border: 1px solid {t.border};
        border-radius: 6px;
    }}
    QListWidget::item:selected {{ background: {t.accent}; color: {t.accent_on_fill}; }}

    QScrollBar:vertical {{ background: {t.bg}; width: 10px; }}
    QScrollBar::handle:vertical {{ background: {t.raised}; border-radius: 5px; min-height: 20px; }}
    QScrollBar:horizontal {{ background: {t.bg}; height: 10px; }}
    QScrollBar::handle:horizontal {{ background: {t.raised}; border-radius: 5px; min-width: 20px; }}

    QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.hairline}; }}
    QLabel#sampleBanner {{
        background: {t.accent};
        color: {t.accent_on_fill};
        font-weight: 600;
        padding: 4px 10px;
    }}
    """
