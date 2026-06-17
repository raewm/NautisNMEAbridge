"""
nautis_nmea_bridge.py  -- NAUTIS Home gRPC -> NMEA 0183 UDP Bridge
==================================================================
Subscribes to the NAUTIS Home gRPC registry, resolves vessel telemetry,
and broadcasts standard NMEA 0183 sentences over UDP.

Can be run in headless CLI mode or in GUI mode with PySide6.
"""

# ---------------------------------------------------------------------------
# Version  (bump this when releasing a new .exe build)
# ---------------------------------------------------------------------------
# Changelog:
#   2.2.0  2026-06-15  Fix AIS Type 1 lat/lon two's-complement encoding for
#                      negative coordinates. Fix traffic vessel position/motion
#                      lookup to search descendant entities, not just the root,
#                      so all scenarios emit correct AIS targets.
__version__ = "2.4.0"

import argparse
import math
import os
import socket
import sys
import time
import queue
import threading
from datetime import datetime, timezone

import grpc
from google.protobuf import any_pb2, duration_pb2, timestamp_pb2  # noqa: F401
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf import message_factory

# ---------------------------------------------------------------------------
# Paths and Constants
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
PB_DIR = os.path.join(_BASE, "proto_extracted")

SUBSCRIBE_TYPES = [
    "vstep.sensors.GPSOutput",
    "vstep.sensors.CompassBaseOutput",
    "vstep.sensors.INSOutput",
    "vstep.sensors.DopplerLogOutput",
    "vstep.sensors.DateTimeOutput",
    "vstep.spatial.PositionGeographic",
    "vstep.spatial.LinearMotion",
    "vstep.spatial.AngularMotion",
    "vstep.spatial.OrientationEuler",
    "vstep.entities.Name",
    "vstep.entities.DisplayName",
    "vstep.entities.Relations",
    "vstep.equipment.MMSI",
    "vstep.sensors.RudderIndicatorOutput",
    "vstep.sensors.PropulsionIndicatorOutput",
    "vstep.sensors.WindmeterOutput",
    "vstep.sensors.EchoSounderOutput",
    "vstep.viewports.AssignedCamera",
    "vstep.spatial.BoundingBox",
    # Actuator components for autopilot writing
    "vstep.dynamics.AngleInput",
    "vstep.dynamics.PropulsionInput",
    "vstep.dynamics.RPMInput",
    "vstep.simulation.external.SetExternalControlRequest",
]

# ---------------------------------------------------------------------------
# Protobuf Descriptor Loader
# ---------------------------------------------------------------------------
def load_descriptors(pb_dir: str) -> int:
    pool = descriptor_pool.Default()
    name_to_bytes = {}

    # Add standard Google protobuf descriptors to pool first
    from google.protobuf import any_pb2, timestamp_pb2, duration_pb2
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

    for fname in os.listdir(pb_dir):
        if not fname.endswith(".proto.pb"):
            continue
        with open(os.path.join(pb_dir, fname), "rb") as f:
            data = f.read()
        
        fdp, truncated_data = parse_with_truncation(data)
        if fdp is not None:
            name_to_bytes[fdp.name] = truncated_data

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

    print(f"[bridge] Loaded {len(added)}/{len(name_to_bytes)} proto descriptors.")
    return len(added)

# ---------------------------------------------------------------------------
# NMEA Helpers
# ---------------------------------------------------------------------------
def _nmea_checksum(sentence: str) -> str:
    cs = 0
    for c in sentence:
        cs ^= ord(c)
    return f"{cs:02X}"

def _nmea(body: str) -> str:
    return f"${body}*{_nmea_checksum(body)}\r\n"

def _ddmm(deg: float) -> tuple:
    hem = "N" if deg >= 0 else "S"
    deg = abs(deg)
    d = int(deg)
    m = (deg - d) * 60.0
    return f"{d:02d}{m:08.5f}", hem

def _dddmm(deg: float) -> tuple:
    hem = "E" if deg >= 0 else "W"
    deg = abs(deg)
    d = int(deg)
    m = (deg - d) * 60.0
    return f"{d:03d}{m:08.5f}", hem

def make_gpgga(lat: float, lon: float, utc: datetime) -> str:
    lat_s, lat_h = _ddmm(lat)
    lon_s, lon_h = _dddmm(lon)
    t = utc.strftime("%H%M%S.00")
    return _nmea(f"GPGGA,{t},{lat_s},{lat_h},{lon_s},{lon_h},1,08,0.9,0.0,M,0.0,M,,")

def make_gprmc(lat: float, lon: float, sog_kn: float, cog_deg: float, utc: datetime) -> str:
    lat_s, lat_h = _ddmm(lat)
    lon_s, lon_h = _dddmm(lon)
    t = utc.strftime("%H%M%S.00")
    d = utc.strftime("%d%m%y")
    return _nmea(f"GPRMC,{t},A,{lat_s},{lat_h},{lon_s},{lon_h},{sog_kn:.2f},{cog_deg:.2f},{d},,")

def make_gpvtg(cog_deg: float, sog_kn: float) -> str:
    sog_kmh = sog_kn * 1.852
    return _nmea(f"GPVTG,{cog_deg:.2f},T,,M,{sog_kn:.2f},N,{sog_kmh:.2f},K,A")

def make_gphdg(heading_deg: float) -> str:
    return _nmea(f"GPHDG,{heading_deg:.2f},,,,")

def make_gprot(rot_deg_per_min: float) -> str:
    return _nmea(f"GPROT,{rot_deg_per_min:.2f},A")

def make_iirsa(stbd_deg: float, port_deg: float = None) -> str:
    stbd_s = f"{stbd_deg:.1f}" if stbd_deg is not None else ""
    port_s = f"{port_deg:.1f}" if port_deg is not None else ""
    stbd_valid = "A" if stbd_deg is not None else ""
    port_valid = "A" if port_deg is not None else ""
    return _nmea(f"IIRSA,{stbd_s},{stbd_valid},{port_s},{port_valid}")

def make_iirpm(eng_idx: int, rpm: float) -> str:
    return _nmea(f"IIRPM,E,{eng_idx},{rpm:.1f},A")

def make_iimwv(wind_dir: float, wind_speed_mps: float, is_true: bool = False) -> str:
    ref = "T" if is_true else "R"
    return _nmea(f"IIMWV,{wind_dir:.1f},{ref},{wind_speed_mps:.1f},M,A")

def make_iidbt(depth_m: float) -> str:
    depth_ft = depth_m * 3.28084
    depth_fa = depth_m * 0.546807
    return _nmea(f"IIDBT,{depth_ft:.1f},f,{depth_m:.1f},M,{depth_fa:.1f},F")

