"""
test_ais.py -- test AIS encoding and decoding round-trip
"""
import math

def encode_ais_msg(mmsi, lat, lon, sog_kn, cog_deg, heading_deg, rot_dpm, time_sec):
    msg_type = 1
    repeat = 0
    mmsi = int(mmsi) & 0x3FFFFFFF
    nav_status = 0
    
    if rot_dpm == 0.0:
        rot_ais = 0
    else:
        sign = 1 if rot_dpm > 0 else -1
        try:
            rot_ais = int(sign * 4.733 * math.sqrt(abs(rot_dpm)))
            rot_ais = max(-126, min(126, rot_ais))
        except:
            rot_ais = -128
            
    sog_val = int(sog_kn * 10.0)
    sog_val = max(0, min(1022, sog_val))
    
    pos_accuracy = 1
    
    lon_val = int(lon * 600000.0) & 0xFFFFFFF
    lat_val = int(lat * 600000.0) & 0x7FFFFFF
    
    cog_val = int(cog_deg * 10.0) % 3600
    if cog_val < 0:
        cog_val = 3600
        
    heading_val = int(heading_deg) % 360
    if heading_val < 0:
        heading_val = 511
        
    ts = int(time_sec) % 60
    
    bits = 0
    bits = (bits << 6) | msg_type
    bits = (bits << 2) | repeat
    bits = (bits << 30) | mmsi
    bits = (bits << 4) | nav_status
    bits = (bits << 8) | (rot_ais & 0xFF)
    bits = (bits << 10) | sog_val
    bits = (bits << 1) | pos_accuracy
    bits = (bits << 28) | lon_val
    bits = (bits << 27) | lat_val
    bits = (bits << 12) | cog_val
    bits = (bits << 9) | heading_val
    bits = (bits << 6) | ts
    bits = (bits << 2) | 0 # maneuver
    bits = (bits << 3) | 0 # spare
    bits = (bits << 1) | 0 # RAIM
    bits = (bits << 19) | 0 # radio
    
    payload = ""
    for i in range(27, -1, -1):
        val = (bits >> (i * 6)) & 0x3F
        if val < 40:
            payload += chr(val + 48)
        else:
            payload += chr(val + 56)
            
    return payload

def decode_ais_msg(payload):
    bits = 0
    for char in payload:
        val = ord(char)
        if val < 96:
            val -= 48
        else:
            val -= 56
        bits = (bits << 6) | val
        
    # Extract fields
    radio = bits & 0x7FFFF
    bits >>= 19
    raim = bits & 1
    bits >>= 1
    spare = bits & 7
    bits >>= 3
    maneuver = bits & 3
    bits >>= 2
    ts = bits & 0x3F
    bits >>= 6
    heading = bits & 0x1FF
    bits >>= 9
    cog = bits & 0xFFF
    bits >>= 12
    
    # Lat/Lon are signed 28 and 27 bit integers!
    lat_bits = bits & 0x7FFFFFF
    if lat_bits & 0x4000000:  # Sign bit
        lat_bits -= 0x8000000
    lat = lat_bits / 600000.0
    bits >>= 27
    
    lon_bits = bits & 0xFFFFFFF
    if lon_bits & 0x8000000:  # Sign bit
        lon_bits -= 0x10000000
    lon = lon_bits / 600000.0
    bits >>= 28
    
    pos_acc = bits & 1
    bits >>= 1
    sog = bits & 0x3FF
    bits >>= 10
    
    rot = bits & 0xFF
    if rot & 0x80:
        rot -= 256
    bits >>= 8
    
    nav_status = bits & 0xF
    bits >>= 4
    mmsi = bits & 0x3FFFFFFF
    bits >>= 30
    repeat = bits & 3
    bits >>= 2
    msg_type = bits & 0x3F
    
    return {
        "msg_type": msg_type,
        "mmsi": mmsi,
        "nav_status": nav_status,
        "rot": rot,
        "sog": sog / 10.0,
        "pos_acc": pos_acc,
        "lon": lon,
        "lat": lat,
        "cog": cog / 10.0,
        "heading": heading,
        "ts": ts
    }

def main():
    mmsi = 244226640
    lat = 22.227686
    lon = 114.273489
    sog = 10.0
    cog = 7.9
    heading = 70.8
    rot = 0.0
    ts = 15
    
    payload = encode_ais_msg(mmsi, lat, lon, sog, cog, heading, rot, ts)
    print(f"Encoded Payload: {payload}")
    
    decoded = decode_ais_msg(payload)
    print("Decoded fields:")
    for k, v in decoded.items():
        print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
