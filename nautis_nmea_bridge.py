"""
nautis_nmea_bridge.py  -- NAUTIS Home gRPC -> NMEA 0183 UDP Bridge
==================================================================
Subscribes to the NAUTIS Home gRPC registry using lightweight, periodic
GetComponents queries, resolves vessel telemetry using a multi-layered fallback
matrix, and broadcasts standard NMEA 0183 sentences over UDP.

Compatible with ALL possible vessels in NAUTIS Home.
Completely deadlock-free and safe for long-term simulation runs.

Sentences produced:
  $GPGGA  -- position fix (lat, lon, UTC time)
  $GPRMC  -- recommended minimum navigation info (lat, lon, SOG, COG, date)
  $GPVTG  -- course and speed over ground
  $GPHDG  -- magnetic heading
  $GPROT  -- rate of turn
  $IIRSA  -- rudder sensor angle (port & starboard)
  $IIRPM  -- engine revolutions (RPM)
  $IIMWV  -- wind speed and angle (apparent & true)
  $IIDPT  -- depth
  $IIDBT  -- depth below transducer
  !AIVDO  -- own-ship Class A AIS reports (Type 1 and Type 5)
  !AIVDM  -- traffic vessels Class A AIS reports (Type 1 and Type 5)

Requirements:
  pip install grpcio protobuf

Usage:
  python nautis_nmea_bridge.py [options]
"""

import argparse
import math
import os
import socket
import sys
import time
from datetime import datetime, timezone

import grpc
# Pre-import well-known types so they are registered in the global descriptor pool
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
]

# ---------------------------------------------------------------------------
# Protobuf Descriptor Loader
# ---------------------------------------------------------------------------
def load_descriptors(pb_dir: str) -> int:
    pool = descriptor_pool.Default()
    name_to_bytes = {}

    for fname in os.listdir(pb_dir):
        if not fname.endswith(".proto.pb"):
            continue
        with open(os.path.join(pb_dir, fname), "rb") as f:
            data = f.read()
        try:
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.MergeFromString(data)
            name_to_bytes[fdp.name] = data
        except Exception:
            pass

    added = set()
    for _ in range(len(name_to_bytes) + 2):
        progress = False
        for proto_name, data in name_to_bytes.items():
            if proto_name in added:
                continue
            try:
                fdp = descriptor_pb2.FileDescriptorProto()
                fdp.MergeFromString(data)
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


# ---------------------------------------------------------------------------
# AIS Helpers
# ---------------------------------------------------------------------------
def encode_ais_string(s: str, length: int) -> int:
    """Encode a string into packed AIS 6-bit ASCII (ITU-R M.1371 Table 3).
    
    AIS 6-bit value mapping:
      ASCII 64-95 (@, A-Z, [, \\, ], ^, _) -> val = code - 64   (A=1 .. Z=26)
      ASCII 32-63 (space, !, 0-9, ...)     -> val = code         (space=32, 0=48)
    """
    s = s.upper()[:length]
    s = s.ljust(length, '@')   # pad with @ (6-bit 0) per ITU standard
    bits = 0
    for char in s:
        code = ord(char)
        if 64 <= code <= 95:    # @, A-Z, [\]^_
            val = code - 64
        elif 32 <= code <= 63:  # space, digits, punctuation
            val = code
        else:
            val = 0             # substitute @ for anything out of range
        bits = (bits << 6) | val
    return bits


def make_ais_sentence(payload: str, is_own: bool = False) -> str:
    talker = "AIVDO" if is_own else "AIVDM"
    body = f"{talker},1,1,,A,{payload},0"
    return f"!{body}*{_nmea_checksum(body)}\r\n"


