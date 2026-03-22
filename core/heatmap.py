import numpy as np
import math
from .physics import estimate_cispr_band

try:
    import scipy.ndimage as ndimage
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

def append_viol(current, new): 
    if current == "None": return new
    if new in current: return current
    return current + f" & {new}"

def add_thick_trace_vectorized(grid, x1, y1, x2, y2, width, weight, res_x, res_y, board_w, board_h):
    px1, py1 = int((x1 / board_w) * res_x), int((y1 / board_h) * res_y)
    px2, py2 = int((x2 / board_w) * res_x), int((y2 / board_h) * res_y)
    p_width = max(1.0, (width / board_w) * res_x)
    
    pad = int(p_width) + 2
    min_x, max_x = max(0, min(px1, px2) - pad), min(res_x, max(px1, px2) + pad + 1)
    min_y, max_y = max(0, min(py1, py2) - pad), min(res_y, max(py1, py2) + pad + 1)
    
    if min_x >= max_x or min_y >= max_y: return

    Y, X = np.ogrid[min_y:max_y, min_x:max_x]
    
    dx, dy = px2 - px1, py2 - py1
    l2 = dx*dx + dy*dy
    if l2 == 0: dist = np.hypot(X - px1, Y - py1)
    else:
        t = np.clip(((X - px1) * dx + (Y - py1) * dy) / l2, 0.0, 1.0)
        proj_x, proj_y = px1 + t * dx, py1 + t * dy
        dist = np.hypot(X - proj_x, Y - proj_y)

    mask = dist <= (p_width / 2.0)
    grid[min_y:max_y, min_x:max_x][mask] += weight

