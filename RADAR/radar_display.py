"""
radar_display.py -- NAUTIS Home Standalone Networked Radar Display
==================================================================
PySide6 application that receives ASTERIX Cat 240 radar video UDP packets
and renders a Plan Position Indicator (PPI) display.

Run on the radar display computer:
    python radar_display.py

Requirements:
    pip install PySide6 grpcio protobuf

Splitter setup (on sim machine):
    python radar_splitter.py --display <this-machine-ip>
"""

import math
import socket
import struct
import sys
import threading
import time
import os
from collections import deque

from PySide6.QtCore import (
    Qt, QTimer, QPointF, QRectF, Signal, QObject, QThread, QMutex,
    QMutexLocker
)
from PySide6.QtGui import (
    QColor, QFont, QPainter, QPixmap, QImage, QPen, QBrush,
    QRadialGradient, QLinearGradient, QFontMetrics, QIcon, QConicalGradient,
    QPainterPath
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QSlider, QPushButton, QComboBox, QGroupBox, QSizePolicy,
    QLineEdit, QSpinBox, QDoubleSpinBox, QDialog, QFormLayout, QDialogButtonBox,
    QMessageBox, QFrame, QCheckBox
)

# ─── ASTERIX Cat 240 Decoder ──────────────────────────────────────────────────

SPEED_OF_LIGHT = 299_792_458  # m/s

class RadarSpoke:
    """Decoded radar spoke from one ASTERIX Cat 240 packet."""
    __slots__ = ("start_az_deg", "end_az_deg", "start_range_m", "cell_size_m",
                 "cells", "nb_cells", "timestamp")

    def __init__(self):
        self.start_az_deg = 0.0
        self.end_az_deg = 0.0
        self.start_range_m = 0.0
        self.cell_size_m = 0.0
        self.cells = b""
        self.nb_cells = 0
        self.timestamp = 0.0


def decode_cat240(data: bytes):
    """
    Decode an ASTERIX Cat 240 packet.
    Returns a RadarSpoke or None on failure.

    Packet layout (from FSPEC analysis of NAUTIS output E7 A0):
      [0]     CAT=240
      [1-2]   LEN (big-endian uint16)
      [3-4]   FSPEC = E7 A0
      [5-6]   I240/010: SAC, SIC
      [7]     I240/000: Message Type (1=Summary, 2=Video)
      [8-11]  I240/020: MSG_INDEX (uint32 BE)
      [12-23] I240/041: START_AZ(2B), END_AZ(2B), START_RG(4B), CELL_DUR(4B)
      [24-25] I240/048: C(1b)|spare(7b)|RES(8b)
      [26-30] I048/049: NB_VB(2B), NB_CELLS(3B)
      [31]    I240/051 rep factor
      [32..]  I240/051 video blocks (rep * 64 bytes)
    """
    if len(data) < 32 or data[0] != 240:
        return None

    try:
        offset = 5  # Skip CAT(1) + LEN(2) + FSPEC(2)

        # I240/010: SAC (1B) + SIC (1B)
        # sac = data[offset]
        # sic = data[offset+1]
        offset += 2

        # I240/000: Message Type (1B)
        msg_type = data[offset]
        offset += 1
        if msg_type != 2:  # Only process Video Messages, not Summary
            return None

        # I240/020: MSG_INDEX (4B BE)
        offset += 4

        # I240/041: Video Header Femto (12B)
        start_az_raw = struct.unpack_from(">H", data, offset)[0]
        end_az_raw   = struct.unpack_from(">H", data, offset + 2)[0]
        start_rg_raw = struct.unpack_from(">I", data, offset + 4)[0]
        cell_dur_fs  = struct.unpack_from(">I", data, offset + 8)[0]  # femtoseconds
        offset += 12

        start_az = start_az_raw * 360.0 / 65536.0
        end_az   = end_az_raw   * 360.0 / 65536.0

        # Cell size in meters: CELL_DUR (s) * c / 2
        cell_dur_s = cell_dur_fs * 1e-15
        cell_size_m = cell_dur_s * SPEED_OF_LIGHT / 2.0

        # Start range in meters
        start_range_m = cell_size_m * start_rg_raw

        # I240/048: C + spare + RES (2B)
        compression = (data[offset] >> 7) & 1
        # res = data[offset + 1]  # 4 = 8 bits per cell
        offset += 2
        if compression:
            return None  # Not handling compressed data for now

        # I048/049: NB_VB (2B) + NB_CELLS (3B)
        nb_vb    = struct.unpack_from(">H", data, offset)[0]
        nb_cells = (data[offset+2] << 16) | (data[offset+3] << 8) | data[offset+4]
        offset += 5

        # I240/051: repetitive video blocks
        if offset >= len(data):
            return None
        rep_factor = data[offset]
        offset += 1
        video_bytes = data[offset:offset + rep_factor * 64]

        spoke = RadarSpoke()
        spoke.start_az_deg  = start_az
        spoke.end_az_deg    = end_az
        spoke.start_range_m = start_range_m
        spoke.cell_size_m   = cell_size_m
        spoke.cells         = video_bytes
        spoke.nb_cells      = min(nb_cells, len(video_bytes))
        spoke.timestamp     = time.monotonic()
        return spoke

    except (struct.error, IndexError):
        return None


# ─── UDP Receiver Thread ───────────────────────────────────────────────────────

class AsterixReceiver(QThread):
    """Background thread that receives ASTERIX Cat 240 UDP packets."""
    spoke_received = Signal(object)   # emits RadarSpoke
    status_changed = Signal(str)

    def __init__(self, port: int = 54322, parent=None):
        super().__init__(parent)
        self.port = port
        self._stop_event = threading.Event()
        self._sock = None

    def set_port(self, port: int):
        self.port = port

    def stop(self):
        self._stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def run(self):
        self._stop_event.clear()
        while not self._stop_event.is_set():
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._sock.bind(("0.0.0.0", self.port))
                self._sock.settimeout(1.0)
                self.status_changed.emit(f"Listening on UDP port {self.port}")

                while not self._stop_event.is_set():
                    try:
                        data, _ = self._sock.recvfrom(65535)
                        spoke = decode_cat240(data)
                        if spoke is not None:
                            self.spoke_received.emit(spoke)
                    except socket.timeout:
                        pass
            except OSError as e:
                self.status_changed.emit(f"Socket error: {e}")
                time.sleep(3)
            finally:
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:
                        pass


class RadarSplitterThread(QThread):
    status_changed = Signal(str)

    def __init__(self, listen_port: int = 54321, ingame_port: int = 44444,
                 display_hosts=None, display_port: int = 54322,
                 forward_ingame: bool = True, parent=None):
        super().__init__(parent)
        self.listen_port = listen_port
        self.ingame_port = ingame_port
        self.display_hosts = display_hosts or []
        self.display_port = display_port
        self.forward_ingame = forward_ingame
        self._stop_event = threading.Event()
        self._sock = None

    def stop(self):
        self._stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def run(self):
        self._stop_event.clear()
        
        # Build destinations list
        destinations = []
        if self.forward_ingame:
            destinations.append(("127.0.0.1", self.ingame_port))
        for host in self.display_hosts:
            if host.strip():
                destinations.append((host.strip(), self.display_port))
                
        while not self._stop_event.is_set():
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self._sock.bind(("0.0.0.0", self.listen_port))
                self._sock.settimeout(1.0)
                
                send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self.status_changed.emit(f"Splitter listening on port {self.listen_port}")
                
                while not self._stop_event.is_set():
                    try:
                        data, addr = self._sock.recvfrom(65535)
                        for dst_ip, dst_port in destinations:
                            try:
                                send_sock.sendto(data, (dst_ip, dst_port))
                            except Exception:
                                pass
                    except socket.timeout:
                        pass
            except OSError as e:
                self.status_changed.emit(f"Splitter error: {e}")
                time.sleep(3)
            finally:
                if self._sock:
                    try:
                        self._sock.close()
                    except Exception:
                        pass


