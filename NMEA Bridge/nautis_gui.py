"""
nautis_gui.py  --  NAUTIS Home NMEA Bridge GUI Dashboard
=========================================================
PySide6 maritime console dashboard.  Launched by:
    python nautis_nmea_bridge.py --gui
"""

import sys
import time
import queue
import threading
from datetime import datetime

from nautis_nmea_bridge import __version__

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QThread
from PySide6.QtGui import QColor, QFont, QPalette, QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QCheckBox, QGroupBox, QGridLayout,
    QTextEdit, QPlainTextEdit, QFrame, QSizePolicy, QScrollArea, QSplitter,
    QSpacerItem, QSlider
)

# ---------------------------------------------------------------------------
# Design Tokens
# ---------------------------------------------------------------------------
BG_DEEP    = "#080f1e"          # near-black navy background
BG_CARD    = "#0d1a2e"          # card surface
BG_CARD2   = "#111d33"          # slightly lighter card
BORDER     = "#1c3050"          # subtle border
ACCENT     = "#00b4d8"          # vibrant cyan accent
ACCENT2    = "#0077b6"          # secondary accent
ACCENT_DIM = "#006080"          # dimmed accent
GREEN      = "#22c55e"          # success / running
RED        = "#ef4444"          # danger / stop
AMBER      = "#f59e0b"          # warning
TEXT_PRI   = "#e2eaf6"          # primary text
TEXT_SEC   = "#6e8aad"          # secondary / label text
TEXT_DIM   = "#3a5070"          # very dim text

FONT_MONO  = "Consolas, 'Courier New', monospace"
FONT_SANS  = "'Segoe UI', Arial, sans-serif"


MASTER_STYLE = f"""
/* ── Base ───────────────────────────────────────────────── */
QMainWindow, QWidget {{
    background-color: {BG_DEEP};
    color: {TEXT_PRI};
    font-family: 'Segoe UI', Arial, sans-serif;
    font-size: 12px;
}}

/* ── Scroll Area ─────────────────────────────────────────── */
QScrollArea {{
    border: none;
    background: transparent;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: {BG_CARD};
    width: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER};
    border-radius: 3px;
}}

/* ── Group Box ───────────────────────────────────────────── */
QGroupBox {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 18px;
    padding: 8px 10px 10px 10px;
    font-weight: 600;
    font-size: 11px;
    color: {ACCENT};
    letter-spacing: 0.5px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    padding: 0 6px;
    left: 10px;
}}

/* ── Line Edit ───────────────────────────────────────────── */
QLineEdit {{
    background-color: {BG_DEEP};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT_PRI};
    font-family: Consolas, monospace;
    font-size: 12px;
    selection-background-color: {ACCENT2};
}}
QLineEdit:focus {{
    border: 1px solid {ACCENT};
}}

/* ── Label ───────────────────────────────────────────────── */
QLabel {{
    color: {TEXT_SEC};
    font-size: 11px;
}}

/* ── Check Box ───────────────────────────────────────────── */
QCheckBox {{
    color: {TEXT_PRI};
    spacing: 6px;
    font-size: 11px;
}}
QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {BG_DEEP};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
}}

/* ── Push Button ─────────────────────────────────────────── */
QPushButton {{
    background-color: {BG_CARD2};
    color: {TEXT_PRI};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 14px;
    font-size: 11px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: {ACCENT2};
    border-color: {ACCENT};
    color: #ffffff;
}}
QPushButton:pressed {{
    background-color: {ACCENT};
}}
QPushButton:disabled {{
    color: {TEXT_DIM};
    border-color: {TEXT_DIM};
}}

/* ── Text Edit (NMEA console) ────────────────────────────── */
QTextEdit, QPlainTextEdit {{
    background-color: {BG_DEEP};
    color: #4ade80;
    font-family: Consolas, monospace;
    font-size: 11px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px;
}}

/* ── Frame / Divider ─────────────────────────────────────── */
QFrame[frameShape="4"], QFrame[frameShape="5"] {{
    color: {BORDER};
}}
"""


def _label(text, color=TEXT_SEC, bold=False, size=11):
    lbl = QLabel(text)
    style = f"color: {color}; font-size: {size}px;"
    if bold:
        style += " font-weight: 700;"
    lbl.setStyleSheet(style)
    return lbl


def _value_label(text="---", color=TEXT_PRI, mono=True, size=13):
    lbl = QLabel(text)
    font_fam = "Consolas" if mono else "'Segoe UI'"
    lbl.setStyleSheet(f"color: {color}; font-size: {size}px; font-family: {font_fam};")
    lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return lbl


# ---------------------------------------------------------------------------
# Status LED Widget
# ---------------------------------------------------------------------------
class StatusLED(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(10, 10)
        self._set(False)

    def _set(self, on: bool):
        color = GREEN if on else RED
        self.setStyleSheet(
            f"background:{color}; border-radius:5px; border:1px solid rgba(255,255,255,0.15);"
        )

    def set_running(self, on: bool):
        self._set(on)


# ---------------------------------------------------------------------------
# Telemetry Row
# ---------------------------------------------------------------------------
class TelemetryRow(QWidget):
    def __init__(self, label: str, unit: str = "", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 8, 2)
        lay.setSpacing(6)

        self._lbl = _label(label)
        self._lbl.setFixedWidth(120)

        self._val = _value_label()
        self._val.setMinimumWidth(90)

        self._unit = _label(unit, color=TEXT_DIM, size=10)
        self._unit.setFixedWidth(36)
        self._unit.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)

        lay.addWidget(self._lbl)
        lay.addStretch()
        lay.addWidget(self._val)
        lay.addWidget(self._unit)

    def update_value(self, text: str, color=TEXT_PRI):
        self._val.setText(text)
        self._val.setStyleSheet(
            f"color: {color}; font-size: 13px; font-family: Consolas;"
        )


