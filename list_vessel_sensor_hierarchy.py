"""
list_vessel_sensor_hierarchy.py -- print hierarchy of vessels and their sensors
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
    
    # Query for Relations and Names
    QUERY_TYPES = [
        "vstep.entities.Name",
        "vstep.entities.DisplayName",
        "vstep.entities.Relations",
        "vstep.equipment.MMSI",
        "vstep.sensors.CompassBaseOutput",
        "vstep.sensors.GPSOutput",
        "vstep.sensors.RudderIndicatorOutput",
        "vstep.sensors.PropulsionIndicatorOutput",
        "vstep.sensors.WindmeterOutput",
        "vstep.sensors.EchoSounderOutput"
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
        
        print(f"Loaded {len(entities)} entities.")
        
        # Print parent-child relationship for MMSI vessels
        vessels = {eid: comps for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps}
        
        for veid, vcomps in sorted(vessels.items()):
            disp = vcomps["vstep.entities.DisplayName"].name if "vstep.entities.DisplayName" in vcomps else "Unknown"
            mmsi = vcomps["vstep.equipment.MMSI"].identifier
            v_name = vcomps["vstep.entities.Name"].entity_name if "vstep.entities.Name" in vcomps else "Unknown"
            
            print(f"\nVessel '{disp}' (MMSI: {mmsi}, Entity: {veid}, Name: {v_name}):")
            
            # Look at children from Relations
            relations = vcomps.get("vstep.entities.Relations", None)
            if relations:
                print(f"  Direct children listed: {list(relations.children)}")
                # Find which of these children are sensors
                for child_id in relations.children:
                    if child_id in entities:
                        child_comps = entities[child_id]
                        c_name = child_comps["vstep.entities.Name"].entity_name if "vstep.entities.Name" in child_comps else f"Entity {child_id}"
                        c_types = [t for t in child_comps.keys() if t not in ["vstep.entities.Name", "vstep.entities.Relations"]]
                        if c_types:
                            print(f"    - Child {child_id} ({c_name}): {c_types}")
            else:
                print("  No Relations component!")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
