# NAUTIS Home → NMEA 0183 UDP Bridge

**Current version: `2.4.0`** — see [Changelog](#changelog) for history.

A production-grade, deadlock-free integration bridge that reads live telemetry from the
[NAUTIS Home](https://vstep.nl/nautis-home/) maritime simulator and re-broadcasts it as
standard NMEA 0183 sentences and AIS reports over UDP.

Compatible with any chart plotter or navigation software that accepts NMEA 0183 input —
OpenCPN, Coastal Explorer, Expedition, Furuno TZT, and others.

Available as a standalone Windows executable: **`dist/nautis_nmea_bridge.exe`** (no Python required).

---

## Features

- **Integrated Autopilot** — supports Standby, Heading (yaw hold), and Route (OpenCPN waypoint/cross-track tracking via `$APB`) modes
- **Pre-tuned Vessel Presets** — Slow, Medium, and Fast vessel preset sliders to automatically tune the PID controller for tankers, bulkers, tugs, or patrol boats
- **Advanced PID Override** — raw Kp, Ki, and Kd fields remain available for custom expert tuning
- **Magnetic Variation offset** — handles True vs. Magnetic coordinate mismatch when chart plotters emit Magnetic NMEA sentences
- **Compact & Pop-Out layouts** — dock the AP panel in a mini-dashboard window or pop it out into an always-on-top window to clear your screen
- **Universal vessel compatibility** — works on every vessel in NAUTIS Home, regardless of its sensor loadout (GPS, compass, or raw physics)
- **Own-ship automatic detection** — automatically detects the trainee's vessel or falls back to coordinate proximity matching
- **AIS vessel traffic** — broadcasts all other vessels in the scenario as Class A AIS targets (`!AIVDM`) so they appear in your chart plotter alongside your own position
- **Own-ship AIS** — emits `!AIVDO` sentences for the own-ship, static voyage data (Type 5) with vessel name and call sign
- **Supplementary telemetry** — outputs rudder angle, engine RPM, wind speed/direction, and water depth whenever the vessel's sensor loadout includes them
- **Hierarchical telemetry fallback** — automatically selects the best available data source for each metric at every poll cycle
- **Deadlock-free** — uses a request/response connection loop pattern, which maintains simulator performance during sustained use
- **Auto-reconnect** — exponential backoff reconnection loop; the bridge survives simulator restarts without manual intervention
- **Configurable** — poll rate, simulator host/port, and UDP destinations are all configurable at runtime

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

- **NAUTIS Home** simulator installed and running.
- **nautis_nmea_bridge.exe** copy to run. No Python installation or package setup is required.

---

## Quick Start

1. **Start NAUTIS Home** and load a scenario.

2. **Run the bridge (GUI Mode):**
   Double-click **`nautis_nmea_bridge.exe`** to launch the Maritime Console GUI dashboard.
   
   *For headless CLI deployment, run from a terminal:*
   ```
   nautis_nmea_bridge.exe --cli
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

A pre-built Windows executable is included at `dist/nautis_nmea_bridge.exe`. It bundles the runtime, UI libraries, and communication dependencies into a single portable file.

### Distribution

Only the `nautis_nmea_bridge.exe` needs to be distributed. Copy it to any Windows machine — no installation or additional files required:

```
nautis_nmea_bridge.exe [options]
```

---

## Options

| Argument | Default | Description |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Simulator connection host |
| `--port PORT` | `53457` | Simulator connection port |
| `--udp-host HOST` | `127.0.0.1` | UDP destination host (NMEA output) |
| `--udp-port PORT` | `10110` | UDP destination port (NMEA output) |
| `--rate RATE` | `2.0` | Poll and broadcast rate (Hz) |
| `--verbose` | off | Print NMEA output to stdout (CLI mode only) |
| `--cli` | off | Run in headless CLI mode instead of GUI |

---

## File Structure

```
NMEA Bridge/
├── dist/
│   └── nautis_nmea_bridge.exe  ← Standalone executable
└── README.md                   ← This file
```

---

## Port Allocation & Compatibility

The NMEA Bridge uses the following ports for network communication:

| Program / Service | Port | Protocol | Direction | Purpose |
|-------------------|------|----------|-----------|---------|
| NAUTIS Simulator | 53457 | TCP | Inbound | Simulator connection port |
| NMEA Bridge | 53457 | TCP | Outbound | Connects to simulator to retrieve telemetry & send autopilot commands |
| NMEA Bridge | 10110 | UDP | Outbound | Broadcasts NMEA 0183 sentences & AIS targets to chart plotter |
| NMEA Bridge | 10115 | UDP | Inbound | Listens for incoming autopilot routing sentences (`$APB`) |

### Coexistence with Standalone Radar
- The NMEA Bridge and the **Radar Display** both connect to the simulator's data interface on port `53457`. The connection supports multiple simultaneous clients, so running both programs at the same time does not cause conflicts.
- The NMEA Bridge uses port `10110` and `10115` for UDP NMEA data, which are completely separate from the Radar's UDP ASTERIX data ports (`54321` and `54322`). Both programs can run together without any network overlap.

---

## How It Works

The NMEA Bridge runs a background loop to retrieve real-time navigation parameters from the simulator, resolve telemetry sources, format standard marine sentences, and execute autopilot corrections.

### 1. Telemetry Query & Extraction
At a regular interval (configurable, default 2 Hz), the bridge retrieves data for active scenario vessels. It decodes the telemetry parameters for positioning, speeds, compass heading, wind speed, water depth, and engine metrics.

### 2. Own-Ship & Traffic Sorting
- **Own-Ship Resolution:** The bridge identifies the user's active vessel by querying the viewport status or falling back to proximity matching against active coordinates.
- **Traffic Tracking:** All other vessels in the simulation scenario are resolved and tracked individually as separate AIS targets.

### 3. Metric Fallbacks & Unit Conversions
The bridge applies a robust priority cascade for crucial values. If high-resolution sensor outputs (e.g. GPS, Gyro) are absent, it falls back to raw physics or basic coordinates. Speeds are translated from m/s to knots, and rotation angles from radians to degrees.

### 4. AIS Target Generation
- **Position Reports (Type 1):** Encodes latitude, longitude, course (COG), speed (SOG), heading, and rate of turn (ROT). Emitted every 2 seconds for active targets.
- **Vessel Details (Type 5):** Encodes static details including names, synthetic call signs, and physical lengths/beams (derived dynamically from vessel bounding boxes). Emitted every 10 seconds.

### 5. Closed-Loop Autopilot Control
When active, the autopilot executes a 1 Hz PID feedback loop:
- **Heading Filtering:** Gyro readings are passed through a low-pass filter to smooth out noise.
- **Rudder Control:** Target helm angles are computed based on the target heading (Heading Mode) or course correction (Route Mode using `$APB` cross-track error). Commands are rate-limited to 5°/second to protect steering systems and written back to the simulator.

### 6. Connection Resilience
The simulator connection logic runs in an outer loop with exponential backoff (starting at 2s, doubling up to 30s) to handle restarts or scenario changes gracefully without needing manual bridge restarts.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Connection unavailable | Simulator not running | Start NAUTIS Home and load a scenario |
| Waiting for position | Scenario paused or camera in free-roam | Unpause or take control of a vessel |
| No AIS targets in chart plotter | AIS not enabled on connection | Enable AIS input on the UDP connection in your chart plotter |
| Warning: could not resolve schema | Missing configuration descriptor | Restore required configuration files |
| OpenCPN shows no vessel | Wrong port or host | Verify UDP connection settings match `--udp-port` |
| No rudder / RPM / wind / depth | Vessel has no such sensor | Normal — those sentences are only emitted when the sensor entity is present |

---

## Known Limitations

- `$GPGGA` hardcodes satellite count (08), HDOP (0.9), and altitude (0.0 M) — these are not available from the simulator telemetry.
- `$GPHDG` carries the true heading value from the simulator. NAUTIS Home does not model magnetic deviation, so the magnetic deviation fields in the sentence are left empty.
- AIS Type 5 ship type is hardcoded to `70` (cargo). Vessel type is not exposed in the NAUTIS simulator's public telemetry.
- AIS Type 5 call sign is synthetic (`TS` + last 5 digits of MMSI) — no call sign field exists in the NAUTIS telemetry output.

---

## Changelog

The version string is displayed in the GUI footer.

| Version | Date | exe in dist/ | Notes |
|---------|------|:---:|-------|
| **2.4.0** | 2026-06-17 | ✅ | Dynamic vessel dimensions from simulator telemetry. Computes true length/beam for ownship and traffic dynamically. |
| **2.3.0** | 2026-06-17 | ❌ | Fix AIS Type 5 (`!AIVDM`) vessel dimensions. Hardcoded value `0x1E0502` (480 m × 22 m) replaced with 0 (ITU-R M.1371 “not available”). All targets now report unknown dimensions instead of an incorrect 480 m ship footprint. |
| **2.2.0** | 2026-06-15 | ✅ | Fix AIS Type 1 lat/lon encoding for negative coordinates. Resolve traffic vessel position/motion components across descendants. Fix autopilot preset slider crash on start. |
| **2.1.0** | 2026-06-14 | ❌ outdated | Reorganised project into `NautisHomeMods/NMEA Bridge/`. Added single-source versioning. |
| **2.0.0** | 2026-06-07 | ✅ | Integrated autopilot with Heading & Route (`$APB`) modes. Vessel response preset slider, pop-out AP window, compact mode, magnetic variation offset. |
| **1.0.0** | 2026-06-01 | ✅ | Initial release — polling bridge connection, NMEA + AIS output, deadlock-free architecture. |
