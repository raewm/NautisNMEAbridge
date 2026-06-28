# NautisHomeMods

A collection of standalone integration extensions and tools for the [NAUTIS Home](https://vstep.nl/nautis-home/) maritime simulator by VSTEP. Each sub-project is self-contained and can be used independently.

---

## Sub-Projects

### [NMEA Bridge](./NMEA%20Bridge/README.md)

Reads live telemetry from NAUTIS Home and re-broadcasts it as standard NMEA 0183 sentences and AIS reports over UDP. Allows any chart plotter or navigation software (OpenCPN, Coastal Explorer, Expedition, Furuno TZT, etc.) to receive real-time position, heading, wind, depth, and AIS traffic from the simulator.

**Key Features**
- Own-ship position, heading, SOG/COG, ROT, rudder angle, engine RPM, wind, and depth
- Full Class A AIS traffic (Type 1 + Type 5) for all scenario vessels
- Integrated autopilot with Heading Hold and Route (OpenCPN `$APB`) modes
- Pre-tuned vessel presets and full PID override
- Deadlock-free connection architecture — maintains simulator performance during long runs
- Distributed as a standalone Windows `.exe` (`dist/nautis_nmea_bridge.exe`) — no Python required

**Executable**: `NMEA Bridge/dist/nautis_nmea_bridge.exe`

---

### [RADAR](./RADAR/README.md)

Provides a standalone, networked radar Plan Position Indicator (PPI) display for NAUTIS Home. Intercepts the simulator's ASTERIX Cat 240 radar data stream and renders it in a real-time phosphor-decay display window. Supports same-machine display or routing to a separate computer for multi-monitor bridge setups.

**Key Features**
- Decodes EUROCONTROL ASTERIX Category 240 radar sweep packets in real time
- PPI display with persistence/afterglow decay, range rings, bearing scale, and sweep line
- Three experimental modes: Doppler Shift color coding (closing/opening), fading motion trails, and AIS target overlay
- Configurable range (0.25 – 24 NM), gain, and persistence (controls are local to display). Sea/rain clutter must be adjusted in-game.
- Integrated background UDP splitter forwards the stream to the in-game radar simultaneously
- Optional connection link to simulator/NMEA Bridge to poll own-ship heading for North Up (NU) mode
- Distributed as a standalone Windows `.exe` (`dist/radar_display.exe`) — no Python required

**Executable**: `RADAR/dist/radar_display.exe`

---

## Port Allocation & Coexistence

Both sub-projects can run simultaneously without port conflicts. Below is the complete port map for both applications:

| Program / Service | Port | Protocol | Direction | Description |
|-------------------|------|----------|-----------|-------------|
| NAUTIS Simulator | 53457 | TCP | Inbound | Simulator connection port |
| NMEA Bridge | 53457 | TCP | Outbound | Connects to simulator to retrieve telemetry and send autopilot commands |
| Radar Display | 53457 | TCP | Outbound | Connects to simulator to retrieve own-ship heading for North Up mode |
| NMEA Bridge | 10110 | UDP | Outbound | Broadcasts NMEA 0183 sentences & AIS targets to chart plotter |
| NMEA Bridge | 10115 | UDP | Inbound | Listens for incoming autopilot routing sentences (`$APB`) |
| Radar Display (Splitter) | 54321 | UDP | Inbound | Intercepts simulator's raw ASTERIX Cat 240 radar stream |
| In-Game Radar | 44444 | UDP | Inbound | Standard in-game radar display (forwarded by splitter) |
| Radar Display (Receiver) | 54322 | UDP | Inbound | Receives radar spokes from splitter |

### Coexistence details:
- **Simulator Connection Coexistence:** Port `53457` is the simulator's telemetry and control port. Both NMEA Bridge and Radar Display connect to this port. Multiple concurrent connections are fully supported.
- **UDP Coexistence:** The NMEA Bridge and Radar Display use completely distinct UDP ports for their telemetry and radar streams, ensuring no conflicts when running both tools at the same time.

---

## Shared Requirements

All sub-projects require:
- **NAUTIS Home** installed and a scenario loaded

Both the NMEA Bridge and the Radar Display include pre-built standalone `.exe` executables that bundle all dependencies.

---

## Repository Layout

```
NautisHomeMods/
├── README.md               ← This file
├── NMEA Bridge/
│   ├── README.md           ← Full NMEA Bridge documentation
│   └── dist/
│       └── nautis_nmea_bridge.exe
└── RADAR/
    ├── README.md           ← Full Radar Display documentation
    └── dist/
        └── radar_display.exe
```

---

## Adding Future Sub-Projects

Create a new subdirectory under `NautisHomeMods/` and add an entry to this file following the pattern above. Each sub-project should include:
1. A `README.md` covering setup, dependencies, usage, and architecture
2. A brief summary entry here with key features and entry points
