"""
list_rpc_methods.py -- list all services and RPC methods in the proto files
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

for pn, data in sorted(name_to_bytes.items()):
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.MergeFromString(data)
    if fdp.service:
        print(f"\nFile: {pn} (package {fdp.package})")
        for svc in fdp.service:
            print(f"  service {svc.name}:")
            for m in svc.method:
                stream_in = " (stream)" if m.client_streaming else ""
                stream_out = " (stream)" if m.server_streaming else ""
                print(f"    rpc {m.name}{stream_in}({m.input_type}) returns{stream_out}({m.output_type})")
