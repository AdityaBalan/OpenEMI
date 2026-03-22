import math
from .geometry import dist_segment_to_segment
from .physics import get_emi_weight, get_edge_rate

def analyze_bom_for_emi(components):
    bom_suggestions = []
    
    for c in components.get("C", []):
        fp_str = c["footprint"].upper()
        if "1206" in fp_str or "1210" in fp_str or "0805" in fp_str:
            is_decoupling = any("GND" in str(p["net"]).upper() for p in c["pads"])
            if is_decoupling:
                bom_suggestions.append({
                    "ref": c["ref"], "type": "High ESL Footprint", "abs_x": c["abs_x"], "abs_y": c["abs_y"],
                    "message": f"Change {c['ref']} ({c['val']}) to 0402 or 0603 to reduce loop inductance."
                })

    for l in components.get("L", []):
        is_sw_node = any("SW" in str(p["net"]).upper() or "LX" in str(p["net"]).upper() for p in l["pads"])
        if is_sw_node:
            bom_suggestions.append({
                "ref": l["ref"], "type": "Magnetic Radiator", "abs_x": l["abs_x"], "abs_y": l["abs_y"],
                "message": f"Ensure {l['ref']} is a SHIELDED inductor to contain magnetic emissions."
            })
                
    for u in components.get("U", []):
        pwr_pads = [p for p in u["pads"] if "V" in str(p["net"]).upper() or "+5" in str(p["net"]).upper() or "+3" in str(p["net"]).upper()]
        if pwr_pads:
            has_small_cap = False
            for c in components.get("C", []):
                dist = math.hypot(c["x"] - u["x"], c["y"] - u["y"])
                if dist < 10.0: 
                    val_str = c["val"].upper()
                    if any(v in val_str for v in ["100N", "0.1U", ".1U", "10N", "1N"]):
                        has_small_cap = True; break
            
            if not has_small_cap:
                bom_suggestions.append({
                    "ref": u["ref"], "type": "Missing HF Bypassing", "abs_x": u["abs_x"], "abs_y": u["abs_y"],
                    "message": f"IC lacks local high-frequency decoupling. Add a 100nF or 1nF capacitor nearby."
                })

    for j in components.get("J", []):
        for pad in j["pads"]:
            net = pad["net"].upper()
            if net and "GND" not in net:
                has_filter = False
                for f_type in ["L", "FB", "D"]:
                    for f_comp in components.get(f_type, []):
                        if math.hypot(j["x"] - f_comp["x"], j["y"] - f_comp["y"]) < 10.0:
                            has_filter = True; break
                if not has_filter and get_emi_weight("", net) >= 5.0:
                    bom_suggestions.append({
                        "ref": j["ref"], "type": "Unfiltered I/O Antenna", "abs_x": j["abs_x"], "abs_y": j["abs_y"],
                        "message": f"Connector net '{net}' lacks filtering. Add a Ferrite Bead or TVS diode within 10mm."
                    })

    return bom_suggestions