def make_iidpt(depth_m: float, draught_m: float = 0.0) -> str:
    return _nmea(f"IIDPT,{depth_m:.1f},{draught_m:.1f}")

def make_gppat(heading: float, pitch: float, roll: float) -> str:
    return _nmea(f"GPPAT,{heading:.2f},{pitch:.2f},{roll:.2f}")

# ---------------------------------------------------------------------------
# AIS Helpers
# ---------------------------------------------------------------------------
def encode_ais_string(s: str, length: int) -> int:
    s = s.upper()[:length]
    s = s.ljust(length, '@')
    bits = 0
    for char in s:
        code = ord(char)
        if 64 <= code <= 95:
            val = code - 64
        elif 32 <= code <= 63:
            val = code
        else:
            val = 0
        bits = (bits << 6) | val
    return bits

def make_ais_sentence(payload: str, is_own: bool = False) -> str:
    talker = "AIVDO" if is_own else "AIVDM"
    body = f"{talker},1,1,,A,{payload},0"
    return f"!{body}*{_nmea_checksum(body)}\r\n"

def make_ais_type1(mmsi: int, lat: float, lon: float, sog_kn: float, cog_deg: float, heading_deg: float, rot_dpm: float, is_own: bool = False) -> str:
    msg_type = 1
    repeat = 0
    mmsi_val = int(mmsi) & 0x3FFFFFFF
    nav_status = 0
    
    if rot_dpm == 0.0:
        rot_ais = 0
    else:
        sign = 1 if rot_dpm > 0 else -1
        try:
            rot_ais = int(sign * 4.733 * math.sqrt(abs(rot_dpm)))
            rot_ais = max(-126, min(126, rot_ais))
        except Exception:
            rot_ais = -128
            
    sog_val = int(sog_kn * 10.0)
    sog_val = max(0, min(1022, sog_val))
    
    pos_accuracy = 1
    # AIS lat/lon are signed integers in 1/10000 minute units (two's complement)
    # lon: 28-bit signed, lat: 27-bit signed
    lon_int = int(round(lon * 600000.0))
    lat_int = int(round(lat * 600000.0))
    lon_val = lon_int & 0xFFFFFFF   # 28-bit two's complement
    lat_val = lat_int & 0x7FFFFFF   # 27-bit two's complement
    
    # COG: 0–3599 in 0.1° units; 3600 = not available
    cog_int = int(round(cog_deg * 10.0)) % 3600
    cog_val = cog_int if cog_int >= 0 else 3600
        
    # Heading: 0–359 degrees true; 511 = not available
    heading_val = int(heading_deg) % 360
    if heading_val < 0:
        heading_val = 511
        
    ts = int(time.time()) % 60
    
    bits = 0
    bits = (bits << 6) | msg_type
    bits = (bits << 2) | repeat
    bits = (bits << 30) | mmsi_val
    bits = (bits << 4) | nav_status
    bits = (bits << 8) | (rot_ais & 0xFF)
    bits = (bits << 10) | sog_val
    bits = (bits << 1) | pos_accuracy
    bits = (bits << 28) | lon_val
    bits = (bits << 27) | lat_val
    bits = (bits << 12) | cog_val
    bits = (bits << 9) | heading_val
    bits = (bits << 6) | ts
    bits = (bits << 2) | 0
    bits = (bits << 3) | 0
    bits = (bits << 1) | 0   # RAIM flag (1 bit)
    # Communication State (19 bits)
    bits = (bits << 19) | 0
    
    payload = ""
    for i in range(27, -1, -1):
        val = (bits >> (i * 6)) & 0x3F
        if val < 40:
            payload += chr(val + 48)
        else:
            payload += chr(val + 56)
            
    return make_ais_sentence(payload, is_own)

def extract_vessel_dimensions(bbox_comp) -> tuple:
    """
    Extract (to_bow, to_stern, to_port, to_starboard) in meters from BoundingBox component.
    Handles coordinate system alignment dynamically using dx/dy comparison.
    Clamps values to standard AIS bit widths (to_bow/stern: 9 bits, to_port/stbd: 6 bits).
    """
    if not bbox_comp:
        return 0, 0, 0, 0
    try:
        box = bbox_comp.box
        min_c = box.minimum_coordinates
        max_c = box.maximum_coordinates
        dy = max_c.y - min_c.y
        dx = max_c.x - min_c.x
        if dx > dy:
            # X is the longitudinal axis (vessel oriented along X-axis)
            to_bow = int(round(max(0.0, max_c.x)))
            to_stern = int(round(max(0.0, -min_c.x)))
            to_port = int(round(max(0.0, -min_c.y)))
            to_starboard = int(round(max(0.0, max_c.y)))
        else:
            # Y is the longitudinal axis (vessel oriented along Y-axis, default)
            to_bow = int(round(max(0.0, max_c.y)))
            to_stern = int(round(max(0.0, -min_c.y)))
            to_port = int(round(max(0.0, -min_c.x)))
            to_starboard = int(round(max(0.0, max_c.x)))
        return (
            min(511, max(0, to_bow)),
            min(511, max(0, to_stern)),
            min(63, max(0, to_port)),
            min(63, max(0, to_starboard))
        )
    except Exception:
        return 0, 0, 0, 0

def make_ais_type5(mmsi: int, name: str, is_own: bool = False, to_bow: int = 0, to_stern: int = 0, to_port: int = 0, to_starboard: int = 0) -> str:
    mmsi = int(mmsi) & 0x3FFFFFFF
    callsign = f"TS{str(mmsi)[-5:]}"
    
    name_bits = encode_ais_string(name, 20)
    call_bits = encode_ais_string(callsign, 7)
    dest_bits = encode_ais_string("NAUTIS", 20)
    
    bits = 0
    bits = (bits << 6) | 5
    bits = (bits << 2) | 0
    bits = (bits << 30) | mmsi
    bits = (bits << 2) | 0
    bits = (bits << 30) | 0
    bits = (bits << 42) | call_bits
    bits = (bits << 120) | name_bits
    bits = (bits << 8) | 70
    
    dim_val = ((to_bow & 0x1FF) << 21) | ((to_stern & 0x1FF) << 12) | ((to_port & 0x3F) << 6) | (to_starboard & 0x3F)
    bits = (bits << 30) | dim_val  # Dimensions: to_bow(9) | to_stern(9) | to_port(6) | to_starboard(6)
    bits = (bits << 4) | 1
    bits = (bits << 20) | 0
    bits = (bits << 8) | 0
    bits = (bits << 120) | dest_bits
    bits = (bits << 1) | 0
    bits = (bits << 1) | 0
    bits = (bits << 2) | 0
    
    payload = ""
    for i in range(70, -1, -1):
        val = (bits >> (i * 6)) & 0x3F
        if val < 40:
            payload += chr(val + 48)
        else:
            payload += chr(val + 56)
            
    return make_ais_sentence(payload, is_own)