def generate_heatmap_fast(tracks, pads, gnd_zones, diff_skews, components, net_lengths, board_w, board_h, origin_x, origin_y, resolution_x=1000):
    resolution_y = max(10, int(resolution_x * (board_h / board_w)))
    Z = np.zeros((resolution_y, resolution_x), dtype=np.float32)
    GND_Z = np.zeros((resolution_y, resolution_x), dtype=np.float32)
    COOL_Z = np.zeros((resolution_y, resolution_x), dtype=np.float32) 

    def to_px(val, max_val, res): return int(max(0, min(res - 1, (val / max_val) * res)))

    for z in gnd_zones:
        x1, x2 = max(0, to_px(z["x"], board_w, resolution_x)), min(resolution_x, to_px(z["x"] + z["w"], board_w, resolution_x))
        y1, y2 = max(0, to_px(z["y"], board_h, resolution_y)), min(resolution_y, to_px(z["y"] + z["h"], board_h, resolution_y))
        if x1 < x2 and y1 < y2: GND_Z[y1:y2, x1:x2] = 1.0

    for p in pads:
        if "GND" in str(p["net"]).upper(): 
            GND_Z[to_px(p["y"], board_h, resolution_y), to_px(p["x"], board_w, resolution_x)] = 1.0
            
    for t in tracks:
        if "GND" in str(t["net"]).upper():
            add_thick_trace_vectorized(GND_Z, t["x1"], t["y1"], t["x2"], t["y2"], t["width"], 1.0, resolution_x, resolution_y, board_w, board_h)

    if HAS_SCIPY: GND_blurred = ndimage.gaussian_filter(GND_Z, sigma=15.0)
    else: GND_blurred = GND_Z

    violators = []

    for prefix, comp_list in components.items():
        for c in comp_list:
            is_noisy = prefix in ['L', 'U', 'VR', 'Y', 'X', 'D']
            if is_noisy:
                x1 = max(0, to_px(c["bbox_cx"] - c["bbox_w"]/2, board_w, resolution_x))
                x2 = min(resolution_x, to_px(c["bbox_cx"] + c["bbox_w"]/2, board_w, resolution_x))
                y1 = max(0, to_px(c["bbox_cy"] - c["bbox_h"]/2, board_h, resolution_y))
                y2 = min(resolution_y, to_px(c["bbox_cy"] + c["bbox_h"]/2, board_h, resolution_y))
                
                if x1 < x2 and y1 < y2:
                    gnd_coverage = np.mean(GND_blurred[y1:y2, x1:x2])
                    if gnd_coverage < 0.4:
                        violators.append({
                            "net": c["ref"],
                            "violation": "[EMI] Component Body Straddles Split-Plane (Severe Radiation)",
                            "abs_x": c["abs_x"], "abs_y": c["abs_y"],
                            "risk": 150.0, "type": "component_rpd"
                        })

    for p in pads:
        if p["weight"] >= 0.5: 
            px, py = to_px(p["x"], board_w, resolution_x), to_px(p["y"], board_h, resolution_y)
            Z[py, px] += p["weight"] * 5.0 
            
        if p.get("cooling", 0) > 0: 
            COOL_Z[to_px(p["y"], board_h, resolution_y), to_px(p["x"], board_w, resolution_x)] += p["cooling"] * 10.0
            
    if HAS_SCIPY: COOL_blurred = ndimage.gaussian_filter(COOL_Z, sigma=35.0)
    else: COOL_blurred = COOL_Z

    for t in tracks:
        w, tr = t["weight"], t.get("tr_ns", 20.0)
        if w < 0.5: 
            t["total_risk"] = 0
            t["violation"] = "None"
            continue
            
        violation_type = "None"
        skew = diff_skews.get(t["net"], 0.0)
        net_len = net_lengths.get(t["net"], t.get("len", 0.0))
        edge_rate_multiplier = max(1.0, 10.0 / tr) 
        
        max_heat_val, max_heat_x, max_heat_y = 0, t["abs_x"], t["abs_y"] 
        accumulated_heat = 0
        
        cx_base, cy_base = t["x1"], t["y1"]
        dx, dy = t["x2"] - t["x1"], t["y2"] - t["y1"]
        pts = max(1, int(math.hypot(dx, dy) * 2))
        
        for i in range(pts + 1):
            cx = cx_base + dx * (i/pts) if pts else cx_base
            cy = cy_base + dy * (i/pts) if pts else cy_base
            px, py = to_px(cx, board_w, resolution_x), to_px(cy, board_h, resolution_y)
            
            penalty = 1.0
            
            if w >= 10.0 and GND_blurred[py, px] < 0.15:
                penalty *= (15.0 * edge_rate_multiplier)
                band = estimate_cispr_band(t["net"], net_len, tr)
                violation_type = append_viol(violation_type, f"[EMI] RPD / Large Loop Area [{band}]")
                
            edge_dist = min(cx, cy, board_w - cx, board_h - cy)
            if w >= 10.0 and edge_dist < 3.0:
                penalty *= (1.0 + ((3.0 - edge_dist) * 5.0))
                band = estimate_cispr_band(t["net"], net_len, tr)
                violation_type = append_viol(violation_type, f"[EMI] 20H Edge Fringing [{band}]")
                
            if w >= 10.0 and skew > 1.0:
                penalty *= 15.0
                violation_type = append_viol(violation_type, "[EMI] Diff-Pair Skew")

            heat_point = w * t["width"] * penalty
            if COOL_blurred[py, px] > heat_point:
                heat_point *= 0.3 
                if violation_type == "None": violation_type = "[EMI] Mitigated by Decoupling"
                
            if heat_point > max_heat_val:
                max_heat_val, max_heat_x, max_heat_y = heat_point, cx + origin_x, cy + origin_y
                
            Z[py, px] += heat_point
            accumulated_heat += heat_point

        t["total_risk"] = accumulated_heat
        t["violation"] = violation_type

        if accumulated_heat > 50.0 and "None" not in violation_type and "Mitigated" not in violation_type:
            violators.append({
                "net": t["net"], "violation": violation_type,
                "abs_x": max_heat_x, "abs_y": max_heat_y,
                "risk": accumulated_heat, "type": "heatmap"
            })

        add_thick_trace_vectorized(Z, t["x1"], t["y1"], t["x2"], t["y2"], t["width"] * 2.0, heat_point / 10.0, resolution_x, resolution_y, board_w, board_h)

    if HAS_SCIPY: FINAL_Z = np.clip(ndimage.gaussian_filter(Z, sigma=15.0) - COOL_blurred, 0, None)
    else: FINAL_Z = np.clip(Z - COOL_blurred, 0, None)
        
    return FINAL_Z, violators