def make_ais_type1(mmsi: int, lat: float, lon: float, sog_kn: float, cog_deg: float, heading_deg: float, rot_dpm: float, is_own: bool = False) -> str:
    msg_type = 1
    repeat = 0
    mmsi = int(mmsi) & 0x3FFFFFFF
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
    lon_val = int(lon * 600000.0) & 0xFFFFFFF
    lat_val = int(lat * 600000.0) & 0x7FFFFFF
    
    cog_val = int(cog_deg * 10.0) % 3600
    if cog_val < 0:
        cog_val = 3600
        
    heading_val = int(heading_deg) % 360
    if heading_val < 0:
        heading_val = 511
        
    ts = int(time.time()) % 60
    
    bits = 0
    bits = (bits << 6) | msg_type
    bits = (bits << 2) | repeat
    bits = (bits << 30) | mmsi
    bits = (bits << 4) | nav_status
    bits = (bits << 8) | (rot_ais & 0xFF)
    bits = (bits << 10) | sog_val
    bits = (bits << 1) | pos_accuracy
    bits = (bits << 28) | lon_val
    bits = (bits << 27) | lat_val
    bits = (bits << 12) | cog_val
    bits = (bits << 9) | heading_val
    bits = (bits << 6) | ts
    bits = (bits << 2) | 0 # maneuver
    bits = (bits << 3) | 0 # spare
    bits = (bits << 1) | 0 # RAIM
    bits = (bits << 19) | 0 # radio
    
    payload = ""
    for i in range(27, -1, -1):
        val = (bits >> (i * 6)) & 0x3F
        if val < 40:
            payload += chr(val + 48)
        else:
            payload += chr(val + 56)
            
    return make_ais_sentence(payload, is_own)


