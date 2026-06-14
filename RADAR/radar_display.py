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
    QRadialGradient, QLinearGradient, QFontMetrics, QIcon, QConicalGradient
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QLabel, QSlider, QPushButton, QComboBox, QGroupBox, QSizePolicy,
    QLineEdit, QSpinBox, QDialog, QFormLayout, QDialogButtonBox,
    QMessageBox, QFrame
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

    def __init__(self, port: int = 54321, parent=None):
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
        self._persistence = 0.85   # How much each frame decays (0=none, 0.99=max)

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

        # Fade timer — applies persistence decay every N ms
        self._fade_timer = QTimer(self)
        self._fade_timer.timeout.connect(self._apply_fade)
        self._fade_timer.start(100)  # 10 fps fade

        self._mutex = QMutex()

    def set_range_m(self, range_m: float):
        self._range_m = range_m

    def set_gain(self, gain: float):
        self._gain = gain  # 0.0 – 2.0

    def set_persistence(self, p: float):
        self._persistence = p

    def _ensure_ppi_image(self, size: int):
        if self._ppi_image is None or self._ppi_size != size:
            self._ppi_size = size
            self._ppi_image = QImage(size, size, QImage.Format.Format_ARGB32)
            self._ppi_image.fill(QColor(0, 0, 0, 255))

    def add_spoke(self, spoke: RadarSpoke):
        """Draw a radar spoke onto the persistence buffer."""
        with QMutexLocker(self._mutex):
            # Determine PPI image size based on current widget size
            side = min(self.width(), self.height()) - 4
            if side < 50:
                return
            self._ensure_ppi_image(side)

            center = side / 2.0
            px_per_meter = center / self._range_m

            # Mid-azimuth for this spoke
            az_mid_rad = math.radians((spoke.start_az_deg + spoke.end_az_deg) / 2.0)

            # Draw cells
            painter = QPainter(self._ppi_image)
            painter.setRenderHint(QPainter.Antialiasing, False)

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

    def _apply_fade(self):
        """Apply persistence decay to the PPI buffer."""
        if self._ppi_image is None:
            return
        # Use QPainter with a semi-transparent black overlay to simulate decay
        with QMutexLocker(self._mutex):
            fade_alpha = int((1.0 - self._persistence) * 255)
            if fade_alpha <= 0:
                return
            painter = QPainter(self._ppi_image)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.fillRect(self._ppi_image.rect(), QColor(0, 0, 0, fade_alpha))
            painter.end()
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
        az_rad = math.radians(self._current_az)
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
            fan_az = math.radians(self._current_az - deg_back)
            fan_x = cx + radius * math.sin(fan_az)
            fan_y = cy - radius * math.cos(fan_az)
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

        # ── Bearing scale (tick marks every 10°, labels every 30°) ──────────
        painter.setFont(QFont("Consolas", 7))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            sin_r, cos_r = math.sin(rad), math.cos(rad)
            is_major = (deg % 30 == 0)
            tick_len = 10 if is_major else 5
            x1 = cx + (radius) * sin_r
            y1 = cy - (radius) * cos_r
            x2 = cx + (radius - tick_len) * sin_r
            y2 = cy - (radius - tick_len) * cos_r
            tick_pen = QPen(QColor(0, 200, 0, 220 if is_major else 120))
            tick_pen.setWidth(1)
            painter.setPen(tick_pen)
            painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
            if is_major:
                label = str(deg)
                lx = cx + (radius + 12) * sin_r - 8
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