# ---------------------------------------------------------------------------
# Dynamic Telemetry Resolver
# ---------------------------------------------------------------------------
class TelemetryResolver:
    def __init__(self):
        self.lat = 0.0
        self.lon = 0.0
        self.sog_kn = 0.0
        self.cog_deg = 0.0
        self.heading_deg = 0.0
        self.rot_dpm = 0.0
        self.sim_dt = None
        self.pitch_deg = 0.0
        self.roll_deg = 0.0

    def resolve(self, components: dict) -> bool:
        self.sim_dt = None
        dt_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.DateTimeOutput"]
        if dt_msgs:
            try:
                m = dt_msgs[0]
                self.sim_dt = datetime(
                    int(m.year), int(m.month), int(m.day),
                    int(m.hours), int(m.minutes), int(m.seconds),
                    tzinfo=timezone.utc
                )
            except Exception:
                pass
        if self.sim_dt is None:
            self.sim_dt = datetime.now(tz=timezone.utc)

        gps_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.GPSOutput"]
        compass_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.CompassBaseOutput"]
        ins_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.INSOutput"]
        doppler_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.DopplerLogOutput"]
        lin_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.LinearMotion"]
        ang_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.AngularMotion"]
        euler_msgs   = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.OrientationEuler"]
        geom_msgs    = {eid: m for (tn, eid), m in components.items() if tn == "vstep.spatial.PositionGeographic"}

        has_pos = False
        if gps_msgs:
            self.lat = gps_msgs[0].latitude
            self.lon = gps_msgs[0].longitude
            has_pos = True
        else:
            motion_eids = [eid for (tn, eid), m in components.items() if tn == "vstep.spatial.LinearMotion"]
            active_eid = next((eid for eid in motion_eids if eid in geom_msgs), None)
            if active_eid is None and geom_msgs:
                active_eid = next(iter(geom_msgs))
            if active_eid is not None:
                m = geom_msgs[active_eid]
                self.lat = m.position.coordinates.latitude
                self.lon = m.position.coordinates.longitude
                has_pos = True

        if compass_msgs:
            self.heading_deg = math.degrees(compass_msgs[0].heading) % 360.0
        elif ins_msgs:
            self.heading_deg = math.degrees(ins_msgs[0].heading) % 360.0
        elif euler_msgs:
            self.heading_deg = math.degrees(euler_msgs[0].angles.z) % 360.0
        elif gps_msgs and gps_msgs[0].cog > 0:
            self.heading_deg = math.degrees(gps_msgs[0].cog) % 360.0
        else:
            self.heading_deg = 0.0

        if euler_msgs:
            self.pitch_deg = math.degrees(euler_msgs[0].angles.x)
            self.roll_deg = math.degrees(euler_msgs[0].angles.y)
        else:
            self.pitch_deg = 0.0
            self.roll_deg = 0.0

        if gps_msgs:
            self.sog_kn = gps_msgs[0].sog * 1.9438445
        elif ins_msgs:
            self.sog_kn = ins_msgs[0].sog * 1.9438445
        elif doppler_msgs:
            self.sog_kn = doppler_msgs[0].sog * 1.9438445
        elif lin_msgs:
            m = lin_msgs[0]
            self.sog_kn = math.sqrt(m.velocity.x**2 + m.velocity.y**2 + m.velocity.z**2) * 1.9438445
        else:
            self.sog_kn = 0.0

        if gps_msgs:
            self.cog_deg = math.degrees(gps_msgs[0].cog) % 360.0
        elif ins_msgs:
            self.cog_deg = math.degrees(ins_msgs[0].cog) % 360.0
        elif lin_msgs:
            vx = lin_msgs[0].velocity.x
            vy = lin_msgs[0].velocity.y
            if abs(vx) > 0.01 or abs(vy) > 0.01:
                self.cog_deg = math.degrees(math.atan2(vx, vy)) % 360.0
            else:
                self.cog_deg = self.heading_deg
        else:
            self.cog_deg = self.heading_deg

        if compass_msgs:
            self.rot_dpm = math.degrees(compass_msgs[0].rot) * 60.0
        elif ins_msgs:
            self.rot_dpm = math.degrees(ins_msgs[0].rot) * 60.0
        elif ang_msgs:
            self.rot_dpm = math.degrees(ang_msgs[0].velocity.z) * 60.0
        else:
            self.rot_dpm = 0.0

        return has_pos

# ---------------------------------------------------------------------------
# Autopilot NMEA UDP Listener Thread
# ---------------------------------------------------------------------------
class AutopilotListener(threading.Thread):
    def __init__(self, port, callback):
        super().__init__()
        self.daemon = True
        self.port = port
        self.callback = callback
        self.running = False
        self.sock = None

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.settimeout(1.0)
            self.running = True
            while self.running:
                try:
                    data, addr = self.sock.recvfrom(4096)
                    sentence = data.decode("ascii", errors="replace").strip()
                    self.callback(sentence)
                except socket.timeout:
                    continue
                except Exception:
                    time.sleep(0.1)
        except Exception as e:
            pass
        finally:
            if self.sock:
                self.sock.close()

    def stop(self):
        self.running = False