def make_ais_type5(mmsi: int, name: str, is_own: bool = False) -> str:
    mmsi = int(mmsi) & 0x3FFFFFFF
    name_bits = encode_ais_string(name, 20)
    
    mmsi_str = str(mmsi)
    callsign = "TS" + (mmsi_str[-5:] if len(mmsi_str) >= 5 else mmsi_str.zfill(5))
    call_bits = encode_ais_string(callsign, 7)
    dest_bits = encode_ais_string("NAUTIS", 20)
    
    bits = 0
    bits = (bits << 6) | 5 # msg type
    bits = (bits << 2) | 0 # repeat
    bits = (bits << 30) | mmsi
    bits = (bits << 2) | 0 # AIS version
    bits = (bits << 30) | 0 # IMO
    bits = (bits << 42) | call_bits
    bits = (bits << 120) | name_bits
    bits = (bits << 8) | 70 # cargo ship
    bits = (bits << 30) | 0x1E0502 # dimensions (30m x 10m x 5m x 2m)
    bits = (bits << 4) | 1 # position fix: GPS
    bits = (bits << 20) | 0 # ETA
    bits = (bits << 8) | 0 # draught
    bits = (bits << 120) | dest_bits
    bits = (bits << 1) | 0 # DTE
    bits = (bits << 1) | 0 # spare
    bits = (bits << 2) | 0 # padding (426 bits total)
    
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
        
        # Source indicators for logging
        self.pos_source = "None"
        self.hdg_source = "None"
        self.sog_source = "None"
        self.cog_source = "None"
        self.rot_source = "None"
        self.time_source = "None"

    def resolve(self, components: dict) -> bool:
        """
        Parses active registry components and resolves the best available telemetry
        using the full hierarchical fallback matrix.
        """
        # 1. DateTime / Simulation Time
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
                self.time_source = "DateTimeOutput"
            except Exception:
                pass
        if self.sim_dt is None:
            self.sim_dt = datetime.now(tz=timezone.utc)
            self.time_source = "System UTC"

        # 2. Pre-extract sensor and spatial component lists
        gps_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.GPSOutput"]
        compass_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.CompassBaseOutput"]
        ins_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.INSOutput"]
        doppler_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.DopplerLogOutput"]
        lin_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.LinearMotion"]
        ang_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.AngularMotion"]
        euler_msgs   = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.OrientationEuler"]
        geom_msgs    = {eid: m for (tn, eid), m in components.items() if tn == "vstep.spatial.PositionGeographic"}

        # 3. Position  (GPSOutput > PositionGeographic)
        has_pos = False
        if gps_msgs:
            self.lat = gps_msgs[0].latitude
            self.lon = gps_msgs[0].longitude
            self.pos_source = "GPSOutput"
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
                self.pos_source = "PositionGeographic"
                has_pos = True

        # 4. Heading  (CompassBaseOutput > INSOutput > OrientationEuler.z > GPS COG)
        if compass_msgs:
            self.heading_deg = math.degrees(compass_msgs[0].heading) % 360.0
            self.hdg_source = "CompassBaseOutput"
        elif ins_msgs:
            self.heading_deg = math.degrees(ins_msgs[0].heading) % 360.0
            self.hdg_source = "INSOutput"
        elif euler_msgs:
            self.heading_deg = math.degrees(euler_msgs[0].angles.z) % 360.0
            self.hdg_source = "OrientationEuler"
        elif gps_msgs and gps_msgs[0].cog > 0:
            self.heading_deg = math.degrees(gps_msgs[0].cog) % 360.0
            self.hdg_source = "GPS COG Fallback"
        else:
            self.heading_deg = 0.0
            self.hdg_source = "None (0.0)"

        # 5. SOG  (GPSOutput > INSOutput > DopplerLogOutput > LinearMotion)
        if gps_msgs:
            self.sog_kn = gps_msgs[0].sog * 1.9438445
            self.sog_source = "GPSOutput"
        elif ins_msgs:
            self.sog_kn = ins_msgs[0].sog * 1.9438445
            self.sog_source = "INSOutput"
        elif doppler_msgs:
            self.sog_kn = doppler_msgs[0].sog * 1.9438445
            self.sog_source = "DopplerLogOutput"
        elif lin_msgs:
            m = lin_msgs[0]
            self.sog_kn = math.sqrt(m.velocity.x**2 + m.velocity.y**2 + m.velocity.z**2) * 1.9438445
            self.sog_source = "LinearMotion Magnitude"
        else:
            self.sog_kn = 0.0
            self.sog_source = "None (0.0)"

        # 6. COG  (GPSOutput > INSOutput > LinearMotion direction > Heading)
        if gps_msgs:
            self.cog_deg = math.degrees(gps_msgs[0].cog) % 360.0
            self.cog_source = "GPSOutput"
        elif ins_msgs:
            self.cog_deg = math.degrees(ins_msgs[0].cog) % 360.0
            self.cog_source = "INSOutput"
        elif lin_msgs:
            vx = lin_msgs[0].velocity.x
            vy = lin_msgs[0].velocity.y
            if abs(vx) > 0.01 or abs(vy) > 0.01:
                # Local world frame: X is East, Y is North. Course is math.atan2(vx, vy)
                self.cog_deg = math.degrees(math.atan2(vx, vy)) % 360.0
                self.cog_source = "LinearMotion Direction"
            else:
                self.cog_deg = self.heading_deg
                self.cog_source = "Heading Fallback (stationary)"
        else:
            self.cog_deg = self.heading_deg
            self.cog_source = "Heading Fallback"

        # 7. ROT  (CompassBaseOutput > INSOutput > AngularMotion.z > 0.0)
        if compass_msgs:
            self.rot_dpm = math.degrees(compass_msgs[0].rot) * 60.0
            self.rot_source = "CompassBaseOutput"
        elif ins_msgs:
            self.rot_dpm = math.degrees(ins_msgs[0].rot) * 60.0
            self.rot_source = "INSOutput"
        elif ang_msgs:
            self.rot_dpm = math.degrees(ang_msgs[0].velocity.z) * 60.0
            self.rot_source = "AngularMotion"
        else:
            self.rot_dpm = 0.0
            self.rot_source = "None (0.0)"

        return has_pos


