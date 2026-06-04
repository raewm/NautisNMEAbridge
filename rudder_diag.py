"""
rudder_diag.py  -- Dump own-ship child entity component types to diagnose
                   why $IIRSA may not be emitting.
"""
import os, sys, time, math
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grpc
from google.protobuf import any_pb2, duration_pb2, timestamp_pb2  # noqa
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
PB_DIR = os.path.join(_BASE, "proto_extracted")

# ---- load descriptors (same as bridge) ----
def load_descriptors(pb_dir):
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
    print(f"Loaded {len(added)} descriptors")
    return len(added)

load_descriptors(PB_DIR)

ALL_TYPES = [
    "vstep.sensors.GPSOutput",
    "vstep.sensors.CompassBaseOutput",
    "vstep.sensors.INSOutput",
    "vstep.sensors.DopplerLogOutput",
    "vstep.sensors.DateTimeOutput",
    "vstep.spatial.PositionGeographic",
    "vstep.spatial.LinearMotion",
    "vstep.spatial.AngularMotion",
    "vstep.spatial.OrientationEuler",
    "vstep.entities.Name",
    "vstep.entities.DisplayName",
    "vstep.entities.Relations",
    "vstep.equipment.MMSI",
    "vstep.sensors.RudderIndicatorOutput",
    "vstep.sensors.PropulsionIndicatorOutput",
    "vstep.sensors.WindmeterOutput",
    "vstep.sensors.EchoSounderOutput",
    "vstep.viewports.AssignedCamera",
]

def build_class(type_name):
    try:
        desc = descriptor_pool.Default().FindMessageTypeByName(type_name)
        return message_factory.GetMessageClass(desc)
    except Exception:
        return None

classes = {t: build_class(t) for t in ALL_TYPES}
classes = {k: v for k, v in classes.items() if v}
print(f"Resolved {len(classes)} message classes")

# ---- gRPC stub ----
channel = grpc.insecure_channel("127.0.0.1:53457")
GetComponentsReq  = build_class("vstep.registry.GetComponentsRequest")
GetComponentsResp = build_class("vstep.registry.GetComponentsResponse")

get_comp_method = channel.unary_unary(
    "/vstep.registry.RegistryService/GetComponents",
    request_serializer=GetComponentsReq.SerializeToString,
    response_deserializer=GetComponentsResp.FromString,
)


req = GetComponentsReq()
req.entity_selection.all_root_entities.CopyFrom(
    build_class("vstep.registry.AllRootEntitiesSelection")()
)
req.entity_selection.all_root_entities.recursion = 1  # RECURSION_INCLUSIVE
for t in ALL_TYPES:
    req.component_type_filter.append(f"type.googleapis.com/{t}")

print("Querying NAUTIS Home...")
resp = get_comp_method(req, timeout=10)

# Parse response into per-entity dict
entities = {}  # eid -> {type_name -> message}
for rec in resp.components:
    eid = rec.entity.id
    type_url = rec.data.type_url
    type_name = type_url.split("/")[-1]
    if type_name not in classes:
        continue
    msg = classes[type_name]()
    msg.ParseFromString(rec.data.value)
    if eid not in entities:
        entities[eid] = {}
    entities[eid][type_name] = msg

print(f"\nTotal entities in response: {len(entities)}")

# Find own-ship (first MMSI entity)
own_eid = None
for eid, comps in entities.items():
    if "vstep.equipment.MMSI" in comps:
        mmsi = comps["vstep.equipment.MMSI"].identifier
        dn = comps.get("vstep.entities.DisplayName")
        name = dn.name if dn else "(no name)"
        print(f"\n  MMSI vessel: eid={eid}  MMSI={mmsi}  name={name!r}")
        if own_eid is None:
            own_eid = eid  # use first one as own-ship for diag

if own_eid is None:
    print("ERROR: no MMSI vessel found")
    sys.exit(1)

print(f"\n=== Own-ship entity {own_eid} components ===")
for t in sorted(entities[own_eid].keys()):
    print(f"  {t}")

own_rel = entities[own_eid].get("vstep.entities.Relations")
children_ids = list(own_rel.children) if own_rel else []
print(f"\n=== Children of own-ship ({len(children_ids)} children) ===")
for cid in children_ids:
    if cid in entities:
        comps = entities[cid]
        cn = comps.get("vstep.entities.Name")
        cd = comps.get("vstep.entities.DisplayName")
        cname = (cn.entity_name if cn else "") + " " + (cd.name if cd else "")
        print(f"\n  Child eid={cid}  name={cname.strip()!r}")
        for t in sorted(comps.keys()):
            val = ""
            if t == "vstep.sensors.RudderIndicatorOutput":
                val = f"  angle={math.degrees(comps[t].angle):.2f} deg"
            elif t == "vstep.sensors.PropulsionIndicatorOutput":
                val = f"  rpm={comps[t].rpm:.1f}  angle={math.degrees(comps[t].angle):.2f} deg"
            print(f"    {t}{val}")
    else:
        print(f"  Child eid={cid}  (not in response)")