# ---------------------------------------------------------------------------
# Core Polling Engine (Thread-Safe Class)
# ---------------------------------------------------------------------------
class NmeaBridgeEngine(threading.Thread):
    def __init__(self, host="127.0.0.1", port=53457, udp_host="127.0.0.1", udp_port=10110, rate=2.0, verbose=False):
        super().__init__()
        self.daemon = True
        self.host = host
        self.port = port
        self.udp_host = udp_host
        self.udp_port = udp_port
        self.rate = rate
        self.verbose = verbose

        self.running = False
        self.stop_requested = False

        # Autopilot states
        self.ap_mode = "Standby"  # "Standby", "Heading", "Route"
        self.ap_target_heading = 0.0
        self.ap_port = 10115
        self.ap_kp = 0.6    # Medium preset default
        self.ap_ki = 0.01
        self.ap_kd = 0.8
        self._last_apb_time = 0.0

        # Autopilot write throttle — AP commands are sent at most once per second.
        # Ships respond on 3-10s timescales; faster writes fight the sim.
        self._ap_write_rate = 1.0   # Hz
        self._last_ap_write_time = 0.0

        # Magnetic variation (degrees, positive = East).
        # Populated if OpenCPN sends Magnetic heading in APB; used to convert
        # to the True heading that the sim compass reports.
        self._magnetic_variation = 0.0   # degrees East (positive)

        # Autopilot telemetry states
        self.ap_current_heading = 0.0
        self.ap_commanded_rudder = 0.0   # last rudder command sent (degrees)
        self.ap_actual_rudder = 0.0      # actual rudder from sim telemetry
        self.ap_xte = 0.0
        self.ap_waypoint = "N/A"
        self._engaged_actuators = set()

        # Active Output toggles (from GUI checkboxes)
        self.toggles = {
            "gpgga": True, "gprmc": True, "gpvtg": True, "gphdg": True, "gprot": True,
            "iirsa": True, "iirpm": True, "iimwv": True, "iidpt": True, "iidbt": True,
            "aivdo": True, "aivdm": True, "pitch": True, "roll": True
        }

        # Shared telemetry data
        self.telemetry_lock = threading.Lock()
        self.telemetry_data = {}

        # Thread-safe console message queue
        self.console_queue = queue.Queue()
        
        # Load PID controller
        from autopilot import PIDController
        self.pid = PIDController(self.ap_kp, self.ap_ki, self.ap_kd, limit=25.0)
        self.ap_listener = None
        self.ap_vessel_preset = "Medium"  # current preset label

    def update_pid_params(self, kp, ki, kd, limit=None):
        self.ap_kp = kp
        self.ap_ki = ki
        self.ap_kd = kd
        self.pid.kp = kp
        self.pid.ki = ki
        self.pid.kd = kd
        if limit is not None:
            self.pid.limit = limit

    def apply_vessel_preset(self, preset_name: str):
        """Apply a named vessel response preset to the PID controller."""
        from autopilot import VESSEL_PRESETS
        if preset_name in VESSEL_PRESETS:
            kp, ki, kd, lim = VESSEL_PRESETS[preset_name]
            self.ap_vessel_preset = preset_name
            self.update_pid_params(kp, ki, kd, limit=lim)
            self.pid.reset()
            self.console_queue.put(f"[AP] Vessel preset '{preset_name}' applied  Kp={kp} Ki={ki} Kd={kd} Lim=±{lim}°")

    def set_autopilot_mode(self, mode):
        self.ap_mode = mode
        if mode == "Standby":
            self.pid.reset()

    def set_target_heading(self, heading):
        self.ap_target_heading = heading % 360.0

    def _handle_incoming_nmea(self, sentence):
        from autopilot import parse_apb
        if "APB" in sentence:
            apb = parse_apb(sentence)
            if apb and apb["valid"]:
                self.ap_xte = apb["xte"]
                self.ap_waypoint = apb["waypoint"]
                self._last_apb_time = time.time()
                if self.ap_mode == "Route" and apb["heading_to_steer"] is not None:
                    hts = apb["heading_to_steer"]

                    # TRUE / MAGNETIC correction ----------------------------------
                    # The sim compass reports True heading.  OpenCPN typically
                    # sends True ('T') in the APB heading-to-steer field, but
                    # some configurations send Magnetic ('M').  If Magnetic,
                    # convert to True by adding the magnetic variation.
                    # Variation is East-positive (e.g. +5 if compass reads 5° high).
                    if apb["heading_ref"] == "M":
                        hts = (hts + self._magnetic_variation) % 360.0

                    # Apply cross-track error (XTE) correction to target heading.
                    # xte in NM; positive = steer right.  300°/NM gives 30° max at 0.1 NM.
                    xte_corr = apb["xte"] * 300.0
                    xte_corr = max(-30.0, min(30.0, xte_corr))
                    self.ap_target_heading = (hts + xte_corr) % 360.0

    def stop(self):
        self.stop_requested = True
        if self.ap_listener:
            self.ap_listener.stop()

    def run(self):
        self.running = True
        self.stop_requested = False

        # Start Autopilot NMEA listener
        self.ap_listener = AutopilotListener(self.ap_port, self._handle_incoming_nmea)
        self.ap_listener.start()

        # Load proto descriptors
        load_descriptors(PB_DIR)
        classes = build_classes()

        # Check classes loaded
        if "vstep.entities.GetComponentsRequest" not in classes:
            self.console_queue.put("[ERROR] Failed to load essential Protobuf schemas.")
            self.running = False
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        req_cls = classes["vstep.entities.GetComponentsRequest"]
        query_cls = classes["vstep.entities.GetComponentsRequest.Query"]
        sel_cls = classes["vstep.entities.EntitySelection"]
        root_cls = classes["vstep.entities.AllRootEntities"]
        resp_cls = classes["vstep.entities.GetComponentsResponse"]

        sel = sel_cls()
        sel.all_root_entities.CopyFrom(root_cls())
        sel.recursion = 1

        query = query_cls()
        query.component_types.extend(SUBSCRIBE_TYPES)
        query.entities.append(sel)

        req = req_cls()
        req.queries.append(query)

        resolver = TelemetryResolver()
        backoff = 2.0
        
        last_type1_sent = {}
        last_type5_sent = {}
        last_nmea_sent_time = 0.0

        while not self.stop_requested:
            try:
                channel = grpc.insecure_channel(f"{self.host}:{self.port}")
                grpc.channel_ready_future(channel).result(timeout=5)
                self.console_queue.put(f"[bridge] Connected to NAUTIS Home gRPC server at {self.host}:{self.port}")
                
                stub = channel.unary_unary(
                    "/vstep.entities.Registry/GetComponents",
                    request_serializer=lambda m: m.SerializeToString(),
                    response_deserializer=resp_cls.FromString,
                )

                backoff = 2.0

                while not self.stop_requested:
                    t_start = time.time()
                    
                    try:
                        resp = stub(req)
                        
                        entities = {}
                        parsed_components_flat = {}
                        
                        for comp in resp.data:
                            url = comp.data.type_url
                            tn = url.split("/")[-1] if "/" in url else url
                            
                            if tn in classes:
                                msg = classes[tn]()
                                msg.MergeFromString(comp.data.value)
                                eid = comp.entity.id
                                parsed_components_flat[(tn, eid)] = msg
                                if eid not in entities:
                                    entities[eid] = {}
                                entities[eid][tn] = msg

                        # Resolve own-ship entity
                        own_ship_eid = None
                        camera_eid = None
                        
                        for eid, comps in entities.items():
                            if "vstep.viewports.AssignedCamera" in comps:
                                camera_eid = comps["vstep.viewports.AssignedCamera"].entity
                                break

                        # Map parents
                        parent_map = {}
                        for eid, comps in entities.items():
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
                                if peid in entities and "vstep.equipment.MMSI" in entities[peid]:
                                    own_ship_eid = peid
                                    break

                        if own_ship_eid is None:
                            first_gps = next((m for (tn, eid), m in parsed_components_flat.items() if tn == "vstep.sensors.GPSOutput"), None)
                            first_geo = next((m for (tn, eid), m in parsed_components_flat.items() if tn == "vstep.spatial.PositionGeographic"), None)
                            lat_ref = first_gps.latitude if first_gps else (first_geo.position.coordinates.latitude if first_geo else 0.0)
                            lon_ref = first_gps.longitude if first_gps else (first_geo.position.coordinates.longitude if first_geo else 0.0)
                            
                            if lat_ref != 0.0:
                                vessels_list = [eid for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps and "vstep.spatial.PositionGeographic" in comps]
                                min_d = float('inf')
                                for veid in vessels_list:
                                    vpos = entities[veid]["vstep.spatial.PositionGeographic"].position.coordinates
                                    dist = math.sqrt((lat_ref - vpos.latitude)**2 + (lon_ref - vpos.longitude)**2)
                                    if dist < min_d:
                                        min_d = dist
                                        own_ship_eid = veid

                        # Resolve descendants recursively
                        descendants = set()
                        if own_ship_eid is not None:
                            to_visit = [own_ship_eid]
                            while to_visit:
                                curr = to_visit.pop()
                                if curr != own_ship_eid:
                                    descendants.add(curr)
                                rel = entities.get(curr, {}).get("vstep.entities.Relations")
                                if rel:
                                    for child in rel.children:
                                        if child not in descendants and child != own_ship_eid:
                                            to_visit.append(child)

                        # Filter own ship components
                        own_ship_components = {}
                        own_ship_mmsi = 0
                        own_ship_name = "Own Ship"
                        
                        if own_ship_eid is not None:
                            mmsi_comp = entities[own_ship_eid].get("vstep.equipment.MMSI")
                            own_ship_mmsi = mmsi_comp.identifier if mmsi_comp else 0
                            disp_comp = entities[own_ship_eid].get("vstep.entities.DisplayName")
                            own_ship_name = disp_comp.name if (disp_comp and disp_comp.name) else "Own Ship"
                            
                            # Auto-load vessel-specific preset if it exists in autopilot.py
                            own_ship_name_upper = own_ship_name.strip().upper()
                            if own_ship_name_upper != "OWN SHIP":
                                if not hasattr(self, '_last_resolved_vessel_name') or self._last_resolved_vessel_name != own_ship_name_upper:
                                    self._last_resolved_vessel_name = own_ship_name_upper
                                    from autopilot import VESSEL_PRESETS
                                    if own_ship_name_upper in VESSEL_PRESETS:
                                        self.console_queue.put(f"[AP] Resolved vessel: '{own_ship_name_upper}'. Auto-loading preset.")
                                        self.apply_vessel_preset(own_ship_name_upper)
                            
                            for (tn, eid), m in parsed_components_flat.items():
                                if eid == own_ship_eid or eid in descendants:
                                    own_ship_components[(tn, eid)] = m
                        else:
                            own_ship_components = parsed_components_flat

                        has_pos = resolver.resolve(own_ship_components)
                        self.ap_current_heading = resolver.heading_deg

                        # ----------------------------------------------------
                        # Autopilot Steering Loop
                        # ----------------------------------------------------
                        if own_ship_eid is not None:
                            if self.ap_mode in ["Heading", "Route"]:
                                # Resolve actuator direct steering descendants (only those with AngleInput)
                                steering_actuator_eids = set()
                                for cid in descendants:
                                    if cid in entities:
                                        ccomps = entities[cid]
                                        if "vstep.sensors.RudderIndicatorOutput" in ccomps or "vstep.sensors.PropulsionIndicatorOutput" in ccomps:
                                            parent = parent_map.get(cid)
                                            if parent and parent in entities:
                                                parent_comps = entities[parent]
                                                if "vstep.dynamics.AngleInput" in parent_comps:
                                                    steering_actuator_eids.add(parent)

                                # Ensure external control is enabled for all steering actuators individually
                                for act_eid in steering_actuator_eids:
                                    if act_eid not in self._engaged_actuators:
                                        try:
                                            ext_req = classes["vstep.simulation.external.SetExternalControlRequest"]()
                                            ext_req.entity = act_eid
                                            ext_stub = channel.unary_unary(
                                                "/vstep.simulation.external.ExternalControl/SetExternalControl",
                                                request_serializer=lambda m: m.SerializeToString(),
                                                response_deserializer=classes["vstep.simulation.external.SetExternalControlResponse"].FromString
                                            )
                                            ext_stub(ext_req)
                                            self._engaged_actuators.add(act_eid)
                                            self.console_queue.put(f"[autopilot] Engaged external steering control for actuator EID {act_eid}")
                                        except Exception as e:
                                            self.console_queue.put(f"[autopilot] ERROR engaging external control for actuator {act_eid}: {e}")

                                # ── AP write throttle ──────────────────────────────────
                                # Ship heading lag is 3-10s.  Writing rudder commands
                                # faster than 1 Hz fights the sim and amplifies PID noise.
                                t_now_ap = time.time()
                                ap_write_due = (t_now_ap - self._last_ap_write_time) >= (1.0 / self._ap_write_rate)

                                if ap_write_due and steering_actuator_eids:
                                    # Update PID
                                    target_rudder_angle = self.pid.update(resolver.heading_deg, self.ap_target_heading)
                                    self.ap_commanded_rudder = target_rudder_angle
                                    self._last_ap_write_time = t_now_ap

                                    # Diagnostic: log actual vs commanded rudder so we can see
                                    # if the ExternalControl lock is being honoured by the sim.
                                    # Logged every 5 seconds to avoid flooding the console.
                                    if not hasattr(self, '_last_rudder_diag_time'):
                                        self._last_rudder_diag_time = 0.0
                                    if t_now_ap - self._last_rudder_diag_time >= 5.0:
                                        self._last_rudder_diag_time = t_now_ap
                                        err = (self.ap_target_heading - resolver.heading_deg + 180) % 360 - 180
                                        self.console_queue.put(
                                            f"[AP] Hdg {resolver.heading_deg:.1f}° → tgt {self.ap_target_heading:.1f}° "
                                            f"err {err:+.1f}°  cmd_rudder {target_rudder_angle:+.1f}°  "
                                            f"actual_rudder {self.ap_actual_rudder:+.1f}°"
                                        )

                                    # Write AngleInput to each resolved actuator
                                    set_req = classes["vstep.entities.SetComponentsRequest"]()
                                    for act_eid in steering_actuator_eids:
                                        angle_input = classes["vstep.dynamics.AngleInput"]()
                                        # Negate: sim positive = Port, NMEA/our convention positive = Stbd
                                        angle_input.angle_target = -math.radians(target_rudder_angle)
                                        angle_input.nfu = False

                                        # Copy existing pump config to avoid overwriting vessel defaults
                                        existing_angle_input = entities.get(act_eid, {}).get("vstep.dynamics.AngleInput")
                                        if existing_angle_input:
                                            if existing_angle_input.pump_active:
                                                angle_input.pump_active.extend(existing_angle_input.pump_active)
                                                angle_input.pump_active[0] = True
                                        else:
                                            angle_input.pump_active.extend([True, True, False, False])

                                        comp_data = classes["vstep.entities.ComponentData"]()
                                        comp_data.entity.id = act_eid
                                        comp_data.data.Pack(angle_input)
                                        set_req.data.append(comp_data)

                                    try:
                                        set_stub = channel.unary_unary(
                                            "/vstep.entities.Registry/SetComponents",
                                            request_serializer=lambda m: m.SerializeToString(),
                                            response_deserializer=classes["vstep.entities.SetComponentsResponse"].FromString
                                        )
                                        set_stub(set_req)
                                    except Exception as e:
                                        self.console_queue.put(f"[autopilot] ERROR writing rudder targets: {e}")

                            else:
                                # Standby mode: Release external control
                                if self._engaged_actuators:
                                    try:
                                        ext_req = classes["vstep.simulation.external.SetExternalControlRequest"]()
                                        ext_req.entity = 0  # Release all
                                        ext_stub = channel.unary_unary(
                                            "/vstep.simulation.external.ExternalControl/SetExternalControl",
                                            request_serializer=lambda m: m.SerializeToString(),
                                            response_deserializer=classes["vstep.simulation.external.SetExternalControlResponse"].FromString
                                        )
                                        ext_stub(ext_req)
                                        self._engaged_actuators.clear()
                                        self.console_queue.put("[autopilot] Released external control back to manual helm.")
                                    except Exception as e:
                                        self.console_queue.put(f"[autopilot] ERROR releasing external control: {e}")

                        # ----------------------------------------------------
                        # Compile Telemetry Data for GUI
                        # ----------------------------------------------------
                        stbd_angle, port_angle = None, None
                        rudder_angles = []
                        rpm_speeds = []
                        water_depth, draught = 0.0, 0.0
                        tws, twa, aws, awa = 0.0, 0.0, 0.0, 0.0

                        if own_ship_eid is not None:
                            children_ids = sorted(list(descendants))
                            for cid in children_ids:
                                if cid in entities:
                                    ccomps = entities[cid]
                                    _cn = ccomps.get("vstep.entities.Name")
                                    _cd = ccomps.get("vstep.entities.DisplayName")
                                    c_name = _cn.entity_name if _cn else ""
                                    c_disp = _cd.name if _cd else ""
                                    full_cname = (c_name + " " + c_disp).lower()

                                    if "vstep.sensors.RudderIndicatorOutput" in ccomps:
                                        # Negate to match NMEA 0183 output (negative = Port, positive = Starboard)
                                        angle_deg = -math.degrees(ccomps["vstep.sensors.RudderIndicatorOutput"].angle)
                                        if "port" in full_cname or "left" in full_cname:
                                            port_angle = angle_deg
                                        elif "stbd" in full_cname or "starboard" in full_cname or "right" in full_cname:
                                            stbd_angle = angle_deg
                                        else:
                                            rudder_angles.append(angle_deg)
                                    
                                    elif "vstep.sensors.PropulsionIndicatorOutput" in ccomps:
                                        angle_deg = -math.degrees(ccomps["vstep.sensors.PropulsionIndicatorOutput"].angle)
                                        if "port" in full_cname or "left" in full_cname:
                                            port_angle = angle_deg
                                        elif "stbd" in full_cname or "starboard" in full_cname or "right" in full_cname:
                                            stbd_angle = angle_deg
                                        else:
                                            rudder_angles.append(angle_deg)
                                        rpm_speeds.append(ccomps["vstep.sensors.PropulsionIndicatorOutput"].rpm)
                                        
                                    elif "vstep.sensors.TermaRPMOutput" in ccomps:
                                        rpm_speeds.append(ccomps["vstep.sensors.TermaRPMOutput"].rpm)

                                    elif "vstep.sensors.WindmeterOutput" in ccomps:
                                        wm = ccomps["vstep.sensors.WindmeterOutput"]
                                        aws, awa = wm.apparent_speed, wm.apparent_dir
                                        tws, twa = wm.true_speed, wm.true_dir
                                        
                                    elif "vstep.sensors.EchoSounderOutput" in ccomps:
                                        water_depth = ccomps["vstep.sensors.EchoSounderOutput"].water_depth
                                        draught = ccomps["vstep.sensors.EchoSounderOutput"].draught

                        # Track actual rudder for AP diagnostics
                        actual_rudder_now = stbd_angle if stbd_angle is not None else (rudder_angles[0] if rudder_angles else 0.0)
                        self.ap_actual_rudder = actual_rudder_now

                        with self.telemetry_lock:
                            self.telemetry_data = {
                                "lat": resolver.lat,
                                "lon": resolver.lon,
                                "sog": resolver.sog_kn,
                                "cog": resolver.cog_deg,
                                "heading": resolver.heading_deg,
                                "rot": resolver.rot_dpm,
                                "pitch": resolver.pitch_deg,
                                "roll": resolver.roll_deg,
                                "water_depth": water_depth,
                                "draught": draught,
                                "rudder": actual_rudder_now,
                                "commanded_rudder": self.ap_commanded_rudder,
                                "rpm": rpm_speeds[0] if rpm_speeds else 0.0,
                                "tws": tws * 1.9438445,
                                "twa": twa,
                                "aws": aws * 1.9438445,
                                "awa": awa,
                                "time": resolver.sim_dt,
                                "own_ship_name": own_ship_name,
                                "ap_route_good": (time.time() - self._last_apb_time < 4.0)
                            }

                        # ----------------------------------------------------
                        # Throttle & Format NMEA Sentence Emission
                        # ----------------------------------------------------
                        t_now = time.time()

                        if t_now - last_nmea_sent_time >= (1.0 / self.rate):
                            last_nmea_sent_time = t_now
                            sentences = []

                            if has_pos:
                                utc = resolver.sim_dt
                                if self.toggles.get("gpgga"):
                                    sentences.append(make_gpgga(resolver.lat, resolver.lon, utc))
                                if self.toggles.get("gprmc"):
                                    sentences.append(make_gprmc(resolver.lat, resolver.lon, resolver.sog_kn, resolver.cog_deg, utc))
                                if self.toggles.get("gpvtg"):
                                    sentences.append(make_gpvtg(resolver.cog_deg, resolver.sog_kn))
                                if self.toggles.get("gphdg"):
                                    sentences.append(make_gphdg(resolver.heading_deg))
                                if self.toggles.get("gprot"):
                                    sentences.append(make_gprot(resolver.rot_dpm))

                                if own_ship_eid is not None:
                                    if self.toggles.get("iirsa"):
                                        if stbd_angle is not None or port_angle is not None:
                                            sentences.append(make_iirsa(stbd_angle, port_angle))
                                        elif rudder_angles:
                                            sentences.append(make_iirsa(rudder_angles[0], rudder_angles[1] if len(rudder_angles) > 1 else None))

                                    if self.toggles.get("iirpm"):
                                        for idx, rpm in enumerate(rpm_speeds):
                                            sentences.append(make_iirpm(idx + 1, rpm))

                                    if self.toggles.get("iimwv"):
                                        sentences.append(make_iimwv(awa, aws, is_true=False))
                                        if tws > 0:
                                            sentences.append(make_iimwv(twa, tws, is_true=True))

                                    if self.toggles.get("iidpt"):
                                        sentences.append(make_iidpt(water_depth, draught))
                                    if self.toggles.get("iidbt"):
                                        sentences.append(make_iidbt(water_depth))
                                    if self.toggles.get("pitch") or self.toggles.get("roll"):
                                        sentences.append(make_gppat(resolver.heading_deg, resolver.pitch_deg, resolver.roll_deg))

                                if own_ship_mmsi > 0:
                                    # Throttled AIS broadcasts
                                    if t_now - last_type1_sent.get(own_ship_mmsi, 0.0) >= 2.0:
                                        if self.toggles.get("aivdo"):
                                            sentences.append(make_ais_type1(own_ship_mmsi, resolver.lat, resolver.lon, resolver.sog_kn, resolver.cog_deg, resolver.heading_deg, resolver.rot_dpm, is_own=True))
                                        last_type1_sent[own_ship_mmsi] = t_now
                                    if t_now - last_type5_sent.get(own_ship_mmsi, 0.0) >= 10.0:
                                        if self.toggles.get("aivdo"):
                                            to_bow, to_stern, to_port, to_starboard = 0, 0, 0, 0
                                            if own_ship_eid in entities:
                                                to_bow, to_stern, to_port, to_starboard = extract_vessel_dimensions(entities[own_ship_eid].get("vstep.spatial.BoundingBox"))
                                            sentences.append(make_ais_type5(own_ship_mmsi, own_ship_name, is_own=True,
                                                                            to_bow=to_bow, to_stern=to_stern,
                                                                            to_port=to_port, to_starboard=to_starboard))
                                        last_type5_sent[own_ship_mmsi] = t_now

                                # Other traffic vessels AIS
                                # Find all entities with MMSI that are not the own ship.
                                # These are root vessel entities. Their spatial data
                                # (PositionGeographic, LinearMotion, OrientationEuler) may live
                                # on the root entity OR on a child body/sensor entity.
                                # We walk the full descendant tree to find the first available.
                                vessels_traffic = [eid for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps and eid != own_ship_eid]
                                for veid in vessels_traffic:
                                    vcomps = entities[veid]
                                    vmmsi = vcomps["vstep.equipment.MMSI"].identifier
                                    _vd = vcomps.get("vstep.entities.DisplayName")
                                    vname = _vd.name if (_vd and _vd.name) else f"Traffic {vmmsi}"

                                    # Collect all entity IDs for this vessel (root + descendants)
                                    v_all_eids = [veid]
                                    v_to_visit = [veid]
                                    v_visited = {veid}
                                    while v_to_visit:
                                        curr = v_to_visit.pop()
                                        rel = entities.get(curr, {}).get("vstep.entities.Relations")
                                        if rel:
                                            for child in rel.children:
                                                if child not in v_visited and child in entities:
                                                    v_visited.add(child)
                                                    v_all_eids.append(child)
                                                    v_to_visit.append(child)

                                    # Search all entity IDs for the first available spatial data
                                    vpos = None
                                    vlin = None
                                    veuler = None
                                    vcompass = None
                                    vbbox = None
                                    for search_eid in v_all_eids:
                                        sc = entities.get(search_eid, {})
                                        if vpos is None:
                                            vpos = sc.get("vstep.spatial.PositionGeographic")
                                        if vlin is None:
                                            vlin = sc.get("vstep.spatial.LinearMotion")
                                        if veuler is None:
                                            veuler = sc.get("vstep.spatial.OrientationEuler")
                                        if vcompass is None:
                                            vcompass = sc.get("vstep.sensors.CompassBaseOutput")
                                        if vbbox is None:
                                            vbbox = sc.get("vstep.spatial.BoundingBox")
                                    
                                    if vpos:
                                        vlat = vpos.position.coordinates.latitude
                                        vlon = vpos.position.coordinates.longitude
                                        vsog_val = 0.0
                                        vcog_val = 0.0
                                        vhdg_val = 0.0
                                        vrot_val = 0.0

                                        if vlin:
                                            vsog_val = math.sqrt(vlin.velocity.x**2 + vlin.velocity.y**2 + vlin.velocity.z**2) * 1.9438445
                                        # Prefer CompassBaseOutput for heading (radians), fall back to OrientationEuler
                                        if vcompass:
                                            vhdg_val = math.degrees(vcompass.heading) % 360.0
                                            vcog_val = vhdg_val
                                            vrot_val = math.degrees(getattr(vcompass, 'rate_of_turn', 0.0)) * 60.0
                                        elif veuler:
                                            vhdg_val = math.degrees(veuler.angles.z) % 360.0
                                            vcog_val = vhdg_val

                                        if t_now - last_type1_sent.get(vmmsi, 0.0) >= 2.0:
                                            if self.toggles.get("aivdm"):
                                                sentences.append(make_ais_type1(vmmsi, vlat, vlon, vsog_val, vcog_val, vhdg_val, vrot_val, is_own=False))
                                            last_type1_sent[vmmsi] = t_now
                                        if t_now - last_type5_sent.get(vmmsi, 0.0) >= 10.0:
                                            if self.toggles.get("aivdm"):
                                                to_bow, to_stern, to_port, to_starboard = extract_vessel_dimensions(vbbox)
                                                sentences.append(make_ais_type5(vmmsi, vname, is_own=False,
                                                                                to_bow=to_bow, to_stern=to_stern,
                                                                                to_port=to_port, to_starboard=to_starboard))
                                            last_type5_sent[vmmsi] = t_now

                            # Send UDP packets
                            for s in sentences:
                                try:
                                    sock.sendto(s.encode("ascii"), (self.udp_host, self.udp_port))
                                    self.console_queue.put(s.strip())
                                except Exception:
                                    pass

                            if self.verbose and has_pos:
                                print(
                                    f"\r[Telemetry] POS: {resolver.lat:.5f}°N, {resolver.lon:.5f}°E | "
                                    f"HDG: {resolver.heading_deg:.1f}° | "
                                    f"SOG: {resolver.sog_kn:.2f} kn | "
                                    f"Active AIS: {len(vessels_traffic)} | "
                                    f"AP Mode: {self.ap_mode}",
                                    end="", flush=True
                                )

                    except grpc.RpcError as e:
                        self.console_queue.put(f"[bridge] Connection lost: gRPC error {e.code()} -- reconnecting...")
                        break
                    except Exception as e:
                        self.console_queue.put(f"[bridge] Error: {e}")
                        time.sleep(1.0)
                    
                    # Core loop rate regulation — steady rate regardless of AP state.
                    # Ships are slow; 2 Hz is sufficient for autopilot control and
                    # avoids overloading the simulator with rapid gRPC calls.
                    t_elapsed = time.time() - t_start
                    sleep_time = max(0, (1.0 / self.rate) - t_elapsed)
                    time.sleep(sleep_time)

            except grpc.RpcError as e:
                self.console_queue.put(f"[bridge] NAUTIS Home gRPC not reachable -- retrying in {backoff:.0f}s ...")
            except Exception as e:
                self.console_queue.put(f"[bridge] Registry connection loop error: {e} -- retrying in {backoff:.0f}s ...")
            
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

        # Release control on stop
        if self._engaged_actuators:
            try:
                ext_req = classes["vstep.simulation.external.SetExternalControlRequest"]()
                ext_req.entity = 0
                ext_stub = channel.unary_unary(
                    "/vstep.simulation.external.ExternalControl/SetExternalControl",
                    request_serializer=lambda m: m.SerializeToString(),
                    response_deserializer=classes["vstep.simulation.external.SetExternalControlResponse"].FromString
                )
                ext_stub(ext_req)
                self._engaged_actuators.clear()
            except Exception:
                pass
        self.running = False
        self.console_queue.put("[bridge] Bridge engine stopped.")

