import pcbnew
import math
from .physics import get_emi_weight, get_edge_rate, get_cooling_power

def make_point(x, y):
    try: return pcbnew.VECTOR2I(int(x), int(y))
    except: return pcbnew.wxPoint(int(x), int(y))

def dist_segment_to_segment(p1, p2, p3, p4):
    cx1, cy1 = (p1[0]+p2[0])/2, (p1[1]+p2[1])/2
    cx2, cy2 = (p3[0]+p4[0])/2, (p3[1]+p4[1])/2
    return math.hypot(cx2-cx1, cy2-cy1)

def point_to_line_dist(px, py, x1, y1, x2, y2):
    line_mag = (x2 - x1)**2 + (y2 - y1)**2
    if line_mag == 0: return math.hypot(px - x1, py - y1)
    t = max(0, min(1, ((px - x1) * (x2 - x1) + (py - y1) * (y2 - y1)) / line_mag))
    proj_x = x1 + t * (x2 - x1)
    proj_y = y1 + t * (y2 - y1)
    return math.hypot(px - proj_x, py - proj_y)

def extract_board_geometry():
    board = pcbnew.GetBoard()
    tracks, pads, silkscreen, gnd_zones, vias = [], [], [], [], []
    net_lengths = {} 
    high_risk_nets = set()
    components = {"U": [], "C": [], "L": [], "J": [], "FB": [], "D": [], "Y": [], "X": []}
    net_endpoints = {}

    bbox = board.GetBoardEdgesBoundingBox()
    if bbox.GetWidth() == 0 or bbox.GetHeight() == 0:
        bbox = board.ComputeBoundingBox()

    origin_x = pcbnew.ToMM(bbox.GetX())
    origin_y = pcbnew.ToMM(bbox.GetY())
    board_w = max(1.0, pcbnew.ToMM(bbox.GetWidth()))
    board_h = max(1.0, pcbnew.ToMM(bbox.GetHeight()))

    board_thickness_mm = 1.6
    layer_depths = {}
    try:
        stackup = board.GetDesignSettings().GetBoardStackup()
        if stackup:
            board_thickness_mm = stackup.GetBrdThickness()
            current_depth = 0.0
            layer_id = 0
            for i in range(stackup.GetCount()):
                item = stackup.GetItem(i)
                if item.GetType() == pcbnew.BS_ITEM_TYPE_COPPER:
                    layer_depths[layer_id] = current_depth
                    layer_id += 1
                    if layer_id == 31: layer_id = 31 
                current_depth += item.GetThickness()
    except Exception as e:
        layer_depths = {0: 0.0, 31: 1.6}

    try:
        for zone in board.Zones():
            try:
                netname = str(zone.GetNetname()).upper()
                if "GND" in netname or "EARTH" in netname:
                    z_bbox = zone.GetBoundingBox()
                    gnd_zones.append({
                        "net": netname,
                        "x": pcbnew.ToMM(z_bbox.GetX()) - origin_x,
                        "y": pcbnew.ToMM(z_bbox.GetY()) - origin_y,
                        "abs_x": pcbnew.ToMM(z_bbox.GetCenter().x), 
                        "abs_y": pcbnew.ToMM(z_bbox.GetCenter().y),
                        "w": pcbnew.ToMM(z_bbox.GetWidth()),
                        "h": pcbnew.ToMM(z_bbox.GetHeight()),
                        "has_via": False
                    })
            except Exception: pass
    except AttributeError: pass 

    for fp in board.GetFootprints():
        try: ref = fp.GetReferenceAsString()
        except: ref = "U?"
        try: val = fp.GetValue()
        except: val = ""
        try: fp_name = str(fp.GetFPID().GetLibItemName())
        except: fp_name = ""
        try: pos_x, pos_y = pcbnew.ToMM(fp.GetPosition().x) - origin_x, pcbnew.ToMM(fp.GetPosition().y) - origin_y
        except: pos_x, pos_y = 0, 0
            
        silkscreen.append({"text": ref, "x": pos_x, "y": pos_y})
        cooling = get_cooling_power(ref, val)
        ref_upper = str(ref).upper()
        
        try:
            fp_bbox = fp.GetBoundingBox()
            bbox_cx = pcbnew.ToMM(fp_bbox.GetCenter().x) - origin_x
            bbox_cy = pcbnew.ToMM(fp_bbox.GetCenter().y) - origin_y
            bbox_w = pcbnew.ToMM(fp_bbox.GetWidth())
            bbox_h = pcbnew.ToMM(fp_bbox.GetHeight())
        except:
            bbox_cx, bbox_cy, bbox_w, bbox_h = pos_x, pos_y, 2.0, 2.0
        
        comp_data = {
            "ref": ref, "val": val, "footprint": fp_name,
            "x": pos_x, "y": pos_y, 
            "abs_x": pcbnew.ToMM(fp.GetPosition().x), 
            "abs_y": pcbnew.ToMM(fp.GetPosition().y),
            "bbox_cx": bbox_cx, "bbox_cy": bbox_cy,
            "bbox_w": bbox_w, "bbox_h": bbox_h,
            "obj": fp,
            "pads": []
        }
        
        comp_prefix = ''.join(filter(str.isalpha, ref_upper))
        if comp_prefix in components: components[comp_prefix].append(comp_data)
        elif ref_upper.startswith('P'): components.setdefault("J", []).append(comp_data)
        elif ref_upper.startswith('VR'): components.setdefault("U", []).append(comp_data)

        is_noisy_comp = ref_upper.startswith('L') or ref_upper.startswith('U') or ref_upper.startswith('VR') or ref_upper.startswith('D') or ref_upper.startswith('Y') or ref_upper.startswith('X')
        
        for pad in fp.Pads():
            try: sz_x = pcbnew.ToMM(pad.GetSize().x); sz_y = pcbnew.ToMM(pad.GetSize().y)
            except: sz_x, sz_y = 1.0, 1.0
            try: pad_net = str(pad.GetNetname())
            except: pad_net = ""
            
            if is_noisy_comp and pad_net and "GND" not in pad_net.upper():
                high_risk_nets.add(pad_net)

            pad_dict = {
                "ref": ref, 
                "x": pcbnew.ToMM(pad.GetPosition().x) - origin_x, 
                "y": pcbnew.ToMM(pad.GetPosition().y) - origin_y,
                "abs_x": pcbnew.ToMM(pad.GetPosition().x), "abs_y": pcbnew.ToMM(pad.GetPosition().y),
                "w": sz_x, "h": sz_y, 
                "weight": get_emi_weight(ref, pad_net) * max(sz_x, sz_y),
                "tr_ns": get_edge_rate(ref, pad_net),
                "net": pad_net, "cooling": cooling,
                "is_noisy_comp": is_noisy_comp
            }
            pads.append(pad_dict)
            comp_data["pads"].append(pad_dict)

            if pad_net:
                ep_key = (round(pad_dict["x"], 3), round(pad_dict["y"], 3))
                net_endpoints.setdefault(pad_net, {})[ep_key] = net_endpoints.get(pad_net, {}).get(ep_key, 0) + 2

    for item in board.GetTracks():
        try: netname = str(item.GetNetname())
        except AttributeError: netname = ""

        if type(item) is pcbnew.PCB_VIA:
            vx, vy = pcbnew.ToMM(item.GetPosition().x) - origin_x, pcbnew.ToMM(item.GetPosition().y) - origin_y
            v_width = pcbnew.ToMM(item.GetWidth())
            
            top_layer, bottom_layer = 0, 31
            if hasattr(item, 'TopLayer') and hasattr(item, 'BottomLayer'):
                top_layer = item.TopLayer()
                bottom_layer = item.BottomLayer()

            vias.append({
                "x": vx, "y": vy, "net": netname,
                "abs_x": pcbnew.ToMM(item.GetPosition().x), "abs_y": pcbnew.ToMM(item.GetPosition().y),
                "weight": get_emi_weight("", netname), "tr_ns": get_edge_rate("", netname),
                "width": v_width,
                "top_layer": top_layer, "bottom_layer": bottom_layer
            })
            
            for z in gnd_zones:
                if z["net"] == netname and z["x"] <= vx <= z["x"]+z["w"] and z["y"] <= vy <= z["y"]+z["h"]:
                    z["has_via"] = True
                    
            if netname:
                ep_key = (round(vx, 3), round(vy, 3))
                net_endpoints.setdefault(netname, {})[ep_key] = net_endpoints.get(netname, {}).get(ep_key, 0) + 2

        elif type(item) is pcbnew.PCB_TRACK and hasattr(item, 'GetStart') and hasattr(item, 'GetEnd'):
            layer = item.GetLayer()
            is_internal = (0 < layer < 31)
            layer_multiplier = 0.1 if is_internal else 1.0
            
            sx, sy = pcbnew.ToMM(item.GetStart().x), pcbnew.ToMM(item.GetStart().y)
            ex, ey = pcbnew.ToMM(item.GetEnd().x), pcbnew.ToMM(item.GetEnd().y)
            seg_len = math.hypot(ex - sx, ey - sy)
            
            if netname: 
                net_lengths[netname] = net_lengths.get(netname, 0.0) + seg_len
                k1, k2 = (round(sx - origin_x, 3), round(sy - origin_y, 3)), (round(ex - origin_x, 3), round(ey - origin_y, 3))
                net_endpoints.setdefault(netname, {})[k1] = net_endpoints.get(netname, {}).get(k1, 0) + 1
                net_endpoints.setdefault(netname, {})[k2] = net_endpoints.get(netname, {}).get(k2, 0) + 1
            
            base_weight = get_emi_weight("", netname)
            tr_ns = get_edge_rate("", netname)
            if netname in high_risk_nets and base_weight < 15.0:
                base_weight = 15.0 
                
            tracks.append({
                "x1": sx - origin_x, "y1": sy - origin_y,
                "x2": ex - origin_x, "y2": ey - origin_y,
                "abs_x": (sx + ex)/2, "abs_y": (sy + ey)/2, 
                "width": pcbnew.ToMM(item.GetWidth()),
                "weight": base_weight * layer_multiplier,
                "tr_ns": tr_ns,
                "net": netname,
                "layer": layer,
                "is_internal": is_internal,
                "len": seg_len,
                "obj": item
            })

    diff_skews = {}
    for net, length in net_lengths.items():
        net_upper = net.upper()
        if net_upper.endswith('_P') or net_upper.endswith('+') or net_upper.endswith('_N') or net_upper.endswith('-'):
            base_name = net[:-1] if (net.endswith('+') or net.endswith('-')) else net[:-2]
            p1, p2 = base_name + '-', base_name + '_N'
            p3, p4 = base_name + '+', base_name + '_P'
            partners = [p for p in [p1, p2, p3, p4] if p in net_lengths and p != net]
            if partners: diff_skews[net] = abs(length - net_lengths[partners[0]])
            else: diff_skews[net] = 999.0 

    return tracks, pads, silkscreen, gnd_zones, vias, net_endpoints, diff_skews, components, net_lengths, layer_depths, board_thickness_mm, board_w, board_h, origin_x, origin_y