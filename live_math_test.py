"""
live_math_test.py -- direct gRPC telemetry inspection for MMSI vessels
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
        "vstep.prefabs.PrefabInfo",
        "vstep.equipment.MMSI",
        "vstep.spatial.PositionGeographic",
        "vstep.spatial.LinearMotion",
        "vstep.spatial.AngularMotion",
        "vstep.spatial.OrientationEuler",
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
            print(f"Error resolving {t}: {e}")
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
        
        # Group parsed objects by entity_id
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
        
        mmsi_vessels = []
        for eid, comps in entities.items():
            if "vstep.equipment.MMSI" in comps:
                mmsi_vessels.append((eid, comps))
                
        print(f"Found {len(mmsi_vessels)} MMSI vessels:")
        for eid, comps in sorted(mmsi_vessels, key=lambda x: x[0]):
            name = comps["vstep.entities.Name"].entity_name if "vstep.entities.Name" in comps else "Unknown"
            disp = comps["vstep.entities.DisplayName"].name if "vstep.entities.DisplayName" in comps else "Unknown"
            mmsi = comps["vstep.equipment.MMSI"].identifier
            
            # Position
            lat, lon = 0.0, 0.0
            if "vstep.spatial.PositionGeographic" in comps:
                pos_geo = comps["vstep.spatial.PositionGeographic"]
                lat = pos_geo.position.coordinates.latitude
                lon = pos_geo.position.coordinates.longitude
                
            # Speed and Course
            vx, vy = 0.0, 0.0
            sog_kn = 0.0
            cog_deg = 0.0
            if "vstep.spatial.LinearMotion" in comps:
                lm = comps["vstep.spatial.LinearMotion"]
                vx = lm.velocity.x
                vy = lm.velocity.y
                sog_kn = math.sqrt(vx**2 + vy**2) * 1.9438445
                if math.sqrt(vx**2 + vy**2) > 0.01:
                    # In NAUTIS, let's verify if vx is East and vy is North or vice-versa
                    # Let's print out the raw velocity vector
                    cog_deg = math.degrees(math.atan2(vx, vy)) % 360.0
            
            # Heading
            heading_deg = 0.0
            yaw_deg = 0.0
            if "vstep.spatial.OrientationEuler" in comps:
                oe = comps["vstep.spatial.OrientationEuler"]
                # angles: x=roll, y=pitch, z=yaw (heading)
                yaw_rad = oe.angles.z
                yaw_deg = math.degrees(yaw_rad) % 360.0
                # If yaw is z in radians, let's check its relation to true heading
                heading_deg = yaw_deg  # Let's print both
                
            # ROT
            rot_dpm = 0.0
            if "vstep.spatial.AngularMotion" in comps:
                am = comps["vstep.spatial.AngularMotion"]
                # velocity.z is yaw rate in rad/s -> convert to deg/min
                rot_dpm = math.degrees(am.velocity.z) * 60.0

            print(f"\n  Vessel '{disp}' (MMSI: {mmsi}, Entity: {eid}, Name: {name}):")
            print(f"    Pos: {lat:.6f}°N, {lon:.6f}°E")
            print(f"    Velocity: vx={vx:.3f}, vy={vy:.3f} m/s -> SOG={sog_kn:.2f} kn, COG={cog_deg:.1f}°")
            print(f"    OrientationEuler.angles.z = {yaw_deg:.2f}° (Heading = {heading_deg:.1f}°)")
            print(f"    ROT = {rot_dpm:.2f}°/min")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