# ---------------------------------------------------------------------------
# Core Polling Loop
# ---------------------------------------------------------------------------
def run_bridge(args, classes: dict):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[bridge] Sending NMEA UDP to {args.udp_host}:{args.udp_port}...")

    # Load gRPC classes
    req_cls = classes["vstep.entities.GetComponentsRequest"]
    query_cls = classes["vstep.entities.GetComponentsRequest.Query"]
    sel_cls = classes["vstep.entities.EntitySelection"]
    root_cls = classes["vstep.entities.AllRootEntities"]
    resp_cls = classes["vstep.entities.GetComponentsResponse"]

    # Setup static query for all required telemetry components
    sel = sel_cls()
    sel.all_root_entities.CopyFrom(root_cls())
    sel.recursion = 1 # RECURSION_INCLUSIVE
    
    query = query_cls()
    query.component_types.extend(SUBSCRIBE_TYPES)
    query.entities.append(sel)
    
    req = req_cls()
    req.queries.append(query)

    resolver = TelemetryResolver()
    _child_dump_done = [False]  # mutable flag: dump child components once on first cycle
    backoff = 2.0

    interval = 1.0 / args.rate
    
    # Track throttled AIS transmission times
    last_type1_sent = {} # MMSI -> timestamp
    last_type5_sent = {} # MMSI -> timestamp

    while True:
        try:
            channel = grpc.insecure_channel(f"{args.host}:{args.port}")
            grpc.channel_ready_future(channel).result(timeout=5)
            print(f"[bridge] Connected successfully to NAUTIS Home gRPC server at {args.host}:{args.port}")
            
            stub = channel.unary_unary(
                "/vstep.entities.Registry/GetComponents",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=resp_cls.FromString,
            )

            backoff = 2.0  # reset backoff upon successful connection
            
            while True:
                t_start = time.time()
                
                try:
                    resp = stub(req)
                    
                    # Store all parsed components mapped by entity_id
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
                    
                    # ----------------------------------------------------
                    # Own-Ship Resolution
                    # ----------------------------------------------------
                    own_ship_eid = None
                    camera_eid = None
                    
                    # 1. Resolve camera viewport target
                    for eid, comps in entities.items():
                        if "vstep.viewports.AssignedCamera" in comps:
                            camera_eid = comps["vstep.viewports.AssignedCamera"].entity
                            break
                    
                    # Climb hierarchy relations to find root vessel entity
                    if camera_eid:
                        curr = camera_eid
                        path = []
                        while True:
                            parent = None
                            for eid, comps in entities.items():
                                rel = comps.get("vstep.entities.Relations")
                                if rel and curr in rel.children:
                                    parent = eid
                                    break
                            if parent:
                                path.append(parent)
                                curr = parent
                            else:
                                break
                        # Match first ancestor with MMSI
                        for peid in path:
                            if peid in entities and "vstep.equipment.MMSI" in entities[peid]:
                                own_ship_eid = peid
                                break
                    
                    # 2. GPS position fallback matching if camera resolution fails
                    if own_ship_eid is None:
                        # Extract first available raw GPS coordinate in components
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

                    # 3. Filter components belonging only to the own-ship vessel entity or its descendants
                    own_ship_components = {}
                    own_ship_mmsi = 0
                    own_ship_name = "Own Ship"
                    
                    if own_ship_eid is not None:
                        mmsi_comp = entities[own_ship_eid].get("vstep.equipment.MMSI")
                        own_ship_mmsi = mmsi_comp.identifier if mmsi_comp else 0
                        disp_comp = entities[own_ship_eid].get("vstep.entities.DisplayName")
                        own_ship_name = disp_comp.name if (disp_comp and disp_comp.name) else "Own Ship"
                        
                        descendants = set()
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
                                        
                        for (tn, eid), m in parsed_components_flat.items():
                            if eid == own_ship_eid or eid in descendants:
                                own_ship_components[(tn, eid)] = m
                    else:
                        # Direct fallback to global flat components if own ship not resolved
                        own_ship_components = parsed_components_flat
                        descendants = set()

                    # Resolve own-ship telemetry
                    has_pos = resolver.resolve(own_ship_components)
                    
                    if has_pos:
                        utc = resolver.sim_dt
                        sentences = []

                        # Base Navigation Sentences
                        sentences.append(make_gpgga(resolver.lat, resolver.lon, utc))
                        sentences.append(make_gprmc(resolver.lat, resolver.lon, resolver.sog_kn, resolver.cog_deg, utc))
                        sentences.append(make_gpvtg(resolver.cog_deg, resolver.sog_kn))
                        sentences.append(make_gphdg(resolver.heading_deg))
                        sentences.append(make_gprot(resolver.rot_dpm))

                        # --- Telemetry Sentences ---
                        if own_ship_eid is not None:
                            # 1. Rudder Angle ($IIRSA)
                            rudder_angles = []
                            stbd_angle = None
                            port_angle = None
                            
                            children_ids = sorted(list(descendants))
                            for cid in children_ids:
                                if cid in entities:
                                    ccomps = entities[cid]
                                    _cn = ccomps.get("vstep.entities.Name")
                                    _cd = ccomps.get("vstep.entities.DisplayName")
                                    c_name = _cn.entity_name if _cn else ""
                                    c_disp = _cd.name if _cd else ""
                                    full_cname = (c_name + " " + c_disp).lower()
                                    
                                    # Conventional rudder indicator
                                    if "vstep.sensors.RudderIndicatorOutput" in ccomps:
                                        angle_deg = -math.degrees(ccomps["vstep.sensors.RudderIndicatorOutput"].angle)
                                        if "port" in full_cname or "left" in full_cname:
                                            port_angle = angle_deg
                                        elif "stbd" in full_cname or "starboard" in full_cname or "right" in full_cname:
                                            stbd_angle = angle_deg
                                        else:
                                            rudder_angles.append(angle_deg)
                                            
                                    # Propulsion nozzle angle (waterjets/azimuth thrusters)
                                    elif "vstep.sensors.PropulsionIndicatorOutput" in ccomps:
                                        angle_deg = -math.degrees(ccomps["vstep.sensors.PropulsionIndicatorOutput"].angle)
                                        if "port" in full_cname or "left" in full_cname:
                                            port_angle = angle_deg
                                        elif "stbd" in full_cname or "starboard" in full_cname or "right" in full_cname:
                                            stbd_angle = angle_deg
                                        else:
                                            rudder_angles.append(angle_deg)
                                            
                            if stbd_angle is not None or port_angle is not None:
                                sentences.append(make_iirsa(stbd_angle, port_angle))
                            elif rudder_angles:
                                if len(rudder_angles) >= 2:
                                    sentences.append(make_iirsa(rudder_angles[0], rudder_angles[1]))
                                else:
                                    sentences.append(make_iirsa(rudder_angles[0]))

                            # 2. Engine RPM ($IIRPM)
                            eng_idx = 1
                            for cid in children_ids:
                                if cid in entities:
                                    ccomps = entities[cid]
                                    if "vstep.sensors.PropulsionIndicatorOutput" in ccomps:
                                        rpm = ccomps["vstep.sensors.PropulsionIndicatorOutput"].rpm
                                        sentences.append(make_iirpm(eng_idx, rpm))
                                        eng_idx += 1
                                    elif "vstep.sensors.TermaRPMOutput" in ccomps:
                                        rpm = ccomps["vstep.sensors.TermaRPMOutput"].rpm
                                        sentences.append(make_iirpm(eng_idx, rpm))
                                        eng_idx += 1

                            # 3. Wind Data ($IIMWV)
                            for cid in children_ids:
                                if cid in entities:
                                    ccomps = entities[cid]
                                    if "vstep.sensors.WindmeterOutput" in ccomps:
                                        wm = ccomps["vstep.sensors.WindmeterOutput"]
                                        sentences.append(make_iimwv(wm.apparent_dir, wm.apparent_speed, is_true=False))
                                        if wm.true_speed > 0:
                                            sentences.append(make_iimwv(wm.true_dir, wm.true_speed, is_true=True))
                                        break

                            # 4. Water Depth ($IIDPT / $IIDBT)
                            for cid in children_ids:
                                if cid in entities:
                                    ccomps = entities[cid]
                                    if "vstep.sensors.EchoSounderOutput" in ccomps:
                                        es = ccomps["vstep.sensors.EchoSounderOutput"]
                                        sentences.append(make_iidpt(es.water_depth, es.draught))
                                        sentences.append(make_iidbt(es.water_depth))
                                        break

                        # --- AIS Own Ship Broadcasting ---
                        if own_ship_mmsi > 0:
                            t_now = time.time()
                            # Own ship Class A position report (Type 1)
                            if t_now - last_type1_sent.get(own_ship_mmsi, 0.0) >= 2.0:
                                sentences.append(make_ais_type1(own_ship_mmsi, resolver.lat, resolver.lon, resolver.sog_kn, resolver.cog_deg, resolver.heading_deg, resolver.rot_dpm, is_own=True))
                                last_type1_sent[own_ship_mmsi] = t_now
                            # Own ship static voyage report (Type 5)
                            if t_now - last_type5_sent.get(own_ship_mmsi, 0.0) >= 10.0:
                                sentences.append(make_ais_type5(own_ship_mmsi, own_ship_name, is_own=True))
                                last_type5_sent[own_ship_mmsi] = t_now

                        # --- AIS Vessel Traffic (Other Vessels) ---
                        vessels_traffic = [eid for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps and eid != own_ship_eid]
                        for veid in vessels_traffic:
                            vcomps = entities[veid]
                            vmmsi = vcomps["vstep.equipment.MMSI"].identifier
                            _vd = vcomps.get("vstep.entities.DisplayName")
                            _vn = vcomps.get("vstep.entities.Name")
                            vname = (_vd.name if _vd and _vd.name else None) or (_vn.entity_name if _vn else None) or f"Vessel {veid}"
                            
                            # Position
                            vlat, vlon = 0.0, 0.0
                            if "vstep.spatial.PositionGeographic" in vcomps:
                                pos_geo = vcomps["vstep.spatial.PositionGeographic"]
                                vlat = pos_geo.position.coordinates.latitude
                                vlon = pos_geo.position.coordinates.longitude
                                
                            if vlat == 0.0:
                                continue # skip vessels without coordinates
                                
                            # Speed and Course
                            vvx, vvy = 0.0, 0.0
                            vsog_kn = 0.0
                            vcog_deg = 0.0
                            if "vstep.spatial.LinearMotion" in vcomps:
                                vlm = vcomps["vstep.spatial.LinearMotion"]
                                vvx = vlm.velocity.x
                                vvy = vlm.velocity.y
                                vsog_kn = math.sqrt(vvx**2 + vvy**2) * 1.9438445
                                if math.sqrt(vvx**2 + vvy**2) > 0.01:
                                    vcog_deg = math.degrees(math.atan2(vvx, vvy)) % 360.0
                            
                            # Heading
                            vhdg_deg = vcog_deg # fallback to COG
                            vchildren = vcomps.get("vstep.entities.Relations")
                            v_gyro_found = False
                            if vchildren:
                                for vcid in vchildren.children:
                                    if vcid in entities and "vstep.sensors.CompassBaseOutput" in entities[vcid]:
                                        vhdg_deg = math.degrees(entities[vcid]["vstep.sensors.CompassBaseOutput"].heading) % 360.0
                                        v_gyro_found = True
                                        break
                            if not v_gyro_found and "vstep.spatial.OrientationEuler" in vcomps:
                                vhdg_deg = math.degrees(vcomps["vstep.spatial.OrientationEuler"].angles.z) % 360.0
                                
                            # Rate of Turn
                            vrot_dpm = 0.0
                            if "vstep.spatial.AngularMotion" in vcomps:
                                vam = vcomps["vstep.spatial.AngularMotion"]
                                vrot_dpm = math.degrees(vam.velocity.z) * 60.0
                                
                            # Throttled Class A position report transmission (Type 1)
                            t_now = time.time()
                            if t_now - last_type1_sent.get(vmmsi, 0.0) >= 2.0:
                                sentences.append(make_ais_type1(vmmsi, vlat, vlon, vsog_kn, vcog_deg, vhdg_deg, vrot_dpm, is_own=False))
                                last_type1_sent[vmmsi] = t_now
                            # Throttled static voyage report transmission (Type 5)
                            if t_now - last_type5_sent.get(vmmsi, 0.0) >= 10.0:
                                sentences.append(make_ais_type5(vmmsi, vname, is_own=False))
                                last_type5_sent[vmmsi] = t_now

                        # Send NMEA sentences over UDP
                        for s in sentences:
                            sock.sendto(s.encode("ascii"), (args.udp_host, args.udp_port))
                            if args.verbose:
                                print(f"  UDP: {s.strip()}")
                        
                        # Print console telemetry dashboard
                        if not args.verbose:
                            print(
                                f"\r[Telemetry] POS: {resolver.lat:.5f}°N, {resolver.lon:.5f}°E | "
                                f"HDG: {resolver.heading_deg:.1f}° | "
                                f"SOG: {resolver.sog_kn:.2f} kn | "
                                f"Active AIS Targets: {len(vessels_traffic)} | "
                                f"Time: {utc.strftime('%H:%M:%S')} UTC",
                                end="", flush=True
                            )
                    else:
                        print("\r[Telemetry] Waiting for valid vessel spatial or GPS position...", end="", flush=True)

                except grpc.RpcError as e:
                    print(f"\n[bridge] Connection lost: gRPC error {e.code()} -- reconnecting...")
                    break
                except Exception as e:
                    print(f"\n[bridge] Processing error: {e}")
                    time.sleep(1.0)
                
                # Regulate polling rate
                t_elapsed = time.time() - t_start
                sleep_time = max(0, interval - t_elapsed)
                time.sleep(sleep_time)

        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.UNAVAILABLE:
                print(f"[bridge] NAUTIS Home not reachable at {args.host}:{args.port} -- retrying in {backoff:.0f}s ...")
            else:
                print(f"[bridge] Connection error {e.code()}: {e.details()} -- retrying in {backoff:.0f}s ...")
        except Exception as e:
            print(f"[bridge] Connection loop error: {e} -- retrying in {backoff:.0f}s ...")
        
        time.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


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
    ]
    classes = {}
    for t in needed:
        try:
            desc = pool.FindMessageTypeByName(t)
            classes[t] = message_factory.GetMessageClass(desc)
        except Exception as e:
            print(f"[bridge] Warning: could not resolve {t}: {e}")
    return classes


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(description="NAUTIS Home -> NMEA 0183 UDP Bridge")
    p.add_argument("--host",     default="127.0.0.1")
    p.add_argument("--port",     default=53457, type=int)
    p.add_argument("--udp-host", default="127.0.0.1", dest="udp_host")
    p.add_argument("--udp-port", default=10110, type=int, dest="udp_port")
    p.add_argument("--rate",     default=2.0, type=float, help="Polling and broadcast rate in Hz")
    p.add_argument("--verbose",  action="store_true", help="Print NMEA UDP sentences to terminal")
    return p.parse_args()


def main():
    args = parse_args()

    print("=" * 60)
    print("  NAUTIS Home -> Universal NMEA 0183 Bridge (Direct Polling)")
    print("=" * 60)
    print(f"  gRPC Host : {args.host}:{args.port}")
    print(f"  UDP Port  : {args.udp_host}:{args.udp_port}")
    print(f"  Poll Rate : {args.rate} Hz")
    print("=" * 60)

    if not os.path.isdir(PB_DIR):
        print(f"[bridge] ERROR: proto_extracted/ not found at {PB_DIR}")
        sys.exit(1)

    load_descriptors(PB_DIR)
    classes = build_classes()

    run_bridge(args, classes)


if __name__ == "__main__":
    main()
