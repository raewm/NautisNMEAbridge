"""
find_sensor_vessel_map.py -- find correlation between vessels, gyros, and GPS sensors
"""
import os, sys, time, math
import grpc
from google.protobuf import any_pb2, duration_pb2, timestamp_pb2  # noqa: F401
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf import message_factory

PB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto_extracted")

def load_descriptors(pb_dir: str):
    pool = descriptor_pool.Default()
    name_to_bytes = {}
    for fname in os.listdir(pb_dir):
        if not fname.endswith(".proto.pb"):
            continue
        with open(os.path.join(pb_dir, fname), "rb") as f:
            data = f.read()
        try:
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.MergeFromString(data)
            name_to_bytes[fdp.name] = data
        except Exception:
            pass

    added = set()
    for _ in range(len(name_to_bytes) + 2):
        progress = False
        for proto_name, data in name_to_bytes.items():
            if proto_name in added:
                continue
            try:
                fdp = descriptor_pb2.FileDescriptorProto()
                fdp.MergeFromString(data)
                pool.Add(fdp)
                added.add(proto_name)
                progress = True
            except Exception:
                pass
        if not progress:
            break

def main():
    load_descriptors(PB_DIR)
    pool = descriptor_pool.Default()
    
    QUERY_TYPES = [
        "vstep.entities.Name",
        "vstep.entities.DisplayName",
        "vstep.equipment.MMSI",
        "vstep.spatial.PositionGeographic",
        "vstep.spatial.LinearMotion",
        "vstep.spatial.OrientationEuler",
        "vstep.sensors.CompassBaseOutput",
        "vstep.sensors.GPSOutput"
    ]
    
    classes = {}
    needed = [
        "vstep.entities.GetComponentsRequest",
        "vstep.entities.GetComponentsRequest.Query",
        "vstep.entities.GetComponentsResponse",
        "vstep.entities.EntitySelection",
        "vstep.entities.AllRootEntities",
    ] + QUERY_TYPES
    
    for t in needed:
        try:
            desc = pool.FindMessageTypeByName(t)
            classes[t] = message_factory.GetMessageClass(desc)
        except Exception as e:
            print(f"Error: {e}")
            return

    req_cls = classes["vstep.entities.GetComponentsRequest"]
    query_cls = classes["vstep.entities.GetComponentsRequest.Query"]
    sel_cls = classes["vstep.entities.EntitySelection"]
    root_cls = classes["vstep.entities.AllRootEntities"]
    resp_cls = classes["vstep.entities.GetComponentsResponse"]

    sel = sel_cls()
    sel.all_root_entities.CopyFrom(root_cls())
    sel.recursion = 1  # RECURSION_INCLUSIVE
    
    query = query_cls()
    query.component_types.extend(QUERY_TYPES)
    query.entities.append(sel)
    
    req = req_cls()
    req.queries.append(query)

    channel = grpc.insecure_channel("127.0.0.1:53457")
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    stub = channel.unary_unary(
        "/vstep.entities.Registry/GetComponents",
        request_serializer=lambda m: m.SerializeToString(),
        response_deserializer=resp_cls.FromString,
    )

    try:
        resp = stub(req)
        
        entities = {}
        for comp in resp.data:
            eid = comp.entity.id
            url = comp.data.type_url
            tn = url.split("/")[-1] if "/" in url else url
            if tn in classes:
                msg = classes[tn]()
                msg.MergeFromString(comp.data.value)
                if eid not in entities:
                    entities[eid] = {}
                entities[eid][tn] = msg
        
        print(f"Total entities with components: {len(entities)}")
        
        # Let's print out all gyros and DGPSs and find which vessel they are physically close to!
        gyros = []
        gpss = []
        vessels = []
        
        for eid, comps in entities.items():
            if "vstep.sensors.CompassBaseOutput" in comps:
                gyros.append((eid, comps))
            if "vstep.sensors.GPSOutput" in comps:
                gpss.append((eid, comps))
            if "vstep.equipment.MMSI" in comps and "vstep.spatial.PositionGeographic" in comps:
                vessels.append((eid, comps))
                
        print(f"Found {len(vessels)} vessels, {len(gyros)} gyros, {len(gpss)} GPSs.")
        
        # Map each gyro to the closest vessel
        for geid, gcomps in gyros:
            g_name = gcomps["vstep.entities.Name"].entity_name if "vstep.entities.Name" in gcomps else f"Gyro {geid}"
            g_hdg = math.degrees(gcomps["vstep.sensors.CompassBaseOutput"].heading) % 360.0
            
            # Find closest vessel by coordinate proximity if gyro has PositionGeographic, or by Entity ID proximity
            # Wait, does the gyro have PositionGeographic? Let's check.
            closest_vessel = None
            min_dist = float('inf')
            
            # Let's just find the vessel with the closest Entity ID, or matching coordinates
            # Since sensors are sub-entities of the vessel, they usually have Entity IDs slightly larger than the vessel's Entity ID.
            # E.g. Vessel is 1156, Gyro is 1327.
            # Let's check proximity of coordinates if both have PositionGeographic.
            g_lat, g_lon = 0.0, 0.0
            if "vstep.spatial.PositionGeographic" in gcomps:
                g_lat = gcomps["vstep.spatial.PositionGeographic"].position.coordinates.latitude
                g_lon = gcomps["vstep.spatial.PositionGeographic"].position.coordinates.longitude
            
            for veid, vcomps in vessels:
                v_lat = vcomps["vstep.spatial.PositionGeographic"].position.coordinates.latitude
                v_lon = vcomps["vstep.spatial.PositionGeographic"].position.coordinates.longitude
                dist = math.sqrt((g_lat - v_lat)**2 + (g_lon - v_lon)**2) if (g_lat != 0.0) else abs(geid - veid)
                if dist < min_dist:
                    min_dist = dist
                    closest_vessel = (veid, vcomps)
            
            if closest_vessel:
                v_disp = closest_vessel[1]["vstep.entities.DisplayName"].name
                v_euler = closest_vessel[1].get("vstep.spatial.OrientationEuler", None)
                v_yaw = math.degrees(v_euler.angles.z) % 360.0 if v_euler else 0.0
                v_name = closest_vessel[1]["vstep.entities.Name"].entity_name
                print(f"Gyro '{g_name}' (ID: {geid}): Hdg = {g_hdg:.1f}°")
                print(f"  -> Closest Vessel '{v_disp}' (ID: {closest_vessel[0]}): OrientationEuler.angles.z = {v_yaw:.1f}°")
                
                # Check mapping formula!
                # E.g. is there a simple formula: heading = (constant - yaw) % 360?
                # Let's calculate: (g_hdg + v_yaw) % 360, (g_hdg - v_yaw) % 360, (v_yaw - g_hdg) % 360
                sum_ang = (g_hdg + v_yaw) % 360.0
                diff_ang1 = (g_hdg - v_yaw) % 360.0
                diff_ang2 = (v_yaw - g_hdg) % 360.0
                print(f"     Sum: {sum_ang:.1f}°, Diff1 (Gyro-Yaw): {diff_ang1:.1f}°, Diff2 (Yaw-Gyro): {diff_ang2:.1f}°")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
