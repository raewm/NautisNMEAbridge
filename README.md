# NAUTIS Home → NMEA 0183 UDP Bridge

Integration bridge that reads live telemetry from the
[NAUTIS Home](https://vstep.nl/nautis-home/) maritime simulator and re-broadcasts it as
standard NMEA 0183 sentences and AIS reports over UDP.

Compatible with any chart plotter or navigation software that accepts NMEA 0183 input —
OpenCPN, Coastal Explorer, Expedition, Furuno TZT, and others.

Available in two forms:
- **`nautis_nmea_bridge.exe`** — standalone executable, no Python required
- **`nautis_nmea_bridge.py`** — Python script, requires `pip install grpcio protobuf`

---

## Features

- **Integrated Autopilot** — supports Standby, Heading (yaw hold), and Route (OpenCPN waypoint/cross-track tracking via `$APB`) modes
- **Pre-tuned Vessel Presets** — Slow, Medium, and Fast vessel preset sliders to automatically tune the PID controller for tankers, bulkers, tugs, or patrol boats
- **Advanced PID Override** — raw Kp, Ki, and Kd fields remain available for custom expert tuning
- **Magnetic Variation offset** — handles True vs. Magnetic coordinate mismatch when chart plotters emit Magnetic NMEA sentences
- **Compact & Pop-Out layouts** — dock the AP panel in a mini-dashboard window or pop it out into an always-on-top window to clear your screen
- **Universal vessel compatibility** — works on every vessel in NAUTIS Home, regardless of its sensor loadout (GPS, compass, INS, Doppler log, or raw physics only)
- **Own-ship automatic detection** — uses the simulator's viewport camera assignment to identify the trainee's vessel; automatically falls back to coordinate proximity matching
- **AIS vessel traffic** — broadcasts all other vessels in the scenario as Class A AIS targets (`!AIVDM`) so they appear in your chart plotter alongside your own position
- **Own-ship AIS** — emits `!AIVDO` sentences for the own-ship, static voyage data (Type 5) with vessel name and call sign
- **Supplementary telemetry** — outputs rudder angle, engine RPM, wind speed/direction, and water depth whenever the vessel's sensor loadout includes them
- **Hierarchical telemetry fallback** — automatically selects the best available data source for each metric at every poll cycle
- **Deadlock-free** — uses a request/response polling pattern instead of the simulator's streaming subscription API, which was found to cause simulator physics freezes during sustained use
- **Auto-reconnect** — exponential backoff reconnection loop; the bridge survives simulator restarts without manual intervention
- **Configurable** — poll rate, gRPC host/port, and UDP destinations are all configurable at runtime

---

## NMEA Sentences Handled

### Output to Chart Plotter (UDP broadcast, default port 10110)

| Sentence  | Content |
|-----------|---------|
| `$GPGGA`  | Position fix — latitude, longitude, UTC time |
| `$GPRMC`  | Recommended minimum navigation data — position, SOG, COG, date |
| `$GPVTG`  | Course and speed over ground (true, knots, km/h) |
| `$GPHDG`  | Heading |
| `$GPROT`  | Rate of turn (degrees per minute) |
| `$IIRSA`  | Rudder angle — port and/or starboard |
| `$IIRPM`  | Engine revolutions per minute (one sentence per engine/thruster) |
| `$IIMWV`  | Wind speed and angle — apparent and true |
| `$IIDPT`  | Depth below transducer with draught offset |
| `$IIDBT`  | Depth below transducer in feet, metres, fathoms |
| `!AIVDO`  | Own-ship Class A position report (Type 1) and static voyage data (Type 5) |
| `!AIVDM`  | Traffic vessel Class A position report (Type 1) and static voyage data (Type 5) |

### Input from Chart Plotter (UDP listener, default port 10115)

| Sentence  | Content |
|-----------|---------|
| `$APB`    | Autopilot Sentence "B" — contains cross-track error (XTE), bearing to waypoint, and destination waypoint name |

---

## Requirements

**Using the standalone executable** (`nautis_nmea_bridge.exe`):
- No Python installation required
- No pip packages required
- Just copy the `.exe` and run it

**Using the Python script** (`nautis_nmea_bridge.py`):
- Python 3.8 or later
- `pip install grpcio protobuf PySide6`
- `proto_extracted/` directory must be present alongside the script

---

## Quick Start

1. **Start NAUTIS Home** and load a scenario.

2. **Run the bridge (GUI Mode):**
   ```
   # Launches the PySide6 Maritime Console
   python nautis_nmea_bridge.py
   ```
   *For headless deployment, run:*
   ```
   python nautis_nmea_bridge.py --cli
   ```

3. **Configure your chart plotter:** See the detailed [OpenCPN Connection Guide](#opencpn-connection-guide) section below.

4. **Operate the Autopilot:**
   - **Standby Mode**: Control the helm manually inside NAUTIS Home.
   - **Heading Mode**: The autopilot holds the current heading. Use the `−10`, `−1`, `+1`, `+10` buttons to adjust the target heading.
   - **Route Mode**: Activate a route in OpenCPN. The bridge receives `$APB` sentences and steers the vessel along the path.

---

## OpenCPN Connection Guide

To integrate the bridge with OpenCPN for both telemetry display (including AIS traffic) and active autopilot route steering, you must configure two network connections in OpenCPN:

### Step 1 — Add NMEA/AIS Input Connection (Bridge → OpenCPN)
This connection receives position, heading, depth, wind, engine data, and Class A AIS traffic targets from the simulator:
1. In OpenCPN, open the **Options** dialog (gear icon in the top toolbar).
2. Go to the **Connections** tab.
3. In the **Data Connections** panel, click **Add Connection**.
4. Configure the settings:
   - **Type**: Select `Network`
   - **Protocol**: Select `UDP`
   - **Address**: Enter `0.0.0.0` (or `127.0.0.1`)
   - **DataPort**: Enter `10110`
   - **User Comment**: Enter `NAUTIS Telemetry & AIS Input`
5. Ensure **Receive traffic on this port** is checked.
6. Leave other settings at default and click **Apply**.

### Step 2 — Add Autopilot Output Connection (OpenCPN → Bridge)
This connection transmits active route steering sentences (`$APB`) from OpenCPN to the autopilot:
1. In the **Connections** tab, click **Add Connection** again.
2. Configure the settings:
   - **Type**: Select `Network`
   - **Protocol**: Select `UDP`
   - **Address**: Enter `127.0.0.1`
   - **DataPort**: Enter `10115`
   - **User Comment**: Enter `NAUTIS Autopilot Control Output`
3. Check **Output on this port (as client or multicast)**.

### Step 3 — Filter Autopilot Sentences (Crucial)
To avoid network loops and ensure the autopilot receives only the routing sentences it expects:
1. Under the connection options for the port `10115` connection you just created, click the **Input/Output Filtering** button (under the "Output on this port" checkbox).
2. Set **Output Filter Policy** to **`Drop all except...`**.
3. In the text box, select or type **`APB`** and add it to the list.
4. Click **OK** to close the filter dialog, then click **Apply** and **OK** to save options.

### Step 4 — Activating a Route
To test the autopilot route tracking:
1. Create a route using the Route tool in OpenCPN.
2. Right-click the route line and select **Activate Route**.
3. Ensure the Autopilot Panel in the bridge GUI is set to **Route Mode**.
4. In the bridge GUI, verify that:
   - **Route Data** changes from `NO SIGNAL` to `OK`.
   - **Waypoint** shows the active OpenCPN waypoint name.
   - **Cross-Track Error (XTE)** shows the current cross-track deviation.
   - The vessel begins steering to track the route.

---

## Standalone Executable

A pre-built Windows executable is included at `nautis_nmea_bridge.exe`. It bundles the Python runtime, PySide6 GUI library, gRPC dependency, and type descriptors into a single portable file.

### Distribution

Only the `.exe` needs to be distributed. Copy it to any Windows machine — no Python installation, no pip, no additional files required:

```
nautis_nmea_bridge.exe [options]
```

All CLI options are identical to the Python script version.

### Rebuilding the executable

```
# Rebuild using the spec file (runs in windowed mode, suppresses blank background console)
pyinstaller --clean nautis_nmea_bridge.spec
```

The updated executable will be written to `dist/nautis_nmea_bridge.exe`.

---

## Options

| Argument | Default | Description |
|---|---|---|
| `--host HOST` | `127.0.0.1` | gRPC server host |
| `--port PORT` | `53457` | gRPC server port |
| `--udp-host HOST` | `127.0.0.1` | UDP destination host (NMEA output) |
| `--udp-port PORT` | `10110` | UDP destination port (NMEA output) |
| `--rate RATE` | `2.0` | Poll and broadcast rate (Hz) |
| `--verbose` | off | Print NMEA output to stdout (CLI mode only) |
| `--cli` | off | Run in headless CLI mode instead of GUI |

---

## File Structure

```
NautisNMEAsender/
├── dist/
│   └── nautis_nmea_bridge.exe  ← Standalone executable (distribute this)
├── nautis_nmea_bridge.py       ← gRPC client, NMEA parser, and core engine
├── nautis_gui.py               ← PySide6 Maritime Console GUI dashboard
├── autopilot.py                ← PID steering controller and $APB sentence router
├── nautis_nmea_bridge.spec     ← PyInstaller spec for windowed build compilation
├── .gitignore                  ← Git ignore configuration for PyInstaller build artifacts
├── README.md                   ← This file
├── proto_extracted/            ← Runtime-required binary descriptors (*.proto.pb)
└── proto_files/                ← Human-readable .proto schemas (reference)
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

### Phase 7 — Autopilot Control Loop (Steering Write-back)

When the Autopilot is set to **Heading** or **Route** mode, the bridge executes a closed-loop PID control cycle throttled to **1 Hz** (ships respond on 3–10s timescales, making faster updates counterproductive):

1. **Heading Low-Pass Filter**:
   The own-ship gyro heading is filtered using a simple Exponential Moving Average (EMA) with $\alpha = 0.3$:
   $$\text{filtered\_heading} = 0.3 \times \text{heading} + 0.7 \times \text{prev\_heading}$$
   This smooths the coarse 2 Hz gRPC position/heading quantization steps, eliminating derivative kick noise in the PID controller.

2. **PID Calculations**:
   - In **Heading Mode**, the error is the difference between the filtered own-ship heading and the target heading.
   - In **Route Mode**, the error is calculated by adding a cross-track correction to the waypoint bearing received via `$APB`:
     $$\text{Target Heading} = \text{Bearing to Waypoint} + \text{XTE Correction}$$
     XTE correction scales with cross-track error to guide the vessel back onto the route path.

3. **External Control Actuator Lock**:
   To prevent conflicts with the simulator's trainee helm, the bridge issues a `SetExternalControl` gRPC request targeting the own-ship's rudder actuator entity IDs. This locks the rudders to external gRPC control. On stop or switch to Standby, a release call returns control to the simulator's helm.

4. **Rudder Command Rate Limiter**:
   Commanded rudder angles are rate-limited to a maximum change of **5.0° per second** to prevent steering gear slamming. The resulting target angle (up to $\pm 25^\circ$) is packed into a `SetComponents` gRPC write-back payload and transmitted to the simulator.

---

### Connection Resilience

The outer reconnection loop in `run_bridge()` uses exponential backoff:

- Initial retry delay: 2 seconds
- Doubles on each failure, capped at 30 seconds
- Resets to 2 seconds on successful connection

The bridge can be started before the simulator and will connect automatically. It recovers from simulator restarts without any manual intervention.

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
