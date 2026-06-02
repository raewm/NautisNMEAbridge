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

Requirements:
  pip install grpcio protobuf

Usage:
  python nautis_nmea_bridge.py [options]

  --host HOST       gRPC server host (default: 127.0.0.1)
  --port PORT       gRPC server port (default: 53457)
  --udp-host HOST   UDP destination host (default: 127.0.0.1)
  --udp-port PORT   UDP destination port (default: 10110)
  --rate RATE       Polling and broadcast rate in Hz (default: 2.0)
  --verbose         Print every NMEA sentence to stdout
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
# Resolve the proto_extracted/ directory whether running as a plain .py
# script or as a PyInstaller one-file bundle (where data is unpacked to
# sys._MEIPASS at runtime).
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
PB_DIR = os.path.join(_BASE, "proto_extracted")

# All telemetry component types to query -- covers every possible vessel configuration.
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

        Fallback priority:
          Position : GPSOutput > PositionGeographic (active entity)
          Heading  : CompassBaseOutput > INSOutput > OrientationEuler (yaw) > GPS COG
          SOG      : GPSOutput > INSOutput > DopplerLogOutput > LinearMotion magnitude
          COG      : GPSOutput > INSOutput > LinearMotion direction (atan2) > Heading
          ROT      : CompassBaseOutput > INSOutput > AngularMotion yaw speed > 0.0
          Time     : DateTimeOutput > System UTC
        """
        # ------------------------------------------------------------------ #
        # 1. DateTime / Simulation Time
        # ------------------------------------------------------------------ #
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

        # ------------------------------------------------------------------ #
        # 2. Pre-extract sensor and spatial component lists
        # ------------------------------------------------------------------ #
        gps_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.GPSOutput"]
        compass_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.CompassBaseOutput"]
        ins_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.INSOutput"]
        doppler_msgs = [m for (tn, eid), m in components.items() if tn == "vstep.sensors.DopplerLogOutput"]
        lin_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.LinearMotion"]
        ang_msgs     = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.AngularMotion"]
        euler_msgs   = [m for (tn, eid), m in components.items() if tn == "vstep.spatial.OrientationEuler"]
        geom_msgs    = {eid: m for (tn, eid), m in components.items() if tn == "vstep.spatial.PositionGeographic"}

        # ------------------------------------------------------------------ #
        # 3. Position  (GPSOutput > PositionGeographic)
        # ------------------------------------------------------------------ #
        has_pos = False
        if gps_msgs:
            self.lat = gps_msgs[0].latitude
            self.lon = gps_msgs[0].longitude
            self.pos_source = "GPSOutput"
            has_pos = True
        else:
            # Find the entity that has active motion as the best candidate
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

        # ------------------------------------------------------------------ #
        # 4. Heading  (CompassBaseOutput > INSOutput > OrientationEuler.z > GPS COG)
        #    All angular registry values are in RADIANS.
        # ------------------------------------------------------------------ #
        if compass_msgs:
            self.heading_deg = math.degrees(compass_msgs[0].heading) % 360.0
            self.hdg_source = "CompassBaseOutput"
        elif ins_msgs:
            self.heading_deg = math.degrees(ins_msgs[0].heading) % 360.0
            self.hdg_source = "INSOutput"
        elif euler_msgs:
            # OrientationEuler.angles: x=roll, y=pitch, z=yaw (heading)
            self.heading_deg = math.degrees(euler_msgs[0].angles.z) % 360.0
            self.hdg_source = "OrientationEuler"
        elif gps_msgs and gps_msgs[0].cog > 0:
            self.heading_deg = math.degrees(gps_msgs[0].cog) % 360.0
            self.hdg_source = "GPS COG Fallback"
        else:
            self.heading_deg = 0.0
            self.hdg_source = "None (0.0)"

        # ------------------------------------------------------------------ #
        # 5. SOG  (GPSOutput > INSOutput > DopplerLogOutput > LinearMotion)
        # ------------------------------------------------------------------ #
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

        # ------------------------------------------------------------------ #
        # 6. COG  (GPSOutput > INSOutput > LinearMotion direction > Heading)
        # ------------------------------------------------------------------ #
        if gps_msgs:
            self.cog_deg = math.degrees(gps_msgs[0].cog) % 360.0
            self.cog_source = "GPSOutput"
        elif ins_msgs:
            self.cog_deg = math.degrees(ins_msgs[0].cog) % 360.0
            self.cog_source = "INSOutput"
        elif lin_msgs:
            vx = lin_msgs[0].velocity.x
            vy = lin_msgs[0].velocity.y
            if abs(vx) > 0.01 or abs(vy) > 0.01:  # only derive if actually moving
                # atan2(east, north) -- vstep body frame: x=north/forward, y=east/starboard
                self.cog_deg = math.degrees(math.atan2(vy, vx)) % 360.0
                self.cog_source = "LinearMotion Direction"
            else:
                self.cog_deg = self.heading_deg
                self.cog_source = "Heading Fallback (stationary)"
        else:
            self.cog_deg = self.heading_deg
            self.cog_source = "Heading Fallback"

        # ------------------------------------------------------------------ #
        # 7. ROT  (CompassBaseOutput > INSOutput > AngularMotion.z > 0.0)
        #    Registry stores ROT in rad/s -- convert to deg/min.
        # ------------------------------------------------------------------ #
        if compass_msgs:
            self.rot_dpm = math.degrees(compass_msgs[0].rot) * 60.0
            self.rot_source = "CompassBaseOutput"
        elif ins_msgs:
            self.rot_dpm = math.degrees(ins_msgs[0].rot) * 60.0
            self.rot_source = "INSOutput"
        elif ang_msgs:
            # AngularMotion.velocity: x=roll_rate, y=pitch_rate, z=yaw_rate (heading change)
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
    # Initialize UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[bridge] Sending NMEA UDP to {args.udp_host}:{args.udp_port}...")

    # Load gRPC request classes
    req_cls = classes["vstep.entities.GetComponentsRequest"]
    query_cls = classes["vstep.entities.GetComponentsRequest.Query"]
    sel_cls = classes["vstep.entities.EntitySelection"]
    root_cls = classes["vstep.entities.AllRootEntities"]
    resp_cls = classes["vstep.entities.GetComponentsResponse"]

    # Setup static recursive query for only the 5 essential components
    sel = sel_cls()
    sel.all_root_entities.CopyFrom(root_cls())
    sel.recursion = 1 # RECURSION_INCLUSIVE
    
    query = query_cls()
    query.component_types.extend(SUBSCRIBE_TYPES)
    query.entities.append(sel)
    
    req = req_cls()
    req.queries.append(query)

    resolver = TelemetryResolver()
    backoff = 2.0
    interval = 1.0 / args.rate

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
                    
                    # Store all parsed messages in a dictionary mapped by (type_name, entity_id)
                    parsed_components = {}
                    for comp in resp.data:
                        url = comp.data.type_url
                        tn = url.split("/")[-1] if "/" in url else url
                        
                        if tn in classes:
                            msg = classes[tn]()
                            msg.MergeFromString(comp.data.value)
                            parsed_components[(tn, comp.entity.id)] = msg
                    
                    # Resolve telemetry using our robust fallbacks
                    has_pos = resolver.resolve(parsed_components)
                    
                    if has_pos:
                        utc = resolver.sim_dt
                        
                        # Generate NMEA sentences
                        sentences = [
                            make_gpgga(resolver.lat, resolver.lon, utc),
                            make_gprmc(resolver.lat, resolver.lon, resolver.sog_kn, resolver.cog_deg, utc),
                            make_gpvtg(resolver.cog_deg, resolver.sog_kn),
                            make_gphdg(resolver.heading_deg),
                            make_gprot(resolver.rot_dpm),
                        ]
                        
                        # Send NMEA over UDP
                        for s in sentences:
                            sock.sendto(s.encode("ascii"), (args.udp_host, args.udp_port))
                            if args.verbose:
                                print(f"  UDP: {s.strip()}")
                        
                        # Print beautiful console telemetry dashboard
                        if not args.verbose:
                            print(
                                f"\r[Telemetry] POS: {resolver.lat:.5f}°N, {resolver.lon:.5f}°W (Src: {resolver.pos_source}) | "
                                f"HDG: {resolver.heading_deg:.1f}° (Src: {resolver.hdg_source}) | "
                                f"SOG: {resolver.sog_kn:.2f} kn (Src: {resolver.sog_source}) | "
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
