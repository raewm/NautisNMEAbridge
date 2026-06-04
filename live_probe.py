"""
live_probe.py -- query the running NAUTIS simulator for all entity components
"""
import os, sys, time
import grpc
from google.protobuf import any_pb2, duration_pb2, timestamp_pb2  # noqa: F401
from google.protobuf import descriptor_pb2, descriptor_pool
from google.protobuf import message_factory

PB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto_extracted")

# Load all proto descriptors
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
            except Exception as e:
                pass
        if not progress:
            break
    print(f"Loaded {len(added)}/{len(name_to_bytes)} proto descriptors.")

# All component types to query for the live probe
QUERY_TYPES = [
    "vstep.entities.Name",
    "vstep.entities.DisplayName",
    "vstep.prefabs.PrefabInfo",
    "vstep.equipment.MMSI",
    "vstep.spatial.PositionGeographic",
    "vstep.spatial.LinearMotion",
    "vstep.spatial.AngularMotion",
    "vstep.spatial.OrientationEuler",
    "vstep.sensors.GPSOutput",
    "vstep.sensors.CompassBaseOutput",
    "vstep.sensors.INSOutput",
    "vstep.sensors.DopplerLogOutput",
    "vstep.sensors.RudderIndicatorOutput",
    "vstep.sensors.PropulsionIndicatorOutput",
    "vstep.sensors.WindmeterOutput",
    "vstep.sensors.EchoSounderOutput"
]

def main():
    load_descriptors(PB_DIR)
    pool = descriptor_pool.Default()
    
    # Build classes
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
            print(f"Warning: could not resolve {t}: {e}")

    # Set up query
    req_cls = classes["vstep.entities.GetComponentsRequest"]
    query_cls = classes["vstep.entities.GetComponentsRequest.Query"]
    sel_cls = classes["vstep.entities.EntitySelection"]
    root_cls = classes["vstep.entities.AllRootEntities"]
    resp_cls = classes["vstep.entities.GetComponentsResponse"]

    sel = sel_cls()
    sel.all_root_entities.CopyFrom(root_cls())
    # Query recursively to find child components of all root entities
    sel.recursion = 1  # Let's try recursion 1 (RECURSION_INCLUSIVE) first, if not enough we'll do 2.
    
    query = query_cls()
    query.component_types.extend(QUERY_TYPES)
    query.entities.append(sel)
    
    req = req_cls()
    req.queries.append(query)

    print("Connecting to simulator gRPC at 127.0.0.1:53457...")
    channel = grpc.insecure_channel("127.0.0.1:53457")
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        print("Connected!")
    except Exception as e:
        print(f"Failed to connect: {e}")
        return

    stub = channel.unary_unary(
        "/vstep.entities.Registry/GetComponents",
        request_serializer=lambda m: m.SerializeToString(),
        response_deserializer=resp_cls.FromString,
    )

    try:
        print("Sending GetComponents query...")
        resp = stub(req)
        print(f"Received response with {len(resp.data)} components.")
        
        # Group components by entity
        entities = {}
        for comp in resp.data:
            eid = comp.entity.id
            url = comp.data.type_url
            tn = url.split("/")[-1] if "/" in url else url
            if tn in classes:
                msg = classes[tn]()
                msg.MergeFromString(comp.data.value)
                if eid not in entities:
                    entities[eid] = []
                entities[eid].append((tn, msg))
        
        # Print results to file
        with open("live_probe_results.txt", "w", encoding="utf-8") as f:
            f.write(f"PROBE AT {time.asctime()}\n")
            f.write(f"Total entities found: {len(entities)}\n\n")
            
            for eid, comps in sorted(entities.items()):
                f.write(f"=== Entity ID: {eid} ===\n")
                # Sort components by type name
                for tn, msg in sorted(comps, key=lambda x: x[0]):
                    f.write(f"  {tn}:\n")
                    # Format message fields
                    for field, value in msg.ListFields():
                        f.write(f"    {field.name}: {value}\n")
                f.write("\n")
        print("Done. Saved to live_probe_results.txt")

    except Exception as e:
        print(f"Error executing query: {e}")

if __name__ == "__main__":
    main()