class RadarController:
    """Thin wrapper around the NAUTIS gRPC interface for radar controls."""

    def __init__(self, grpc_host: str = "127.0.0.1", grpc_port: int = 8086):
        self._host = grpc_host
        self._port = grpc_port
        self._channel = None
        self._classes = {}
        self._radar_entity_id = None
        cur_dir = os.path.dirname(os.path.abspath(__file__))
        self._pb_dir = os.path.join(cur_dir, "proto_extracted")
        if not os.path.exists(self._pb_dir):
            self._pb_dir = os.path.join(os.path.dirname(cur_dir), "proto_extracted")
        self._connected = False

    def connect(self):
        """Load proto descriptors and establish gRPC channel."""
        try:
            import grpc
            from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
            from google.protobuf import any_pb2, timestamp_pb2, duration_pb2

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
            self._connected = True
            return True
        except Exception as e:
            print(f"[radar_ctrl] Connect failed: {e}")
            self._connected = False
            return False

    def _find_radar_entity(self) -> int | None:
        """Use GetComponents to find the first entity with RadarParams."""
        if not self._connected or "vstep.radar.RadarParams" not in self._classes:
            return None
        try:
            import grpc
            sel = self._classes["vstep.entities.EntitySelection"]()
            sel.all_root_entities.CopyFrom(self._classes["vstep.entities.AllRootEntities"]())
            sel.recursion = 99

            query = self._classes["vstep.entities.GetComponentsRequest.Query"]()
            query.component_types.append("vstep.radar.RadarParams")
            query.entities.append(sel)

            req = self._classes["vstep.entities.GetComponentsRequest"]()

            get_stub = self._channel.unary_stream(
                "/vstep.entities.Registry/GetComponents",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=self._classes["vstep.entities.GetComponentsResponse"].FromString,
            )
            for resp in get_stub(req):
                for comp in resp.components:
                    return comp.entity_id
        except Exception as e:
            print(f"[radar_ctrl] Find entity failed: {e}")
        return None

    def _set_component(self, entity_id: int, component_msg):
        """Send a SetComponents call."""
        if not self._connected:
            return False
        try:
            from google.protobuf import any_pb2

            comp_data = self._classes["vstep.entities.ComponentData"]()
            comp_data.entity_id = entity_id
            comp_data.data.Pack(component_msg)

            req = self._classes["vstep.entities.SetComponentsRequest"]()
            req.components.append(comp_data)

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
        if self._radar_entity_id is None:
            self._radar_entity_id = self._find_radar_entity()
        if self._radar_entity_id is None:
            return
        params = self._classes["vstep.radar.RadarParams"]()
        params.gain = gain_norm
        self._set_component(self._radar_entity_id, params)

    def set_sea_clutter(self, strength: float):
        """Set sea clutter filter strength (0.0–1.0)."""
        if "vstep.radar.ClutterParams" not in self._classes:
            return
        if self._radar_entity_id is None:
            self._radar_entity_id = self._find_radar_entity()
        if self._radar_entity_id is None:
            return
        params = self._classes["vstep.radar.ClutterParams"]()
        params.sea_filter_strength = strength
        self._set_component(self._radar_entity_id, params)

    def set_rain_clutter(self, strength: float):
        """Set rain clutter filter strength (0.0–1.0)."""
        if "vstep.radar.ClutterParams" not in self._classes:
            return
        if self._radar_entity_id is None:
            self._radar_entity_id = self._find_radar_entity()
        if self._radar_entity_id is None:
            return
        params = self._classes["vstep.radar.ClutterParams"]()
        params.rain_filter_strength = strength
        self._set_component(self._radar_entity_id, params)

    def set_transmit(self, enabled: bool):
        """Enable/disable radar transmit (render_enabled)."""
        if "vstep.radar.OperationState" not in self._classes:
            return
        if self._radar_entity_id is None:
            self._radar_entity_id = self._find_radar_entity()
        if self._radar_entity_id is None:
            return
        state = self._classes["vstep.radar.OperationState"]()
        state.render_enabled = enabled
        self._set_component(self._radar_entity_id, state)


# ─── Connection Settings Dialog ───────────────────────────────────────────────

