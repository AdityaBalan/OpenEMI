def get_emi_weight(ref, netname):
    ref = str(ref).upper() if ref else ""
    net = str(netname).upper() if netname else ""
    weight = 1.0

    if "GND" in net or "EARTH" in net: return 0.05
    
    if "SW" in net or "LX" in net or "PHASE" in net: weight = 80.0
    elif "CLK" in net or "OSC" in net or "XTAL" in net: weight = 50.0
    elif any(x in net for x in ["TX", "RX", "D0", "MISO", "USB", "HDMI", "PCIE"]) or net.endswith("+") or net.endswith("-") or net.endswith("_P") or net.endswith("_N"): 
        weight = 15.0
    elif "V" in net or "+5" in net or "+3" in net: weight = 5.0

    if ref.startswith('L'): weight *= 25.0
    elif ref.startswith('D'): weight *= 10.0
    elif ref.startswith('U') or ref.startswith('VR'): weight *= 12.0
    elif ref.startswith('Y') or ref.startswith('X'): weight *= 20.0
    elif ref.startswith('J') or ref.startswith('P'): weight *= 5.0

    return weight

def get_edge_rate(ref, netname):
    ref = str(ref).upper() if ref else ""
    net = str(netname).upper() if netname else ""
    tr_ns = 20.0 # Default slow edge rate
    
    if "GND" in net or "EARTH" in net: return 100.0
    
    if any(x in net for x in ["USB", "HDMI", "PCIE", "MIPI", "LVDS", "D0", "TX", "RX"]): 
        tr_ns = 0.5 
    elif any(x in net for x in ["CLK", "OSC", "XTAL", "SPI", "MISO", "MOSI"]): 
        tr_ns = 1.5 
    elif "SW" in net or "LX" in net or "PHASE" in net: 
        tr_ns = 5.0 
    elif "V" in net or "+5" in net or "+3" in net: 
        tr_ns = 100.0 
        
    if ref.startswith('L'): tr_ns = min(tr_ns, 5.0)
    elif ref.startswith('Y') or ref.startswith('X'): tr_ns = min(tr_ns, 1.5)
    
    return tr_ns

def get_cooling_power(ref, value):
    ref = str(ref).upper() if ref else ""
    val = str(value).upper().replace(" ", "") if value else ""
    if not ref.startswith('C'): return 0.0
    
    if any(b in val for b in ['100N', '0.1U', '.1U', '10N', '1N', '47N']): return 20.0  
    elif any(b in val for b in ['1U', '4.7U', '10U', '22U', '47U', '100U']): return 10.0
    return 2.0 

def estimate_cispr_band(netname, length_mm, tr_ns=20.0):
    c_pcb = 1.5e8 
    length_m = max(0.001, length_mm / 1000.0)
    net = str(netname).upper()
    
    if "SW" in net or "LX" in net or "PHASE" in net:
        return "Conducted/Radiated"
        
    f_res_hz = c_pcb / (4.0 * length_m)
    f_knee_hz = 0.5 / (max(0.1, tr_ns) * 1e-9)
    f_eval_mhz = min(f_res_hz, f_knee_hz) / 1e6
    
    if f_eval_mhz < 30.0: return "Conducted (<30MHz)"
    elif 30.0 <= f_eval_mhz <= 230.0: return f"Radiated (~{int(f_eval_mhz)}MHz)"
    else: return f"Radiated (~{f_eval_mhz/1000:.1f}GHz)"