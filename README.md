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

Provides a standalone, networked radar Plan Position Indicator (PPI) display for NAUTIS Home. Intercepts the simulator's ASTERIX Cat 240 radar data stream via a lightweight UDP splitter and renders it in a real-time phosphor-decay display window. Supports same-machine display or routing to a separate computer for multi-monitor bridge setups.

**Key Features**
- Decodes EUROCONTROL ASTERIX Category 240 radar sweep packets in real time
- PPI display with persistence/afterglow decay, range rings, bearing scale, and sweep line
- Configurable range (0.25 – 24 NM), gain, sea clutter, rain clutter, and persistence
- Forwards the full stream to the in-game radar simultaneously (no in-game functionality lost)
- Optional gRPC link-back to the NMEA Bridge for synchronized gain/TX control
- Runs on the simulator computer or a separate machine on the same LAN

**Entry Points**: `RADAR/radar_splitter.py` (simulator computer), `RADAR/radar_display.py` (display computer)

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
    ├── radar_splitter.py
    └── radar_display.py
```

---

## Adding Future Sub-Projects

Create a new subdirectory under `NautisHomeMods/` and add an entry to this file following the pattern above. Each sub-project should include:
1. A `README.md` covering setup, dependencies, usage, and architecture
2. A brief summary entry here with key features and entry points