class ConnectionDialog(QDialog):
    def __init__(self, udp_port: int, grpc_host: str, grpc_port: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connection Settings")
        self.setMinimumWidth(350)

        layout = QFormLayout(self)

        self.udp_port_spin = QSpinBox()
        self.udp_port_spin.setRange(1024, 65535)
        self.udp_port_spin.setValue(udp_port)
        layout.addRow("UDP Listen Port:", self.udp_port_spin)

        self.grpc_host_edit = QLineEdit(grpc_host)
        layout.addRow("Simulator IP (gRPC):", self.grpc_host_edit)

        self.grpc_port_spin = QSpinBox()
        self.grpc_port_spin.setRange(1024, 65535)
        self.grpc_port_spin.setValue(grpc_port)
        layout.addRow("Simulator gRPC Port:", self.grpc_port_spin)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    @property
    def udp_port(self): return self.udp_port_spin.value()
    @property
    def grpc_host(self): return self.grpc_host_edit.text().strip()
    @property
    def grpc_port(self): return self.grpc_port_spin.value()


# ─── Main Window ──────────────────────────────────────────────────────────────

class RadarMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("NAUTIS Standalone Radar Display")
        self.setMinimumSize(820, 680)

        self._udp_port = 54322
        self._grpc_host = "127.0.0.1"
        self._grpc_port = 8086

        self._controller = RadarController(self._grpc_host, self._grpc_port)
        self._transmitting = True

        self._build_ui()
        self._apply_stylesheet()
        self._start_receiver()

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
        layout.setSpacing(8)

        # ── Title ─────────────────────────────────────────────────────────
        title = QLabel("RADAR")
        title.setAlignment(Qt.AlignCenter)
        title.setFont(QFont("Consolas", 18, QFont.Bold))
        title.setStyleSheet("color: #00FF00; letter-spacing: 4px;")
        layout.addWidget(title)

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
        layout.addWidget(self._make_slider_group("Persistence", "persist_slider",
                         0, 98, 85, self._on_persistence))

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
                           callback) -> QGroupBox:
        group = QGroupBox(label)
        v_layout = QVBoxLayout(group)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(min_v, max_v)
        slider.setValue(default)
        val_label = QLabel(f"{default}")
        val_label.setAlignment(Qt.AlignCenter)
        val_label.setFont(QFont("Consolas", 8))
        slider.valueChanged.connect(lambda v, lbl=val_label: lbl.setText(str(v)))
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
            QLineEdit, QSpinBox {
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

    def _on_tx(self):
        self._transmitting = True
        self._tx_btn.setChecked(True)
        self._stby_btn.setChecked(False)
        if self._controller._connected:
            threading.Thread(
                target=self._controller.set_transmit,
                args=(True,), daemon=True
            ).start()

    def _on_stby(self):
        self._transmitting = False
        self._stby_btn.setChecked(True)
        self._tx_btn.setChecked(False)
        if self._controller._connected:
            threading.Thread(
                target=self._controller.set_transmit,
                args=(False,), daemon=True
            ).start()

    def _on_range_changed(self, index: int):
        range_m = RANGE_OPTIONS_NM[index] * NM_TO_METERS
        self._ppi.set_range_m(range_m)

    def _on_gain(self, value: int):
        gain = value / 100.0  # 0–2.0
        self._ppi.set_gain(gain)
        if self._controller._connected:
            threading.Thread(
                target=self._controller.set_gain,
                args=(value / 200.0,), daemon=True
            ).start()

    def _on_sea(self, value: int):
        if self._controller._connected:
            threading.Thread(
                target=self._controller.set_sea_clutter,
                args=(value / 100.0,), daemon=True
            ).start()

    def _on_rain(self, value: int):
        if self._controller._connected:
            threading.Thread(
                target=self._controller.set_rain_clutter,
                args=(value / 100.0,), daemon=True
            ).start()

    def _on_persistence(self, value: int):
        self._ppi.set_persistence(value / 100.0)

    def _on_connection_settings(self):
        dlg = ConnectionDialog(
            self._udp_port, self._grpc_host, self._grpc_port, self
        )
        if dlg.exec() == QDialog.Accepted:
            self._udp_port = dlg.udp_port
            self._grpc_host = dlg.grpc_host
            self._grpc_port = dlg.grpc_port
            # Restart receiver with new port
            self._receiver.stop()
            self._receiver.wait(2000)
            self._receiver.set_port(self._udp_port)
            self._receiver.start()
            self._status_label.setText(f"Restarting on port {self._udp_port}...")

    def _on_connect_grpc(self):
        self._controller = RadarController(self._grpc_host, self._grpc_port)
        self._status_label.setText("Connecting gRPC...")
        def _do_connect():
            ok = self._controller.connect()
            if ok:
                eid = self._controller._find_radar_entity()
                self._controller._radar_entity_id = eid
                if eid is not None:
                    self._status_label.setText(
                        f"gRPC OK\nRadar entity: {eid}"
                    )
                    self._status_label.setStyleSheet("color: #00FF00;")
                else:
                    self._status_label.setText("gRPC OK\nNo radar entity found")
                    self._status_label.setStyleSheet("color: #AAAA00;")
            else:
                self._status_label.setText("gRPC FAILED")
                self._status_label.setStyleSheet("color: #FF4400;")
        threading.Thread(target=_do_connect, daemon=True).start()

    def _on_status_changed(self, msg: str):
        self._status_label.setText(msg)

    # ── Receiver ─────────────────────────────────────────────────────────────

    def _start_receiver(self):
        self._receiver = AsterixReceiver(self._udp_port)
        self._receiver.spoke_received.connect(self._ppi.add_spoke)
        self._receiver.status_changed.connect(self._on_status_changed)
        self._receiver.start()

    def closeEvent(self, event):
        self._receiver.stop()
        self._receiver.wait(3000)
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
