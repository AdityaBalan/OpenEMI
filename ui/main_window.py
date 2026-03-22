import pcbnew
import wx
import math
import traceback
import matplotlib.patches as patches

from matplotlib.figure import Figure
from matplotlib.backends.backend_wxagg import FigureCanvasWxAgg as FigureCanvas

# Import the core engine tools using relative imports
from ..core.geometry import point_to_line_dist, make_point, extract_board_geometry
from ..core.analyzer import analyze_bom_for_emi, analyze_advanced_layout_rules
from ..core.heatmap import generate_heatmap_fast

class MainEMIPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.tracks, self.pads, self.components = [], [], {}
        self.current_violators, self.current_bom_suggestions = [], []
        self.error_markers = []
        self.init_ui()

    def init_ui(self):
        main_sizer = wx.BoxSizer(wx.VERTICAL)
        header_sizer = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(self, label="OpenEMI – Professional Layout Analyzer")
        title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        header_sizer.Add(title, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP | wx.BOTTOM, 15)
        main_sizer.Add(header_sizer, 0, wx.EXPAND)

        content_sizer = wx.BoxSizer(wx.HORIZONTAL)
        
        left_sizer = wx.BoxSizer(wx.VERTICAL)
        lbl_title = wx.StaticText(self, label="Solver Log & Action Items")
        lbl_title.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        left_sizer.Add(lbl_title, 0, wx.LEFT | wx.TOP, 10)
        
        self.log_box = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        left_sizer.Add(self.log_box, 1, wx.EXPAND | wx.ALL, 10)
        
        op_sizer = wx.BoxSizer(wx.HORIZONTAL)
        op_label = wx.StaticText(self, label="Heatmap Opacity:")
        self.opacity_slider = wx.Slider(self, value=75, minValue=0, maxValue=100, style=wx.SL_HORIZONTAL)
        self.opacity_slider.Bind(wx.EVT_SCROLL, self.on_opacity_change)
        op_sizer.Add(op_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 5)
        op_sizer.Add(self.opacity_slider, 1, wx.EXPAND | wx.ALL, 5)
        left_sizer.Add(op_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 5)

        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)
        self.analyze_btn = wx.Button(self, label="1. Analyze")
        self.clear_btn = wx.Button(self, label="2. Clear Board Markers")
        
        self.analyze_btn.Bind(wx.EVT_BUTTON, self.on_analyze)
        self.clear_btn.Bind(wx.EVT_BUTTON, self.on_clear_all)
        
        btn_sizer.AddStretchSpacer(1)
        btn_sizer.Add(self.analyze_btn, 0, wx.RIGHT, 10)
        btn_sizer.Add(self.clear_btn, 0, wx.RIGHT, 10)
        left_sizer.Add(btn_sizer, 0, wx.EXPAND | wx.BOTTOM | wx.TOP, 10)
        
        right_sizer = wx.BoxSizer(wx.VERTICAL)
        self.figure = Figure(facecolor='#111111') 
        self.figure.subplots_adjust(left=0.08, right=0.95, top=0.92, bottom=0.08)
        self.canvas = FigureCanvas(self, -1, self.figure)
        
        # BINDING CLICK EVENT
        self.canvas.mpl_connect('button_press_event', self.on_click)
        
        self.ax = self.figure.add_subplot(111)
        self.colorbar = None
        self.heatmap_img = None 
        right_sizer.Add(self.canvas, 1, wx.EXPAND | wx.ALL, 5)

        content_sizer.Add(left_sizer, 1, wx.EXPAND | wx.ALL, 5)   
        content_sizer.Add(right_sizer, 2, wx.EXPAND | wx.ALL, 5)  
        
        main_sizer.Add(content_sizer, 1, wx.EXPAND | wx.ALL, 5)
        self.SetSizer(main_sizer)
        self.draw_empty_state()

    def draw_empty_state(self):
        self.ax.clear()
        self.ax.set_facecolor('#111111')
        self.ax.text(0.5, 0.5, "Simulation not yet run.\nClick '1. Analyze' to begin.", color='#888888', fontsize=10, ha='center', va='center', transform=self.ax.transAxes)
        self.ax.set_xticks([]); self.ax.set_yticks([])
        self.canvas.draw()

    def on_opacity_change(self, event):
        if self.heatmap_img:
            self.heatmap_img.set_alpha(self.opacity_slider.GetValue() / 100.0)
            self.canvas.draw_idle()

    def on_click(self, event):
        if event.inaxes != self.ax: return
        if event.xdata is None or event.ydata is None: return

        click_x, click_y = event.xdata, event.ydata
        
        closest_track = None
        min_track_dist = float('inf')
        for t in self.tracks:
            dist = point_to_line_dist(click_x, click_y, t["x1"], t["y1"], t["x2"], t["y2"])
            if dist < min_track_dist: 
                min_track_dist = dist
                closest_track = t

        closest_comp = None
        min_comp_dist = float('inf')
        for prefix, comp_list in self.components.items():
            for c in comp_list:
                dist = math.hypot(c["bbox_cx"] - click_x, c["bbox_cy"] - click_y)
                if dist < min_comp_dist:
                    min_comp_dist = dist
                    closest_comp = c

        board = pcbnew.GetBoard()

        # Deselect all currently selected items in KiCad
        for fp in board.GetFootprints():
            fp.ClearSelected()
            for pad in fp.Pads():
                pad.ClearSelected()
        for t in board.GetTracks():
            t.ClearSelected()

        selected = False

        # Determine hit radius for component based on its bounding box
        comp_hit_radius = max(closest_comp["bbox_w"]/2, closest_comp["bbox_h"]/2, 2.0) if closest_comp else 0

        # Logic: If we clicked closer to a component center than a track, select the component
        if closest_comp and min_comp_dist < comp_hit_radius and min_comp_dist <= min_track_dist:
            if "obj" in closest_comp and closest_comp["obj"]:
                closest_comp["obj"].SetSelected()
                selected = True
                self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(0, 200, 255)))
                self.log_box.AppendText(f"\n[Cross-Probe] Highlighted Component: {closest_comp['ref']} in KiCad.\n")

        # Logic: Otherwise, if we clicked near a track (within 2.0mm tolerance), select the whole net
        elif closest_track and min_track_dist < 2.0:
            net_name = str(closest_track["net"])
            if net_name:
                # Select all tracks on the net
                for t in board.GetTracks():
                    if str(t.GetNetname()) == net_name:
                        t.SetSelected()
                
                # Select all pads on the net to make the signal path obvious
                for fp in board.GetFootprints():
                    for pad in fp.Pads():
                        if str(pad.GetNetname()) == net_name:
                            pad.SetSelected()

                selected = True
                self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(0, 200, 255)))
                self.log_box.AppendText(f"\n[Cross-Probe] Highlighted full Net: '{net_name}' in KiCad.\n")

        if selected:
            pcbnew.Refresh()

    def clear_error_markers(self):
        board = pcbnew.GetBoard()
        for m in self.error_markers:
            try: board.Remove(m)
            except: pass
        self.error_markers.clear()

    def on_clear_all(self, event=None):
        self.clear_error_markers()
        pcbnew.Refresh()

    def draw_errors(self):
        self.clear_error_markers()
        board = pcbnew.GetBoard()
        box_half_size = int(0.5 * 1e6); line_width = int(0.15 * 1e6)
        
        for b in self.current_bom_suggestions:
            x_iu, y_iu = int(b["abs_x"] * 1e6), int(b["abs_y"] * 1e6)
            warn = pcbnew.PCB_SHAPE(board)
            warn.SetShape(pcbnew.SHAPE_T_CIRCLE)
            warn.SetCenter(make_point(x_iu, y_iu))
            warn.SetStart(make_point(x_iu + int(1.5 * 1e6), y_iu)) 
            warn.SetLayer(pcbnew.Dwgs_User); warn.SetWidth(line_width * 2)
            board.Add(warn); self.error_markers.append(warn)

        for v in self.current_violators:
            x_iu = int(v.get("max_heat_x", v["abs_x"]) * 1e6)
            y_iu = int(v.get("max_heat_y", v["abs_y"]) * 1e6)
            box = pcbnew.PCB_SHAPE(board)
            box.SetShape(pcbnew.SHAPE_T_RECT)
            box.SetStart(make_point(x_iu - box_half_size, y_iu - box_half_size))
            box.SetEnd(make_point(x_iu + box_half_size, y_iu + box_half_size))
            box.SetLayer(pcbnew.Margin); box.SetWidth(line_width)
            board.Add(box); self.error_markers.append(box)
        pcbnew.Refresh()

    def update_log_box(self):
        self.log_box.Clear()
        
        if self.current_bom_suggestions:
            self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(200, 100, 0))) 
            self.log_box.AppendText(f"COMPONENT & BOM WARNINGS ({len(self.current_bom_suggestions)}):\n\n")
            for b in self.current_bom_suggestions:
                self.log_box.AppendText(f"■ {b['ref']} [{b['type']}]\n")
                self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(0, 100, 200))) 
                self.log_box.AppendText(f"  → ACTION: {b['message']}\n\n")
                self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(200, 100, 0)))

        if self.current_violators:
            self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(200, 0, 0))) 
            self.log_box.AppendText(f"LAYOUT VIOLATIONS ({len(self.current_violators)}):\n\n")
            seen_nets = set()
            for v in self.current_violators:
                unique_key = v['net'] + v['violation']
                if unique_key not in seen_nets:
                    v_str = "\n  • ".join(v['violation'].split(" & "))
                    self.log_box.AppendText(f"■ Net: {v['net']}\n  • {v_str}\n")
                    
                    self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(0, 100, 200))) 
                    
                    x_c, y_c = v.get("max_heat_x", v["abs_x"]), v.get("max_heat_y", v["abs_y"])
                    
                    if "RPD" in v['violation'] or "Split-Plane" in v['violation']:
                        self.log_box.AppendText(f"  → ACTION: Fix split plane or add GND via near X:{x_c:.1f}, Y:{y_c:.1f}\n")
                    elif "Z-Axis" in v['violation']:
                        self.log_box.AppendText(f"  → ACTION: Add a GND stitching via near this signal via to close the 3D loop area.\n")
                    elif "Edge Fringing" in v['violation']:
                        self.log_box.AppendText(f"  → ACTION: Move trace inward ~3mm from edge.\n")
                    elif "Crosstalk" in v['violation']: 
                        self.log_box.AppendText("  → ACTION: Space parallel traces apart by at least 3x their width.\n")
                    elif "Stub" in v['violation']: 
                        self.log_box.AppendText("  → ACTION: Delete the dangling trace segment to prevent RF reflection.\n")
                    elif "Floating" in v['violation']: 
                        self.log_box.AppendText("  → ACTION: Drop a GND via into this orphaned pour, or delete it.\n")
                    elif "Hot-Loop" in v['violation']: 
                        self.log_box.AppendText("  → ACTION: Move the Inductor, IC, and Input/Output Capacitor closer together to minimize the high di/dt loop area.\n")
                    self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(200, 0, 0)))
                        
                    self.log_box.AppendText("\n")
                    seen_nets.add(unique_key)
                    
        if not self.current_violators and not self.current_bom_suggestions:
            self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(0, 150, 0))) 
            self.log_box.AppendText("NO ERRORS!\n\nNo major EMI or BOM violations detected.")

    def on_analyze(self, event):
        self.on_clear_all() 
        self.analyze_btn.SetLabel("Simulating...")
        self.analyze_btn.Disable()
        self.log_box.Clear()
        self.log_box.SetDefaultStyle(wx.TextAttr(wx.Colour(50, 50, 50)))
        self.log_box.AppendText("Extracting geometry & analyzing layout...")
        wx.Yield() 
        wx.BeginBusyCursor()

        try:
            self.tracks, self.pads, silkscreen, gnd_zones, vias, net_endpoints, diff_skews, components, net_lengths, layer_depths, board_thickness_mm, self.board_w, self.board_h, self.origin_x, self.origin_y = extract_board_geometry()
            
            self.components = components

            Z, heat_violators = generate_heatmap_fast(self.tracks, self.pads, gnd_zones, diff_skews, components, net_lengths, self.board_w, self.board_h, self.origin_x, self.origin_y, resolution_x=1000)
            self.current_bom_suggestions = analyze_bom_for_emi(components)
            advanced_violations = analyze_advanced_layout_rules(self.tracks, vias, gnd_zones, net_endpoints, layer_depths, board_thickness_mm, components)

            unique_v = {}
            for v in (heat_violators + advanced_violations):
                key = v["net"] + v["violation"]
                if key not in unique_v or v.get("risk", 0) > unique_v[key].get("risk", 0):
                    unique_v[key] = v
            self.current_violators = list(unique_v.values())
            self.current_violators.sort(key=lambda x: x.get("risk", 0), reverse=True)

            self.ax.clear() 
            self.ax.set_facecolor('#111111') 

            for z in gnd_zones:
                self.ax.add_patch(patches.Rectangle((z["x"], z["y"]), z["w"], z["h"], facecolor='#2A2A2A', edgecolor='none', zorder=0))

            for t in self.tracks:
                lw = max(0.5, t["width"]) 
                if "GND" in str(t["net"]).upper(): color = '#333333' 
                elif t["layer"] == pcbnew.F_Cu: color = '#992222' 
                elif t["layer"] == pcbnew.B_Cu: color = '#225588' 
                else: color = '#555522' 
                self.ax.plot([t["x1"], t["x2"]], [t["y1"], t["y2"]], color=color, linewidth=lw, solid_capstyle='round', alpha=0.5 if t["is_internal"] else 0.85, zorder=1)

            for p in self.pads:
                self.ax.add_patch(patches.Rectangle((p["x"] - p["w"]/2, p["y"] - p["h"]/2), p["w"], p["h"], facecolor='#CC8800', edgecolor='none', zorder=2, alpha=0.9))

            self.heatmap_img = self.ax.imshow(Z, extent=[0, self.board_w, self.board_h, 0], cmap="inferno", alpha=self.opacity_slider.GetValue()/100.0, zorder=3, interpolation='gaussian')

            self.ax.set_title("Radiated Emissions Map & Loop Check", color='#DDDDDD', pad=10, fontsize=12, fontweight='bold')
            self.ax.set_aspect('equal')
            self.ax.set_xlim(0, self.board_w)
            self.ax.set_ylim(self.board_h, 0)
            self.ax.set_xticks([])
            self.ax.set_yticks([])

            if self.colorbar is not None: self.colorbar.remove()
            self.colorbar = self.figure.colorbar(self.heatmap_img, ax=self.ax, shrink=0.8, pad=0.02)
            for label in self.colorbar.ax.axes.get_yticklabels(): label.set_color('#888888')

            self.canvas.draw()
            
            self.update_log_box()
            self.draw_errors()
            
        except Exception as e:
            wx.MessageBox(f"An error occurred:\n\n{traceback.format_exc()}", "Execution Error", wx.ICON_ERROR)
            self.log_box.Clear()
            self.log_box.AppendText("Simulation Failed.")
        finally:
            self.analyze_btn.SetLabel("1. Analyze")
            self.analyze_btn.Enable()
            wx.EndBusyCursor()

class OpenEMIGUI(wx.Frame):
    def __init__(self):
        super().__init__(None, title="OpenEMI – Professional SI/EMI Analyzer", size=(1200, 750))
        self.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_FRAMEBK))
        
        panel = MainEMIPanel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.Centre()