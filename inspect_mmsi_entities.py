"""
inspect_mmsi_entities.py -- detailed print of the 17 entities with MMSI
"""
import re

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

mmsi_entities = [eid for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps]
print(f"Found {len(mmsi_entities)} MMSI entities:")
for eid in mmsi_entities:
    print(f"\n================= Entity ID: {eid} =================")
    for cname, fields in sorted(entities[eid].items()):
        print(f"  {cname}:")
        for fname, val in sorted(fields.items()):
            print(f"    {fname}: {val}")
