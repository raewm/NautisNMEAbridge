import os
import sys
import time
import math
import grpc

# Add current folder to path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from nautis_nmea_bridge import load_descriptors, build_classes, PB_DIR

def run_identification(host="127.0.0.1", port=53457):
    # 1. Load descriptors and classes
    print("[Autotuner] Loading protobuf descriptors...")
    num_loaded = load_descriptors(PB_DIR)
    if num_loaded == 0:
        print("[Autotuner] ERROR: Failed to load descriptors.")
        return None
    
    classes = build_classes()
    if not classes:
        print("[Autotuner] ERROR: Failed to build message classes.")
        return None
        
    print("[Autotuner] Connecting to NAUTIS Home gRPC server...")
    try:
        channel = grpc.insecure_channel(f"{host}:{port}")
        grpc.channel_ready_future(channel).result(timeout=5)
    except Exception as e:
        print(f"[Autotuner] ERROR: Cannot connect to simulator: {e}")
        return None

    # Resolve entity query setup
    req_cls = classes["vstep.entities.GetComponentsRequest"]
    query_cls = classes["vstep.entities.GetComponentsRequest.Query"]
    sel_cls = classes["vstep.entities.EntitySelection"]
    root_cls = classes["vstep.entities.AllRootEntities"]
    resp_cls = classes["vstep.entities.GetComponentsResponse"]

    sel = sel_cls()
    sel.all_root_entities.CopyFrom(root_cls())
    sel.recursion = 1

    from nautis_nmea_bridge import SUBSCRIBE_TYPES
    query = query_cls()
    query.component_types.extend(SUBSCRIBE_TYPES)
    query.entities.append(sel)

    req = req_cls()
    req.queries.append(query)

    stub = channel.unary_unary(
        "/vstep.entities.Registry/GetComponents",
        request_serializer=lambda m: m.SerializeToString(),
        response_deserializer=resp_cls.FromString,
    )

    print("[Autotuner] Scanning simulator registry to resolve own ship...")
    try:
        resp = stub(req)
    except Exception as e:
        print(f"[Autotuner] Registry scan failed: {e}")
        return None

    # Parse registry response
    entities = {}
    parsed_components_flat = {}
    for comp in resp.data:
        url = comp.data.type_url
        tn = url.split("/")[-1] if "/" in url else url
        if tn in classes:
            msg = classes[tn]()
            msg.MergeFromString(comp.data.value)
            eid = comp.entity.id
            parsed_components_flat[(tn, eid)] = msg
            if eid not in entities:
                entities[eid] = {}
            entities[eid][tn] = msg

    # Resolve own-ship
    own_ship_eid = None
    camera_eid = None
    for eid, comps in entities.items():
        if "vstep.viewports.AssignedCamera" in comps:
            camera_eid = comps["vstep.viewports.AssignedCamera"].entity
            break

    parent_map = {}
    for eid, comps in entities.items():
        rel = comps.get("vstep.entities.Relations")
        if rel:
            for child in rel.children:
                parent_map[child] = eid

    if camera_eid:
        curr = camera_eid
        path = []
        while True:
            parent = parent_map.get(curr)
            if parent:
                path.append(parent)
                curr = parent
            else:
                break
        for peid in path:
            if peid in entities and "vstep.equipment.MMSI" in entities[peid]:
                own_ship_eid = peid
                break

    if own_ship_eid is None:
        # Proximity fallback
        first_gps = next((m for (tn, eid), m in parsed_components_flat.items() if tn == "vstep.sensors.GPSOutput"), None)
        first_geo = next((m for (tn, eid), m in parsed_components_flat.items() if tn == "vstep.spatial.PositionGeographic"), None)
        lat_ref = first_gps.latitude if first_gps else (first_geo.position.coordinates.latitude if first_geo else 0.0)
        lon_ref = first_gps.longitude if first_gps else (first_geo.position.coordinates.longitude if first_geo else 0.0)
        if lat_ref != 0.0:
            vessels_list = [eid for eid, comps in entities.items() if "vstep.equipment.MMSI" in comps and "vstep.spatial.PositionGeographic" in comps]
            min_d = float('inf')
            for veid in vessels_list:
                vpos = entities[veid]["vstep.spatial.PositionGeographic"].position.coordinates
                dist = math.sqrt((lat_ref - vpos.latitude)**2 + (lon_ref - vpos.longitude)**2)
                if dist < min_d:
                    min_d = dist
                    own_ship_eid = veid

    if own_ship_eid is None:
        print("[Autotuner] ERROR: Could not resolve own ship. Please make sure a vessel is active in the simulator.")
        return None

    # Resolve descendants and ship name
    descendants = set()
    to_visit = [own_ship_eid]
    while to_visit:
        curr = to_visit.pop()
        if curr != own_ship_eid:
            descendants.add(curr)
        rel = entities.get(curr, {}).get("vstep.entities.Relations")
        if rel:
            for child in rel.children:
                if child not in descendants and child != own_ship_eid:
                    to_visit.append(child)

    disp_comp = entities[own_ship_eid].get("vstep.entities.DisplayName")
    own_ship_name = disp_comp.name if (disp_comp and disp_comp.name) else "Own Ship"
    own_ship_name = own_ship_name.strip().upper()
    print(f"[Autotuner] Own Ship Resolved: {own_ship_name} (Entity ID: {own_ship_eid})")

    # Resolve steering actuators
    steering_actuator_eids = set()
    for cid in descendants:
        if cid in entities:
            ccomps = entities[cid]
            if "vstep.sensors.RudderIndicatorOutput" in ccomps or "vstep.sensors.PropulsionIndicatorOutput" in ccomps:
                parent = parent_map.get(cid)
                if parent and parent in entities:
                    parent_comps = entities[parent]
                    if "vstep.dynamics.AngleInput" in parent_comps:
                        steering_actuator_eids.add(parent)

    if not steering_actuator_eids:
        print("[Autotuner] ERROR: Could not resolve any rudder angle actuators.")
        return None
    print(f"[Autotuner] Resolved {len(steering_actuator_eids)} rudder actuator(s): {list(steering_actuator_eids)}")

    # Choose test profile based on own-ship name keywords
    profile_type = "Medium"  # Default
    large_keywords = ["TANKER", "BULKER", "CARRIER", "CONTAINER", "VLCC", "CARGO", "SHIP"]
    small_keywords = ["YACHT", "PATROL", "TUG", "RIB", "SPEED", "BOAT"]
    
    if any(kw in own_ship_name for kw in large_keywords):
        profile_type = "Large"
    elif any(kw in own_ship_name for kw in small_keywords):
        profile_type = "Small"
        
    print(f"\n[Autotuner] Detected vessel class: {profile_type} (based on name '{own_ship_name}')")
    print("Select test profile size:")
    print("  1. Small / Fast Vessel (30s test - yachts, patrol boats, tugs)")
    print("  2. Medium / Standard Vessel (60s test - cargo, passenger, ferries)")
    print("  3. Large / Slow Vessel (180s test - supertankers, bulkers, container ships)")
    
    sel = input(f"Choose option (1-3) [default recommended: {profile_type}]: ").strip()
    if sel == "1":
        profile_type = "Small"
    elif sel == "2":
        profile_type = "Medium"
    elif sel == "3":
        profile_type = "Large"
        
    # Configure phase durations based on profile
    if profile_type == "Small":
        p1_dur, p2_dur, p3_dur = 10.0, 15.0, 5.0
    elif profile_type == "Large":
        p1_dur, p2_dur, p3_dur = 60.0, 90.0, 30.0
    else:  # Medium
        p1_dur, p2_dur, p3_dur = 20.0, 30.0, 10.0
        
    test_duration = p1_dur + p2_dur + p3_dur

    # Test rudder angle: sized per profile so it stays within each vessel's
    # physical hydraulic envelope. Large vessels cap below 15° at speed.
    if profile_type == "Small":
        test_angle = 15.0   # fast craft: large step for clear response
    elif profile_type == "Large":
        test_angle = 7.0    # supertankers: stay well below hydraulic limit
    else:
        test_angle = 10.0   # medium: safe for most vessel types

    print("\n" + "="*70)
    print("  AUTOPILOT AUTOTUNER MANEUVER TEST")
    print("="*70)
    print("  Instructions:")
    print("  1. Find a wide, open stretch of water in the simulator.")
    print("  2. Get the vessel moving at a steady cruise speed (e.g. 10 - 15 knots).")
    print("  3. Center the rudder and keep the heading straight.")
    print(f"  4. Press [ENTER] here to start the {test_duration:.0f}-second zig-zag test.")
    print(f"     The profile is set to: {profile_type.upper()}")
    print("="*70)
    input("\nPress ENTER to begin the test...")

    # Engage external control
    engaged_actuators = set()
    for act_eid in steering_actuator_eids:
        try:
            ext_req = classes["vstep.simulation.external.SetExternalControlRequest"]()
            ext_req.entity = act_eid
            ext_stub = channel.unary_unary(
                "/vstep.simulation.external.ExternalControl/SetExternalControl",
                request_serializer=lambda m: m.SerializeToString(),
                response_deserializer=classes["vstep.simulation.external.SetExternalControlResponse"].FromString
            )
            ext_stub(ext_req)
            engaged_actuators.add(act_eid)
        except Exception as e:
            print(f"[Autotuner] Failed to engage control for actuator {act_eid}: {e}")
            return None

    print("\n[Autotuner] Engaged external control. Starting test log...")

    start_time = time.time()
    data = []
    _diag_done = False       # first-poll diagnostic flag
    _cached_pump_state = {}  # act_eid -> list[bool], populated on first successful poll
    poll_entities = {}       # initialised here so pump lookup on first iter doesn't crash

    try:
        set_stub = channel.unary_unary(
            "/vstep.entities.Registry/SetComponents",
            request_serializer=lambda m: m.SerializeToString(),
            response_deserializer=classes["vstep.entities.SetComponentsResponse"].FromString
        )
        
        while True:
            t_elapsed = time.time() - start_time
            if t_elapsed >= test_duration:
                break

            # Determine target rudder angle based on phase.
            # Angle is profile-sized to stay within each vessel's hydraulic envelope.
            if t_elapsed < p1_dur:
                target_rudder = test_angle
                phase_str = f"Phase 1/3 (Stbd +{test_angle:.0f}°) [{t_elapsed:4.1f}/{p1_dur:2.0f}s]"
            elif t_elapsed < (p1_dur + p2_dur):
                target_rudder = -test_angle
                phase_str = f"Phase 2/3 (Port -{test_angle:.0f}°) [{t_elapsed - p1_dur:4.1f}/{p2_dur:2.0f}s]"
            else:
                target_rudder = 0.0
                phase_str = f"Phase 3/3 (Cntr   0°) [{t_elapsed - p1_dur - p2_dur:4.1f}/{p3_dur:2.0f}s]"
                
            # 1. Send rudder command to actuators
            # Copy existing pump config from initial scan to avoid feedback loop
            set_req = classes["vstep.entities.SetComponentsRequest"]()
            for act_eid in steering_actuator_eids:
                angle_input = classes["vstep.dynamics.AngleInput"]()
                angle_input.angle_target = -math.radians(target_rudder)  # Negate for simulator convention
                angle_input.nfu = False
                
                existing_ai = entities.get(act_eid, {}).get("vstep.dynamics.AngleInput")
                if existing_ai and len(existing_ai.pump_active) > 0:
                    pump_list = list(existing_ai.pump_active)
                    pump_list[0] = True   # ensure at least pump 0 is active
                else:
                    pump_list = [True, True, False, False]
                
                angle_input.pump_active.extend(pump_list)
                
                comp_data = classes["vstep.entities.ComponentData"]()
                comp_data.entity.id = act_eid
                comp_data.data.Pack(angle_input)
                set_req.data.append(comp_data)
            
            try:
                set_stub(set_req)
            except Exception:
                pass
                
            # 2. Query telemetry — rebuild full entity map (no descendants filter)
            resp = stub(req)
            poll_entities = {}
            for comp in resp.data:
                url = comp.data.type_url
                tn = url.split("/")[-1] if "/" in url else url
                if tn in classes:
                    msg = classes[tn]()
                    msg.MergeFromString(comp.data.value)
                    eid = comp.entity.id
                    if eid not in poll_entities:
                        poll_entities[eid] = {}
                    poll_entities[eid][tn] = msg

            # ---------- HEADING + ROT ----------
            # CompassBaseOutput.heading is authoritative for heading.
            # CompassBaseOutput.rot is unreliable on many vessels (returns near-zero
            # even when turning). Always compute ROT numerically from heading delta;
            # sensor value is only used as a cross-check.
            heading = 0.0
            rot_degs = 0.0

            # Priority 1: CompassBaseOutput heading (any entity)
            for eid_scan, comps_scan in poll_entities.items():
                cb = comps_scan.get("vstep.sensors.CompassBaseOutput")
                if cb and cb.heading != 0.0:
                    heading = math.degrees(cb.heading) % 360
                    break

            # Priority 2: INSOutput heading
            if heading == 0.0:
                for eid_scan, comps_scan in poll_entities.items():
                    ins = comps_scan.get("vstep.sensors.INSOutput")
                    if ins and ins.heading != 0.0:
                        heading = math.degrees(ins.heading) % 360
                        break

            # Priority 3: GPS COG as last resort
            if heading == 0.0:
                for eid_scan, comps_scan in poll_entities.items():
                    gps = comps_scan.get("vstep.sensors.GPSOutput")
                    if gps and gps.course_over_ground != 0.0:
                        heading = math.degrees(gps.course_over_ground) % 360
                        break

            # ROT: always use numerical derivative from heading delta.
            # 5 Hz poll = 200 ms resolution; at 7 deg/min we see ~0.023 deg/sample.
            if data:
                prev_t, _, _, prev_hdg = data[-1]
                dt_rot = t_elapsed - prev_t
                if dt_rot > 0.05:  # guard against duplicate timestamps
                    diff = (heading - prev_hdg + 180) % 360 - 180
                    rot_degs = diff / dt_rot
            # else rot_degs stays 0.0 for the very first sample

            # ---------- ACTUAL RUDDER ----------
            # Try own-ship descendants first, then fall back to all entities.
            # Restricting to descendants prevents reading another vessel's rudder.
            rudder_angles = []
            own_ship_entity_ids = descendants | {own_ship_eid}
            for eid_scan in own_ship_entity_ids:
                comps_scan = poll_entities.get(eid_scan, {})
                rud = comps_scan.get("vstep.sensors.RudderIndicatorOutput")
                if rud:
                    rudder_angles.append((-math.degrees(rud.angle), eid_scan, "RudderIndicator"))
                elif comps_scan.get("vstep.sensors.PropulsionIndicatorOutput"):
                    prop = comps_scan["vstep.sensors.PropulsionIndicatorOutput"]
                    rudder_angles.append((-math.degrees(prop.angle), eid_scan, "PropulsionIndicator"))

            # Fallback: scan all entities if nothing found in own-ship tree
            if not rudder_angles:
                for eid_scan, comps_scan in poll_entities.items():
                    rud = comps_scan.get("vstep.sensors.RudderIndicatorOutput")
                    if rud:
                        rudder_angles.append((-math.degrees(rud.angle), eid_scan, "RudderIndicator(global)"))
                    elif comps_scan.get("vstep.sensors.PropulsionIndicatorOutput"):
                        prop = comps_scan["vstep.sensors.PropulsionIndicatorOutput"]
                        rudder_angles.append((-math.degrees(prop.angle), eid_scan, "PropulsionIndicator(global)"))

            if rudder_angles:
                actual_rudder = sum(v for v, _, _ in rudder_angles) / len(rudder_angles)
            else:
                actual_rudder = target_rudder  # fallback to commanded

            # ---------- FIRST-POLL DIAGNOSTIC ----------
            if not _diag_done:
                _diag_done = True
                print("\n\n[Diag] Sensor sources on first poll:")
                # Heading source
                hdg_src = "none"
                for eid_scan, comps_scan in poll_entities.items():
                    if comps_scan.get("vstep.sensors.CompassBaseOutput") and \
                       comps_scan["vstep.sensors.CompassBaseOutput"].heading != 0.0:
                        cb_raw = comps_scan["vstep.sensors.CompassBaseOutput"]
                        hdg_src = f"CompassBaseOutput eid={eid_scan}  raw_hdg={cb_raw.heading:.4f}rad ({math.degrees(cb_raw.heading):.2f}deg)  raw_rot={cb_raw.rot:.5f}rad/s ({math.degrees(cb_raw.rot)*60:.3f}deg/min)"
                        break
                print(f"  Heading : {hdg_src}")
                # Rudder source(s)
                for ang, eid_scan, src in rudder_angles:
                    raw_field = poll_entities.get(eid_scan, {}).get("vstep.sensors.RudderIndicatorOutput")
                    raw_val = raw_field.angle if raw_field else "N/A"
                    print(f"  Rudder  : {src} eid={eid_scan}  raw_angle={raw_val}  converted={ang:.2f}deg  (own_ship={own_ship_eid}, actuator={list(steering_actuator_eids)})")
                if not rudder_angles:
                    print("  Rudder  : NO SOURCE FOUND")
                print("[Diag] End sensor sources\n")

            print(f"\r{phase_str} | Cmd: {target_rudder:+5.1f}° | Act: {actual_rudder:+5.1f}° | Hdg: {heading:5.1f}° | ROT: {rot_degs:+6.3f}°/s", end="", flush=True)

            data.append((t_elapsed, actual_rudder, rot_degs, heading))
            time.sleep(0.2)  # 5 Hz poll

    finally:
        print("\n\n[Autotuner] Releasing external control back to simulator...")
        for act_eid in engaged_actuators:
            try:
                ext_req = classes["vstep.simulation.external.SetExternalControlRequest"]()
                ext_req.entity = 0  # Release all
                ext_stub = channel.unary_unary(
                    "/vstep.simulation.external.ExternalControl/SetExternalControl",
                    request_serializer=lambda m: m.SerializeToString(),
                    response_deserializer=classes["vstep.simulation.external.SetExternalControlResponse"].FromString
                )
                ext_stub(ext_req)
            except Exception as e:
                pass
                
    if len(data) < 10:
        print("[Autotuner] ERROR: Not enough data points recorded.")
        return None

    # Perform least squares fitting
    print("[Autotuner] Processing track data for System Identification...")
    smoothed = []
    for i in range(len(data)):
        t, u, v, psi = data[i]
        start = max(0, i - 2)
        end = min(len(data), i + 3)
        v_avg = sum(data[j][2] for j in range(start, end)) / (end - start)
        smoothed.append((t, u, v_avg, psi))
        
    Y = []
    X = []
    for i in range(1, len(smoothed) - 1):
        t_prev, u_prev, v_prev, _ = smoothed[i-1]
        t_curr, u_curr, v_curr, _ = smoothed[i]
        t_next, u_next, v_next, _ = smoothed[i+1]
        dt = t_next - t_prev
        if dt <= 0:
            continue
        ydot = (v_next - v_prev) / dt
        Y.append(ydot)
        X.append((u_curr, v_curr))
        
    sum_u2 = sum(x[0]**2 for x in X)
    sum_uv = sum(x[0]*x[1] for x in X)
    sum_v2 = sum(x[1]**2 for x in X)
    sum_uy = sum(X[j][0]*Y[j] for j in range(len(X)))
    sum_vy = sum(X[j][1]*Y[j] for j in range(len(X)))
    
    det = sum_u2 * sum_v2 - sum_uv**2
    if abs(det) < 1e-6:
        print("[Autotuner] ERROR: Singular matrix. Rudder movements were too small.")
        return None
        
    A = (sum_v2 * sum_uy - sum_uv * sum_vy) / det
    B = (sum_u2 * sum_vy - sum_uv * sum_uy) / det
    
    if B >= 0:
        print("[Autotuner] Warning: Identified unstable/noisy dynamics (B >= 0). Using default medium vessel response parameters.")
        T = 5.0
        K = 0.1
    else:
        T = -1.0 / B
        K = A * T
        
    print(f"\n[Autotuner] Identified Nomoto parameters:")
    print(f"  - Gain K      : {K:.4f} 1/s (Steering effectiveness)")
    print(f"  - Time Constant T: {T:.2f} s (Rotational inertia / lag)")

    # Run grid search optimization
    print("\n[Autotuner] Running offline PID parameter grid search optimization...")
    best_score = float('inf')
    best_gains = (0.6, 0.01, 0.8, 25.0)  # Default fallbacks
    
    # Kp range: 0.1 to 2.5
    # Kd range: 0.1 to 5.0
    # Ki range: 0.0 to 0.05
    kp_candidates = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.5, 1.8, 2.0, 2.5]
    kd_candidates = [0.1, 0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]
    ki_candidates = [0.0, 0.005, 0.01, 0.02, 0.03]
    
    # Limit rudder to 25 deg or 30 deg based on T
    rudder_limit = 30.0 if T < 4.0 else 25.0
    
    # Euler integration simulation parameters
    sim_time = 60.0
    sim_dt = 0.05
    target_step = 20.0
    
    for kp in kp_candidates:
        for kd in kd_candidates:
            for ki in ki_candidates:
                # Run simulation
                psi = 0.0
                r = 0.0
                integral = 0.0
                prev_error = 0.0
                curr_rudder = 0.0
                last_pid_update = 0.0
                
                filtered_psi = 0.0
                
                t = 0.0
                iae = 0.0
                max_overshoot = 0.0
                rudder_changes = 0.0
                
                steps = int(sim_time / sim_dt)
                for _ in range(steps):
                    # AP controller update at 1 Hz
                    if (t - last_pid_update) >= 1.0:
                        error = (target_step - filtered_psi + 180) % 360 - 180
                        integral += error * 1.0
                        max_i = rudder_limit / max(0.001, ki) if ki > 0 else 0.0
                        integral = max(-max_i, min(max_i, integral))
                        
                        derivative = (error - prev_error) / 1.0
                        prev_error = error
                        
                        target_rudder = (kp * error) + (ki * integral) + (kd * derivative)
                        target_rudder = max(-rudder_limit, min(rudder_limit, target_rudder))
                        
                        # Rudder rate limit: max 5 deg/s
                        rud_change = target_rudder - curr_rudder
                        rud_change = max(-5.0, min(5.0, rud_change))
                        curr_rudder += rud_change
                        
                        rudder_changes += abs(rud_change)
                        last_pid_update = t
                        
                    # Physics update: T * dr/dt + r = K * rudder
                    dr = (K * curr_rudder - r) * (sim_dt / T)
                    r += dr
                    psi += r * sim_dt
                    
                    # 2 Hz filter approximation (tau = 1.4s)
                    alpha_sim = 1.0 - math.exp(-sim_dt / 1.4)
                    diff = (psi - filtered_psi + 180) % 360 - 180
                    filtered_psi = (filtered_psi + alpha_sim * diff) % 360.0
                    
                    # Cost metrics
                    err_abs = abs(target_step - psi)
                    iae += err_abs * sim_dt
                    
                    if psi > target_step:
                        overshoot = psi - target_step
                        if overshoot > max_overshoot:
                            max_overshoot = overshoot
                            
                    t += sim_dt
                    
                # Calculate score
                overshoot_penalty = 100.0 * (max_overshoot ** 2)
                rudder_penalty = 0.5 * rudder_changes
                score = iae + overshoot_penalty + rudder_penalty
                
                if abs(psi - target_step) > 2.0:
                    score += 5000.0
                    
                if score < best_score:
                    best_score = score
                    best_gains = (kp, ki, kd, rudder_limit)
                    
    kp_opt, ki_opt, kd_opt, lim_opt = best_gains
    print(f"\n[Autotuner] Optimization Completed! Recommended Gains:")
    print(f"  - Kp: {kp_opt:.2f}")
    print(f"  - Ki: {ki_opt:.4f}")
    print(f"  - Kd: {kd_opt:.2f}")
    print(f"  - Rudder Limit: {lim_opt:.1f}°")

    # Ask the user if they want to save these gains
    print("\n" + "-"*70)
    print(f"  Do you want to save these gains for '{own_ship_name}' in autopilot.py?")
    print("  This will automatically add/overwrite the vessel preset.")
    print("-"*70)
    ans = input("Save preset? (y/n): ").strip().lower()
    
    if ans == 'y':
        # Write to autopilot.py
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ap_path = os.path.join(script_dir, "autopilot.py")
        
        try:
            with open(ap_path, "r", encoding="utf-8") as f:
                code = f.read()
                
            key = "VESSEL_PRESETS = {"
            idx = code.find(key)
            if idx == -1:
                print("[Autotuner] ERROR: Could not find VESSEL_PRESETS dictionary in autopilot.py.")
                return None
                
            insert_pos = idx + len(key)
            search_key = f'"{own_ship_name}":'
            
            if search_key in code:
                # Replace the existing line
                lines = code.splitlines()
                for i, line in enumerate(lines):
                    if search_key in line:
                        lines[i] = f'    "{own_ship_name}": ({kp_opt:.2f}, {ki_opt:.4f}, {kd_opt:.2f}, {lim_opt:.1f}),'
                        break
                new_code = "\n".join(lines) + "\n"
                print(f"[Autotuner] Preset updated for '{own_ship_name}' in autopilot.py.")
            else:
                # Insert at the beginning of the dictionary
                new_preset_line = f'\n    "{own_ship_name}": ({kp_opt:.2f}, {ki_opt:.4f}, {kd_opt:.2f}, {lim_opt:.1f}),'
                new_code = code[:insert_pos] + new_preset_line + code[insert_pos:]
                print(f"[Autotuner] Preset created for '{own_ship_name}' in autopilot.py.")
                
            with open(ap_path, "w", encoding="utf-8") as f:
                f.write(new_code)
                
        except Exception as e:
            print(f"[Autotuner] ERROR writing to autopilot.py: {e}")
            return None
            
    print("\n[Autotuner] Finished. Safe travels!")
    return best_gains

if __name__ == "__main__":
    run_identification()