# ─── PPI Radar Widget ──────────────────────────────────────────────────────────

# Range options in nautical miles
RANGE_OPTIONS_NM = [0.25, 0.5, 0.75, 1.5, 3.0, 6.0, 12.0, 24.0]
NM_TO_METERS = 1852.0

# Colour table: amplitude (0–255) → RGB colour components
# Classic green phosphor radar palette
def make_colour_table():
    table = []
    for amp in range(256):
        if amp == 0:
            table.append((0, 0, 0, 0))  # Transparent background
        elif amp < 40:
            # Very weak returns — dim green
            v = int(amp * 2.5)
            table.append((0, v // 3, 0, 200))
        elif amp < 120:
            # Moderate returns — green
            v = int(40 + (amp - 40) * 1.5)
            table.append((0, v, 0, 220))
        elif amp < 200:
            # Strong returns — bright green-yellow
            r = int((amp - 120) * 1.2)
            g = min(255, 150 + int((amp - 120) * 0.8))
            table.append((r, g, 0, 235))
        else:
            # Very strong — near-white with green tint
            r = min(255, int(200 + (amp - 200) * 0.5))
            g = 255
            b = min(100, int((amp - 200) * 0.5))
            table.append((r, g, b, 255))
    return table

COLOUR_TABLE = make_colour_table()


class RadarPPI(QWidget):
    """
    Plan Position Indicator (PPI) radar display widget.
    Renders ASTERIX Cat 240 spokes with persistence/afterglow.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(500, 500)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._range_m = 3.0 * NM_TO_METERS  # Default 3 NM
        self._gain = 1.0           # 0.0–2.0 gain multiplier

        # Persistence: each spoke erases its own wedge before drawing.
        # _clear_alpha=255 = full erase (single sweep), 0 = no erase (infinite ghost)
        # Slider 0 → 255 (default), slider 100 → 0
        self._clear_alpha = 255

        # Persistence buffer — ARGB32 image, updated per spoke
        self._ppi_image = None
        self._ppi_size = 0

        # Sweep line tracking
        self._current_az = 0.0
        self._sweep_width_deg = 0.5

        # Stats
        self._spoke_count = 0
        self._last_spoke_time = 0.0
        self._pkt_rate = 0.0
        self._rate_window = deque()

        # Orientation & Heading
        self._orientation_mode = "Heading Up"
        self._own_heading_deg = 0.0
        self._grpc_connected = False

        # Plotting tools attributes
        self._ebl_enabled = False
        self._ebl_bearing = 0
        self._vrm_enabled = False
        self._vrm_range_nm = 1.0
        self._pi_enabled = False
        self._pi_offset_nm = 0.5
        self._standby = False

        self._mutex = QMutex()

    def set_range_m(self, range_m: float):
        self._range_m = range_m

    def set_standby(self, standby: bool):
        if self._standby != standby:
            self._standby = standby
            if standby and self._ppi_image is not None:
                self._ppi_image.fill(QColor(0, 0, 0, 255))
            self.update()

    def set_gain(self, gain: float):
        self._gain = gain  # 0.0 – 2.0

    def set_persistence(self, p_slider_val: int):
        # Slider 0   → _clear_alpha 255 = full erase per spoke (single sweep, default)
        # Slider 100 → _clear_alpha   0 = no erase (full ghost, targets accumulate)
        self._clear_alpha = int((1.0 - p_slider_val / 100.0) * 255)

    def set_grpc_connected(self, connected: bool):
        self._grpc_connected = connected
        self.update()

    def set_orientation_mode(self, mode: str):
        if self._orientation_mode != mode:
            self._orientation_mode = mode
            if self._ppi_image is not None:
                self._ppi_image.fill(QColor(0, 0, 0, 255))
            self.update()

    def set_heading(self, heading: float):
        if self._own_heading_deg != heading:
            self._own_heading_deg = heading
            self.update()

    def set_ebl_enabled(self, enabled: bool):
        self._ebl_enabled = enabled
        self.update()

    def set_ebl_bearing(self, bearing: int):
        self._ebl_bearing = bearing
        self.update()

    def set_vrm_enabled(self, enabled: bool):
        self._vrm_enabled = enabled
        self.update()

    def set_vrm_range_nm(self, range_nm: float):
        self._vrm_range_nm = range_nm
        self.update()

    def set_pi_enabled(self, enabled: bool):
        self._pi_enabled = enabled
        self.update()

    def set_pi_offset_nm(self, offset_nm: float):
        self._pi_offset_nm = offset_nm
        self.update()

    def _ensure_ppi_image(self, size: int):
        if self._ppi_image is None or self._ppi_size != size:
            self._ppi_size = size
            self._ppi_image = QImage(size, size, QImage.Format.Format_ARGB32)
            self._ppi_image.fill(QColor(0, 0, 0, 255))

    def add_spoke(self, spoke: RadarSpoke):
        """Draw a radar spoke onto the persistence buffer."""
        if self._standby:
            return
        with QMutexLocker(self._mutex):
            # Determine PPI image size based on current widget size
            side = min(self.width(), self.height()) - 4
            if side < 50:
                return
            self._ensure_ppi_image(side)

            center = side / 2.0
            px_per_meter = center / self._range_m

            # Azimuth bounds for this spoke
            az_start = spoke.start_az_deg
            az_end   = spoke.end_az_deg
            az_mid   = (az_start + az_end) / 2.0
            if self._orientation_mode == "North Up" and self._grpc_connected:
                offset = self._own_heading_deg
                az_start = (az_start + offset) % 360.0
                az_end   = (az_end   + offset) % 360.0
                az_mid   = (az_mid   + offset) % 360.0
            az_mid_rad = math.radians(az_mid)

            painter = QPainter(self._ppi_image)
            painter.setRenderHint(QPainter.Antialiasing, False)

            # ── Sweep-synchronous clear ─────────────────────────────────────
            # Erase the wedge the sweep is about to paint.  At _clear_alpha=255
            # this gives a clean single-sweep display; lower values leave a
            # fading ghost of previous rotations.
            if self._clear_alpha > 0:
                # Angular span of this spoke (plus a small margin to avoid gaps)
                span_deg = abs(az_end - az_start)
                if span_deg > 180:          # wrapped spoke
                    span_deg = 360.0 - span_deg
                span_deg = max(span_deg, 0.5) + 0.5   # at least 1° total

                # Qt drawPie uses 1/16th-degree units; angles from 3-o'clock CCW
                # We need to convert our North-Up clockwise convention:
                #   Qt angle = 90 - az_start, span negative (CW)
                qt_start = int((90.0 - az_start) * 16)
                qt_span  = int(-span_deg * 16)  # negative = clockwise

                erase_rect = QRectF(
                    center - center, center - center,
                    center * 2, center * 2
                )
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 0, 0, self._clear_alpha))
                painter.drawPie(erase_rect, qt_start, qt_span)

            painter.setRenderHint(QPainter.Antialiasing, False)

            # ── Echo cells ─────────────────────────────────────────────────
            n_cells = spoke.nb_cells
            cell_data = spoke.cells

            for i in range(min(n_cells, len(cell_data))):
                amp = cell_data[i]
                if amp == 0:
                    continue

                # Apply gain
                amp_g = min(255, int(amp * self._gain))
                if amp_g == 0:
                    continue

                r, g, b, a = COLOUR_TABLE[amp_g]

                # Range of this cell
                range_m = spoke.start_range_m + spoke.cell_size_m * i
                if range_m > self._range_m:
                    break
                if range_m < 0:
                    continue

                # Pixel position (North Up: 0° = up = negative Y)
                px = center + range_m * px_per_meter * math.sin(az_mid_rad)
                py = center - range_m * px_per_meter * math.cos(az_mid_rad)

                # Draw as a small rect to ensure coverage (cell_size in pixels)
                cell_px = max(1.5, spoke.cell_size_m * px_per_meter)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(r, g, b, a))
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_SourceOver
                )
                painter.drawEllipse(QPointF(px, py), cell_px / 2, cell_px / 2)

            painter.end()

        self._current_az = spoke.start_az_deg
        self._spoke_count += 1
        now = time.monotonic()
        self._last_spoke_time = now
        self._rate_window.append(now)
        cutoff = now - 5.0
        while self._rate_window and self._rate_window[0] < cutoff:
            self._rate_window.popleft()
        self._pkt_rate = len(self._rate_window) / 5.0

        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        w, h = self.width(), self.height()
        side = min(w, h)
        cx = w / 2.0
        cy = h / 2.0
        radius = side / 2.0 - 2.0

        # ── Background ───────────────────────────────────────────────────────
        painter.fillRect(self.rect(), QColor(0, 8, 0))  # Very dark green tint

        # ── Draw PPI image (echo data) ───────────────────────────────────────
        if self._ppi_image is not None:
            with QMutexLocker(self._mutex):
                img_copy = self._ppi_image.copy()
            img_size = img_copy.width()
            x_off = cx - img_size / 2.0
            y_off = cy - img_size / 2.0
            painter.drawImage(int(x_off), int(y_off), img_copy)

        # ── Sweep line ───────────────────────────────────────────────────────
        az = self._current_az
        if self._orientation_mode == "North Up" and self._grpc_connected:
            az = (az + self._own_heading_deg) % 360.0
        az_rad = math.radians(az)
        sx = cx + radius * math.sin(az_rad)
        sy = cy - radius * math.cos(az_rad)
        sweep_pen = QPen(QColor(0, 255, 0, 80))
        sweep_pen.setWidth(2)
        painter.setPen(sweep_pen)
        painter.drawLine(QPointF(cx, cy), QPointF(sx, sy))

        # ── Sweep afterglow (fan gradient) ───────────────────────────────────
        # Draw a fading fan 15 degrees behind the sweep
        for deg_back in range(1, 16):
            alpha = max(0, 60 - deg_back * 4)
            fan_az = self._current_az - deg_back
            if self._orientation_mode == "North Up" and self._grpc_connected:
                fan_az = (fan_az + self._own_heading_deg) % 360.0
            fan_az_rad = math.radians(fan_az)
            fan_x = cx + radius * math.sin(fan_az_rad)
            fan_y = cy - radius * math.cos(fan_az_rad)
            fan_pen = QPen(QColor(0, 200, 0, alpha))
            fan_pen.setWidth(1)
            painter.setPen(fan_pen)
            painter.drawLine(QPointF(cx, cy), QPointF(fan_x, fan_y))

        # ── Clip to circle ───────────────────────────────────────────────────
        # Draw dark overlay outside the radar circle
        painter.setPen(Qt.NoPen)
        outer_path = __import__("PySide6.QtGui", fromlist=["QPainterPath"]).QPainterPath()
        outer_path.addRect(QRectF(0, 0, w, h))
        inner_path = __import__("PySide6.QtGui", fromlist=["QPainterPath"]).QPainterPath()
        inner_path.addEllipse(QPointF(cx, cy), radius, radius)
        clip_path = outer_path.subtracted(inner_path)
        painter.setBrush(QColor(5, 5, 5, 255))
        painter.drawPath(clip_path)

        # ── Range rings ──────────────────────────────────────────────────────
        ring_pen = QPen(QColor(0, 120, 0, 160))
        ring_pen.setWidth(1)
        painter.setPen(ring_pen)
        painter.setBrush(Qt.NoBrush)
        n_rings = 4
        for i in range(1, n_rings + 1):
            r_ring = radius * i / n_rings
            painter.drawEllipse(QPointF(cx, cy), r_ring, r_ring)

            # Range label
            ring_range_nm = (self._range_m / NM_TO_METERS) * i / n_rings
            if ring_range_nm >= 1.0:
                label = f"{ring_range_nm:.1f} NM"
            else:
                label = f"{ring_range_nm * 10:.2f} NM"
            painter.setPen(QColor(0, 180, 0, 200))
            painter.setFont(QFont("Consolas", 8))
            painter.drawText(QPointF(cx + 4, cy - r_ring + 12), label)
            painter.setPen(ring_pen)

        # ── Outer ring (radar circle border) ─────────────────────────────────
        border_pen = QPen(QColor(0, 200, 0, 255))
        border_pen.setWidth(2)
        painter.setPen(border_pen)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # ── Bearing scale (ticks every 5°, major labeled every 30° - rotates with own_heading in Heading Up)
        scale_offset = 0.0
        if self._orientation_mode == "Heading Up" and self._grpc_connected:
            scale_offset = self._own_heading_deg

        painter.setFont(QFont("Consolas", 7))
        for deg in range(0, 360, 5):
            screen_deg = (deg - scale_offset) % 360.0
            rad = math.radians(screen_deg)
            sin_r, cos_r = math.sin(rad), math.cos(rad)
            
            is_major = (deg % 30 == 0)
            is_medium = (deg % 10 == 0)
            tick_len = 10 if is_major else (6 if is_medium else 3)
            
            x1 = cx + radius * sin_r
            y1 = cy - radius * cos_r
            x2 = cx + (radius - tick_len) * sin_r
            y2 = cy - (radius - tick_len) * cos_r
            
            tick_pen = QPen(QColor(0, 200, 0, 220 if is_major else (120 if is_medium else 60)))
            tick_pen.setWidth(1)
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            
            if is_major:
                label = f"{deg:03d}"
                lx = cx + (radius + 12) * sin_r - 10
                ly = cy - (radius + 12) * cos_r + 4
                painter.setPen(QColor(0, 220, 0, 220))
                painter.drawText(QPointF(lx, ly), label)

        # ── Centre dot ───────────────────────────────────────────────────────
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 255, 0, 200))
        painter.drawEllipse(QPointF(cx, cy), 3, 3)

        # ── Cardinal cross-hairs ─────────────────────────────────────────────
        cross_pen = QPen(QColor(0, 80, 0, 100))
        cross_pen.setWidth(1)
        painter.setPen(cross_pen)
        painter.drawLine(QPointF(cx, cy - radius), QPointF(cx, cy + radius))
        painter.drawLine(QPointF(cx - radius, cy), QPointF(cx + radius, cy))

        # ── Standby Overlay ──────────────────────────────────────────────────
        if self._standby:
            painter.setFont(QFont("Consolas", 28, QFont.Bold))
            painter.setPen(QColor(0, 180, 0, 220))
            fm = painter.fontMetrics()
            text = "STANDBY"
            tw = fm.horizontalAdvance(text)
            th = fm.height()
            painter.drawText(QPointF(cx - tw / 2.0, cy + th / 4.0), text)

        # ── Plotting Tools Overlays ──────────────────────────────────────────
        # EBL (rotated with scale_offset to keep true bearing alignment)
        if self._ebl_enabled:
            ebl_rad = math.radians(self._ebl_bearing - scale_offset)
            ebl_x = cx + radius * math.sin(ebl_rad)
            ebl_y = cy - radius * math.cos(ebl_rad)
            ebl_pen = QPen(QColor(0, 255, 255, 180), 1.5, Qt.DashLine)
            painter.setPen(ebl_pen)
            painter.drawLine(QPointF(cx, cy), QPointF(ebl_x, ebl_y))
            
            # Bearing label at the outer end
            painter.setPen(QColor(0, 255, 255, 220))
            painter.setFont(QFont("Consolas", 8, QFont.Bold))
            label_x = cx + (radius + 15) * math.sin(ebl_rad) - 15
            label_y = cy - (radius + 15) * math.cos(ebl_rad) + 5
            painter.drawText(QPointF(label_x, label_y), f"{self._ebl_bearing:03d}°")

            # PI Lines (drawn only when EBL is enabled)
            if self._pi_enabled and self._pi_offset_nm > 0:
                ux = math.sin(ebl_rad)
                uy = -math.cos(ebl_rad)
                vx = math.cos(ebl_rad)
                vy = math.sin(ebl_rad)
                
                px_per_nm = radius / (self._range_m / NM_TO_METERS)
                D = self._pi_offset_nm * px_per_nm
                
                if D < radius:
                    half_len = math.sqrt(radius**2 - D**2)
                    pi_pen = QPen(QColor(255, 165, 0, 150), 1.0, Qt.DashLine)
                    painter.setPen(pi_pen)
                    
                    # Right line
                    rx1 = cx + D * vx - half_len * ux
                    ry1 = cy + D * vy - half_len * uy
                    rx2 = cx + D * vx + half_len * ux
                    ry2 = cy + D * vy + half_len * uy
                    painter.drawLine(QPointF(rx1, ry1), QPointF(rx2, ry2))
                    
                    # Left line
                    lx1 = cx - D * vx - half_len * ux
                    ly1 = cy - D * vy - half_len * uy
                    lx2 = cx - D * vx + half_len * ux
                    ly2 = cy - D * vy + half_len * uy
                    painter.drawLine(QPointF(lx1, ly1), QPointF(lx2, ly2))

        # VRM
        if self._vrm_enabled:
            px_per_nm = radius / (self._range_m / NM_TO_METERS)
            vrm_radius = self._vrm_range_nm * px_per_nm
            if vrm_radius < radius:
                vrm_pen = QPen(QColor(255, 0, 255, 180), 1.5, Qt.DashLine)
                painter.setPen(vrm_pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(QPointF(cx, cy), vrm_radius, vrm_radius)
                
                # Range label at 090° position
                painter.setPen(QColor(255, 0, 255, 220))
                painter.setFont(QFont("Consolas", 8, QFont.Bold))
                label_x = cx + vrm_radius + 5
                if label_x + 40 > cx + radius:
                    label_x = cx + vrm_radius - 45
                label_y = cy + 4
                painter.drawText(QPointF(label_x, label_y), f"{self._vrm_range_nm:.2f} NM")

        # ── Orientation Overlay (top left) ───────────────────────────────────
        painter.setFont(QFont("Consolas", 10, QFont.Bold))
        if self._orientation_mode == "North Up" and not self._grpc_connected:
            painter.setPen(QColor(255, 100, 0, 220))  # Warning orange color
            mode_text = "MODE: NORTH UP (FALLBACK: HU)"
            hdg_text = "HDG: ---.-° (NO gRPC)"
        else:
            painter.setPen(QColor(0, 255, 0, 220))
            mode_text = f"MODE: {self._orientation_mode.upper()}"
            hdg_text = f"HDG: {self._own_heading_deg:.1f}°" if self._grpc_connected else "HDG: ---.-° (HU)"
        
        painter.drawText(QPointF(cx - radius + 15, cy - radius + 25), mode_text)
        painter.drawText(QPointF(cx - radius + 15, cy - radius + 40), hdg_text)

        # ── Status overlay (bottom left) ──────────────────────────────────────
        painter.setFont(QFont("Consolas", 8))
        now = time.monotonic()
        age = now - self._last_spoke_time if self._last_spoke_time > 0 else 99
        if age < 2.0:
            status_color = QColor(0, 255, 0, 200)
            status = f"LIVE  {self._pkt_rate:.0f} spk/s  AZ:{self._current_az:.1f}"
        else:
            status_color = QColor(255, 100, 0, 200)
            status = f"NO SIGNAL  ({age:.0f}s ago)"
        painter.setPen(status_color)
        painter.drawText(QPointF(cx - radius + 5, cy + radius - 5), status)


# ─── gRPC Radar Control ────────────────────────────────────────────────────────

# ─── versioning ───────────────────────────────────────────────────────────────
__version__ = "2.1.0"


def _find_proto_dir():
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    
    # Candidate list
    candidates = [
        os.path.join(base, "proto_extracted"),
        os.path.join(os.path.dirname(base), "proto_extracted"),
        os.path.join(os.path.dirname(base), "NMEA Bridge", "proto_extracted"),
        os.path.join(os.path.dirname(os.path.dirname(base)), "proto_extracted"),
        os.path.join(os.path.dirname(os.path.dirname(base)), "NMEA Bridge", "proto_extracted"),
    ]
    for cand in candidates:
        if os.path.exists(cand) and os.path.isdir(cand):
            return cand
            
    # Search sibling recursively up to 3 levels
    p = base
    for _ in range(3):
        if not p:
            break
        try:
            for entry in os.listdir(p):
                full = os.path.join(p, entry)
                if os.path.isdir(full):
                    if entry == "proto_extracted":
                        return full
                    sub = os.path.join(full, "proto_extracted")
                    if os.path.isdir(sub):
                        return sub
        except Exception:
            pass
        p = os.path.dirname(p)
    return os.path.join(base, "proto_extracted")


# ─── gRPC Radar Control ────────────────────────────────────────────────────────

class RadarController:
    """Thin wrapper around the NAUTIS gRPC interface for radar controls."""

    def __init__(self, grpc_host: str = "127.0.0.1", grpc_port: int = 8086):
        self._host = grpc_host
        self._port = grpc_port
        self._channel = None
        self._classes = {}
        self._radar_entity_id = None
        self._pb_dir = _find_proto_dir()
        self._connected = False

    def close(self):
        """Cleanly shut down the gRPC channel so its C threads stop."""
        self._connected = False
        if self._channel is not None:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None

    def connect(self) -> tuple[bool, str]:
        """Load proto descriptors and establish gRPC channel."""
        try:
            import grpc
            from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
            from google.protobuf import any_pb2, timestamp_pb2, duration_pb2

            if not os.path.exists(self._pb_dir):
                return False, f"Descriptor directory not found: {self._pb_dir}"

            pool = descriptor_pool.Default()

            # Add standard descriptors
            for std_module in [any_pb2, timestamp_pb2, duration_pb2]:
                fdp = descriptor_pb2.FileDescriptorProto()
                std_module.DESCRIPTOR.CopyToProto(fdp)
                try:
                    pool.Add(fdp)
                except Exception:
                    pass

            def parse_with_truncation(data):
                for length in range(len(data), 9, -1):
                    try:
                        fdp = descriptor_pb2.FileDescriptorProto()
                        fdp.ParseFromString(data[:length])
                        return fdp, data[:length]
                    except Exception:
                        pass
                return None, None

            name_to_bytes = {}
            for fname in os.listdir(self._pb_dir):
                if not fname.endswith(".proto.pb"):
                    continue
                with open(os.path.join(self._pb_dir, fname), "rb") as f:
                    data = f.read()
                fdp, truncated = parse_with_truncation(data)
                if fdp is not None:
                    name_to_bytes[fdp.name] = truncated

            added = set()
            for _ in range(len(name_to_bytes) + 2):
                progress = False
                for proto_name, data in name_to_bytes.items():
                    if proto_name in added:
                        continue
                    try:
                        fdp = descriptor_pb2.FileDescriptorProto()
                        fdp.ParseFromString(data)
                        pool.Add(fdp)
                        added.add(proto_name)
                        progress = True
                    except Exception:
                        pass
                if not progress:
                    break

            needed = [
                "vstep.entities.GetComponentsRequest",
                "vstep.entities.GetComponentsRequest.Query",
                "vstep.entities.GetComponentsResponse",
                "vstep.entities.SetComponentsRequest",
                "vstep.entities.SetComponentsResponse",
                "vstep.entities.ComponentData",
                "vstep.entities.EntitySelection",
                "vstep.entities.AllRootEntities",
                "vstep.radar.RadarParams",
                "vstep.radar.ClutterParams",
                "vstep.radar.OperationState",
                "vstep.radar.OperationParams",
                "vstep.sensors.CompassBaseOutput",
                "vstep.viewports.AssignedCamera",
                "vstep.entities.Relations",
                "vstep.equipment.MMSI",
            ]
            classes = {}
            for t in needed:
                try:
                    desc = pool.FindMessageTypeByName(t)
                    classes[t] = message_factory.GetMessageClass(desc)
                except Exception:
                    pass

            self._classes = classes
            self._channel = grpc.insecure_channel(f"{self._host}:{self._port}")

            # Do NOT use channel_ready_future — it spawns a gRPC C polling thread
            # (_poll_connectivity) that causes an access violation on Python 3.14
            # at process exit. Connectivity is implicitly tested by the
            # _find_radar_entity() RPC call made immediately after connect() returns.
            self._connected = True
            return True, "Success"
        except Exception as e:
            self._connected = False
            return False, str(e)

    def _ensure_radar_entity(self) -> bool:
        if self._radar_entity_id is not None:
            return True
        self._radar_entity_id = self._find_radar_entity()
        return self._radar_entity_id is not None

    def _find_radar_entity(self) -> int | None:
        """Use GetComponents to find the first entity with RadarParams."""
        if not self._connected or "vstep.radar.RadarParams" not in self._classes:
            return None
        try:
            sel = self._classes["vstep.entities.EntitySelection"]()
            sel.all_root_entities.CopyFrom(self._classes["vstep.entities.AllRootEntities"]())
            sel.recursion = 99

            query = self._classes["vstep.entities.GetComponentsRequest.Query"]()
            query.component_types.append("vstep.radar.RadarParams")
            query.entities.append(sel)

            req = self._classes["vstep.entities.GetComponentsRequest"]()
            req.queries.append(query)

            get_stub = self._channel.unary_unary(
                "/vstep.entities.Registry/GetComponents",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=self._classes["vstep.entities.GetComponentsResponse"].FromString,
            )
            resp = get_stub(req)
            for comp in resp.data:
                url = comp.data.type_url
                tn = url.split("/")[-1] if "/" in url else url
                if tn == "RadarParams":
                    return comp.entity.id
        except Exception as e:
            print(f"[radar_ctrl] Find entity failed: {e}")
        return None

    def _grpc_get(self, component_type: str):
        """Query registry for a component type and return the one matching our radar entity."""
        if not self._connected or not self._ensure_radar_entity():
            return None
        if component_type not in self._classes:
            return None
        try:
            sel = self._classes["vstep.entities.EntitySelection"]()
            sel.all_root_entities.CopyFrom(self._classes["vstep.entities.AllRootEntities"]())
            sel.recursion = 99

            query = self._classes["vstep.entities.GetComponentsRequest.Query"]()
            query.component_types.append(component_type)
            query.entities.append(sel)

            req = self._classes["vstep.entities.GetComponentsRequest"]()
            req.queries.append(query)

            get_stub = self._channel.unary_unary(
                "/vstep.entities.Registry/GetComponents",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=self._classes["vstep.entities.GetComponentsResponse"].FromString,
            )
            resp = get_stub(req)
            for comp in resp.data:
                eid = comp.entity.id
                if eid == self._radar_entity_id:
                    url = comp.data.type_url
                    tn = url.split("/")[-1] if "/" in url else url
                    if component_type.endswith(tn):
                        msg = self._classes[component_type]()
                        msg.MergeFromString(comp.data.value)
                        return msg
        except Exception as e:
            print(f"[radar_ctrl] _grpc_get failed for {component_type}: {e}")
        return None

    def get_heading(self) -> float | None:
        """Get the own-ship heading in degrees from the simulator."""
        if not self._connected:
            return None
        try:
            needed_types = [
                "vstep.sensors.CompassBaseOutput",
                "vstep.viewports.AssignedCamera",
                "vstep.entities.Relations",
                "vstep.equipment.MMSI"
            ]
            for t in needed_types:
                if t not in self._classes:
                    return None

            sel = self._classes["vstep.entities.EntitySelection"]()
            sel.all_root_entities.CopyFrom(self._classes["vstep.entities.AllRootEntities"]())
            sel.recursion = 99

            query = self._classes["vstep.entities.GetComponentsRequest.Query"]()
            for t in needed_types:
                query.component_types.append(t)
            query.entities.append(sel)

            req = self._classes["vstep.entities.GetComponentsRequest"]()
            req.queries.append(query)

            get_stub = self._channel.unary_unary(
                "/vstep.entities.Registry/GetComponents",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=self._classes["vstep.entities.GetComponentsResponse"].FromString,
            )
            resp = get_stub(req)

            entities = {}
            for comp in resp.data:
                url = comp.data.type_url
                tn = url.split("/")[-1] if "/" in url else url
                eid = comp.entity.id
                if eid not in entities:
                    entities[eid] = {}
                full_tn = f"vstep.sensors.{tn}" if tn == "CompassBaseOutput" else (
                    f"vstep.viewports.{tn}" if tn == "AssignedCamera" else (
                        f"vstep.entities.{tn}" if tn in ("Relations", "Name", "DisplayName") else (
                            f"vstep.equipment.{tn}" if tn == "MMSI" else tn
                        )
                    )
                )
                entities[eid][tn] = comp.data
                entities[eid][full_tn] = comp.data

            parsed_entities = {}
            for eid, comps in entities.items():
                parsed_entities[eid] = {}
                for key, any_msg in comps.items():
                    for t in needed_types:
                        if t.endswith(key) or key == t:
                            try:
                                msg = self._classes[t]()
                                msg.MergeFromString(any_msg.value)
                                parsed_entities[eid][t] = msg
                            except Exception:
                                pass

            own_ship_eid = None
            camera_eid = None
            for eid, comps in parsed_entities.items():
                if "vstep.viewports.AssignedCamera" in comps:
                    camera_eid = comps["vstep.viewports.AssignedCamera"].entity
                    break

            parent_map = {}
            for eid, comps in parsed_entities.items():
                rel = comps.get("vstep.entities.Relations")
                if rel:
                    for child in rel.children:
                        parent_map[child] = eid

            if camera_eid:
                curr = camera_eid
                path = []
                while True:
                    parent = parent_map.get(curr)
                    if parent:
                        path.append(parent)
                        curr = parent
                    else:
                        break
                for peid in path:
                    if peid in parsed_entities and "vstep.equipment.MMSI" in parsed_entities[peid]:
                        own_ship_eid = peid
                        break

            if own_ship_eid is not None:
                descendants = set()
                to_visit = [own_ship_eid]
                while to_visit:
                    curr = to_visit.pop()
                    if curr != own_ship_eid:
                        descendants.add(curr)
                    rel = parsed_entities.get(curr, {}).get("vstep.entities.Relations")
                    if rel:
                        for child in rel.children:
                            if child not in descendants and child != own_ship_eid:
                                to_visit.append(child)

                for eid in [own_ship_eid] + list(descendants):
                    if eid in parsed_entities and "vstep.sensors.CompassBaseOutput" in parsed_entities[eid]:
                        compass = parsed_entities[eid]["vstep.sensors.CompassBaseOutput"]
                        return math.degrees(compass.heading) % 360.0

            for eid, comps in parsed_entities.items():
                if "vstep.sensors.CompassBaseOutput" in comps:
                    compass = comps["vstep.sensors.CompassBaseOutput"]
                    return math.degrees(compass.heading) % 360.0
        except Exception as e:
            print(f"[radar_ctrl] get_heading failed: {e}")
        return None

    def _set_component(self, entity_id: int, component_msg):
        """Send a SetComponents call."""
        if not self._connected:
            return False
        try:
            comp_data = self._classes["vstep.entities.ComponentData"]()
            comp_data.entity.id = entity_id
            comp_data.data.Pack(component_msg)

            req = self._classes["vstep.entities.SetComponentsRequest"]()
            req.data.append(comp_data)

            set_stub = self._channel.unary_unary(
                "/vstep.entities.Registry/SetComponents",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=self._classes["vstep.entities.SetComponentsResponse"].FromString,
            )
            set_stub(req)
            return True
        except Exception as e:
            print(f"[radar_ctrl] SetComponents failed: {e}")
            return False

    def set_gain(self, gain_norm: float):
        """Set radar gain (0.0–1.0 normalised)."""
        if "vstep.radar.RadarParams" not in self._classes:
            return
        if not self._ensure_radar_entity():
            return
        params = self._classes["vstep.radar.RadarParams"]()
        params.gain = gain_norm
        self._set_component(self._radar_entity_id, params)

    def set_sea_clutter(self, strength: float):
        """Set sea clutter filter strength (0.0–1.0)."""
        if "vstep.radar.ClutterParams" not in self._classes:
            return
        if not self._ensure_radar_entity():
            return
        params = self._classes["vstep.radar.ClutterParams"]()
        params.sea_filter_strength = strength
        self._set_component(self._radar_entity_id, params)

    def set_rain_clutter(self, strength: float):
        """Set rain clutter filter strength (0.0–1.0)."""
        if "vstep.radar.ClutterParams" not in self._classes:
            return
        if not self._ensure_radar_entity():
            return
        params = self._classes["vstep.radar.ClutterParams"]()
        params.rain_filter_strength = strength
        self._set_component(self._radar_entity_id, params)

    def set_transmit(self, enabled: bool):
        """Enable/disable radar transmit (render_enabled)."""
        if "vstep.radar.OperationState" not in self._classes:
            return
        if not self._ensure_radar_entity():
            return
        state = self._classes["vstep.radar.OperationState"]()
        state.render_enabled = enabled
        self._set_component(self._radar_entity_id, state)


# ─── Connection Settings Dialog ───────────────────────────────────────────────

class ConnectionDialog(QDialog):
    def __init__(self, udp_port: int, grpc_host: str, grpc_port: int,
                 splitter_enabled: bool, splitter_port: int, splitter_forward: bool, splitter_remotes: str,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        # Basic Settings Group
        basic_group = QGroupBox("Radar Display Settings")
        basic_layout = QFormLayout(basic_group)

        self.udp_port_spin = QSpinBox()
        self.udp_port_spin.setRange(1024, 65535)
        self.udp_port_spin.setValue(udp_port)
        basic_layout.addRow("Radar Listen Port:", self.udp_port_spin)

        self.grpc_host_edit = QLineEdit(grpc_host)
        basic_layout.addRow("Simulator IP (gRPC):", self.grpc_host_edit)

        self.grpc_port_spin = QSpinBox()
        self.grpc_port_spin.setRange(1024, 65535)
        self.grpc_port_spin.setValue(grpc_port)
        basic_layout.addRow("Simulator gRPC Port:", self.grpc_port_spin)

        layout.addWidget(basic_group)

        # Splitter Settings Group
        split_group = QGroupBox("Integrated UDP Splitter (Sim Machine)")
        split_layout = QFormLayout(split_group)

        self.split_enable_cb = QCheckBox("Enable Background Splitter")
        self.split_enable_cb.setChecked(splitter_enabled)
        split_layout.addRow("", self.split_enable_cb)

        self.split_port_spin = QSpinBox()
        self.split_port_spin.setRange(1024, 65535)
        self.split_port_spin.setValue(splitter_port)
        split_layout.addRow("Splitter Listen Port:", self.split_port_spin)

        self.split_forward_cb = QCheckBox("Forward to In-Game Radar")
        self.split_forward_cb.setChecked(splitter_forward)
        split_layout.addRow("", self.split_forward_cb)

        self.split_remotes_edit = QLineEdit(splitter_remotes)
        self.split_remotes_edit.setPlaceholderText("e.g. 192.168.1.50, 192.168.1.51")
        split_layout.addRow("Remote Display IPs:", self.split_remotes_edit)

        layout.addWidget(split_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def udp_port(self): return self.udp_port_spin.value()
    @property
    def grpc_host(self): return self.grpc_host_edit.text().strip()
    @property
    def grpc_port(self): return self.grpc_port_spin.value()
    @property
    def splitter_enabled(self): return self.split_enable_cb.isChecked()
    @property
    def splitter_port(self): return self.split_port_spin.value()
    @property
    def splitter_forward(self): return self.split_forward_cb.isChecked()
    @property
    def splitter_remotes(self): return self.split_remotes_edit.text().strip()


# ─── Main Window ──────────────────────────────────────────────────────────────

class RadarMainWindow(QMainWindow):
    grpc_connected = Signal(bool, str, object)
    heading_received = Signal(float)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("NAUTIS Standalone Radar Display")
        self.setMinimumSize(850, 720)

        # Default port 53457 to match NMEA Bridge/sim default for gRPC, and 54322 for UDP
        self._udp_port = 54322
        self._grpc_host = "127.0.0.1"
        self._grpc_port = 53457

        # Splitter settings
        self._splitter_enabled = True
        self._splitter_listen_port = 54321
        self._splitter_forward_ingame = True
        self._splitter_remote_hosts = ""
        self._splitter_thread = None

        self._controller = RadarController(self._grpc_host, self._grpc_port)
        self._transmitting = True

        self._build_ui()
        self._apply_stylesheet()
        self._ppi.set_standby(False)
        self._start_receiver()
        self._start_splitter()

        # Heading poll timer (1 Hz)
        self._heading_timer = QTimer(self)
        self._heading_timer.setInterval(1000)
        self._heading_timer.timeout.connect(self._poll_heading)

        # Connect thread-safe signals
        self.grpc_connected.connect(self._on_grpc_connected_result)
        self.heading_received.connect(self._ppi.set_heading)

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        # Left: PPI radar display
        self._ppi = RadarPPI()
        root_layout.addWidget(self._ppi, stretch=1)

        # Right: Control panel
        ctrl_panel = self._build_control_panel()
        root_layout.addWidget(ctrl_panel, stretch=0)

    def _build_control_panel(self):
        panel = QWidget()
        panel.setFixedWidth(200)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(6)

        # ── Title ─────────────────────────────────────────────────────────
        title = QLabel("RADAR")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Consolas", 18, QFont.Bold))
        title.setStyleSheet("color: #00FF00; letter-spacing: 4px;")
        layout.addWidget(title)

        version_lbl = QLabel(f"v{__version__}")
        version_lbl.setAlignment(Qt.AlignCenter)
        version_lbl.setFont(QFont("Consolas", 8))
        version_lbl.setStyleSheet("color: #006600;")
        layout.addWidget(version_lbl)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #004400;")
        layout.addWidget(sep)

        # ── TX / STBY ──────────────────────────────────────────────────────
        tx_group = QGroupBox("Transmit")
        tx_layout = QHBoxLayout(tx_group)
        self._tx_btn = QPushButton("TX")
        self._stby_btn = QPushButton("STBY")
        self._tx_btn.setCheckable(True)
        self._stby_btn.setCheckable(True)
        self._tx_btn.setChecked(True)
        self._tx_btn.clicked.connect(self._on_tx)
        self._stby_btn.clicked.connect(self._on_stby)
        tx_layout.addWidget(self._tx_btn)
        tx_layout.addWidget(self._stby_btn)
        layout.addWidget(tx_group)

        # ── Range ──────────────────────────────────────────────────────────
        range_group = QGroupBox("Range")
        range_layout = QVBoxLayout(range_group)
        self._range_combo = QComboBox()
        for r in RANGE_OPTIONS_NM:
            self._range_combo.addItem(f"{r} NM")
        self._range_combo.setCurrentIndex(4)  # Default 3 NM
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        range_layout.addWidget(self._range_combo)
        layout.addWidget(range_group)

        # ── Orientation ────────────────────────────────────────────────────
        orient_group = QGroupBox("Orientation")
        orient_layout = QHBoxLayout(orient_group)
        self._hu_btn = QPushButton("HU")
        self._nu_btn = QPushButton("NU")
        self._hu_btn.setCheckable(True)
        self._nu_btn.setCheckable(True)
        self._hu_btn.setChecked(True)
        self._hu_btn.clicked.connect(self._on_hu)
        self._nu_btn.clicked.connect(self._on_nu)
        orient_layout.addWidget(self._hu_btn)
        orient_layout.addWidget(self._nu_btn)
        layout.addWidget(orient_group)

        # ── Plotting Tools ─────────────────────────────────────────────────
        plot_group = QGroupBox("Plotting Tools")
        plot_layout = QVBoxLayout(plot_group)
        plot_layout.setSpacing(4)
        plot_layout.setContentsMargins(4, 4, 4, 4)

        # EBL row
        ebl_row = QHBoxLayout()
        self._ebl_btn = QPushButton("EBL")
        self._ebl_btn.setCheckable(True)
        self._ebl_btn.clicked.connect(self._on_ebl_toggled)
        self._ebl_spin = QSpinBox()
        self._ebl_spin.setRange(0, 359)
        self._ebl_spin.setSuffix("°")
        self._ebl_spin.setValue(0)
        self._ebl_spin.valueChanged.connect(self._on_ebl_val_changed)
        ebl_row.addWidget(self._ebl_btn, stretch=1)
        ebl_row.addWidget(self._ebl_spin, stretch=1)
        plot_layout.addLayout(ebl_row)

        # VRM row
        vrm_row = QHBoxLayout()
        self._vrm_btn = QPushButton("VRM")
        self._vrm_btn.setCheckable(True)
        self._vrm_btn.clicked.connect(self._on_vrm_toggled)
        self._vrm_spin = QDoubleSpinBox()
        self._vrm_spin.setRange(0.05, 24.0)
        self._vrm_spin.setDecimals(2)
        self._vrm_spin.setSingleStep(0.1)
        self._vrm_spin.setSuffix(" NM")
        self._vrm_spin.setValue(1.0)
        self._vrm_spin.valueChanged.connect(self._on_vrm_val_changed)
        vrm_row.addWidget(self._vrm_btn, stretch=1)
        vrm_row.addWidget(self._vrm_spin, stretch=1)
        plot_layout.addLayout(vrm_row)

        # Parallel Index (PI) row
        pi_row = QHBoxLayout()
        self._pi_btn = QPushButton("PI")
        self._pi_btn.setCheckable(True)
        self._pi_btn.clicked.connect(self._on_pi_toggled)
        self._pi_spin = QDoubleSpinBox()
        self._pi_spin.setRange(0.01, 5.0)
        self._pi_spin.setDecimals(2)
        self._pi_spin.setSingleStep(0.05)
        self._pi_spin.setSuffix(" NM")
        self._pi_spin.setValue(0.5)
        self._pi_spin.valueChanged.connect(self._on_pi_val_changed)
        pi_row.addWidget(self._pi_btn, stretch=1)
        pi_row.addWidget(self._pi_spin, stretch=1)
        plot_layout.addLayout(pi_row)

        layout.addWidget(plot_group)

        # ── Gain ───────────────────────────────────────────────────────────
        layout.addWidget(self._make_slider_group("Gain", "gain_slider",
                         0, 200, 100, self._on_gain))

        # ── Sea Clutter ────────────────────────────────────────────────────
        layout.addWidget(self._make_slider_group("Sea Clutter", "sea_slider",
                         0, 100, 0, self._on_sea))

        # ── Rain Clutter ───────────────────────────────────────────────────
        layout.addWidget(self._make_slider_group("Rain Clutter", "rain_slider",
                         0, 100, 0, self._on_rain))

        # ── Persistence ────────────────────────────────────────────────────
        def format_persistence(v):
            if v == 0:   return "Single Sweep"
            elif v <= 25: return f"Light Ghost ({v})"
            elif v <= 60: return f"Medium Ghost ({v})"
            elif v <= 90: return f"Heavy Ghost ({v})"
            else:         return f"Full Persistence ({v})"

        layout.addWidget(self._make_slider_group("Persistence", "persist_slider",
                         0, 100, 0, self._on_persistence, formatter=format_persistence))

        layout.addStretch()

        # ── Connection ─────────────────────────────────────────────────────
        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color: #004400;")
        layout.addWidget(sep2)

        self._status_label = QLabel("Not connected")
        self._status_label.setWordWrap(True)
        self._status_label.setAlignment(Qt.AlignCenter)
        self._status_label.setFont(QFont("Consolas", 7))
        self._status_label.setStyleSheet("color: #888800;")
        layout.addWidget(self._status_label)

        conn_btn = QPushButton("Connection Settings")
        conn_btn.clicked.connect(self._on_connection_settings)
        layout.addWidget(conn_btn)

        grpc_btn = QPushButton("Connect gRPC")
        grpc_btn.clicked.connect(self._on_connect_grpc)
        layout.addWidget(grpc_btn)

        return panel

    def _make_slider_group(self, label: str, attr_name: str, min_v, max_v, default,
                           callback, formatter=str) -> QGroupBox:
        group = QGroupBox(label)
        v_layout = QVBoxLayout(group)
        v_layout.setSpacing(2)
        v_layout.setContentsMargins(4, 4, 4, 4)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(min_v, max_v)
        slider.setValue(default)
        val_label = QLabel(formatter(default))
        val_label.setAlignment(Qt.AlignCenter)
        val_label.setFont(QFont("Consolas", 8))
        slider.valueChanged.connect(lambda v, lbl=val_label: lbl.setText(formatter(v)))
        slider.valueChanged.connect(callback)
        v_layout.addWidget(slider)
        v_layout.addWidget(val_label)
        setattr(self, attr_name, slider)
        return group

    def _apply_stylesheet(self):
        self.setStyleSheet("""
            QMainWindow, QWidget {
                background-color: #050f05;
                color: #00CC00;
            }
            QGroupBox {
                border: 1px solid #004400;
                border-radius: 4px;
                margin-top: 6px;
                color: #00CC00;
                font-family: Consolas;
                font-size: 9px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 6px;
                padding: 0 4px;
            }
            QSlider::groove:horizontal {
                height: 6px;
                background: #002200;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #00AA00;
                border: 1px solid #00FF00;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
            QSlider::sub-page:horizontal {
                background: #006600;
                border-radius: 3px;
            }
            QPushButton {
                background-color: #002800;
                border: 1px solid #006600;
                border-radius: 4px;
                color: #00CC00;
                font-family: Consolas;
                font-size: 11px;
                padding: 4px;
            }
            QPushButton:hover { background-color: #004400; }
            QPushButton:checked { background-color: #006600; color: #00FF00;
                                  border-color: #00FF00; }
            QPushButton:pressed { background-color: #005500; }
            QComboBox {
                background-color: #002800;
                border: 1px solid #006600;
                color: #00CC00;
                font-family: Consolas;
                padding: 3px;
            }
            QComboBox::drop-down { border: none; }
            QComboBox QAbstractItemView {
                background: #001800;
                color: #00CC00;
                selection-background-color: #004400;
            }
            QLabel { color: #00CC00; font-family: Consolas; }
            QLineEdit, QSpinBox, QDoubleSpinBox {
                background-color: #001800;
                border: 1px solid #006600;
                color: #00CC00;
                font-family: Consolas;
            }
            QDialog {
                background-color: #050f05;
            }
            QDialogButtonBox QPushButton {
                min-width: 60px;
            }
        """)

    # ── Signal Handlers ───────────────────────────────────────────────────────

    def _on_hu(self):
        self._hu_btn.setChecked(True)
        self._nu_btn.setChecked(False)
        self._ppi.set_orientation_mode("Heading Up")

    def _on_nu(self):
        self._hu_btn.setChecked(False)
        self._nu_btn.setChecked(True)
        self._ppi.set_orientation_mode("North Up")
        if not self._controller._connected:
            self._status_label.setText("North Up requires gRPC!")
            self._status_label.setStyleSheet("color: #FF8800;")

    def _on_ebl_toggled(self, checked: bool):
        self._ppi.set_ebl_enabled(checked)

    def _on_ebl_val_changed(self, val: int):
        self._ppi.set_ebl_bearing(val)

    def _on_vrm_toggled(self, checked: bool):
        self._ppi.set_vrm_enabled(checked)

    def _on_vrm_val_changed(self, val: float):
        self._ppi.set_vrm_range_nm(val)

    def _on_pi_toggled(self, checked: bool):
        self._ppi.set_pi_enabled(checked)

    def _on_pi_val_changed(self, val: float):
        self._ppi.set_pi_offset_nm(val)

    def _on_tx(self):
        self._transmitting = True
        self._tx_btn.setChecked(True)
        self._stby_btn.setChecked(False)
        self._ppi.set_standby(False)

    def _on_stby(self):
        self._transmitting = False
        self._stby_btn.setChecked(True)
        self._tx_btn.setChecked(False)
        self._ppi.set_standby(True)

    def _on_range_changed(self, index: int):
        range_m = RANGE_OPTIONS_NM[index] * NM_TO_METERS
        self._ppi.set_range_m(range_m)

    def _on_gain(self, value: int):
        gain = value / 100.0  # 0–2.0
        self._ppi.set_gain(gain)

    def _on_sea(self, value: int):
        pass

    def _on_rain(self, value: int):
        pass

    def _on_persistence(self, value: int):
        self._ppi.set_persistence(value)

    def _on_connection_settings(self):
        dlg = ConnectionDialog(
            self._udp_port, self._grpc_host, self._grpc_port,
            self._splitter_enabled, self._splitter_listen_port,
            self._splitter_forward_ingame, self._splitter_remote_hosts,
            self
        )
        if dlg.exec() == QDialog.Accepted:
            self._udp_port = dlg.udp_port
            self._grpc_host = dlg.grpc_host
            self._grpc_port = dlg.grpc_port
            self._splitter_enabled = dlg.splitter_enabled
            self._splitter_listen_port = dlg.splitter_port
            self._splitter_forward_ingame = dlg.splitter_forward
            self._splitter_remote_hosts = dlg.splitter_remotes

            # Restart receiver with new port (non-blocking)
            self._receiver.stop()
            self._receiver = AsterixReceiver(self._udp_port)
            self._receiver.spoke_received.connect(self._ppi.add_spoke)
            self._receiver.status_changed.connect(self._on_status_changed)
            self._receiver.start()

            # Restart splitter
            self._start_splitter()

            self._status_label.setText(f"Restarting...")

    def _on_connect_grpc(self):
        # Stop heading polling first so the old channel isn't used mid-close
        self._heading_timer.stop()
        self._status_label.setText("Connecting gRPC...")
        self._status_label.setStyleSheet("color: #AAAAAA;")

        old_controller = self._controller
        new_host = self._grpc_host
        new_port = self._grpc_port

        def _do_connect():
            # Cleanly close the old channel first; give gRPC C threads 300 ms to drain
            try:
                old_controller.close()
            except Exception:
                pass
            time.sleep(0.35)

            # Build a fresh controller
            ctrl = RadarController(new_host, new_port)
            ok, msg = ctrl.connect()

            # Hand the new controller to the main thread via signal
            self.grpc_connected.emit(ok, msg, None)
            # Store it here too so the main-thread handler can pick it up
            self._pending_controller = ctrl if ok else None

        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_grpc_connected_result(self, ok: bool, msg: str, eid):
        # Swap in the new controller if connection succeeded
        if ok and getattr(self, '_pending_controller', None) is not None:
            self._controller = self._pending_controller
        self._pending_controller = None

        self._ppi.set_grpc_connected(ok)
        if ok:
            self._status_label.setText("gRPC OK\nHeading active")
            self._status_label.setStyleSheet("color: #00FF00;")
            self._heading_timer.start()
        else:
            self._status_label.setText(f"Connection failed:\n{msg}")
            self._status_label.setStyleSheet("color: #FF4400;")

    def _poll_heading(self):
        """Poll simulator for own-ship heading in a background thread."""
        if not self._controller._connected:
            self._heading_timer.stop()
            self._ppi.set_grpc_connected(False)
            return

        def _do_poll():
            hdg = self._controller.get_heading()
            if hdg is not None:
                self.heading_received.emit(hdg)

        threading.Thread(target=_do_poll, daemon=True).start()

    def _on_status_changed(self, msg: str):
        self._status_label.setText(msg)

    # ── Receiver ─────────────────────────────────────────────────────────────

    def _start_receiver(self):
        self._receiver = AsterixReceiver(self._udp_port)
        self._receiver.spoke_received.connect(self._ppi.add_spoke)
        self._receiver.status_changed.connect(self._on_status_changed)
        self._receiver.start()

    def _start_splitter(self):
        if self._splitter_thread:
            self._splitter_thread.stop()
            self._splitter_thread.wait(2000)
            self._splitter_thread = None
        
        if self._splitter_enabled:
            hosts = [h.strip() for h in self._splitter_remote_hosts.split(",") if h.strip()]
            display_hosts = ["127.0.0.1"] + hosts
            self._splitter_thread = RadarSplitterThread(
                listen_port=self._splitter_listen_port,
                ingame_port=44444,
                display_hosts=display_hosts,
                display_port=self._udp_port,
                forward_ingame=self._splitter_forward_ingame
            )
            self._splitter_thread.status_changed.connect(self._on_splitter_status)
            self._splitter_thread.start()

    def _on_splitter_status(self, msg: str):
        print(f"[Splitter] {msg}")

    def closeEvent(self, event):
        self._heading_timer.stop()
        self._receiver.stop()
        self._receiver.wait(3000)
        if self._splitter_thread:
            self._splitter_thread.stop()
            self._splitter_thread.wait(2000)
        try:
            self._controller.close()
        except Exception:
            pass
        super().closeEvent(event)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("NAUTIS Radar Display")
    app.setStyle("Fusion")

    win = RadarMainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
