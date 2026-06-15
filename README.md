# NautisHomeMods

A collection of Python-based extensions and tools for the [NAUTIS Home](https://vstep.nl/nautis-home/) maritime simulator by VSTEP. Each sub-project is self-contained and can be used independently.

---

## Sub-Projects

### [NMEA Bridge](./NMEA%20Bridge/README.md)

Reads live telemetry from NAUTIS Home over its internal gRPC API and re-broadcasts it as standard NMEA 0183 sentences and AIS reports over UDP. Allows any chart plotter or navigation software (OpenCPN, Coastal Explorer, Expedition, Furuno TZT, etc.) to receive real-time position, heading, wind, depth, and AIS traffic from the simulator.

**Key Features**
- Own-ship position, heading, SOG/COG, ROT, rudder angle, engine RPM, wind, and depth
- Full Class A AIS traffic (Type 1 + Type 5) for all scenario vessels
- Integrated autopilot with Heading Hold and Route (OpenCPN `$APB`) modes
- Pre-tuned vessel presets and full PID override
- Deadlock-free polling architecture — keeps the simulator physics engine healthy during long runs
- Distributed as a standalone Windows `.exe` (`dist/nautis_nmea_bridge.exe`) — no Python required

**Entry Point**: `NMEA Bridge/nautis_nmea_bridge.py` (or `dist/nautis_nmea_bridge.exe`)

---

### [RADAR](./RADAR/README.md)

Provides a standalone, networked radar Plan Position Indicator (PPI) display for NAUTIS Home. Intercepts the simulator's ASTERIX Cat 240 radar data stream and renders it in a real-time phosphor-decay display window. Supports same-machine display or routing to a separate computer for multi-monitor bridge setups.

**Key Features**
- Decodes EUROCONTROL ASTERIX Category 240 radar sweep packets in real time
- PPI display with persistence/afterglow decay, range rings, bearing scale, and sweep line
- Configurable range (0.25 – 24 NM), gain, sea clutter, rain clutter, and persistence (all controls are local to display)
- Integrated background UDP splitter forwards the stream to the in-game radar simultaneously
- Optional gRPC link to simulator/NMEA Bridge to poll own-ship heading for North Up (NU) mode
- Distributed as a standalone Windows `.exe` (`dist/radar_display.exe`) — no Python required

**Entry Point**: `RADAR/radar_display.py` (or `dist/radar_display.exe`)

---

## Port Allocation & Coexistence

Both sub-projects can run simultaneously without port conflicts. Below is the complete port map for both applications:

| Program / Service | Port | Protocol | Direction | Description |
|-------------------|------|----------|-----------|-------------|
| NAUTIS Simulator | 53457 | gRPC | Inbound | Simulator Registry API (server) |
| NMEA Bridge | 53457 | gRPC | Outbound | Connects to poll telemetry and write autopilot commands (client) |
| Radar Display | 53457 | gRPC | Outbound | Connects to poll own-ship heading for North Up mode (client) |
| NMEA Bridge | 10110 | UDP | Outbound | Broadcasts NMEA 0183 sentences & AIS targets to chart plotter |
| NMEA Bridge | 10115 | UDP | Inbound | Listens for incoming autopilot routing sentences (`$APB`) |
| Radar Display (Splitter) | 54321 | UDP | Inbound | Intercepts simulator's raw ASTERIX Cat 240 radar stream |
| In-Game Radar | 44444 | UDP | Inbound | Standard in-game radar display (forwarded by splitter) |
| Radar Display (Receiver) | 54322 | UDP | Inbound | Receives radar spokes from splitter |

### Coexistence details:
- **gRPC Coexistence:** Port `53457` is the simulator's gRPC server. Both NMEA Bridge and Radar Display act as **clients** to this server. Multiple clients are fully supported and will not cause conflicts.
- **UDP Coexistence:** The NMEA Bridge and Radar Display use completely distinct UDP ports for their telemetry and radar streams, ensuring no conflicts when running both tools at the same time.

---

## Shared Requirements

All sub-projects require:
- **NAUTIS Home** installed and a scenario loaded
- **Python 3.8+** (if running `.py` scripts rather than the pre-built executable)
- `proto_extracted/` binary protobuf descriptors extracted from the NAUTIS Home installation — see the individual project READMEs for details

The NMEA Bridge includes a pre-built `.exe` that bundles all dependencies; the Radar Display requires a manual Python environment.

---

## Repository Layout

```
NautisHomeMods/
├── README.md               ← This file
├── NMEA Bridge/
│   ├── README.md           ← Full NMEA Bridge documentation
│   ├── nautis_nmea_bridge.py
│   ├── nautis_gui.py
│   ├── autopilot.py
│   ├── proto_extracted/    ← Runtime protobuf descriptors
│   └── dist/
│       └── nautis_nmea_bridge.exe
└── RADAR/
    ├── README.md           ← Full Radar Display documentation
    ├── radar_display.py
    └── dist/
        └── radar_display.exe
```

---
