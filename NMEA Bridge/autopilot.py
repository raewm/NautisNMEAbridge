import math
import time

# ---------------------------------------------------------------------------
# Vessel Response Presets
# (Kp, Ki, Kd, rudder_limit_deg)
# Tuned for typical simulation vessel response.  Slow vessels have large
# rotational inertia — small Kp, more derivative damping.
# ---------------------------------------------------------------------------
VESSEL_PRESETS = {
    "ODYSSEUS": (1.00, 0.0000, 5.00, 25.0),
    "VIGO": (0.80, 0.0000, 1.00, 30.0),
    "ARCTURUS": (1.50, 0.0000, 5.00, 25.0),
    "Slow":   (0.4, 0.005, 1.2, 25.0),   # Large tanker / bulker
    "Medium": (0.6, 0.010, 0.8, 25.0),   # General cargo / ferry
    "Fast":   (1.0, 0.020, 0.5, 30.0),   # Patrol / small fast craft
}


class PIDController:
    def __init__(self, kp=0.6, ki=0.01, kd=0.8, limit=25.0):
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.limit = limit

        self.integral = 0.0
        self.last_error = 0.0
        self.last_time = time.time()

        # Low-pass filter state for heading input (EMA)
        # alpha=0.3 means new reading contributes 30%, history 70%.
        # This smooths quantisation noise from the ~2 Hz gRPC poll.
        self._heading_alpha = 0.3
        self._filtered_heading = None   # seeded on first call

    def reset(self):
        self.integral = 0.0
        self.last_error = 0.0
        self.last_time = time.time()
        self._filtered_heading = None

    def filter_heading(self, raw: float) -> float:
        """Apply EMA low-pass filter to raw heading (handles 0/360 wrap)."""
        if self._filtered_heading is None:
            self._filtered_heading = raw
            return raw
        # Compute shortest angular difference to handle wrap-around
        diff = (raw - self._filtered_heading + 180) % 360 - 180
        self._filtered_heading = (self._filtered_heading + self._heading_alpha * diff) % 360.0
        return self._filtered_heading

    def update(self, current: float, target: float) -> float:
        """
        Compute rudder demand (degrees) from current and target headings.
        Positive = starboard rudder, negative = port rudder.
        """
        now = time.time()
        dt = now - self.last_time
        if dt <= 0.0:
            dt = 0.01
        self.last_time = now

        # Apply heading filter before computing error
        filtered = self.filter_heading(current)

        # Shortest heading error in [-180, 180]
        error = (target - filtered + 180) % 360 - 180

        # Integrate with anti-windup clamp
        self.integral += error * dt
        max_i = self.limit / max(0.001, self.ki) if self.ki > 0 else 0.0
        self.integral = max(-max_i, min(max_i, self.integral))

        # Derivative (on filtered heading to reduce noise)
        derivative = (error - self.last_error) / dt
        self.last_error = error

        output = (self.kp * error) + (self.ki * self.integral) + (self.kd * derivative)
        return max(-self.limit, min(self.limit, output))

    def apply_preset(self, preset_name: str):
        """Apply a named vessel preset, resetting integral state."""
        kp, ki, kd, lim = VESSEL_PRESETS.get(preset_name, VESSEL_PRESETS["Medium"])
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.limit = lim
        self.reset()


def parse_apb(sentence: str):
    """
    Parse NMEA 0183 $??APB sentence for route guidance.

    Returns dict with keys:
        valid           : bool
        xte             : float  signed NM (positive = steer Right)
        steer_dir       : str    'L' or 'R'
        waypoint        : str
        heading_to_steer: float or None (degrees)
        heading_ref     : str   'T' (true) or 'M' (magnetic)
    """
    if not sentence.startswith("$") or "*" not in sentence:
        return None
    try:
        body = sentence.split("*")[0]
        parts = body.split(",")
        if len(parts) < 15:
            return None

        sent_id = parts[0][1:]   # e.g. GPAPB, INAPB, APAPB
        if not sent_id.endswith("APB"):
            return None

        status    = parts[1]             # A = valid, V = invalid
        xte_val   = float(parts[3]) if parts[3] else 0.0
        steer_dir = parts[4]             # L or R
        wp_id     = parts[10]
        heading_to_steer = float(parts[13]) if parts[13] else None

        # Field 15 (index 14): M = Magnetic, T = True
        # OpenCPN defaults to True; some devices send Magnetic.
        heading_ref = parts[14].strip().upper() if len(parts) > 14 and parts[14].strip() else "T"
        if heading_ref not in ("T", "M"):
            heading_ref = "T"

        signed_xte = xte_val if steer_dir == "R" else -xte_val

        return {
            "valid":            status == "A",
            "xte":              signed_xte,
            "steer_dir":        steer_dir,
            "waypoint":         wp_id,
            "heading_to_steer": heading_to_steer,
            "heading_ref":      heading_ref,
        }
    except Exception:
        return None
