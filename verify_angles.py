"""
verify_angles.py -- verify heading, yaw, cog math for the running simulator
"""
import re, math

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

# Let's find gyro sensors and check their headings and parent entities
gyro_entities = [eid for eid, comps in entities.items() if "vstep.sensors.CompassBaseOutput" in comps]
gps_entities = [eid for eid, comps in entities.items() if "vstep.sensors.GPSOutput" in comps]

print("Gyro Sensors:")
for eid in gyro_entities:
    name = entities[eid]["vstep.entities.Name"].get("entity_name")
    heading_rad = float(entities[eid]["vstep.sensors.CompassBaseOutput"].get("heading", 0))
    print(f"  Entity {eid} ({name}): heading = {math.degrees(heading_rad):.2f}° ({heading_rad:.4f} rad)")

print("\nGPS Sensors:")
for eid in gps_entities:
    name = entities[eid]["vstep.entities.Name"].get("entity_name")
    gps = entities[eid]["vstep.sensors.GPSOutput"]
    lat = float(gps.get("latitude", 0))
    lon = float(gps.get("longitude", 0))
    sog = float(gps.get("sog", 0))
    cog_rad = float(gps.get("cog", 0))
    print(f"  Entity {eid} ({name}): COG = {math.degrees(cog_rad):.2f}° ({cog_rad:.4f} rad), SOG = {sog*1.94384:.2f} kn, Pos = ({lat:.5f}, {lon:.5f})")

# Let's find MMSI vessels and their orientation/motion
mmsi_entities = [eid for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps]
print("\nMMSI Vessels:")
for eid in mmsi_entities:
    disp_name = entities[eid]["vstep.entities.DisplayName"].get("name")
    mmsi = entities[eid]["vstep.equipment.MMSI"].get("identifier")
    
    euler = entities[eid].get("vstep.spatial.OrientationEuler", {})
    angles_str = euler.get("angles", "")
    # Parse x, y, z from angles_str
    # format: x: val \n y: val \n z: val
    yaw = 0.0
    z_match = re.search(r"z:\s*([-\d.e]+)", angles_str)
    if z_match:
        yaw = float(z_match.group(1))
        
    lm = entities[eid].get("vstep.spatial.LinearMotion", {})
    vel_str = lm.get("velocity", "")
    vx = 0.0
    vy = 0.0
    vx_match = re.search(r"x:\s*([-\d.e]+)", vel_str)
    vy_match = re.search(r"y:\s*([-\d.e]+)", vel_str)
    if vx_match: vx = float(vx_match.group(1))
    if vy_match: vy = float(vy_match.group(1))
    
    sog_calc = math.sqrt(vx**2 + vy**2)
    cog_calc_rad = math.atan2(vx, vy) if sog_calc > 0.01 else 0.0
    cog_calc_deg = math.degrees(cog_calc_rad) % 360.0
    
    yaw_deg = math.degrees(yaw) % 360.0
    # Let's print heading by converting yaw
    # If yaw is z in radians, what direction does it point?
    # Usually, heading_deg = 90 - yaw_deg? Let's check.
    heading_derived = (90.0 - yaw_deg) % 360.0
    
    print(f"  Vessel '{disp_name}' (MMSI: {mmsi}):")
    print(f"    OrientationEuler.angles.z = {yaw_deg:.2f}° ({yaw:.4f} rad) -> Derived Heading = {heading_derived:.2f}°")
    print(f"    Velocity: vx={vx:.3f}, vy={vy:.3f} m/s -> Calculated SOG={sog_calc*1.94384:.2f} kn, COG={cog_calc_deg:.2f}°")
