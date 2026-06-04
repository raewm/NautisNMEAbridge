"""
test_ais_type5.py -- test AIS Type 5 encoding and decoding
"""
import sys

def encode_ais_string(s, length):
    s = s.upper()[:length]
    s = s.ljust(length, ' ')
    bits = 0
    for char in s:
        code = ord(char)
        if 32 <= code <= 95:
            val = code - 32
        else:
            val = 0
        bits = (bits << 6) | val
    return bits

def decode_ais_string(bits, length):
    s = ""
    for i in range(length - 1, -1, -1):
        val = (bits >> (i * 6)) & 0x3F
        char_val = val + 32
        s += chr(char_val)
    return s.strip()

def encode_ais_type5(mmsi, name, callsign, ship_type=70, draught=0.0):
    mmsi = int(mmsi) & 0x3FFFFFFF
    name_bits = encode_ais_string(name, 20)
    call_bits = encode_ais_string(callsign, 7)
    dest_bits = encode_ais_string("NAUTIS", 20)
    
    draught_val = int(draught * 10.0) & 0xFF
    
    # Pack 424 bits (padded to 426 bits for 71 6-bit chars)
    # 426 bits total:
    bits = 0
    bits = (bits << 6) | 5 # msg type
    bits = (bits << 2) | 0 # repeat
    bits = (bits << 30) | mmsi
    bits = (bits << 2) | 0 # AIS version
    bits = (bits << 30) | 0 # IMO number
    bits = (bits << 42) | call_bits
    bits = (bits << 120) | name_bits
    bits = (bits << 8) | ship_type
    bits = (bits << 30) | 0x1E0502 # dimensions: bow=30, stern=10, port=5, starboard=2
    bits = (bits << 4) | 1 # position fix: GPS
    bits = (bits << 20) | 0 # ETA
    bits = (bits << 8) | draught_val
    bits = (bits << 120) | dest_bits
    bits = (bits << 1) | 0 # DTE
    bits = (bits << 1) | 0 # spare (1 bit) to make it 424 bits?
    # Wait, let's verify if the total length is 424 bits.
    # If 424 bits: 424 / 6 = 70.66 -> padded to 426 bits (71 characters)
    # Let's add 2 spare bits to make it 426 bits:
    bits = (bits << 2) | 0
    
    payload = ""
    for i in range(70, -1, -1):
        val = (bits >> (i * 6)) & 0x3F
        if val < 40:
            payload += chr(val + 48)
        else:
            payload += chr(val + 56)
            
    return payload

def main():
    mmsi = 244226640
    name = "Avante"
    callsign = "PB1234"
    ship_type = 70
    draught = 5.4
    
    payload = encode_ais_type5(mmsi, name, callsign, ship_type, draught)
    print(f"Encoded Type 5 (length {len(payload)}): {payload}")
    
    # Let's decode it to verify
    bits = 0
    for char in payload:
        val = ord(char)
        if val < 96:
            val -= 48
        else:
            val -= 56
        bits = (bits << 6) | val
        
    # Extract fields from the end
    bits >>= 2 # spare
    dte = bits & 1
    bits >>= 1
    spare_1 = bits & 1
    bits >>= 1
    dest_bits = bits & ((1 << 120) - 1)
    bits >>= 120
    draught_val = bits & 0xFF
    bits >>= 8
    eta = bits & 0xFFFFF
    bits >>= 20
    pos_fix = bits & 0xF
    bits >>= 4
    dim = bits & 0x3FFFFFFF
    bits >>= 30
    ship_t = bits & 0xFF
    bits >>= 8
    name_b = bits & ((1 << 120) - 1)
    bits >>= 120
    call_b = bits & ((1 << 42) - 1)
    bits >>= 42
    imo = bits & 0x3FFFFFFF
    bits >>= 30
    ais_version = bits & 3
    bits >>= 2
    dec_mmsi = bits & 0x3FFFFFFF
    bits >>= 30
    repeat = bits & 3
    bits >>= 2
    msg_type = bits & 0x3F
    
    print("\nDecoded Type 5 fields:")
    print(f"  msg_type: {msg_type}")
    print(f"  mmsi: {dec_mmsi}")
    print(f"  callsign: {decode_ais_string(call_b, 7)}")
    print(f"  name: {decode_ais_string(name_b, 20)}")
    print(f"  ship_type: {ship_t}")
    print(f"  draught: {draught_val / 10.0}")
    print(f"  destination: {decode_ais_string(dest_bits, 20)}")

if __name__ == "__main__":
    main()
