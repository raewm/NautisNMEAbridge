# NAUTIS Home → NMEA 0183 UDP Bridge

Integration bridge that reads live telemetry from the
[NAUTIS Home](https://vstep.nl/nautis-home/) maritime simulator and re-broadcasts it as
standard NMEA 0183 sentences over UDP.

Compatible with any chart plotter or navigation software that accepts NMEA 0183 input —
OpenCPN, Coastal Explorer, Expedition, Furuno TZT, and others.

Available in two forms:
- **`nautis_nmea_bridge.exe`** — standalone executable, no Python required
- **`nautis_nmea_bridge.py`** — Python script, requires `pip install grpcio protobuf`

---

## Features

- **Universal vessel compatibility** — works on every vessel in NAUTIS Home, regardless
  of its sensor loadout (GPS, compass, INS, Doppler log, or raw physics only)
- **Hierarchical telemetry fallback** — automatically selects the best available data
  source for each metric at every poll cycle
- **Deadlock-free** — uses a request/response polling pattern instead of the simulator's
  streaming subscription API, which was found to cause simulator physics freezes during
  sustained use
- **Auto-reconnect** — exponential backoff reconnection loop; the bridge survives
  simulator restarts without manual intervention
- **Configurable** — poll rate, gRPC host/port, and UDP destination are all runtime
  arguments

---

## NMEA Sentences Produced

| Sentence  | Content |
|-----------|---------|
| `$GPGGA`  | Position fix — latitude, longitude, UTC time |
| `$GPRMC`  | Recommended minimum navigation data — position, SOG, COG, date |
| `$GPVTG`  | Course and speed over ground (true, knots, km/h) |
| `$GPHDG`  | Heading |
| `$GPROT`  | Rate of turn (degrees per minute) |

All sentences are NMEA 0183 compliant with correct XOR checksums and `\r\n` line endings.

---

## Requirements

**Using the standalone executable** (`nautis_nmea_bridge.exe`):
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
   python nautis_nmea_bridge.py  OR  nautis_nmea_bridge.exe
   ```
   Defaults: reads from `127.0.0.1:53457`, sends NMEA to `127.0.0.1:10110` at 2 Hz.

3. **Configure your chart plotter** to receive UDP NMEA on port `10110`.

   *OpenCPN example: Options → Connections → Add Connection*
   - Type: `Network`, Protocol: `UDP`
   - Address: `0.0.0.0`, DataPort: `10110`
   - Tick: Input

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

# Step 2 — Rebuild the executable
python -m PyInstaller --onefile --name nautis_nmea_bridge `
    --add-data "proto_extracted;proto_extracted" `
    --hidden-import grpc `
    --hidden-import google.protobuf `
    --hidden-import google.protobuf.descriptor_pb2 `
    --hidden-import google.protobuf.descriptor_pool `
    --hidden-import google.protobuf.message_factory `
    --hidden-import google.protobuf.any_pb2 `
    --hidden-import google.protobuf.duration_pb2 `
    --hidden-import google.protobuf.timestamp_pb2 `
    --distpath dist --workpath build --specpath . --noconfirm `
    nautis_nmea_bridge.py
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
sim_integration/
├── dist/
│   └── nautis_nmea_bridge.exe  ← Standalone executable (distribute this)
├── nautis_nmea_bridge.py       ← Bridge source
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
┌──────────────────────────────────────────────────────┐
│               nautis_nmea_bridge.py                  │
│                                                      │
│  Startup                                             │
│  ├─ load_descriptors()  Load all .proto.pb files     │
│  └─ build_classes()     Resolve 14 message classes   │
│                                                      │
│  Per-cycle (every 1/rate seconds)                    │
│  ├─ GetComponents RPC   Fetch active component data  │
│  ├─ Parse response      Deserialize into keyed dict  │
│  ├─ TelemetryResolver   Apply fallback matrix        │
│  └─ NMEA builder        Format + checksum 5 sentences│
│                                                      │
└──────────────┬───────────────────────────────────────┘
               │  UDP  port 10110  (5 NMEA sentences/cycle)
               ▼
┌──────────────────────────────────┐
│  Chart plotter / nav software    │
│  (OpenCPN, Expedition, etc.)     │
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
class for each of the 14 message types the bridge works with.

---

### Phase 2 — gRPC Polling Loop

The bridge connects to NAUTIS Home and sends a `GetComponents` unary RPC on a fixed
timer. The request is constructed **once at startup** and reused every cycle:

- **Selection**: `AllRootEntities` with `recursion = RECURSION_INCLUSIVE` — all root
  entities and their children (vessel bodies, sensors) are included
- **Component type filter**: only the 9 component types in `SUBSCRIBE_TYPES` are
  returned, reducing payload size

Each response is a flat list of component records. Each record carries:
- `entity.id` — the entity that owns the component
- `data.type_url` — the fully-qualified proto type name
- `data.value` — the serialized component bytes

These are parsed into a `dict[(type_name, entity_id) → message]` and handed to
`TelemetryResolver`.

**Why polling instead of the streaming API?**
NAUTIS Home also offers a `SubscribeComponents` streaming RPC. During development,
sustained streaming subscriptions caused the simulator's physics engine to stall — the
internal registry lock held by an open streaming response blocked physics update threads.
The polling approach releases the lock between every request, keeping the simulator
healthy for multi-hour runs.

---

### Phase 3 — Telemetry Resolver (Fallback Matrix)

`TelemetryResolver.resolve()` applies a strict priority cascade for each telemetry
metric. If the highest-priority source is absent from the current response, the next tier
is tried automatically. This is what makes the bridge vessel-agnostic.

#### Fallback Matrix

| Metric | P1 | P2 | P3 | P4 |
|--------|----|----|----|----|
| **Position** | `GPSOutput` (lat/lon) | `PositionGeographic` (active entity) | — | — |
| **Heading** | `CompassBaseOutput` | `INSOutput` | `OrientationEuler` (z/yaw) | `GPSOutput` (cog) |
| **SOG** | `GPSOutput` (sog) | `INSOutput` (sog) | `DopplerLogOutput` (sog) | `LinearMotion` (‖v‖) |
| **COG** | `GPSOutput` (cog) | `INSOutput` (cog) | `LinearMotion` (atan2) | Heading |
| **ROT** | `CompassBaseOutput` (rot) | `INSOutput` (rot) | `AngularMotion` (z/yaw rate) | `0.0` |
| **Time** | `DateTimeOutput` | System UTC clock | — | — |

**Vessel type coverage:**

| Vessel configuration | Position | Heading | SOG | COG | ROT |
|---|---|---|---|---|---|
| GPS + Compass (typical) | GPSOutput | CompassBaseOutput | GPSOutput | GPSOutput | CompassBaseOutput |
| INS only (military/research) | PositionGeographic | INSOutput | INSOutput | INSOutput | INSOutput |
| Doppler log, no GPS | PositionGeographic | OrientationEuler | DopplerLogOutput | LinearMotion | AngularMotion |
| Raw physics only | PositionGeographic | OrientationEuler | LinearMotion | LinearMotion | AngularMotion |

**Active entity detection**: When falling back to spatial components, the resolver
prefers the entity that co-owns a `LinearMotion` component — a moving entity is the
most likely candidate to be the operator-controlled vessel.

**COG derivation from physics**: When no sensor provides COG directly, the resolver
derives it from the `LinearMotion` velocity vector:

```python
COG = atan2(velocity.y, velocity.x)   # x=north/forward, y=east/starboard
```

This is gated on a minimum speed threshold (0.01 m/s) to suppress noise when stationary.

#### Unit Conversions

All angular values in the NAUTIS registry are stored in **radians**. All speeds are in
**m/s**. The resolver applies the following at the point of extraction:

| Value | Registry unit | Conversion | NMEA unit |
|-------|--------------|------------|-----------|
| Heading, COG | radians | `math.degrees(r) % 360` | degrees true |
| ROT | rad/s | `math.degrees(r) * 60` | degrees/minute |
| SOG (sensor) | m/s | `v * 1.9438445` | knots |
| SOG (physics) | m/s | `‖v‖ * 1.9438445` | knots |

---

### Phase 4 — NMEA Sentence Builder

Five NMEA 0183 sentences are assembled from the resolved telemetry each cycle:

- Latitude/longitude formatted as `DDMM.MMMMM` (degrees + decimal minutes) with
  hemisphere designators — per NMEA 0183 standard
- UTC time from the simulator's own `DateTimeOutput` clock when available, otherwise
  from the system clock
- Checksums computed as XOR of all bytes between `$` and `*`
- Each sentence terminated with `\r\n`

All five sentences are sent as a burst over UDP to the configured destination.

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
| Waiting for position | Scenario paused | Unpause and let physics settle |
| Heading stuck at 0° | Unit conversion not applied | Ensure you are running the current version |
| `Warning: could not resolve vstep.X` | Missing proto descriptor | Re-extract `proto_extracted/` from NAUTIS Home |
| OpenCPN shows no vessel | Wrong port or host | Verify UDP connection settings match `--udp-port` |

---

## Known Limitations

- The bridge targets the **first** component found for each type. In multi-vessel
  scenarios, it will resolve telemetry from whichever vessel's component appears first in
  the response. Multi-vessel disambiguation is not yet implemented.
- `$GPGGA` hardcodes satellite count (08), HDOP (0.9), and altitude (0.0 M) — these are
  not available from the simulator registry.
- `$GPHDG` carries the true heading value from the simulator. NAUTIS Home does not model
  magnetic deviation, so the magnetic deviation fields in the sentence are left empty.
