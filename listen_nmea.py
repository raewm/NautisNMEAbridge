"""Listen on UDP port 10110 and print received NMEA sentences."""
import socket, sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 10110
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind(('0.0.0.0', port))
sock.settimeout(5.0)

print(f'Listening for NMEA on UDP port {port}...', flush=True)
count = 0
while count < 30:
    try:
        data, addr = sock.recvfrom(4096)
        sentences = data.decode('ascii', errors='replace').strip().split('\r\n')
        for s in sentences:
            if s:
                print(s, flush=True)
                count += 1
    except socket.timeout:
        print('(no data for 5s)', flush=True)
        break

sock.close()
print(f'Done. Received {count} sentences.', flush=True)
