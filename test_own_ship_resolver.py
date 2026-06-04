"""
test_own_ship_resolver.py -- verify own-ship resolution logic using camera and GPS proximity
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
        "vstep.entities.Relations",
        "vstep.equipment.MMSI",
        "vstep.spatial.PositionGeographic",
        "vstep.viewports.AssignedCamera",
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

        # ----------------------------------------------------
        # OWN-SHIP RESOLUTION METHOD 1: CAMERA BASED
        # ----------------------------------------------------
        own_ship_eid_camera = None
        
        # 1. Find assigned camera
        camera_eid = None
        for eid, comps in entities.items():
            if "vstep.viewports.AssignedCamera" in comps:
                camera_eid = comps["vstep.viewports.AssignedCamera"].entity
                print(f"[Method 1] Viewport found camera: {camera_eid}")
                break
                
        # 2. Climb up parent tree from camera to find root vessel
        if camera_eid:
            curr = camera_eid
            path = []
            while True:
                # Find parent
                parent = None
                for eid, comps in entities.items():
                    rel = comps.get("vstep.entities.Relations")
                    if rel and curr in rel.children:
                        parent = eid
                        break
                if parent:
                    path.append(parent)
                    curr = parent
                else:
                    break
            print(f"[Method 1] Camera path to root: {camera_eid} -> {' -> '.join(map(str, path))}")
            # Find the root node that is a vessel (has MMSI)
            for peid in path:
                if peid in entities and "vstep.equipment.MMSI" in entities[peid]:
                    own_ship_eid_camera = peid
                    break
                    
        if own_ship_eid_camera:
            disp = entities[own_ship_eid_camera]["vstep.entities.DisplayName"].name
            mmsi = entities[own_ship_eid_camera]["vstep.equipment.MMSI"].identifier
            print(f"[Method 1] CAMERA-RESOLVED OWN-SHIP: Entity {own_ship_eid_camera} ('{disp}', MMSI: {mmsi})")
        else:
            print("[Method 1] Camera-based resolution failed.")

        # ----------------------------------------------------
        # OWN-SHIP RESOLUTION METHOD 2: PROXIMITY BASED
        # ----------------------------------------------------
        own_ship_eid_prox = None
        
        # Resolve best own ship coordinates first (using GPS fallback logic similar to TelemetryResolver)
        gps_msgs = [m for (eid, comps) in entities.items() for tn, m in comps.items() if tn == "vstep.sensors.GPSOutput"]
        geom_msgs = {eid: comps["vstep.spatial.PositionGeographic"] for eid, comps in entities.items() if "vstep.spatial.PositionGeographic" in comps}
        
        lat, lon = 0.0, 0.0
        has_pos = False
        if gps_msgs:
            lat = gps_msgs[0].latitude
            lon = gps_msgs[0].longitude
            has_pos = True
            print(f"[Method 2] GPS Position resolved: ({lat:.6f}, {lon:.6f})")
        else:
            # Fallback to PositionGeographic
            if geom_msgs:
                # take first positiongeographic
                first_geom = next(iter(geom_msgs.values()))
                lat = first_geom.position.coordinates.latitude
                lon = first_geom.position.coordinates.longitude
                has_pos = True
                print(f"[Method 2] Geom Position resolved: ({lat:.6f}, {lon:.6f})")
                
        if has_pos:
            # Find closest vessel having MMSI
            vessels = {eid: comps for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps and "vstep.spatial.PositionGeographic" in comps}
            min_dist = float('inf')
            for veid, vcomps in vessels.items():
                vpos = vcomps["vstep.spatial.PositionGeographic"].position.coordinates
                v_lat = vpos.latitude
                v_lon = vpos.longitude
                dist = math.sqrt((lat - v_lat)**2 + (lon - v_lon)**2)
                if dist < min_dist:
                    min_dist = dist
                    own_ship_eid_prox = veid
                    
        if own_ship_eid_prox:
            disp = entities[own_ship_eid_prox]["vstep.entities.DisplayName"].name
            mmsi = entities[own_ship_eid_prox]["vstep.equipment.MMSI"].identifier
            print(f"[Method 2] PROXIMITY-RESOLVED OWN-SHIP: Entity {own_ship_eid_prox} ('{disp}', MMSI: {mmsi})")
        else:
            print("[Method 2] Proximity-based resolution failed.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
