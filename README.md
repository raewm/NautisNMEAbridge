# NAUTIS Home → NMEA 0183 UDP Bridge

A production-grade, deadlock-free integration bridge that reads live telemetry from the
[NAUTIS Home](https://vstep.nl/nautis-home/) maritime simulator and re-broadcasts it as
standard NMEA 0183 sentences and AIS reports over UDP.

Compatible with any chart plotter or navigation software that accepts NMEA 0183 input —
OpenCPN, Coastal Explorer, Expedition, Furuno TZT, and others.

Available in two forms:
- **`dist/nautis_nmea_bridge.exe`** — standalone executable, no Python required
- **`nautis_nmea_bridge.py`** — Python script, requires `pip install grpcio protobuf`

---

## Features

- **Universal vessel compatibility** — works on every vessel in NAUTIS Home, regardless
  of its sensor loadout (GPS, compass, INS, Doppler log, or raw physics only)
- **Own-ship automatic detection** — uses the simulator's viewport camera assignment to
  identify the trainee's vessel; automatically falls back to coordinate proximity matching
- **AIS vessel traffic** — broadcasts all other vessels in the scenario as Class A AIS
  targets (`!AIVDM`) so they appear in your chart plotter alongside your own position
- **Own-ship AIS** — emits `!AIVDO` sentences for the own-ship, including static voyage
  data (Type 5) with vessel name and call sign
- **Supplementary telemetry** — outputs rudder angle, engine RPM, wind speed/direction,
  and water depth whenever the vessel's sensor loadout includes them
- **Hierarchical telemetry fallback** — automatically selects the best available data
  source for each metric at every poll cycle
- **Deadlock-free** — uses a request/response polling pattern instead of the simulator's
  streaming subscription API, which was found to cause simulator physics freezes during
  sustained use
- **Auto-reconnect** — exponential backoff reconnection loop; the bridge survives
  simulator restarts without manual intervention
- **Configurable** — poll rate, gRPC host/port, and UDP destination are all runtime arguments

---

## NMEA Sentences Produced

### Navigation (own ship)

| Sentence  | Content |
|-----------|---------|
| `$GPGGA`  | Position fix — latitude, longitude, UTC time |
| `$GPRMC`  | Recommended minimum navigation data — position, SOG, COG, date |
| `$GPVTG`  | Course and speed over ground (true, knots, km/h) |
| `$GPHDG`  | Heading |
| `$GPROT`  | Rate of turn (degrees per minute) |

### Supplementary telemetry (when sensor present on vessel)

| Sentence  | Content |
|-----------|---------|
| `$IIRSA`  | Rudder angle — port and/or starboard (conventional rudder or jet/azimuth nozzle) |
| `$IIRPM`  | Engine revolutions per minute (one sentence per engine/thruster) |
| `$IIMWV`  | Wind speed and angle — apparent and true (requires anemometer sensor) |
| `$IIDPT`  | Depth below transducer with draught offset (requires echo sounder sensor) |
| `$IIDBT`  | Depth below transducer in feet, metres, fathoms |

### AIS

| Sentence  | Content |
|-----------|---------|
| `!AIVDO`  | Own-ship Class A position report (Type 1) every 2 s |
| `!AIVDO`  | Own-ship static voyage data (Type 5) every 10 s — vessel name, call sign |
| `!AIVDM`  | Traffic vessel Class A position report (Type 1) every 2 s per vessel |
| `!AIVDM`  | Traffic vessel static voyage data (Type 5) every 10 s per vessel |

All sentences are NMEA 0183 compliant with correct XOR checksums and `\r\n` line endings.

---

## Requirements

**Using the standalone executable** (`dist/nautis_nmea_bridge.exe`):
- No Python installation required
- No pip packages required
- Just copy the `.exe` and run it

**Using the Python script** (`nautis_nmea_bridge.py`):
- Python 3.8 or later
- `pip install grpcio protobuf`
- `proto_extracted/` directory must be present alongside the script

---

## Quick Start

1. **Start NAUTIS Home** and load a scenario.

2. **Run the bridge:**
   ```
   python nautis_nmea_bridge.py
   ```
   Defaults: reads from `127.0.0.1:53457`, sends NMEA to `127.0.0.1:10110` at 2 Hz.

3. **Configure your chart plotter** to receive UDP NMEA on port `10110`.

   *OpenCPN example: Options → Connections → Add Connection*
   - Type: `Network`, Protocol: `UDP`
   - Address: `0.0.0.0`, DataPort: `10110`
   - Tick: Input (enable AIS input on the same connection to see traffic targets)

4. **Optional — verify raw NMEA output** in a second terminal:
   ```
   python nautis_nmea_bridge.py --verbose
   ```

---

## Standalone Executable

A pre-built Windows executable is included at `dist/nautis_nmea_bridge.exe`. It bundles
the Python runtime, all required packages (`grpcio`, `protobuf`), and the
`proto_extracted/` descriptors into a single portable file.

### Distribution

Only the `.exe` needs to be distributed. Copy it to any Windows machine — no Python
installation, no pip, no additional files required:

```
nautis_nmea_bridge.exe [options]
```

All CLI options are identical to the Python script version.

### Rebuilding the executable

If NAUTIS Home is updated and the `proto_extracted/` descriptors need to be refreshed,
rebuild the executable after re-running `grpc_probe.py`:

```
# Step 1 — Re-extract proto descriptors (requires NAUTIS Home running)
python grpc_probe.py

# Step 2 — Rebuild using the existing spec file
pyinstaller nautis_nmea_bridge.spec
```

The updated executable will be written to `dist/nautis_nmea_bridge.exe`.

---

## Options

| Argument | Default | Description |
|---|---|---|
| `--host HOST` | `127.0.0.1` | gRPC server host |
| `--port PORT` | `53457` | gRPC server port |
| `--udp-host HOST` | `127.0.0.1` | UDP destination host |
| `--udp-port PORT` | `10110` | UDP destination port |
| `--rate RATE` | `2.0` | Poll and broadcast rate (Hz) |
| `--verbose` | off | Print every NMEA sentence to stdout |

**Send to a remote chart plotter at 5 Hz:**
```
python nautis_nmea_bridge.py --udp-host 192.168.1.50 --udp-port 10110 --rate 5
```

**Broadcast to all network interfaces:**
```
python nautis_nmea_bridge.py --udp-host 255.255.255.255
```

---

## File Structure

```
NautisNMEAsender/
├── dist/
│   └── nautis_nmea_bridge.exe  ← Standalone executable (distribute this)
├── nautis_nmea_bridge.py       ← Bridge source
├── nautis_nmea_bridge.spec     ← PyInstaller spec for rebuilding exe
├── grpc_probe.py               ← Re-extract proto_extracted/ if NAUTIS updates
├── listen_nmea.py              ← Diagnostic listener (port 10110)
├── README.md                   ← This file
├── proto_extracted/            ← Runtime-required binary descriptors
│   └── *.proto.pb
├── proto_files/                ← Human-readable .proto schemas (reference)
└── build/                      ← PyInstaller intermediate build files (safe to delete)
```

---

## Architecture

### Overview

```
┌──────────────────────────────────┐
│         NAUTIS Home              │
│  (vstep simulator, gRPC server)  │
│  Host: 127.0.0.1  Port: 53457   │
└──────────────┬───────────────────┘
               │  gRPC  GetComponents  (request/response)
               │  Poll at configurable rate (default: 2 Hz)
               ▼
┌─────────────────────────────────────────────────────────────┐
│                  nautis_nmea_bridge.py                      │
│                                                             │
│  Startup                                                    │
│  ├─ load_descriptors()  Load all .proto.pb files            │
│  └─ build_classes()     Resolve message classes             │
│                                                             │
│  Per-cycle (every 1/rate seconds)                           │
│  ├─ GetComponents RPC   Fetch all entity component data     │
│  ├─ Own-ship resolver   Camera → hierarchy → proximity      │
│  ├─ TelemetryResolver   Apply fallback matrix               │
│  ├─ Sensor resolver     Rudder, RPM, Wind, Depth            │
│  ├─ AIS encoder         Type 1 + Type 5 for all vessels     │
│  └─ NMEA builder        Format + checksum all sentences     │
│                                                             │
└─────────────────┬───────────────────────────────────────────┘
                  │  UDP  port 10110
                  ▼
┌──────────────────────────────────┐
│  Chart plotter / nav software    │
│  (OpenCPN, Expedition, etc.)     │
│  Sees own ship + AIS traffic     │
└──────────────────────────────────┘
```

---

### Phase 1 — Protobuf Descriptor Loading

NAUTIS Home's gRPC API packs component data into `google.protobuf.Any` fields. To
deserialize them at runtime we need the `.proto` type schemas.

The `proto_extracted/` directory contains every `.proto.pb` file (binary-encoded
`FileDescriptorProto` messages) from the NAUTIS Home installation. On startup,
`load_descriptors()` reads all of these and registers them into Python's global protobuf
descriptor pool. It iterates repeatedly until no further progress can be made, which
handles dependency ordering — a proto file that depends on another will only be
registered once its dependency is already in the pool.

`build_classes()` then calls `message_factory.GetMessageClass()` to produce a Python
class for each of the message types the bridge works with.

---

### Phase 2 — gRPC Polling Loop

The bridge connects to NAUTIS Home and sends a `GetComponents` unary RPC on a fixed
timer. The request is constructed **once at startup** and reused every cycle:

- **Selection**: `AllRootEntities` with `recursion = RECURSION_INCLUSIVE` — all root
  entities and their children (vessel bodies, sensors) are included
- **Component type filter**: only the component types in `SUBSCRIBE_TYPES` are returned

Each response is a flat list of component records. Each record carries:
- `entity.id` — the entity that owns the component
- `data.type_url` — the fully-qualified proto type name
- `data.value` — the serialized component bytes

These are parsed into a per-entity dictionary and also a flat `dict[(type_name, entity_id) → message]`.

**Why polling instead of the streaming API?**
NAUTIS Home also offers a `SubscribeComponents` streaming RPC. During development,
sustained streaming subscriptions caused the simulator's physics engine to stall — the
internal registry lock held by an open streaming response blocked physics update threads.
The polling approach releases the lock between every request, keeping the simulator
healthy for multi-hour runs.

---

### Phase 3 — Own-Ship Resolution

Before applying the telemetry fallback matrix, the bridge must determine which entity in
the registry corresponds to the operator's vessel. It uses a two-stage strategy:

**Stage 1 — Camera viewport lookup (preferred)**
The simulator maintains an `AssignedCamera` component on the main viewport entity
pointing to the entity ID of the camera currently attached to the trainee vessel.
The bridge climbs the parent hierarchy from that camera until it reaches the root entity
that carries an `MMSI` component — this is definitively the own ship.

**Stage 2 — Proximity fallback**
If no viewport camera is assigned (e.g., free-roam view), the bridge takes the first GPS
or geographic position from the response, then finds the MMSI vessel entity whose
`PositionGeographic` is closest to that coordinate.

Once identified, the bridge extracts the vessel's `Relations.children` list and restricts
all sensor lookups (GPS, compass, rudder, engine, wind, depth) to that entity and its
direct children only.

---

### Phase 4 — Telemetry Resolver (Fallback Matrix)

`TelemetryResolver.resolve()` applies a strict priority cascade for each telemetry
metric. If the highest-priority source is absent from the current response, the next tier
is tried automatically.

#### Fallback Matrix

| Metric | P1 | P2 | P3 | P4 |
|--------|----|----|----|-----|
| **Position** | `GPSOutput` (lat/lon) | `PositionGeographic` (active entity) | — | — |
| **Heading** | `CompassBaseOutput` | `INSOutput` | `OrientationEuler` (z/yaw) | `GPSOutput` (cog) |
| **SOG** | `GPSOutput` (sog) | `INSOutput` (sog) | `DopplerLogOutput` (sog) | `LinearMotion` (‖v‖) |
| **COG** | `GPSOutput` (cog) | `INSOutput` (cog) | `LinearMotion` (atan2) | Heading |
| **ROT** | `CompassBaseOutput` (rot) | `INSOutput` (rot) | `AngularMotion` (z/yaw rate) | `0.0` |
| **Time** | `DateTimeOutput` | System UTC clock | — | — |

#### Unit Conversions

All angular values in the NAUTIS registry are stored in **radians**. All speeds are in
**m/s**. The resolver applies the following at the point of extraction:

| Value | Registry unit | Conversion | NMEA unit |
|-------|--------------|------------|-----------|
| Heading, COG | radians | `math.degrees(r) % 360` | degrees true |
| ROT | rad/s | `math.degrees(r) × 60` | degrees/minute |
| SOG (sensor) | m/s | `v × 1.9438445` | knots |
| SOG (physics) | m/s | `‖v‖ × 1.9438445` | knots |

---

### Phase 5 — Supplementary Sensor Telemetry

After resolving own-ship navigation, the bridge scans the vessel's direct child entities
for optional sensor outputs:

**Rudder Angle (`$IIRSA`)**
Looks for `RudderIndicatorOutput` or `PropulsionIndicatorOutput` children. Child entity
names containing "port"/"left" are mapped to the port field; "stbd"/"starboard"/"right"
to the starboard field. On vessels with a single centred rudder, one value fills the
starboard field. Angle is converted from radians to degrees.

**Engine RPM (`$IIRPM`)**
Looks for `PropulsionIndicatorOutput` or `TermaRPMOutput` children. One `$IIRPM`
sentence is emitted per engine/thruster, sequentially numbered from 1.

**Wind (`$IIMWV`)**
Looks for a `WindmeterOutput` child (anemometer sensor). Both apparent and true wind
sentences are emitted when true wind speed > 0. Wind speed is in m/s as provided by
the registry.

**Depth (`$IIDPT` / `$IIDBT`)**
Looks for an `EchoSounderOutput` child. Both depth sentences are emitted using the
`water_depth` field. `$IIDPT` additionally includes the vessel draught offset.

---

### Phase 6 — AIS Encoder

The bridge implements a self-contained, pure-Python AIS Class A encoder (ITU-R M.1371):

**Message Type 1 — Position Report**
Encodes MMSI, navigation status (underway), ROT (converted via the `4.733 × √|ROT|`
formula), SOG × 10, position in 1/10000 minute units, COG × 10, true heading, and
timestamp. Emitted as `!AIVDM` (traffic) or `!AIVDO` (own ship) every 2 seconds per
vessel.

**Message Type 5 — Static and Voyage Data**
Encodes MMSI, a deterministic call sign (`TS` + last 5 digits of MMSI), vessel name,
ship type (70 — cargo), and destination (`NAUTIS`). 71 six-bit ASCII characters (426
bits). Emitted every 10 seconds per vessel.

Traffic headings use the vessel's gyro child sensor (`CompassBaseOutput`) if available,
falling back to `OrientationEuler.angles.z` (the vessel body's raw yaw angle).

---

### Connection Resilience

The outer reconnection loop in `run_bridge()` uses exponential backoff:

- Initial retry delay: 2 seconds
- Doubles on each failure, capped at 30 seconds
- Resets to 2 seconds on successful connection

The bridge can be started before the simulator and will connect automatically. It
recovers from simulator restarts without any manual intervention.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `gRPC UNAVAILABLE` | Simulator not running | Start NAUTIS Home and load a scenario |
| Waiting for position | Scenario paused or camera in free-roam | Unpause or take control of a vessel |
| No AIS targets in chart plotter | AIS not enabled on connection | Enable AIS input on the UDP connection in your chart plotter |
| `Warning: could not resolve vstep.X` | Missing proto descriptor | Re-extract `proto_extracted/` from NAUTIS Home using `grpc_probe.py` |
| OpenCPN shows no vessel | Wrong port or host | Verify UDP connection settings match `--udp-port` |
| No rudder / RPM / wind / depth | Vessel has no such sensor | Normal — those sentences are only emitted when the sensor entity is present |

---

## Known Limitations

- `$GPGGA` hardcodes satellite count (08), HDOP (0.9), and altitude (0.0 M) — these are
  not available from the simulator registry.
- `$GPHDG` carries the true heading value from the simulator. NAUTIS Home does not model
  magnetic deviation, so the magnetic deviation fields in the sentence are left empty.
- AIS Type 5 ship type is hardcoded to `70` (cargo). Vessel type is not exposed in the
  NAUTIS registry's public component API.
- AIS Type 5 call sign is synthetic (`TS` + last 5 digits of MMSI) — no call sign field
  exists in the NAUTIS entity registry.
