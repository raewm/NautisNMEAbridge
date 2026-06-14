# NAUTIS Home Standalone Networked Radar Display

This project provides a standalone, high-performance radar display application for NAUTIS Home. It allows you to output the simulator's radar video stream to another window or to a separate computer on your local network to create a more realistic cockpit/bridge simulator setup.

---

## Architecture Overview

```
                        [ NAUTIS Home Simulator ]
                                   │
                                   │ (UDP ASTERIX Cat 240 on Port 54321)
                                   ▼
                        [ run_nautis_radar.py ]
                          (Launcher / Splitter)
                            │                │
       (Local loopback on 44444)            │ (Network UDP on 54322)
                            ▼                ▼
                   [ In-Game Radar ]   [ Standalone Radar Display ]
                                          (radar_display.py)
                                             │
                                             │ (gRPC Control on Port 8086)
                                             ▼
                                       [ NMEA Bridge / Sim ]
```

1. **ASTERIX Stream**: The simulator's ExtCommunication engine outputs raw radar sweep data as standard EUROCONTROL ASTERIX Cat 240 packets.
2. **Port Routing**: By default, NAUTIS outputs to port `44444`. To intercept the stream without disabling the in-game display, we configure the simulator to send to `54321`.
3. **Splitter & Launcher (`run_nautis_radar.py`)**: A lightweight launcher binds to `54321` on the simulator machine. It forwards the packets to `127.0.0.1:44444` (so the in-game radar works) and duplicates the stream to target computers.
4. **Standalone Display (`radar_display.py`)**: A PySide6 (Qt) application that listens on UDP port `54322` (locally or remotely), decodes the Cat 240 packets, and draws a Plan Position Indicator (PPI) sweep screen with persistence/afterglow.

---

## File Layout

- **`radar_splitter.py`**: The raw UDP splitter. Listens on port `54321`, forwards to the in-game radar (`44444`) and the standalone display (`54322`).
- **`radar_display.py`**: The PySide6 standalone radar display client.

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

### 2. Install Dependencies (Display Computer)
Ensure you have Python 3 installed. Install the following libraries:
```bash
pip install PySide6 grpcio protobuf
```

---

## Execution Guide

### Scenario A: Running on a Remote Computer (Recommended)
1. **On the Simulator Computer** — start the splitter and point it at the remote IP:
   ```bash
   python radar_splitter.py --display <remote-computer-ip>
   ```
   *(e.g., `python radar_splitter.py --display 192.168.1.50`)*
   *(This forwards to port 54322 on the remote machine by default).*

2. **On the Remote Computer** — start the standalone display:
   ```bash
   python radar_display.py
   ```
   *(This listens on port 54322 by default.)*

### Scenario B: Running on the Same Computer (Default)
1. **Start the Splitter**:
   ```bash
   python radar_splitter.py
   ```
   *(Listens on port 54321, forwards to in-game radar on 44444 and standalone display on 54322.)*
2. **Start the Display**:
   ```bash
   python radar_display.py
   ```
   *(Listens on port 54322. No additional configuration needed.)*

---

## Control Panel & gRPC Integration

The standalone display contains interactive controls in the sidebar:
- **Range Selector**: Adjusts the scale (0.25 NM to 24 NM).
- **Gain, Sea Clutter, Rain Clutter, Persistence**: Adjustable sliders.
- **TX / STBY**: Toggles between radar transmit and standby modes.

### Linking Controls to the Simulator:
1. Press the **Connect gRPC** button in the sidebar.
2. Enter the IP of your simulator computer and the gRPC port (`8086` if using the NMEA Bridge).
3. Once connected, changes made to the sidebar controls (Gain, Clutters, TX/STBY) will be transmitted back to the simulator in real-time, syncing the state of the radar.

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
