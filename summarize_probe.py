"""
summarize_probe.py -- analyze the live probe results to understand the entities and components
"""
import re
from collections import Counter

entities = {}
current_entity = None
current_comp = None

with open("live_probe_results.txt", "r", encoding="utf-8") as f:
    for line in f:
        line = line.rstrip()
        entity_match = re.match(r"^=== Entity ID: (\d+) ===", line)
        if entity_match:
            current_entity = int(entity_match.group(1))
            entities[current_entity] = {}
            current_comp = None
            continue
        
        if line.startswith("  ") and line.endswith(":"):
            current_comp = line.strip()[:-1]
            entities[current_entity][current_comp] = {}
            continue
        
        if line.startswith("    ") and current_entity and current_comp:
            parts = line.strip().split(": ", 1)
            if len(parts) == 2:
                name, val = parts
                entities[current_entity][current_comp][name] = val

print(f"Total entities parsed: {len(entities)}")

# Group entities by the set of components they have
signatures = {}
for eid, comps in entities.items():
    sig = tuple(sorted(comps.keys()))
    if sig not in signatures:
        signatures[sig] = []
    signatures[sig].append(eid)

print("\nEntity Component Combinations:")
for sig, eids in sorted(signatures.items(), key=lambda x: len(x[1]), reverse=True):
    print(f"\n{len(eids)} entities have components: {list(sig)}")
    # Print some examples
    example_eids = eids[:3]
    for ex_eid in example_eids:
        details = []
        c = entities[ex_eid]
        if "vstep.entities.Name" in c:
            details.append(f"Name: {c['vstep.entities.Name'].get('entity_name')}")
        if "vstep.entities.DisplayName" in c:
            details.append(f"DisplayName: {c['vstep.entities.DisplayName'].get('name')}")
        if "vstep.prefabs.PrefabInfo" in c:
            details.append(f"PrefabCode: {c['vstep.prefabs.PrefabInfo'].get('code')}")
        if "vstep.equipment.MMSI" in c:
            details.append(f"MMSI: {c['vstep.equipment.MMSI'].get('identifier')}")
        detail_str = ", ".join(details)
        print(f"  - Entity {ex_eid}: {detail_str}")

# Search for potential ships/vessels
print("\n--- Telemetry Components Presence ---")
has_rudder = []
has_propulsion = []
has_wind = []
has_gps = []
has_ins = []
has_geographic = []
has_mmsi = []

for eid, comps in entities.items():
    if "vstep.sensors.RudderIndicatorOutput" in comps:
        has_rudder.append(eid)
    if "vstep.sensors.PropulsionIndicatorOutput" in comps:
        has_propulsion.append(eid)
    if "vstep.sensors.WindmeterOutput" in comps:
        has_wind.append(eid)
    if "vstep.sensors.GPSOutput" in comps:
        has_gps.append(eid)
    if "vstep.sensors.INSOutput" in comps:
        has_ins.append(eid)
    if "vstep.spatial.PositionGeographic" in comps:
        has_geographic.append(eid)
    if "vstep.equipment.MMSI" in comps:
        has_mmsi.append(eid)

print(f"Entities with RudderIndicatorOutput: {len(has_rudder)}")
print(f"Entities with PropulsionIndicatorOutput: {len(has_propulsion)}")
print(f"Entities with WindmeterOutput: {len(has_wind)}")
print(f"Entities with GPSOutput: {len(has_gps)}")
print(f"Entities with INSOutput: {len(has_ins)}")
print(f"Entities with PositionGeographic: {len(has_geographic)}")
print(f"Entities with MMSI: {len(has_mmsi)}")

print("\n--- Example Entity with MMSI details ---")
if has_mmsi:
    eid = has_mmsi[0]
    print(f"Entity ID: {eid}")
    for cname, fields in entities[eid].items():
        print(f"  {cname}: {fields}")
else:
    print("No entity has MMSI!")

print("\n--- Example Entity with GPSOutput details ---")
if has_gps:
    eid = has_gps[0]
    print(f"Entity ID: {eid}")
    for cname, fields in entities[eid].items():
        print(f"  {cname}: {fields}")
else:
    print("No entity has GPSOutput!")

print("\n--- Check other entities that have PositionGeographic but NOT GPSOutput (Traffic Candidates) ---")
traffic_candidates = [eid for eid in has_geographic if eid not in has_gps]
print(f"Found {len(traffic_candidates)} entities with PositionGeographic but NO GPSOutput.")
for ex_eid in traffic_candidates[:5]:
    print(f"Entity ID: {ex_eid}")
    for cname, fields in entities[ex_eid].items():
        print(f"  {cname}: {fields}")
