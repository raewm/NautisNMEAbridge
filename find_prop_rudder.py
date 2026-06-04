"""
find_prop_rudder.py -- find where rudder and propulsion indicators are located in the hierarchy
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

# Find all entities with RudderIndicatorOutput or PropulsionIndicatorOutput
rudder_eids = [eid for eid, comps in entities.items() if "vstep.sensors.RudderIndicatorOutput" in comps]
prop_eids = [eid for eid, comps in entities.items() if "vstep.sensors.PropulsionIndicatorOutput" in comps]

print("Rudder Indicator Entities:")
for eid in rudder_eids:
    name = entities[eid]["vstep.entities.Name"].get("entity_name") if "vstep.entities.Name" in entities[eid] else "Unknown"
    val = entities[eid]["vstep.sensors.RudderIndicatorOutput"]
    print(f"  Entity {eid} ({name}): {val}")
    
print("\nPropulsion Indicator Entities:")
for eid in prop_eids:
    name = entities[eid]["vstep.entities.Name"].get("entity_name") if "vstep.entities.Name" in entities[eid] else "Unknown"
    val = entities[eid]["vstep.sensors.PropulsionIndicatorOutput"]
    print(f"  Entity {eid} ({name}): {val}")

# Trace the parents of these entities
# We can scan all entities with Relations and find which one has this entity as a child!
def find_parent(child_id):
    for eid, comps in entities.items():
        rel = comps.get("vstep.entities.Relations", {})
        # Note: in live_probe_results.txt, we didn't save children list explicitly, but wait!
        # Did we write it to live_probe_results.txt? No, we queried for QUERY_TYPES, which didn't include Relations!
        # Ah, live_probe.py QUERY_TYPES didn't contain vstep.entities.Relations!
        # That's why in live_probe_results.txt there are no Relations!
        # But wait, in list_vessel_sensor_hierarchy.py, we DID query Relations!
        pass

# Let's print out all entities with Rudder or Propulsion indicator and see if their names tell us anything.
# Yes, e.g. "Rudder Port.Indicator", "Jet Port.JetPropulsion1PropulsionIndicator"
