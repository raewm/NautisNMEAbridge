"""
radar_splitter.py -- NAUTIS Home ASTERIX Radar UDP Splitter
============================================================
Listens for ASTERIX Cat 240 radar packets from NAUTIS Home and forwards
them to multiple destinations (in-game display + networked standalone display).

Normal operation (no network display):
  - Library.xml sends to 127.0.0.1:44444 (in-game radar, unchanged)
  - No splitter needed

Network display mode:
  1. In Library.xml for 'device 03', change <_remotePort> to 54321
  2. Run this splitter (it forwards to 44444 AND your remote display)
  3. Run radar_display.py on your remote computer

Usage:
    python radar_splitter.py [options]
    python radar_splitter.py --listen-port 54321 --targets 127.0.0.1:44444 192.168.1.50:54321
    python radar_splitter.py --listen-port 54321 --ingame-port 44444 --display 192.168.1.50
"""

import argparse
import socket
import struct
import sys
import time
import threading
from collections import deque

# ─── Default configuration ────────────────────────────────────────────────────
DEFAULT_LISTEN_PORT = 54321      # Port we receive ASTERIX data from NAUTIS
DEFAULT_INGAME_PORT = 44444      # The in-game radar's listening port (loopback)
DEFAULT_DISPLAY_PORT = 54322     # Port the remote radar_display.py listens on


def parse_args():
    parser = argparse.ArgumentParser(
        description="NAUTIS Home ASTERIX Radar UDP Splitter",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Example (same machine, add remote display at 192.168.1.50):
  python radar_splitter.py --display 192.168.1.50

Example (custom ports):
  python radar_splitter.py --listen-port 54321 --ingame-port 44444 --display 192.168.1.50 --display-port 54321

Example (multiple remote displays):
  python radar_splitter.py --display 192.168.1.50 --display 192.168.1.51

Library.xml change required:
  In device 03, change <_remotePort> from 44444 to 54321 (the listen port).
""",
    )
    parser.add_argument(
        "--listen-port", type=int, default=DEFAULT_LISTEN_PORT,
        help=f"Port to receive ASTERIX data from NAUTIS (default: {DEFAULT_LISTEN_PORT})"
    )
    parser.add_argument(
        "--ingame-port", type=int, default=DEFAULT_INGAME_PORT,
        help=f"Port of the in-game radar listener on localhost (default: {DEFAULT_INGAME_PORT})"
    )
    parser.add_argument(
        "--display", action="append", dest="display_hosts", default=[],
        metavar="IP",
        help="IP address of a remote radar_display.py client. Can be repeated."
    )
    parser.add_argument(
        "--display-port", type=int, default=DEFAULT_DISPLAY_PORT,
        help=f"UDP port on remote display machine (default: {DEFAULT_DISPLAY_PORT})"
    )
    parser.add_argument(
        "--no-ingame", action="store_true", default=False,
        help="Do NOT forward to the local in-game radar on localhost (default: False)"
    )
    parser.add_argument(
        "--stats-interval", type=float, default=5.0,
        help="How often to print statistics in seconds (default: 5.0, 0 to disable)"
    )
    return parser.parse_args()


class RateMeter:
    """Simple rolling-window packet rate meter."""
    def __init__(self, window=5.0):
        self.window = window
        self.timestamps = deque()
        self.lock = threading.Lock()

    def tick(self):
        now = time.monotonic()
        with self.lock:
            self.timestamps.append(now)
            cutoff = now - self.window
            while self.timestamps and self.timestamps[0] < cutoff:
                self.timestamps.popleft()

    def rate(self):
        now = time.monotonic()
        with self.lock:
            cutoff = now - self.window
            while self.timestamps and self.timestamps[0] < cutoff:
                self.timestamps.popleft()
            return len(self.timestamps) / self.window


def main():
    args = parse_args()

    # Build destination list
    destinations = []
    if not args.no_ingame:
        destinations.append(("127.0.0.1", args.ingame_port, "in-game"))
    
    # Default to local display if no displays are specified
    display_hosts = args.display_hosts if args.display_hosts else ["127.0.0.1"]
    for host in display_hosts:
        destinations.append((host, args.display_port, f"display@{host}"))

    print("=" * 70)
    print("  NAUTIS Home ASTERIX Radar UDP Splitter")
    print("=" * 70)
    print(f"  Listening on: 0.0.0.0:{args.listen_port}")
    print(f"  Forwarding to:")
    if not destinations:
        print("  [WARN] No destinations configured! Use --display or --targets.")
    for ip, port, label in destinations:
        print(f"    - {label}  =>  {ip}:{port}")
    print()
    print("  Library.xml change required:")
    print(f"    In 'device 03', set <_remotePort> to {args.listen_port}")
    print("    (Leave <_remoteIP> as 127.0.0.1)")
    print("=" * 70)
    print()

    # Create receive socket
    recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        recv_sock.bind(("0.0.0.0", args.listen_port))
    except OSError as e:
        print(f"ERROR: Cannot bind to port {args.listen_port}: {e}")
        print("  Is another process (or the sim) already using this port?")
        sys.exit(1)

    recv_sock.settimeout(1.0)

    # Create send socket
    send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    rate_meter = RateMeter(window=5.0)
    total_packets = 0
    total_bytes = 0
    start_time = time.time()
    last_stats = start_time

    print("Splitter running. Press Ctrl+C to stop.\n")

    try:
        while True:
            try:
                data, addr = recv_sock.recvfrom(65535)
            except socket.timeout:
                continue

            # Forward to all destinations
            for dst_ip, dst_port, _ in destinations:
                try:
                    send_sock.sendto(data, (dst_ip, dst_port))
                except Exception as e:
                    print(f"  [WARN] Failed to forward to {dst_ip}:{dst_port}: {e}")

            rate_meter.tick()
            total_packets += 1
            total_bytes += len(data)

            # Print stats periodically
            if args.stats_interval > 0:
                now = time.time()
                if now - last_stats >= args.stats_interval:
                    last_stats = now
                    elapsed = now - start_time
                    rate = rate_meter.rate()
                    print(
                        f"  [{elapsed:.0f}s] "
                        f"{total_packets} packets  "
                        f"{total_bytes/1024/1024:.1f} MB  "
                        f"~{rate:.0f} pkt/s  "
                        f"~{rate * len(data) * 8 / 1e6:.1f} Mbps"
                    )

    except KeyboardInterrupt:
        print("\n\nSplitter stopped.")
    finally:
        recv_sock.close()
        send_sock.close()


if __name__ == "__main__":
    main()
