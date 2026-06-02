"""
grpc_probe.py  —  NAUTIS Home gRPC discovery tool
===================================================
Run this WHILE NautisHome_x64.exe is running.
It connects to 127.0.0.1:53457, uses server reflection to list
all available services and their methods, then saves the proto
schema to proto_extracted/.

Usage:  python grpc_probe.py [host] [port]
Default: 127.0.0.1:53457
"""
import sys
import grpc
from grpc_reflection.v1alpha import reflection_pb2, reflection_pb2_grpc
import json, os, time

HOST = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 53457
TARGET = f"{HOST}:{PORT}"

OUT_DIR = os.path.join(os.path.dirname(__file__), "proto_extracted")
os.makedirs(OUT_DIR, exist_ok=True)

def list_services(stub):
    req = reflection_pb2.ServerReflectionRequest(list_services="")
    resp = list(stub.ServerReflectionInfo(iter([req])))
    services = []
    for r in resp:
        if r.HasField("list_services_response"):
            for svc in r.list_services_response.service:
                services.append(svc.name)
    return services

def get_file_by_symbol(stub, symbol):
    req = reflection_pb2.ServerReflectionRequest(file_containing_symbol=symbol)
    responses = list(stub.ServerReflectionInfo(iter([req])))
    files = []
    for r in responses:
        if r.HasField("file_descriptor_response"):
            files.extend(r.file_descriptor_response.file_descriptor_proto)
    return files

def get_file_by_name(stub, filename):
    req = reflection_pb2.ServerReflectionRequest(file_by_filename=filename)
    responses = list(stub.ServerReflectionInfo(iter([req])))
    files = []
    for r in responses:
        if r.HasField("file_descriptor_response"):
            files.extend(r.file_descriptor_response.file_descriptor_proto)
    return files

def decode_file_descriptor(proto_bytes):
    """Decode a FileDescriptorProto and return a summary dict."""
    from google.protobuf import descriptor_pb2
    fd = descriptor_pb2.FileDescriptorProto()
    fd.ParseFromString(proto_bytes)
    return fd

def main():
    print(f"Connecting to NAUTIS Home gRPC server at {TARGET}...")
    channel = grpc.insecure_channel(TARGET)

    # Test connectivity
    try:
        grpc.channel_ready_future(channel).result(timeout=5)
        print("  ✓ Connected\n")
    except grpc.FutureTimeoutError:
        print(f"  ✗ Could not connect to {TARGET}")
        print("    Make sure NautisHome_x64.exe is running before running this script.")
        sys.exit(1)

    stub = reflection_pb2_grpc.ServerReflectionStub(channel)

    # List all services
    print("Discovering services via gRPC reflection...")
    services = list_services(stub)
    if not services:
        print("  No services found via reflection.")
        print("  The server may not have reflection enabled.")
        print("  Falling back to known service names from binary analysis...")
        services = [
            "vstep.sensors.Sensors",
            "vstep.simulation.DataExtraction",
            "vstep.simulation.Session",
            "vstep.simulation.Clock",
            "vstep.simulation.Exercise",
            "vstep.route_planner.Route",
            "vstep.simulation.external.ExternalControl",
        ]

    print(f"\nServices found ({len(services)}):")
    for svc in services:
        print(f"  - {svc}")

    # Pull file descriptors for each service
    print("\nFetching proto schemas...")
    seen_files = set()
    all_descriptors = {}

    for svc in services:
        try:
            files = get_file_by_symbol(stub, svc)
            for fb in files:
                fd = decode_file_descriptor(fb)
                if fd.name not in seen_files:
                    seen_files.add(fd.name)
                    all_descriptors[fd.name] = (fd, fb)
                    # Save raw binary descriptor
                    safe = fd.name.replace("/", "_")
                    out_path = os.path.join(OUT_DIR, safe)
                    with open(out_path, "wb") as f:
                        f.write(fb)
                    print(f"  Saved: {fd.name} -> {safe}")
        except grpc.RpcError as e:
            print(f"  Warning: Could not get schema for {svc}: {e.code()}")

    # Print a human-readable summary
    print("\n" + "="*60)
    print("PROTO SCHEMA SUMMARY")
    print("="*60)
    for fname, (fd, _) in sorted(all_descriptors.items()):
        print(f"\n[{fname}]  package={fd.package}")
        for svc in fd.service:
            print(f"  service {svc.name}:")
            for m in svc.method:
                stream_in  = " (stream)" if m.client_streaming else ""
                stream_out = " (stream)" if m.server_streaming else ""
                print(f"    rpc {m.name}{stream_in}({m.input_type}) returns{stream_out}({m.output_type})")
        for msg in fd.message_type:
            fields = ", ".join(f"{f.name}:{f.type}" for f in msg.field)
            print(f"  message {msg.name} {{ {fields} }}")

    channel.close()
    print(f"\nDone. {len(all_descriptors)} proto files written to {OUT_DIR}/")

if __name__ == "__main__":
    main()
