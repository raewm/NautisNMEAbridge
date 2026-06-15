# NAUTIS Home Standalone Networked Radar Display

**Current version: `2.4.0`** — see [Changelog](#changelog) for history.

This project provides a standalone, high-performance radar display application for NAUTIS Home. It allows you to output the simulator's radar video stream to another window or to a separate computer on your local network to create a more realistic cockpit/bridge simulator setup.

---

## Architecture Overview

```
                        [ NAUTIS Home Simulator ]
                                   │
                                   │ (UDP ASTERIX Cat 240 on Port 54321)
                                   ▼
                       [ Radar Display (Splitter) ]
                             │                │
        (Local loopback on 44444)             │ (Internal UDP on 54322)
                             ▼                ▼
                    [ In-Game Radar ]   [ Radar Display (Receiver) ]
                                              │
                                              │ (gRPC Control on Port 53457)
                                              ▼
                                     [ NMEA Bridge / Sim ]
```

1. **ASTERIX Stream**: The simulator's ExtCommunication engine outputs raw radar sweep data as standard EUROCONTROL ASTERIX Cat 240 packets.
2. **Port Routing**: By default, NAUTIS outputs to port `44444`. To intercept the stream without disabling the in-game display, we configure the simulator to send to `54321`.
3. **Integrated Splitter**: A background thread inside the standalone display binds to `54321` on the simulator machine. It forwards the packets to `127.0.0.1:44444` (so the in-game radar works) and routes the stream to target displays.
4. **Standalone Display (`radar_display.py` / `dist/radar_display.exe`)**: A PySide6 (Qt) application that listens on UDP port `54322` (locally or remotely), decodes the Cat 240 packets, and draws a Plan Position Indicator (PPI) sweep screen with persistence/afterglow.

---

## Port Allocation & Compatibility

The RADAR tools use the following port layout:

| Program | Port | Protocol | Direction | Purpose |
|---------|------|----------|-----------|---------|
| NAUTIS Simulator | 53457 | gRPC | Inbound | Registry API |
| Radar Display (Splitter) | 54321 | UDP | Inbound | Intercept ASTERIX Cat 240 stream from sim |
| In-Game Radar | 44444 | UDP | Inbound | In-game display (forwarded by splitter) |
| Radar Display (Receiver) | 54322 | UDP | Inbound | Receive spokes (forwarded by splitter) |
| Radar Display | 53457 | gRPC | Outbound | Connect as client to poll own-ship heading |

### No Port Conflicts
- Multiple gRPC clients (like both the **Radar Display** and the **NMEA Bridge**) can connect to the simulator's gRPC server on port `53457` simultaneously without conflict.
- The **Radar Display** binds ONLY to UDP port `54322` to receive packets, avoiding any conflict with the simulator or other mods.

---

## File Layout

- **`radar_display.py`** (and `dist/radar_display.exe`): The PySide6 standalone radar display client, which includes the integrated UDP splitter thread.
- **`proto_extracted/`**: Directory containing the protobuf descriptors (`.proto.pb` files) extracted from the simulator. This folder is required for all gRPC controls (Gain, Clutters, TX toggle) and the North Up orientation mode.

---

## Installation & Setup

### 1. Configure the Simulator (`Library.xml`)

The simulator's `Library.xml` must be edited to redirect the ASTERIX radar stream from its default port (`44444`) to port `54321`, where our splitter listens.

**File location:**
```
C:\Program Files\VSTEP\NAUTIS Home\NautisHome\UserData\home\ExtCommunication\Library.xml
```

> Back up the original file before editing.

**What to change:**

Open the file in a text editor and find the block that contains `ASTERIX radar protocol`. Inside it, locate the `<_remotePort>` tag under the `UDPSender` channel. Change its value from `44444` to `54321`:

```xml
<!-- BEFORE -->
<_remotePort type="int">44444</_remotePort>

<!-- AFTER -->
<_remotePort type="int">54321</_remotePort>
```

Leave `<_remoteIP>` set to `127.0.0.1` — do not change it.

> **This is the only simulator-side file you need to modify.** In particular, `ExtCommunication.DataProvider.Settings.settings` does **not** need to be changed — that file relates to an unrelated NAUTIS TCP data service and is not used by this project.

### 2. Install Dependencies (If running script)
If running `radar_display.py` directly (rather than the standalone executable), ensure you have Python 3 installed and run:
```bash
pip install PySide6 grpcio protobuf
```

---

## Standalone Executable

A pre-built Windows executable is available at `dist/radar_display.exe`. It bundles the Python runtime, PySide6 GUI library, and the required protobuf schemas.

### Distribution
Only `dist/radar_display.exe` needs to be distributed. You can copy it to any display computer or run it on the simulator machine.

### Rebuilding the Executable
If you modify `radar_display.py`, you can rebuild the executable using PyInstaller:
```powershell
pyinstaller --clean --noconsole --onefile --add-data "proto_extracted;proto_extracted" radar_display.py
```

---

## Execution Guide

The application can be run in two ways: using the pre-built standalone executable (`dist/radar_display.exe`) or the Python script (`radar_display.py`).

### Scenario A: Running on the Same Computer (Default & Single-Machine)
Since the UDP Splitter is integrated directly as a background thread:
1. **Launch the Display**:
   - Double-click `dist/radar_display.exe` (or run `python radar_display.py`).
2. **That's it!**
   - The integrated splitter starts automatically, listening on port `54321` and forwarding packets to the in-game radar on `44444` and the local receiver on `54322`. No extra scripts or configurations are needed.

### Scenario B: Running on a Remote Display Computer
If your radar display is on a separate computer:
1. **On the Simulator Computer**:
   - Run `dist/radar_display.exe` (or `python radar_display.py`).
   - Open **Connection Settings** (bottom of the sidebar).
   - Check **Enable Background Splitter**.
   - In **Remote Display IPs**, enter your remote display computer's IP address (e.g., `192.168.1.50`).
   - Click **OK**.
2. **On the Remote Display Computer**:
   - Run `dist/radar_display.exe` (or `python radar_display.py`).
   - Open **Connection Settings**.
   - **Uncheck** "Enable Background Splitter" (since the splitter is running on the sim machine).
   - Click **OK**.

---

## Control Panel, Orientation & Plotting Tools

The standalone display contains interactive controls in the sidebar:
- **TX / STBY**: Toggles between radar transmit and standby modes.
- **Range Selector**: Adjusts the scale (0.25 NM to 24 NM).
- **Orientation Mode**: 
  - **HU (Heading Up)**: Ship's bow is always at the top of the display.
  - **NU (North Up)**: Geographically stabilized display with North at the top. Requires gRPC connection to feed own-ship compass heading.
- **Plotting Tools**:
  - **EBL (Electronic Bearing Line)**: A dashed bearing line with an outer bearing label. Adjust with spin box.
  - **VRM (Variable Range Marker)**: A dashed range circle with range labels. Adjust with spin box.
  - **PI (Parallel Index Lines)**: Offset index lines parallel to the EBL for clearing distance checks.
- **Gain**: Adjusts radar sensitivity locally.
- **Persistence**: Slider to adjust phosphor screen afterglow length (from Short/Medium/Long to Infinite).

### gRPC Heading Integration:
1. Press the **Connect gRPC** button in the sidebar.
2. Enter the IP of your simulator computer and the gRPC port (`53457` by default).
3. Once connected, the own-ship heading will poll at 1 Hz to enable **North Up (NU)** mode.

> [!NOTE]
> **Local Control Separation:** Standby/Transmit toggles and Gain control are purely local display controls. They do not alter the simulator's in-game radar settings, allowing the standalone display to act as an independent radar unit. Sea and rain clutter filtering should be adjusted using the in-game radar controls.

---

## Experimental Radar Modes

The radar display includes three advanced experimental modes configured and toggled from the **Experimental Modes** section in the right sidebar:

### 1. Doppler Shift Radar (Color)
- **Description**: Colors radar echoes based on their relative velocity to your vessel to make motion and collision risks immediately obvious.
- **Color Coding**:
  - **Yellow (Phosphor Amber)**: Stationary targets (landmasses, buoys, anchored vessels).
  - **Red (Closing)**: Vessels moving toward your vessel (potential collision/hazard).
  - **Green (Opening)**: Vessels moving away from your vessel.
- **Settings**:
  - **Radius**: A customizable proximity radius (20m to 300m, default 80m) around resolved targets. Echoes within this radius are colored based on target motion.

### 2. Motion Trails
- **Description**: Displays a fading trail of historical positions for moving targets, making relative course and speed trends visually apparent.
- **Settings**:
  - **Length**: Adjusts the number of past sweeps kept in memory (2 to 20 sweeps, default 6). Historical echoes gradually fade out and shrink.

### 3. AIS Target Overlay
- **Description**: Overlays live Automatic Identification System (AIS) data blocks directly on the radar sweep.
- **Visual Indicators**:
  - **Vessel Icon**: A bright green triangle indicating target position, rotated to its current heading.
  - **Course Vector**: A dashed green line showing the target's projected course and speed (representing 1 minute of travel).
  - **Data Tag**: Displays the target's Name, Range (NM), and Speed Over Ground (SOG, knots) in bright green.

---

## Technical Details

### ASTERIX Cat 240 Decoder
The application decodes the binary UDP packets in real-time. The packet layout matches the EUROCONTROL specification:
- **Header (31 bytes)**: Decodes start/end azimuth angles, start range, and cell resolution scale.
- **Video Spoke Data**: Decodes the 8-bit uncompressed reflectivity values (representing sweep echo intensity from 0 to 255).

### PPI Rendering Engine
The circular screen is drawn using PySide6's `QPainter` rendering into a persistent `QImage` buffer:
- **Persistence Decay**: An overlay fade operation runs at 10 FPS to simulate the gradual decay/afterglow of radar phosphor screens.
- **Sweep Line**: Renders the sweep line tracking the active azimuth returned from the simulator.
- **Range Rings & Bearing scale**: Renders dynamic labels, concentric rings, and a 360-degree scale.

---

## Changelog

| Version | Date | exe in dist/ | Notes |
|---------|------|:---:|-------|
| **2.4.0** | 2026-06-15 | ✅ | Add ship’s heading line (white, 12 o’clock in HU / compass heading in NU). Remove non-functional Sea/Rain clutter sliders; replace with note directing users to in-game radar. Stability: 3-second gRPC timeout on all stubs, one-poll-at-a-time thread guard, controller snapshot on reconnect, remove `__import__` inside paintEvent, AIS overlay recolored to bright green. |
| **2.3.0** | 2026-06-15 | ✅ | Fix range ring labels: values below 1.0 NM were incorrectly multiplied by 10 (e.g., 0.75 NM displayed as 7.50 NM). Now displays correct sub-NM distances with 3 decimal places. |
| **2.2.0** | 2026-06-15 | ✅ | Add three experimental radar modes: Doppler Shift color rendering, fading target motion trails, and AIS target overlay. Add interactive sidebar controls for settings. |
| **2.1.0** | 2026-06-14 | ✅ | Integrated splitter into the main radar display application. Removed legacy `radar_splitter.py`. |
| **1.0.0** | 2026-06-01 | ✅ | Initial release of networked standalone PPI radar. |