def analyze_advanced_layout_rules(tracks, vias, gnd_zones, net_endpoints, layer_depths, board_thickness_mm, components):
    violations = []
    gnd_vias = [v for v in vias if "GND" in v["net"].upper()]
    
    # 1. Z-Axis Layer Jumps (Return Path Vias)
    for v in vias:
        if v.get("weight", 1.0) >= 15.0 and "GND" not in v["net"].upper():
            allowed_dist = 2.5 if v.get("tr_ns", 20.0) > 1.0 else 1.0
            has_return = any(math.hypot(v["x"] - gv["x"], v["y"] - gv["y"]) < allowed_dist for gv in gnd_vias)
            if not has_return:
                violations.append({"net": v["net"], "violation": f"[EMI] Z-Axis Loop Area (No Stitching Via within {allowed_dist}mm)", "abs_x": v["abs_x"], "abs_y": v["abs_y"], "type": "z_axis", "risk": 30.0})
                
        if v.get("tr_ns", 20.0) <= 1.0 and "GND" not in v["net"].upper():
            t_layer = v.get("top_layer", 0)
            b_layer = v.get("bottom_layer", 31)
            t_depth = layer_depths.get(t_layer, 0.0)
            b_depth = layer_depths.get(b_layer, board_thickness_mm)
            stub_length_mm = board_thickness_mm - max(t_depth, b_depth)
            
            if stub_length_mm > 1.0:
                c_pcb = 1.5e8 
                stub_res_ghz = (c_pcb / (4.0 * (stub_length_mm / 1000.0))) / 1e9
                violations.append({
                    "net": v["net"], 
                    "violation": f"[EMI] High-Speed Via Stub ({stub_length_mm:.1f}mm). Resonates at ~{stub_res_ghz:.1f}GHz", 
                    "abs_x": v["abs_x"], "abs_y": v["abs_y"], "type": "stub", "risk": 40.0
                })

    # 2. Swiss Cheese Detection
    for i, v1 in enumerate(gnd_vias):
        for v2 in gnd_vias[i+1:]:
            dist = math.hypot(v1["x"] - v2["x"], v1["y"] - v2["y"])
            clearance = (v1.get("width", 0.6) / 2.0) + (v2.get("width", 0.6) / 2.0) + 0.15 
            if 0.1 < dist < clearance: 
                violations.append({
                    "net": "GND", 
                    "violation": "[EMI] Merged Anti-Pads (Swiss Cheese GND Discontinuity)", 
                    "abs_x": (v1["abs_x"] + v2["abs_x"])/2, "abs_y": (v1["abs_y"] + v2["abs_y"])/2, 
                    "type": "antipad", "risk": 20.0
                })

    # 3. Crosstalk (3W Rule)
    high_speed_tracks = [t for t in tracks if t["weight"] >= 15.0]
    for i, t1 in enumerate(high_speed_tracks):
        for t2 in high_speed_tracks[i+1:]:
            if t1["net"] != t2["net"] and t1["layer"] == t2["layer"]:
                dist = dist_segment_to_segment((t1["x1"], t1["y1"]), (t1["x2"], t1["y2"]), (t2["x1"], t2["y1"]), (t2["x2"], t2["y2"]))
                if dist < (3.0 * max(t1["width"], t2["width"])) and dist < 1.0: 
                    violations.append({"net": f"{t1['net']} & {t2['net']}", "violation": "[EMI] Crosstalk Risk (< 3W Spacing)", "abs_x": t1["abs_x"], "abs_y": t1["abs_y"], "type": "crosstalk", "risk": 25.0})

    # 4. High-Speed Stubs (Quarter-Wave Antennas)
    for net, eps in net_endpoints.items():
        wgt = get_emi_weight("", net)
        tr_ns = get_edge_rate("", net)
        if wgt >= 15.0:
            for (nx, ny), count in eps.items():
                if count == 1: 
                    for t in tracks:
                        if t["net"] == net and (math.isclose(t["x1"], nx, abs_tol=0.01) or math.isclose(t["x2"], nx, abs_tol=0.01)):
                            violations.append({"net": net, "violation": "[EMI] High-Speed Stub (Monopole Antenna)", "abs_x": t["abs_x"], "abs_y": t["abs_y"], "type": "stub", "risk": 20.0})

    # 5. Floating Copper Islands
    for z in gnd_zones:
        if "GND" in z["net"] and not z["has_via"] and z["w"] > 5.0 and z["h"] > 5.0:
            violations.append({"net": z["net"], "violation": "[EMI] Floating Copper Island (Parasitic Antenna)", "abs_x": z["abs_x"], "abs_y": z["abs_y"], "type": "floating", "risk": 15.0})

    # 6. Switching Hot-Loop Detector
    for l_comp in components.get("L", []):
        for p in l_comp["pads"]:
            net = p["net"]
            if not net or "GND" in net.upper(): continue
            
            connected_active = None
            for prefix in ["U", "VR", "D"]:
                for c in components.get(prefix, []):
                    if any(cp["net"] == net for cp in c["pads"]):
                        connected_active = c
                        break
                if connected_active: break
                
            if connected_active:
                closest_c = None
                min_dist = float('inf')
                for c in components.get("C", []):
                    dist = math.hypot(c["abs_x"] - connected_active["abs_x"], c["abs_y"] - connected_active["abs_y"])
                    if dist < min_dist:
                        min_dist = dist
                        closest_c = c
                
                if closest_c:
                    x1, y1 = connected_active["abs_x"], connected_active["abs_y"]
                    x2, y2 = l_comp["abs_x"], l_comp["abs_y"]
                    x3, y3 = closest_c["abs_x"], closest_c["abs_y"]
                    loop_area = 0.5 * abs(x1*(y2 - y3) + x2*(y3 - y1) + x3*(y1 - y2))
                    
                    if loop_area > 20.0: 
                        already_logged = any(v["net"] == net and v["type"] == "hot_loop" for v in violations)
                        if not already_logged:
                            violations.append({
                                "net": net,
                                "violation": f"[EMI] Switching Hot-Loop Area Too Large ({loop_area:.1f} mm²). High magnetic emissions.",
                                "abs_x": (x1+x2+x3)/3.0,
                                "abs_y": (y1+y2+y3)/3.0,
                                "type": "hot_loop",
                                "risk": min(150.0, loop_area * 1.5)
                            })

    return violations