# ---------------------------------------------------------------------------
# Message Class Factory
# ---------------------------------------------------------------------------
def build_classes() -> dict:
    pool = descriptor_pool.Default()
    needed = [
        "vstep.entities.GetComponentsRequest",
        "vstep.entities.GetComponentsRequest.Query",
        "vstep.entities.GetComponentsResponse",
        "vstep.entities.EntitySelection",
        "vstep.entities.AllRootEntities",
        "vstep.spatial.PositionGeographic",
        "vstep.spatial.LinearMotion",
        "vstep.sensors.DateTimeOutput",
        "vstep.sensors.GPSOutput",
        "vstep.sensors.CompassBaseOutput",
        "vstep.sensors.INSOutput",
        "vstep.sensors.DopplerLogOutput",
        "vstep.spatial.AngularMotion",
        "vstep.spatial.OrientationEuler",
        "vstep.entities.Name",
        "vstep.entities.DisplayName",
        "vstep.entities.Relations",
        "vstep.equipment.MMSI",
        "vstep.sensors.RudderIndicatorOutput",
        "vstep.sensors.PropulsionIndicatorOutput",
        "vstep.sensors.WindmeterOutput",
        "vstep.sensors.EchoSounderOutput",
        "vstep.viewports.AssignedCamera",
        "vstep.spatial.BoundingBox",
        # Actuator inputs
        "vstep.dynamics.AngleInput",
        "vstep.dynamics.PropulsionInput",
        "vstep.dynamics.RPMInput",
        # Write-back messages (required for autopilot SetComponents calls)
        "vstep.entities.SetComponentsRequest",
        "vstep.entities.SetComponentsResponse",
        "vstep.entities.ComponentData",
        "vstep.simulation.external.SetExternalControlRequest",
        "vstep.simulation.external.SetExternalControlResponse",
    ]
    classes = {}
    for t in needed:
        try:
            desc = pool.FindMessageTypeByName(t)
            classes[t] = message_factory.GetMessageClass(desc)
        except Exception as e:
            pass
    return classes