# ---------------------------------------------------------------------------
# Autopilot Panel
# ---------------------------------------------------------------------------
class AutopilotPanel(QGroupBox):
    mode_changed   = Signal(str)
    heading_adjust = Signal(float)

    def __init__(self, parent=None):
        super().__init__("⚓  AUTOPILOT CONTROL", parent)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(10, 14, 10, 10)

        # ── Mode selector row ─────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(4)

        self._btn_standby = self._mode_btn("STANDBY", AMBER)
        self._btn_heading = self._mode_btn("HEADING", ACCENT)
        self._btn_route   = self._mode_btn("ROUTE",   GREEN)

        self._btn_standby.clicked.connect(lambda: self._set_mode("Standby"))
        self._btn_heading.clicked.connect(lambda: self._set_mode("Heading"))
        self._btn_route.clicked.connect(lambda:   self._set_mode("Route"))

        mode_row.addWidget(self._btn_standby)
        mode_row.addWidget(self._btn_heading)
        mode_row.addWidget(self._btn_route)
        root.addLayout(mode_row)

        # ── Mode indicator ────────────────────────────────────────
        self._mode_indicator = QLabel("MODE: STANDBY")
        self._mode_indicator.setAlignment(Qt.AlignCenter)
        self._mode_indicator.setStyleSheet(
            f"color: {AMBER}; font-size: 13px; font-weight: 700; "
            f"letter-spacing: 2px; padding: 6px; "
            f"background: {BG_DEEP}; border-radius: 4px; border: 1px solid {BORDER};"
        )
        root.addWidget(self._mode_indicator)

        # ── Status grid ───────────────────────────────────────────
        grid = QGridLayout()
        grid.setSpacing(4)
        grid.setColumnStretch(1, 1)

        grid.addWidget(_label("Current Heading"), 0, 0)
        self._lbl_current_hdg = _value_label("---°", color=TEXT_PRI, size=14)
        grid.addWidget(self._lbl_current_hdg, 0, 1)

        grid.addWidget(_label("Target Heading"), 1, 0)
        self._lbl_target_hdg = _value_label("---°", color=ACCENT, size=14)
        grid.addWidget(self._lbl_target_hdg, 1, 1)

        grid.addWidget(_label("Cross-Track Error"), 2, 0)
        self._lbl_xte = _value_label("0.000 NM", color=AMBER, size=12)
        grid.addWidget(self._lbl_xte, 2, 1)

        grid.addWidget(_label("Waypoint"), 3, 0)
        self._lbl_wpt = _value_label("N/A", color=TEXT_SEC, size=11)
        grid.addWidget(self._lbl_wpt, 3, 1)

        grid.addWidget(_label("Route Data"), 4, 0)
        self._lbl_route_data = _value_label("NO SIGNAL", color=RED, size=11)
        grid.addWidget(self._lbl_route_data, 4, 1)

        root.addLayout(grid)

        # ── Divider ───────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        root.addWidget(div)

        # ── Heading adjustment buttons ─────────────────────────────
        adj_label = _label("HEADING ADJUST  (Heading mode only)", size=10)
        adj_label.setAlignment(Qt.AlignCenter)
        root.addWidget(adj_label)

        adj_row = QHBoxLayout()
        adj_row.setSpacing(4)
        for delta, txt in [(-10, "−10"), (-1, "−1"), (+1, "+1"), (+10, "+10")]:
            btn = QPushButton(txt)
            btn.setFixedHeight(30)
            btn.setStyleSheet(
                f"font-size: 13px; font-weight: 700; "
                f"background: {BG_DEEP}; border: 1px solid {ACCENT_DIM}; "
                f"color: {ACCENT}; border-radius: 4px;"
            )
            btn.clicked.connect(lambda checked=False, d=delta: self._adjust_heading(d))
            adj_row.addWidget(btn)

        root.addLayout(adj_row)

        self._current_mode = "Standby"
        self._target_heading = 0.0
        self._update_mode_ui("Standby")

    def _mode_btn(self, label: str, active_color: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setCheckable(True)
        btn.setFixedHeight(28)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {BG_DEEP};
                border: 1px solid {BORDER};
                color: {TEXT_SEC};
                border-radius: 4px;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1px;
            }}
            QPushButton:checked {{
                background: {active_color};
                border-color: {active_color};
                color: {BG_DEEP};
            }}
        """)
        return btn

    def _set_mode(self, mode: str):
        self._current_mode = mode
        self._update_mode_ui(mode)
        self.mode_changed.emit(mode)

    def _update_mode_ui(self, mode: str):
        self._btn_standby.setChecked(mode == "Standby")
        self._btn_heading.setChecked(mode == "Heading")
        self._btn_route.setChecked(mode == "Route")

        color_map = {"Standby": AMBER, "Heading": ACCENT, "Route": GREEN}
        c = color_map.get(mode, TEXT_PRI)
        self._mode_indicator.setText(f"MODE: {mode.upper()}")
        self._mode_indicator.setStyleSheet(
            f"color: {c}; font-size: 13px; font-weight: 700; "
            f"letter-spacing: 2px; padding: 6px; "
            f"background: {BG_DEEP}; border-radius: 4px; border: 1px solid {BORDER};"
        )

    def _adjust_heading(self, delta: float):
        if self._current_mode != "Heading":
            return
        self.heading_adjust.emit(delta)

    @Slot(float, float, float, str, bool)
    def update_ap_state(self, current_hdg: float, target_hdg: float, xte: float, waypoint: str, route_good: bool):
        self._lbl_current_hdg.setText(f"{current_hdg:.1f}°")
        self._lbl_target_hdg.setText(f"{target_hdg:.1f}°")
        xte_color = AMBER if abs(xte) > 0.05 else GREEN
        self._lbl_xte.setText(f"{xte:+.3f} NM")
        self._lbl_xte.setStyleSheet(f"color: {xte_color}; font-size: 12px; font-family: Consolas;")
        self._lbl_wpt.setText(waypoint or "N/A")
        if route_good:
            self._lbl_route_data.setText("OK")
            self._lbl_route_data.setStyleSheet(f"color: {GREEN}; font-size: 11px; font-weight: 700;")
        else:
            self._lbl_route_data.setText("NO SIGNAL")
            self._lbl_route_data.setStyleSheet(f"color: {RED}; font-size: 11px; font-weight: 700;")


# ---------------------------------------------------------------------------
# Detached Autopilot Window
# ---------------------------------------------------------------------------
class DetachedApWindow(QWidget):
    """
    Small always-on-top window that hosts the AutopilotPanel widget.
    When this window closes, it signals the main window to re-adopt the panel.
    """
    closed = Signal()

    def __init__(self, ap_panel: 'AutopilotPanel', parent=None):
        super().__init__(parent, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("⚓  Autopilot Control")
        self.setMinimumWidth(300)
        self.setStyleSheet(f"""
            QWidget {{
                background-color: {BG_DEEP};
                color: {TEXT_PRI};
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 12px;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(ap_panel)
        ap_panel.show()
        self.adjustSize()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)


class NautisGuiWindow(QMainWindow):
    def __init__(self, args=None):
        super().__init__()
        self.setWindowTitle("NAUTIS Home  —  NMEA Bridge Console")
        self.setMinimumSize(1080, 700)
        self.resize(1280, 800)

        self._engine = None
        self._args   = args

        self._compact_mode = False
        self._detached_win = None
        self._normal_size  = None
        self._normal_geometry = None

        self._build_ui()
        self._apply_args(args)
        self.setStyleSheet(MASTER_STYLE)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._poll_engine)
        self._refresh_timer.setInterval(500)
        self._refresh_timer.start()

    # ── UI Construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # Header bar
        root_layout.addWidget(self._build_header())

        # Body splitter
        splitter = QSplitter(Qt.Horizontal)
        splitter.setHandleWidth(2)
        splitter.setStyleSheet(f"QSplitter::handle {{ background: {BORDER}; }}")

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([360, 920])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        root_layout.addWidget(splitter, 1)

        # Footer bar
        root_layout.addWidget(self._build_footer())

    def _build_header(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(50)
        bar.setStyleSheet(
            f"background: qlineargradient(x1:0, y1:0, x2:1, y2:0, "
            f"stop:0 {BG_CARD}, stop:1 #091428); "
            f"border-bottom: 1px solid {BORDER};"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)

        self._header_icon = QLabel("⚓")
        self._header_icon.setStyleSheet(f"font-size: 22px; color: {ACCENT};")
        self._header_title = QLabel("NAUTIS HOME  NMEA BRIDGE")
        self._header_title.setStyleSheet(
            f"color: {ACCENT}; font-size: 15px; font-weight: 700; letter-spacing: 2px;"
        )
        self._header_subtitle = QLabel("Maritime Console Dashboard")
        self._header_subtitle.setStyleSheet(f"color: {TEXT_SEC}; font-size: 11px;")

        self._status_led = StatusLED()
        self._status_label = QLabel("OFFLINE")
        self._status_label.setStyleSheet(f"color: {RED}; font-size: 11px; font-weight: 700;")

        self._ship_name_label = QLabel("")
        self._ship_name_label.setStyleSheet(f"color: {AMBER}; font-size: 11px; font-weight: 600;")

        # ── Compact mode toggle
        self._compact_btn = QPushButton("□  Compact")
        self._compact_btn.setFixedWidth(100)
        self._compact_btn.setFixedHeight(28)
        self._compact_btn.setToolTip("Hide telemetry & settings — show Autopilot panel only")
        self._compact_btn.setStyleSheet(
            f"background: {BG_CARD2}; border: 1px solid {BORDER}; "
            f"color: {TEXT_SEC}; border-radius: 4px; font-size: 10px; font-weight: 600;"
        )
        self._compact_btn.clicked.connect(self._toggle_compact)

        # ── Pop-out autopilot
        self._popout_btn = QPushButton("⤢  Pop Out AP")
        self._popout_btn.setFixedWidth(110)
        self._popout_btn.setFixedHeight(28)
        self._popout_btn.setToolTip("Detach Autopilot panel into its own floating window")
        self._popout_btn.setStyleSheet(
            f"background: {BG_CARD2}; border: 1px solid {ACCENT_DIM}; "
            f"color: {ACCENT}; border-radius: 4px; font-size: 10px; font-weight: 600;"
        )
        self._popout_btn.clicked.connect(self._toggle_popout)

        lay.addWidget(self._header_icon)
        lay.addSpacing(8)
        lay.addWidget(self._header_title)
        lay.addSpacing(12)
        lay.addWidget(self._header_subtitle)
        lay.addStretch()
        lay.addWidget(self._ship_name_label)
        lay.addSpacing(12)
        lay.addWidget(self._compact_btn)
        lay.addSpacing(4)
        lay.addWidget(self._popout_btn)
        lay.addSpacing(8)
        lay.addWidget(self._status_led)
        lay.addSpacing(4)
        lay.addWidget(self._status_label)

        return bar

    def _build_left_panel(self) -> QWidget:
        self._left_scroll = QScrollArea()
        self._left_scroll.setWidgetResizable(True)
        self._left_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._left_scroll.setMinimumWidth(320)
        self._left_scroll.setMaximumWidth(400)

        container = QWidget()
        container.setStyleSheet(f"background: {BG_DEEP};")
        lay = QVBoxLayout(container)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        # Connection config
        lay.addWidget(self._build_connection_group())
        # Sentence toggles
        lay.addWidget(self._build_toggles_group())
        # Autopilot settings (PID sliders, AP UDP port)
        lay.addWidget(self._build_ap_settings_group())
        lay.addStretch()

        self._left_scroll.setWidget(container)
        return self._left_scroll

    def _build_connection_group(self) -> QGroupBox:
        grp = QGroupBox("CONNECTION")
        lay = QGridLayout(grp)
        lay.setSpacing(6)
        lay.setColumnStretch(1, 1)

        lay.addWidget(_label("gRPC Host"), 0, 0)
        self._grpc_host = QLineEdit("127.0.0.1")
        lay.addWidget(self._grpc_host, 0, 1)

        lay.addWidget(_label("gRPC Port"), 1, 0)
        self._grpc_port = QLineEdit("53457")
        lay.addWidget(self._grpc_port, 1, 1)

        lay.addWidget(_label("UDP Host"), 2, 0)
        self._udp_host = QLineEdit("127.0.0.1")
        lay.addWidget(self._udp_host, 2, 1)

        lay.addWidget(_label("UDP Port"), 3, 0)
        self._udp_port = QLineEdit("10110")
        lay.addWidget(self._udp_port, 3, 1)

        lay.addWidget(_label("Poll Rate (Hz)"), 4, 0)
        self._rate = QLineEdit("2.0")
        lay.addWidget(self._rate, 4, 1)

        return grp

    def _build_toggles_group(self) -> QGroupBox:
        grp = QGroupBox("NMEA OUTPUT SENTENCES")
        lay = QGridLayout(grp)
        lay.setSpacing(4)

        sentences = [
            ("GPGGA", "gpgga"),  ("GPRMC", "gprmc"),
            ("GPVTG", "gpvtg"),  ("GPHDG", "gphdg"),
            ("GPROT", "gprot"),  ("IIRSA", "iirsa"),
            ("IIRPM", "iirpm"),  ("IIMWV", "iimwv"),
            ("IIDPT", "iidpt"),  ("IIDBT", "iidbt"),
            ("AIVDO", "aivdo"),  ("AIVDM", "aivdm"),
            ("PITCH", "pitch"),  ("ROLL",  "roll"),
        ]
        self._toggles = {}
        for i, (name, key) in enumerate(sentences):
            cb = QCheckBox(f"${name}")
            cb.setChecked(True)
            self._toggles[key] = cb
            lay.addWidget(cb, i // 2, i % 2)

        return grp

    def _build_ap_settings_group(self) -> QGroupBox:
        grp = QGroupBox("AUTOPILOT SETTINGS")
        lay = QGridLayout(grp)
        lay.setSpacing(6)
        lay.setColumnStretch(1, 1)

        # AP Listen Port
        lay.addWidget(_label("AP Listen Port"), 0, 0)
        self._ap_port = QLineEdit("10115")
        lay.addWidget(self._ap_port, 0, 1)

        # Magnetic variation (True = Magnetic + Variation)
        lay.addWidget(_label("Mag Variation (°E)"), 1, 0)
        self._mag_var = QLineEdit("0.0")
        self._mag_var.setToolTip(
            "Magnetic variation in degrees East (positive).\n"
            "Only needed if OpenCPN sends Magnetic (M) headings in $APB.\n"
            "Leave 0 if OpenCPN is configured to send True headings."
        )
        lay.addWidget(self._mag_var, 1, 1)

        self._apply_var_btn = QPushButton("Apply Variation")
        self._apply_var_btn.clicked.connect(self._apply_variation)
        lay.addWidget(self._apply_var_btn, 2, 0, 1, 2)

        # ── Divider ──────────────────────────────────────────────
        div = QFrame()
        div.setFrameShape(QFrame.HLine)
        lay.addWidget(div, 3, 0, 1, 2)

        # Vessel Response Preset slider
        preset_lbl = _label("Vessel Response", bold=True)
        lay.addWidget(preset_lbl, 4, 0, 1, 2)

        # Load presets and find slowest and fastest endpoints.
        # Sort by Kp/Kd aggressiveness ratio (higher = faster/less-damped vessel).
        # Exclude generic named presets so auto-tuned vessel entries drive the scale.
        from autopilot import VESSEL_PRESETS
        import os as _os
        _GENERIC = {"Slow", "Medium", "Fast"}
        def _resp_ratio(g): return g[0] / max(g[2], 0.001)   # Kp / Kd
        _candidates = [(n, v) for n, v in VESSEL_PRESETS.items() if n not in _GENERIC]
        if not _candidates:
            _candidates = list(VESSEL_PRESETS.items())
        _sorted = sorted(_candidates, key=lambda x: _resp_ratio(x[1]))
        self._slowest_name  = _sorted[0][0]
        self._fastest_name  = _sorted[-1][0]
        self._slowest_gains = VESSEL_PRESETS[self._slowest_name]
        self._fastest_gains = VESSEL_PRESETS[self._fastest_name]
        self._slowest_ratio = _resp_ratio(self._slowest_gains)
        self._fastest_ratio = _resp_ratio(self._fastest_gains)
        self._last_gui_vessel_preset = ""
        # Track autopilot.py modification time so we detect tuner updates
        _ap_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "autopilot.py")
        self._ap_mtime = _os.path.getmtime(_ap_path) if _os.path.exists(_ap_path) else 0.0

        # Default starting position: place Medium preset on the scale
        med_gains = VESSEL_PRESETS.get("Medium", (0.6, 0.01, 0.8, 25.0))
        med_ratio = _resp_ratio(med_gains)
        if self._fastest_ratio != self._slowest_ratio:
            init_f = (med_ratio - self._slowest_ratio) / (self._fastest_ratio - self._slowest_ratio)
            init_val = int(max(0.0, min(1.0, init_f)) * 100)
        else:
            init_val = 50

        self._preset_slider = QSlider(Qt.Horizontal)
        self._preset_slider.setMinimum(0)
        self._preset_slider.setMaximum(100)
        self._preset_slider.setValue(init_val)
        self._preset_slider.setTickPosition(QSlider.TicksBelow)
        self._preset_slider.setTickInterval(10)
        self._preset_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: {BORDER}; height: 4px; border-radius: 2px;
            }}
            QSlider::handle:horizontal {{
                background: {ACCENT}; width: 16px; height: 16px;
                margin: -6px 0; border-radius: 8px;
            }}
            QSlider::sub-page:horizontal {{
                background: {ACCENT2}; border-radius: 2px;
            }}
        """)
        self._preset_slider.valueChanged.connect(self._on_preset_slider_changed)
        lay.addWidget(self._preset_slider, 5, 0, 1, 2)

        # Slider label row: slowest name and fastest name at the ends
        lbl_row = QHBoxLayout()
        lbl_row.setContentsMargins(0, 0, 0, 0)
        
        self._slow_end_lbl = _label(f"Slow ({self._slowest_name})", size=9)
        self._slow_end_lbl.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        
        self._fast_end_lbl = _label(f"Fast ({self._fastest_name})", size=9)
        self._fast_end_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        
        lbl_row.addWidget(self._slow_end_lbl)
        lbl_row.addStretch()
        lbl_row.addWidget(self._fast_end_lbl)
        lay.addLayout(lbl_row, 6, 0, 1, 2)

        self._preset_name_lbl = _label(f"MEDIUM ({init_val}%)", color=ACCENT, bold=True)
        self._preset_name_lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(self._preset_name_lbl, 7, 0, 1, 2)

        # ── Divider ──────────────────────────────────────────────
        div2 = QFrame()
        div2.setFrameShape(QFrame.HLine)
        lay.addWidget(div2, 8, 0, 1, 2)

        # Advanced: manual PID override
        adv_lbl = _label("Advanced PID Override", size=10)
        lay.addWidget(adv_lbl, 9, 0, 1, 2)

        lay.addWidget(_label("Kp"), 10, 0)
        self._kp = QLineEdit("0.6")
        lay.addWidget(self._kp, 10, 1)

        lay.addWidget(_label("Ki"), 11, 0)
        self._ki = QLineEdit("0.01")
        lay.addWidget(self._ki, 11, 1)

        lay.addWidget(_label("Kd"), 12, 0)
        self._kd = QLineEdit("0.8")
        lay.addWidget(self._kd, 12, 1)

        self._apply_pid_btn = QPushButton("Apply PID Override")
        self._apply_pid_btn.clicked.connect(self._apply_pid)
        lay.addWidget(self._apply_pid_btn, 13, 0, 1, 2)

        return grp

    def _build_right_panel(self) -> QWidget:
        self._right_container = QWidget()
        self._right_container.setStyleSheet(f"background: {BG_DEEP};")
        lay = QVBoxLayout(self._right_container)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(10)

        # Top row: telemetry + autopilot control side by side
        self._top_row_widget = QWidget()
        top_row = QHBoxLayout(self._top_row_widget)
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(10)
        self._top_row_layout = top_row
        self._telemetry_group = self._build_telemetry_group()
        top_row.addWidget(self._telemetry_group, 1)
        top_row.addWidget(self._build_autopilot_panel(), 0)
        lay.addWidget(self._top_row_widget)

        # NMEA console log
        self._console_group = self._build_console_group()
        lay.addWidget(self._console_group, 1)

        return self._right_container

    def _build_telemetry_group(self) -> QGroupBox:
        grp = QGroupBox("LIVE TELEMETRY")
        lay = QVBoxLayout(grp)
        lay.setSpacing(0)
        lay.setContentsMargins(4, 14, 4, 6)

        self._telem_rows = {}

        fields = [
            ("Latitude",        "lat",            "°"),
            ("Longitude",       "lon",            "°"),
            ("Speed (SOG)",     "sog",            "kn"),
            ("Course (COG)",    "cog",            "°"),
            ("Heading (HDG)",   "heading",        "°"),
            ("Rate of Turn",    "rot",            "°/min"),
            ("Pitch",           "pitch",          "°"),
            ("Roll",            "roll",           "°"),
            ("Water Depth",     "depth",          "m"),
            ("Rudder (Actual)", "rudder",         "°"),
            ("Rudder (Cmd AP)", "commanded_rudder","°"),
            ("Engine RPM",      "rpm",            "rpm"),
            ("True Wind Speed", "tws",            "kn"),
            ("True Wind Dir",   "twa",            "°"),
            ("App Wind Speed",  "aws",            "kn"),
            ("App Wind Dir",    "awa",            "°"),
            ("Sim Time",        "time",           "UTC"),
        ]

        for i, (label, key, unit) in enumerate(fields):
            row = TelemetryRow(label, unit)
            if i % 2 == 1:
                row.setStyleSheet(f"background: {BG_CARD2}; border-radius:3px;")
            lay.addWidget(row)
            self._telem_rows[key] = row

        return grp

    def _build_autopilot_panel(self) -> AutopilotPanel:
        self._ap_panel = AutopilotPanel()
        self._ap_panel.setFixedWidth(280)
        self._ap_panel.mode_changed.connect(self._on_mode_changed)
        self._ap_panel.heading_adjust.connect(self._on_heading_adjust)
        return self._ap_panel

    def _build_console_group(self) -> QGroupBox:
        grp = QGroupBox("NMEA SENTENCE LOG")
        lay = QVBoxLayout(grp)
        lay.setContentsMargins(6, 14, 6, 6)

        self._console = QPlainTextEdit()
        self._console.setReadOnly(True)
        self._console.setMaximumBlockCount(500)
        self._console.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._console.setStyleSheet(
            f"background: {BG_DEEP}; color: {ACCENT}; "
            f"font-family: Consolas; font-size: 11px; "
            f"border: 1px solid {BORDER}; border-radius: 4px;"
        )
        lay.addWidget(self._console)
        return grp

    def _build_footer(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(44)
        bar.setStyleSheet(
            f"background: {BG_CARD}; border-top: 1px solid {BORDER};"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(8)

        self._start_btn = QPushButton("▶  START")
        self._start_btn.setFixedWidth(110)
        self._start_btn.setStyleSheet(
            f"background: {GREEN}; color: #000; border: none; font-weight: 700; border-radius: 5px;"
        )
        self._start_btn.clicked.connect(self._start_bridge)

        self._stop_btn = QPushButton("■  STOP")
        self._stop_btn.setFixedWidth(110)
        self._stop_btn.setEnabled(False)
        self._stop_btn.setStyleSheet(
            f"background: {RED}; color: #fff; border: none; font-weight: 700; border-radius: 5px;"
        )
        self._stop_btn.clicked.connect(self._stop_bridge)

        self._version_lbl = _label(f"v{__version__}  |  © NAUTIS Home NMEA Bridge", color=TEXT_DIM)

        lay.addWidget(self._start_btn)
        lay.addWidget(self._stop_btn)
        lay.addStretch()
        lay.addWidget(self._version_lbl)

        return bar

    # ── Engine Control ───────────────────────────────────────────────────────

    def _apply_args(self, args):
        if args is None:
            return
        if hasattr(args, "host"):
            self._grpc_host.setText(str(args.host))
        if hasattr(args, "port"):
            self._grpc_port.setText(str(args.port))
        if hasattr(args, "udp_host"):
            self._udp_host.setText(str(args.udp_host))
        if hasattr(args, "udp_port"):
            self._udp_port.setText(str(args.udp_port))
        if hasattr(args, "rate"):
            self._rate.setText(str(args.rate))

    def _start_bridge(self):
        if self._engine is not None:
            return

        from nautis_nmea_bridge import NmeaBridgeEngine

        host     = self._grpc_host.text().strip() or "127.0.0.1"
        grpc_p   = int(self._grpc_port.text().strip() or "53457")
        udp_h    = self._udp_host.text().strip() or "127.0.0.1"
        udp_p    = int(self._udp_port.text().strip() or "10110")
        rate     = float(self._rate.text().strip() or "2.0")
        ap_port  = int(self._ap_port.text().strip() or "10115")

        self._engine = NmeaBridgeEngine(
            host=host, port=grpc_p,
            udp_host=udp_h, udp_port=udp_p,
            rate=rate
        )
        self._engine.ap_port = ap_port

        # Apply current variation setting
        try:
            self._engine._magnetic_variation = float(self._mag_var.text())
        except ValueError:
            pass

        self._engine.start()

        # Apply current vessel preset from slider (interpolated PID gains)
        _sv = self._preset_slider.value()
        _f  = _sv / 100.0
        _kp  = self._slowest_gains[0] + _f * (self._fastest_gains[0] - self._slowest_gains[0])
        _ki  = self._slowest_gains[1] + _f * (self._fastest_gains[1] - self._slowest_gains[1])
        _kd  = self._slowest_gains[2] + _f * (self._fastest_gains[2] - self._slowest_gains[2])
        _lim = self._slowest_gains[3] + _f * (self._fastest_gains[3] - self._slowest_gains[3])
        self._engine.update_pid_params(_kp, _ki, _kd, limit=_lim)
        self._engine.ap_vessel_preset = f"Custom ({_sv}%)"

        # Push current toggle states to engine
        self._sync_toggles()

        self._start_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._status_led.set_running(True)
        self._status_label.setText("RUNNING")
        self._status_label.setStyleSheet(f"color: {GREEN}; font-size: 11px; font-weight: 700;")
        self._log(f"[GUI] Bridge started  →  gRPC {host}:{grpc_p}  UDP→{udp_h}:{udp_p}")

    def _stop_bridge(self):
        if self._engine is None:
            return
        self._engine.stop()
        self._engine.join(timeout=3.0)
        self._engine = None

        self._start_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._status_led.set_running(False)
        self._status_label.setText("OFFLINE")
        self._status_label.setStyleSheet(f"color: {RED}; font-size: 11px; font-weight: 700;")
        self._log("[GUI] Bridge stopped.")

    def _sync_toggles(self):
        if self._engine is None:
            return
        for key, cb in self._toggles.items():
            self._engine.toggles[key] = cb.isChecked()

    # ── Autopilot Signal Handlers ─────────────────────────────────────────────

    @Slot(str)
    def _on_mode_changed(self, mode: str):
        if self._engine:
            self._engine.set_autopilot_mode(mode)
            self._log(f"[AP] Mode set → {mode}")

    @Slot(float)
    def _on_heading_adjust(self, delta: float):
        if self._engine:
            new_hdg = (self._engine.ap_target_heading + delta) % 360.0
            self._engine.set_target_heading(new_hdg)
            self._log(f"[AP] Target heading → {new_hdg:.1f}°")

    def _apply_pid(self):
        if self._engine is None:
            self._log("[GUI] Cannot apply PID — bridge not running.")
            return
        try:
            kp = float(self._kp.text())
            ki = float(self._ki.text())
            kd = float(self._kd.text())
            self._engine.update_pid_params(kp, ki, kd)
            self._log(f"[AP] PID override  Kp={kp}  Ki={ki}  Kd={kd}")
        except ValueError:
            self._log("[GUI] Invalid PID values — check inputs.")

    def _apply_variation(self):
        try:
            var = float(self._mag_var.text())
            if self._engine:
                self._engine._magnetic_variation = var
                self._log(f"[AP] Magnetic variation set to {var:+.1f}° East")
            else:
                self._log("[GUI] Bridge not running — variation will be applied on next start.")
        except ValueError:
            self._log("[GUI] Invalid magnetic variation value.")

    def _update_slider_endpoints(self):
        """Reload autopilot.py from disk and recalibrate slider endpoints."""
        import importlib, os as _os
        import autopilot as _ap_mod
        try:
            importlib.reload(_ap_mod)
            from autopilot import VESSEL_PRESETS
            _GENERIC = {"Slow", "Medium", "Fast"}
            def _resp_ratio(g): return g[0] / max(g[2], 0.001)
            _candidates = [(n, v) for n, v in VESSEL_PRESETS.items() if n not in _GENERIC]
            if not _candidates:
                _candidates = list(VESSEL_PRESETS.items())
            _sorted = sorted(_candidates, key=lambda x: _resp_ratio(x[1]))
            self._slowest_name  = _sorted[0][0]
            self._fastest_name  = _sorted[-1][0]
            self._slowest_gains = VESSEL_PRESETS[self._slowest_name]
            self._fastest_gains = VESSEL_PRESETS[self._fastest_name]
            self._slowest_ratio = _resp_ratio(self._slowest_gains)
            self._fastest_ratio = _resp_ratio(self._fastest_gains)
            # Refresh labels
            self._slow_end_lbl.setText(f"Slow ({self._slowest_name})")
            self._fast_end_lbl.setText(f"Fast ({self._fastest_name})")
            # Update mtime so we don't re-trigger immediately
            _ap_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "autopilot.py")
            self._ap_mtime = _os.path.getmtime(_ap_path) if _os.path.exists(_ap_path) else 0.0
        except Exception:
            pass

    def _on_preset_slider_changed(self, val: int):
        f = val / 100.0
        kp = self._slowest_gains[0] + f * (self._fastest_gains[0] - self._slowest_gains[0])
        ki = self._slowest_gains[1] + f * (self._fastest_gains[1] - self._slowest_gains[1])
        kd = self._slowest_gains[2] + f * (self._fastest_gains[2] - self._slowest_gains[2])
        lim = self._slowest_gains[3] + f * (self._fastest_gains[3] - self._slowest_gains[3])
        
        self._kp.setText(f"{kp:.2f}")
        self._ki.setText(f"{ki:.4f}")
        self._kd.setText(f"{kd:.2f}")
        
        self._preset_name_lbl.setText(f"CUSTOM RESPONSE ({val}%)")
        self._last_gui_vessel_preset = f"Custom ({val}%)"
        
        if self._engine:
            self._engine.update_pid_params(kp, ki, kd, limit=lim)
            self._engine.ap_vessel_preset = f"Custom ({val}%)"

    def _toggle_compact(self):
        if not self._compact_mode:
            # Enable compact mode
            self._normal_size = self.size()
            self._normal_geometry = self.saveGeometry()
            
            # Hide left panel scroll area
            self._left_scroll.hide()
            
            # Hide right panel elements except AP panel
            self._telemetry_group.hide()
            self._console_group.hide()
            
            # Hide header elements
            self._header_icon.hide()
            self._header_title.hide()
            self._header_subtitle.hide()
            self._status_led.hide()
            self._status_label.hide()
            self._ship_name_label.hide()
            
            # Hide footer elements
            self._version_lbl.hide()
            
            # Adjust window constraints
            self.setMinimumSize(310, 480)
            self.resize(310, 520)
            
            self._compact_btn.setText("▣  Standard")
            self._compact_mode = True
            self._log("[GUI] Switched to Compact mode.")
        else:
            # Disable compact mode
            # Show left panel scroll area
            self._left_scroll.show()
            
            # Show right panel elements
            self._telemetry_group.show()
            self._console_group.show()
            
            # Show header elements
            self._header_icon.show()
            self._header_title.show()
            self._header_subtitle.show()
            self._status_led.show()
            self._status_label.show()
            self._ship_name_label.show()
            
            # Show footer elements
            self._version_lbl.show()
            
            # Restore window constraints and geometry
            self.setMinimumSize(1080, 700)
            if self._normal_geometry:
                self.restoreGeometry(self._normal_geometry)
            elif self._normal_size:
                self.resize(self._normal_size)
            
            self._compact_btn.setText("□  Compact")
            self._compact_mode = False
            self._log("[GUI] Switched to Standard mode.")

    def _toggle_popout(self):
        if self._detached_win is None:
            # Pop out
            self._detached_win = DetachedApWindow(self._ap_panel, self)
            self._detached_win.closed.connect(self._on_detached_win_closed)
            self._detached_win.show()
            self._popout_btn.setText("🔌  Dock AP")
            self._log("[GUI] Autopilot popped out.")
        else:
            # Dock
            self._detached_win.close()

    def _on_detached_win_closed(self):
        if self._detached_win:
            try:
                self._detached_win.layout().removeWidget(self._ap_panel)
            except Exception:
                pass
            self._detached_win = None
        self._top_row_layout.addWidget(self._ap_panel)
        self._ap_panel.show()
        self._popout_btn.setText("⤢  Pop Out AP")
        self._log("[GUI] Autopilot docked.")

    # ── Polling & Updates ────────────────────────────────────────────────────

    def _poll_engine(self):
        if self._engine is None:
            return

        # Drain console queue
        try:
            while True:
                msg = self._engine.console_queue.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass

        # Sync toggles
        self._sync_toggles()

        # Read telemetry snapshot
        with self._engine.telemetry_lock:
            td = dict(self._engine.telemetry_data)

        if not td:
            return

        def _fmt(val, prec=4): return f"{val:.{prec}f}" if isinstance(val, float) else str(val)

        self._telem_rows["lat"].update_value(f"{td.get('lat', 0.0):.6f}")
        self._telem_rows["lon"].update_value(f"{td.get('lon', 0.0):.6f}")
        self._telem_rows["sog"].update_value(f"{td.get('sog', 0.0):.2f}")
        self._telem_rows["cog"].update_value(f"{td.get('cog', 0.0):.1f}")
        self._telem_rows["heading"].update_value(f"{td.get('heading', 0.0):.1f}")
        self._telem_rows["rot"].update_value(f"{td.get('rot', 0.0):+.2f}")
        self._telem_rows["pitch"].update_value(f"{td.get('pitch', 0.0):+.2f}")
        self._telem_rows["roll"].update_value(f"{td.get('roll', 0.0):+.2f}")
        self._telem_rows["depth"].update_value(f"{td.get('water_depth', 0.0):.1f}")

        rudder = td.get("rudder", 0.0)
        r_color = ACCENT if abs(rudder) < 2 else (AMBER if abs(rudder) < 15 else RED)
        self._telem_rows["rudder"].update_value(f"{rudder:+.1f}", color=r_color)

        cmd_rudder = td.get("commanded_rudder", 0.0)
        cr_color = ACCENT if self._engine and self._engine.ap_mode == "Standby" else (
            ACCENT if abs(cmd_rudder) < 2 else (AMBER if abs(cmd_rudder) < 15 else RED)
        )
        self._telem_rows["commanded_rudder"].update_value(f"{cmd_rudder:+.1f}", color=cr_color)

        self._telem_rows["rpm"].update_value(f"{td.get('rpm', 0.0):.0f}")
        self._telem_rows["tws"].update_value(f"{td.get('tws', 0.0):.1f}")
        self._telem_rows["twa"].update_value(f"{td.get('twa', 0.0):.1f}")
        self._telem_rows["aws"].update_value(f"{td.get('aws', 0.0):.1f}")
        self._telem_rows["awa"].update_value(f"{td.get('awa', 0.0):.1f}")

        sim_dt = td.get("time")
        if isinstance(sim_dt, datetime):
            self._telem_rows["time"].update_value(sim_dt.strftime("%H:%M:%S"))

        ship = td.get("own_ship_name", "")
        if ship:
            self._ship_name_label.setText(f"[ {ship.upper()} ]")

        # Detect if autopilot.py was updated on disk (e.g. by the autotuner)
        # and force a full slider re-sync even if the vessel name hasn't changed.
        import os as _os
        _ap_path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "autopilot.py")
        if _os.path.exists(_ap_path):
            _cur_mtime = _os.path.getmtime(_ap_path)
            if _cur_mtime != getattr(self, '_ap_mtime', 0.0):
                self._ap_mtime = _cur_mtime
                self._last_gui_vessel_preset = ""   # force re-sync below

        # Sync GUI slider/inputs with engine's active preset (handles auto-load)
        if self._engine:
            ep = self._engine.ap_vessel_preset
            if ep != self._last_gui_vessel_preset:
                self._update_slider_endpoints()   # reloads autopilot.py from disk
                import importlib, autopilot as _ap_mod
                importlib.reload(_ap_mod)
                from autopilot import VESSEL_PRESETS
                if ep in VESSEL_PRESETS:
                    kp, ki, kd, lim = VESSEL_PRESETS[ep]
                    self._kp.setText(f"{kp:.2f}")
                    self._ki.setText(f"{ki:.4f}")
                    self._kd.setText(f"{kd:.2f}")
                    # Position slider using Kp/Kd aggressiveness ratio
                    ratio = kp / max(kd, 0.001)
                    if self._fastest_ratio != self._slowest_ratio:
                        f = (ratio - self._slowest_ratio) / (self._fastest_ratio - self._slowest_ratio)
                        slider_val = int(max(0.0, min(1.0, f)) * 100)
                    else:
                        slider_val = 50
                    self._preset_slider.blockSignals(True)
                    self._preset_slider.setValue(slider_val)
                    self._preset_slider.blockSignals(False)
                    self._preset_name_lbl.setText(f"AUTO: {ep.upper()}")
                else:
                    self._preset_name_lbl.setText(ep.upper())
                self._last_gui_vessel_preset = ep

        # Update autopilot panel
        self._ap_panel.update_ap_state(
            current_hdg=self._engine.ap_current_heading,
            target_hdg=self._engine.ap_target_heading,
            xte=self._engine.ap_xte,
            waypoint=self._engine.ap_waypoint,
            route_good=td.get("ap_route_good", False)
        )

    def _log(self, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self._console.appendPlainText(f"[{timestamp}]  {msg}")
        sb = self._console.verticalScrollBar()
        sb.setValue(sb.maximum())

    def closeEvent(self, event):
        self._stop_bridge()
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Standalone entry (for development testing without the bridge)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = NautisGuiWindow()
    win.show()
    sys.exit(app.exec())
