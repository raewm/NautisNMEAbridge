"""
trace_ancestry.py -- trace ancestors of rudders and propulsion indicators to root vessels
"""
import os, sys, math
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
        "vstep.sensors.RudderIndicatorOutput",
        "vstep.sensors.PropulsionIndicatorOutput"
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
        
        rudders = [eid for eid in entities if "vstep.sensors.RudderIndicatorOutput" in entities[eid]]
        props = [eid for eid in entities if "vstep.sensors.PropulsionIndicatorOutput" in entities[eid]]
        
        print(f"Rudders ({len(rudders)}):")
        for reid in rudders:
            # Trace ancestors
            curr = reid
            path = []
            while True:
                # Find parent manually since Relations.ancestors might not be populated or contains all
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
            
            # Print path with names
            path_str_list = []
            for peid in [reid] + path:
                disp_name = entities[peid].get("vstep.entities.DisplayName", None)
                ent_name = entities[peid].get("vstep.entities.Name", None)
                name = disp_name.name if disp_name else (ent_name.entity_name if ent_name else "Unknown")
                mmsi_str = f" (MMSI: {entities[peid]['vstep.equipment.MMSI'].identifier})" if "vstep.equipment.MMSI" in entities[peid] else ""
                path_str_list.append(f"{peid} '{name}'{mmsi_str}")
            print(" -> ".join(path_str_list))

        print(f"\nPropulsions ({len(props)}):")
        for peid in props:
            # Trace ancestors
            curr = peid
            path = []
            while True:
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
            
            # Print path with names
            path_str_list = []
            for p_eid in [peid] + path:
                disp_name = entities[p_eid].get("vstep.entities.DisplayName", None)
                ent_name = entities[p_eid].get("vstep.entities.Name", None)
                name = disp_name.name if disp_name else (ent_name.entity_name if ent_name else "Unknown")
                mmsi_str = f" (MMSI: {entities[p_eid]['vstep.equipment.MMSI'].identifier})" if "vstep.equipment.MMSI" in entities[p_eid] else ""
                path_str_list.append(f"{p_eid} '{name}'{mmsi_str}")
            print(" -> ".join(path_str_list))

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    main()