# ---------------------------------------------------------------------------
# CLI/GUI Entry Point
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="NAUTIS Home -> NMEA 0183 UDP Bridge")
    p.add_argument("--host",     default="127.0.0.1")
    p.add_argument("--port",     default=53457, type=int)
    p.add_argument("--udp-host", default="127.0.0.1", dest="udp_host")
    p.add_argument("--udp-port", default=10110, type=int, dest="udp_port")
    p.add_argument("--rate",     default=2.0, type=float, help="Polling and broadcast rate in Hz")
    p.add_argument("--verbose",  action="store_true", help="Print NMEA UDP sentences to terminal (CLI mode only)")
    p.add_argument("--cli",      action="store_true", help="Run in headless CLI mode (default is GUI)")
    return p.parse_args()

def main():
    args = parse_args()

    if not args.cli:
        # GUI Mode (default)
        try:
            from PySide6.QtWidgets import QApplication
            from nautis_gui import NautisGuiWindow
        except ImportError:
            print("[bridge] ERROR: PySide6 is required for GUI mode. Run with --cli for headless mode, or install PySide6.")
            sys.exit(1)

        app = QApplication(sys.argv)
        app.setStyle("Fusion")
        window = NautisGuiWindow(args)
        window.show()
        sys.exit(app.exec())
    else:
        # Headless CLI Mode
        print("=" * 60)
        print("  NAUTIS Home -> Universal NMEA 0183 Bridge (Headless CLI)")
        print("=" * 60)
        print(f"  gRPC Host : {args.host}:{args.port}")
        print(f"  UDP Port  : {args.udp_host}:{args.udp_port}")
        print(f"  Poll Rate : {args.rate} Hz")
        print("=" * 60)

        if not os.path.isdir(PB_DIR):
            print(f"[bridge] ERROR: proto_extracted/ not found at {PB_DIR}")
            sys.exit(1)

        engine = NmeaBridgeEngine(
            host=args.host,
            port=args.port,
            udp_host=args.udp_host,
            udp_port=args.udp_port,
            rate=args.rate,
            verbose=True
        )
        engine.start()

        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[bridge] Stopping bridge engine...")
            engine.stop()
            engine.join()
            print("[bridge] Done.")

if __name__ == "__main__":
    main()
