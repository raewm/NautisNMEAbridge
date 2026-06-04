"""
list_all_types.py -- write all proto types and fields to a file
"""
import os, sys
from google.protobuf import descriptor_pb2, descriptor_pool

pool = descriptor_pool.Default()
pb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proto_extracted")

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
    prog = False
    for pn, data in name_to_bytes.items():
        if pn in added:
            continue
        try:
            fdp = descriptor_pb2.FileDescriptorProto()
            fdp.MergeFromString(data)
            pool.Add(fdp)
            added.add(pn)
            prog = True
        except Exception:
            pass
    if not prog:
        break

all_types = []
for pn, data in name_to_bytes.items():
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.MergeFromString(data)
    for msg in fdp.message_type:
        full_name = (fdp.package + "." + msg.name) if fdp.package else msg.name
        fields = [f"{f.name} ({f.type})" for f in msg.field]
        all_types.append((full_name, fields))

all_types.sort()
with open("all_types_detailed.txt", "w") as f:
    cur_pkg = None
    for full_name, fields in all_types:
        pkg = ".".join(full_name.split(".")[:-1])
        if pkg != cur_pkg:
            f.write(f"\n=== {pkg} ===\n")
            cur_pkg = pkg
        short = full_name.split(".")[-1]
        f.write(f"  {short}: [{', '.join(fields)}]\n")
print("Done. Saved to all_types_detailed.txt")
