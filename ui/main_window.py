import pcbnew
import wx
import math
import traceback
import io
import sys

# Block wx.svg before matplotlib loads it (avoids _nanosvg crash in KiCad's wx)
import unittest.mock as mock
sys.modules.setdefault('wx.svg', mock.MagicMock())

import matplotlib
matplotlib.use('Agg')
import matplotlib.patches as patches
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg

from ..core.geometry import point_to_line_dist, make_point, extract_board_geometry
from ..core.analyzer import analyze_bom_for_emi, analyze_advanced_layout_rules
from ..core.heatmap import generate_heatmap_fast


# ===================================================================
# Matplotlib canvas with debounced scroll-to-zoom and drag-to-pan
# ===================================================================
class MatplotlibPanel(wx.Panel):
    def __init__(self, parent, figure):
        super().__init__(parent)
        self.figure        = figure
        self._bitmap       = None
        self._scroll_timer = None
        self._drag_start      = None
        self._drag_xlim_start = None
        self._drag_ylim_start = None

        self.Bind(wx.EVT_PAINT,      self._on_paint)
        self.Bind(wx.EVT_SIZE,       self._on_size)
        self.Bind(wx.EVT_MOUSEWHEEL, self._on_scroll)
        self.Bind(wx.EVT_RIGHT_DOWN, self._on_drag_start)
        self.Bind(wx.EVT_RIGHT_UP,   self._on_drag_end)
        self.Bind(wx.EVT_MOTION,     self._on_drag_move)

    # ------------------------------------------------------------------
    def render(self):
        pw, ph = self.GetSize()
        if pw < 10 or ph < 10:
            return
        dpi = 96
        self.figure.set_size_inches(pw / dpi, ph / dpi)
        agg = FigureCanvasAgg(self.figure)
        agg.draw()
        buf = io.BytesIO()
        agg.print_png(buf)
        buf.seek(0)
        img = wx.Image(buf, wx.BITMAP_TYPE_PNG)
        self._bitmap = wx.Bitmap(img)
        self.Refresh()

    def _on_paint(self, event):
        dc = wx.PaintDC(self)
        if self._bitmap:
            dc.DrawBitmap(self._bitmap, 0, 0)

    def _on_size(self, event):
        self.render()
        event.Skip()

    # ------------------------------------------------------------------
    def _get_ax(self):
        axes = self.figure.get_axes()
        return axes[0] if axes else None

    def data_coords(self, mouse_x, mouse_y):
        """wx pixel → data (mm). Returns (x, y) or (None, None)."""
        ax = self._get_ax()
        if ax is None:
            return None, None
        pw, ph = self.GetSize()
        if pw < 1 or ph < 1:
            return None, None
        fig_x =  mouse_x / pw
        fig_y =  1.0 - mouse_y / ph          # wx y top-down; mpl bottom-up
        bbox  = ax.get_position()
        if not (bbox.x0 <= fig_x <= bbox.x1 and bbox.y0 <= fig_y <= bbox.y1):
            return None, None
        ax_nx = (fig_x - bbox.x0) / bbox.width
        ax_ny = (fig_y - bbox.y0) / bbox.height
        xlim  = ax.get_xlim()
        ylim  = ax.get_ylim()
        return (xlim[0] + ax_nx * (xlim[1] - xlim[0]),
                ylim[0] + ax_ny * (ylim[1] - ylim[0]))

    # ------------------------------------------------------------------
    # Scroll-to-zoom with 80 ms debounce so rapid scrolling stays smooth
    # ------------------------------------------------------------------
    def _on_scroll(self, event):
        ax = self._get_ax()
        if ax is None:
            return

        cx, cy = self.data_coords(event.GetX(), event.GetY())
        if cx is None:                        # cursor outside axes → zoom from centre
            xlim = ax.get_xlim()
            ylim = ax.get_ylim()
            cx   = (xlim[0] + xlim[1]) / 2
            cy   = (ylim[0] + ylim[1]) / 2

        factor = 0.82 if event.GetWheelRotation() > 0 else 1.0 / 0.82
        xlim = ax.get_xlim()
        ylim = ax.get_ylim()
        ax.set_xlim(cx + (xlim[0] - cx) * factor,
                    cx + (xlim[1] - cx) * factor)
        ax.set_ylim(cy + (ylim[0] - cy) * factor,
                    cy + (ylim[1] - cy) * factor)

        # debounce: cancel previous pending render, schedule new one
        if self._scroll_timer and self._scroll_timer.IsRunning():
            self._scroll_timer.Stop()
        self._scroll_timer = wx.CallLater(80, self.render)

    # ------------------------------------------------------------------
    # Right-drag to pan
    # ------------------------------------------------------------------
    def _on_drag_start(self, event):
        ax = self._get_ax()
        if ax is None:
            return
        self._drag_start      = (event.GetX(), event.GetY())
        self._drag_xlim_start = ax.get_xlim()
        self._drag_ylim_start = ax.get_ylim()

    def _on_drag_end(self, event):
        self._drag_start = None

    def _on_drag_move(self, event):
        if not event.RightIsDown() or self._drag_start is None:
            return
        ax = self._get_ax()
        if ax is None:
            return
        pw, ph   = self.GetSize()
        dx_px    = event.GetX() - self._drag_start[0]
        dy_px    = event.GetY() - self._drag_start[1]
        bbox     = ax.get_position()
        ax_w_px  = pw * bbox.width
        ax_h_px  = ph * bbox.height
        xl       = self._drag_xlim_start
        yl       = self._drag_ylim_start
        dx_data  =  dx_px / ax_w_px * (xl[1] - xl[0])
        dy_data  = -dy_px / ax_h_px * (yl[1] - yl[0])   # y flipped
        ax.set_xlim(xl[0] - dx_data, xl[1] - dx_data)
        ax.set_ylim(yl[0] - dy_data, yl[1] - dy_data)

        if self._scroll_timer and self._scroll_timer.IsRunning():
            self._scroll_timer.Stop()
        self._scroll_timer = wx.CallLater(30, self.render)


# ===================================================================
# Main panel
# ===================================================================
class MainEMIPanel(wx.Panel):
    def __init__(self, parent):
        super().__init__(parent)
        self.tracks     = []
        self.pads       = []
        self.components = {}
        self.current_violators       = []
        self.current_bom_suggestions = []
        self.error_markers  = []
        self.board_w  = 0.0
        self.board_h  = 0.0
        self.origin_x = 0.0
        self.origin_y = 0.0
        self._last_Z         = None
        self._last_gnd_zones = []
        self._init_figure()
        self._init_ui()

    # ------------------------------------------------------------------
    def _init_figure(self):
        self.figure = Figure(facecolor='#111111')
        self.figure.subplots_adjust(left=0.07, right=0.88, top=0.93, bottom=0.07)
        self.ax       = self.figure.add_subplot(111)
        self.colorbar = None

    # ------------------------------------------------------------------
    def _init_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)

        title = wx.StaticText(self, label="OpenEMI – Professional Layout Analyzer")
        title.SetFont(wx.Font(14, wx.FONTFAMILY_DEFAULT,
                              wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        root.Add(title, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP | wx.BOTTOM, 12)

        row = wx.BoxSizer(wx.HORIZONTAL)

        # ---- left: log + controls -----------------------------------
        left = wx.BoxSizer(wx.VERTICAL)

        lbl = wx.StaticText(self, label="Solver Log & Action Items")
        lbl.SetFont(wx.Font(10, wx.FONTFAMILY_DEFAULT,
                            wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD))
        left.Add(lbl, 0, wx.LEFT | wx.TOP, 8)

        self.log_box = wx.TextCtrl(
            self, style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2)
        left.Add(self.log_box, 1, wx.EXPAND | wx.ALL, 8)

        hint = wx.StaticText(self,
            label="Scroll = zoom  |  Right-drag = pan  |  Left-click = cross-probe")
        hint.SetFont(wx.Font(8, wx.FONTFAMILY_DEFAULT,
                             wx.FONTSTYLE_ITALIC, wx.FONTWEIGHT_NORMAL))
        hint.SetForegroundColour(wx.Colour(120, 120, 120))
        left.Add(hint, 0, wx.LEFT | wx.BOTTOM, 8)

        op_row = wx.BoxSizer(wx.HORIZONTAL)
        op_row.Add(wx.StaticText(self, label="Heatmap Opacity:"),
                   0, wx.ALIGN_CENTER_VERTICAL | wx.ALL, 4)
        self.opacity_slider = wx.Slider(self, value=75, minValue=0, maxValue=100)
        self.opacity_slider.Bind(wx.EVT_SCROLL, self._on_opacity)
        op_row.Add(self.opacity_slider, 1, wx.EXPAND | wx.ALL, 4)
        left.Add(op_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 4)

        btns = wx.BoxSizer(wx.HORIZONTAL)
        self.analyze_btn = wx.Button(self, label="1. Analyze")
        self.clear_btn   = wx.Button(self, label="2. Clear Board Markers")
        self.reset_btn   = wx.Button(self, label="Reset Zoom")
        self.analyze_btn.Bind(wx.EVT_BUTTON, self._on_analyze)
        self.clear_btn.Bind(wx.EVT_BUTTON,   self._on_clear_all)
        self.reset_btn.Bind(wx.EVT_BUTTON,   self._on_reset_zoom)
        btns.AddStretchSpacer(1)
        btns.Add(self.analyze_btn, 0, wx.RIGHT, 6)
        btns.Add(self.clear_btn,   0, wx.RIGHT, 6)
        btns.Add(self.reset_btn,   0, wx.RIGHT, 8)
        left.Add(btns, 0, wx.EXPAND | wx.TOP | wx.BOTTOM, 8)

        row.Add(left, 1, wx.EXPAND | wx.ALL, 4)

        # ---- right: canvas ------------------------------------------
        self.canvas = MatplotlibPanel(self, self.figure)
        self.canvas.Bind(wx.EVT_LEFT_DOWN, self._on_click)
        row.Add(self.canvas, 2, wx.EXPAND | wx.ALL, 4)

        root.Add(row, 1, wx.EXPAND | wx.ALL, 4)
        self.SetSizer(root)
        self._draw_empty_state()

    # ------------------------------------------------------------------
    def _fit_view(self):
        """Set axis limits tightly around the board with a small margin."""
        if self.board_w == 0 or self.board_h == 0:
            return
        mx = self.board_w * 0.05
        my = self.board_h * 0.05
        self.ax.set_xlim(-mx, self.board_w + mx)
        self.ax.set_ylim(self.board_h + my, -my)   # y-flipped (KiCad coords)

    def _on_reset_zoom(self, event):
        self._fit_view()
        self.canvas.render()

    # ------------------------------------------------------------------
    def _draw_empty_state(self):
        self.ax.clear()
        self.ax.set_facecolor('#111111')
        self.ax.text(0.5, 0.5,
                     "Simulation not yet run.\nClick '1. Analyze' to begin.",
                     color='#888888', fontsize=10, ha='center', va='center',
                     transform=self.ax.transAxes)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        self.canvas.render()

    # ------------------------------------------------------------------
    def _redraw_heatmap(self):
        if self._last_Z is None:
            return

        alpha = self.opacity_slider.GetValue() / 100.0
        self.ax.clear()
        self.ax.set_facecolor('#111111')

        # board outline
        self.ax.add_patch(patches.Rectangle(
            (0, 0), self.board_w, self.board_h,
            linewidth=1.2, edgecolor='#44FF44',
            facecolor='none', zorder=5, linestyle='--'
        ))

        # GND pours
        for z in self._last_gnd_zones:
            self.ax.add_patch(patches.Rectangle(
                (z["x"], z["y"]), z["w"], z["h"],
                facecolor='#1E3A1E', edgecolor='#2A5A2A',
                linewidth=0.4, zorder=0
            ))

        # tracks
        for t in self.tracks:
            lw = max(0.3, t["width"] * 1.5)
            if "GND" in str(t["net"]).upper():
                color, al = '#2A6A2A', 0.7
            elif t["layer"] == pcbnew.F_Cu:
                color, al = '#CC3333', 0.85
            elif t["layer"] == pcbnew.B_Cu:
                color, al = '#3366CC', 0.85
            else:
                color, al = '#887722', 0.5
            self.ax.plot(
                [t["x1"], t["x2"]], [t["y1"], t["y2"]],
                color=color, linewidth=lw, solid_capstyle='round',
                alpha=0.4 if t["is_internal"] else al, zorder=1
            )

        # pads
        for p in self.pads:
            self.ax.add_patch(patches.Rectangle(
                (p["x"] - p["w"] / 2, p["y"] - p["h"] / 2),
                p["w"], p["h"],
                facecolor='#CC8800', edgecolor='#FFB000',
                linewidth=0.3, zorder=2, alpha=0.9
            ))

        # heatmap overlay
        hm = self.ax.imshow(
            self._last_Z,
            extent=[0, self.board_w, self.board_h, 0],
            cmap='inferno', alpha=alpha, zorder=3,
            interpolation='gaussian'
        )

        # violation markers — red X + box
        for v in self.current_violators:
            vx = v.get("max_heat_x", v["abs_x"]) - self.origin_x
            vy = v.get("max_heat_y", v["abs_y"]) - self.origin_y
            self.ax.plot(vx, vy, 'x', color='#FF4444',
                         markersize=9, markeredgewidth=2.0, zorder=6)
            self.ax.add_patch(patches.Rectangle(
                (vx - 0.6, vy - 0.6), 1.2, 1.2,
                linewidth=0.9, edgecolor='#FF4444',
                facecolor='none', zorder=6
            ))

        # BOM markers — orange circle
        for b in self.current_bom_suggestions:
            bx = b["abs_x"] - self.origin_x
            by = b["abs_y"] - self.origin_y
            self.ax.plot(bx, by, 'o', color='#FFA500',
                         markersize=8, markeredgewidth=1.5,
                         fillstyle='none', zorder=6)

        # axes — no set_aspect; _fit_view controls limits directly
        self.ax.set_title("Radiated Emissions Map & Loop Check",
                          color='#DDDDDD', pad=8, fontsize=11, fontweight='bold')
        self.ax.tick_params(colors='#666666', labelsize=7)
        for spine in self.ax.spines.values():
            spine.set_edgecolor('#333333')
        self.ax.set_xlabel("mm", color='#666666', fontsize=8)
        self.ax.set_ylabel("mm", color='#666666', fontsize=8)

        # fit view tightly to board extents
        self.ax.set_aspect('equal', adjustable='box')
        self._fit_view()

        # colorbar
        if self.colorbar is not None:
            try:
                self.colorbar.remove()
            except Exception:
                pass
        self.colorbar = self.figure.colorbar(
            hm, ax=self.ax, shrink=0.75, pad=0.02, label='EMI Risk'
        )
        self.colorbar.ax.yaxis.label.set_color('#888888')
        for lbl in self.colorbar.ax.get_yticklabels():
            lbl.set_color('#888888')

        self.canvas.render()

    # ------------------------------------------------------------------
    def _on_opacity(self, event):
        if self._last_Z is not None:
            self._redraw_heatmap()

    # ------------------------------------------------------------------
    def _on_click(self, event):
        if self.board_w == 0:
            return
        click_x, click_y = self.canvas.data_coords(event.GetX(), event.GetY())
        if click_x is None:
            return

        closest_track, min_td = None, float('inf')
        for t in self.tracks:
            d = point_to_line_dist(click_x, click_y,
                                   t["x1"], t["y1"], t["x2"], t["y2"])
            if d < min_td:
                min_td, closest_track = d, t

        closest_comp, min_cd = None, float('inf')
        for comp_list in self.components.values():
            for c in comp_list:
                d = math.hypot(c["bbox_cx"] - click_x, c["bbox_cy"] - click_y)
                if d < min_cd:
                    min_cd, closest_comp = d, c

        board = pcbnew.GetBoard()
        for fp in board.GetFootprints():
            fp.ClearSelected()
            for pad in fp.Pads():
                pad.ClearSelected()
        for t in board.GetTracks():
            t.ClearSelected()

        selected = False
        hit_r = max(closest_comp["bbox_w"] / 2,
                    closest_comp["bbox_h"] / 2, 2.0) if closest_comp else 0

        if closest_comp and min_cd < hit_r and min_cd <= min_td:
            if closest_comp.get("obj"):
                closest_comp["obj"].SetSelected()
                selected = True
                self._log(f"\n[Cross-Probe] Component: {closest_comp['ref']}\n",
                          wx.Colour(0, 200, 255))

        elif closest_track and min_td < 2.0:
            net_name = str(closest_track["net"])
            if net_name:
                for t in board.GetTracks():
                    if str(t.GetNetname()) == net_name:
                        t.SetSelected()
                for fp in board.GetFootprints():
                    for pad in fp.Pads():
                        if str(pad.GetNetname()) == net_name:
                            pad.SetSelected()
                selected = True
                self._log(f"\n[Cross-Probe] Net: '{net_name}'\n",
                          wx.Colour(0, 200, 255))

        if selected:
            pcbnew.Refresh()

    # ------------------------------------------------------------------
    def _on_clear_all(self, event=None):
        board = pcbnew.GetBoard()
        for m in self.error_markers:
            try:
                board.Remove(m)
            except Exception:
                pass
        self.error_markers.clear()
        pcbnew.Refresh()

    # ------------------------------------------------------------------
    def _on_analyze(self, event):
        self._on_clear_all()
        self.analyze_btn.SetLabel("Simulating...")
        self.analyze_btn.Disable()
        self.log_box.Clear()
        self._log("Extracting geometry & analyzing layout...", wx.Colour(80, 80, 80))
        wx.Yield()
        wx.BeginBusyCursor()

        try:
            (self.tracks, self.pads, silkscreen, gnd_zones, vias,
             net_endpoints, diff_skews, components, net_lengths,
             layer_depths, board_thickness_mm,
             self.board_w, self.board_h,
             self.origin_x, self.origin_y) = extract_board_geometry()

            self.components = components

            Z, heat_violators = generate_heatmap_fast(
                self.tracks, self.pads, gnd_zones, diff_skews,
                components, net_lengths,
                self.board_w, self.board_h,
                self.origin_x, self.origin_y,
                resolution_x=1000
            )
            self.current_bom_suggestions = analyze_bom_for_emi(components)
            advanced_violations = analyze_advanced_layout_rules(
                self.tracks, vias, gnd_zones, net_endpoints,
                layer_depths, board_thickness_mm, components
            )

            unique_v = {}
            for v in heat_violators + advanced_violations:
                key = v["net"] + v["violation"]
                if key not in unique_v or v.get("risk", 0) > unique_v[key].get("risk", 0):
                    unique_v[key] = v
            self.current_violators = sorted(
                unique_v.values(),
                key=lambda x: x.get("risk", 0), reverse=True
            )

            self._last_Z         = Z
            self._last_gnd_zones = gnd_zones

            self._redraw_heatmap()
            self._update_log()
            self._draw_board_markers()

        except Exception:
            wx.MessageBox(f"An error occurred:\n\n{traceback.format_exc()}",
                          "Execution Error", wx.ICON_ERROR)
            self.log_box.Clear()
            self._log("Simulation Failed.", wx.Colour(200, 0, 0))
        finally:
            self.analyze_btn.SetLabel("1. Analyze")
            self.analyze_btn.Enable()
            wx.EndBusyCursor()

    # ------------------------------------------------------------------
    def _draw_board_markers(self):
        self._on_clear_all()
        board = pcbnew.GetBoard()
        half  = int(0.5 * 1e6)
        lw    = int(0.15 * 1e6)

        for b in self.current_bom_suggestions:
            x_iu = int(b["abs_x"] * 1e6)
            y_iu = int(b["abs_y"] * 1e6)
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_CIRCLE)
            s.SetCenter(make_point(x_iu, y_iu))
            s.SetStart(make_point(x_iu + int(1.5e6), y_iu))
            s.SetLayer(pcbnew.Dwgs_User)
            s.SetWidth(lw * 2)
            board.Add(s)
            self.error_markers.append(s)

        for v in self.current_violators:
            x_iu = int(v.get("max_heat_x", v["abs_x"]) * 1e6)
            y_iu = int(v.get("max_heat_y", v["abs_y"]) * 1e6)
            s = pcbnew.PCB_SHAPE(board)
            s.SetShape(pcbnew.SHAPE_T_RECT)
            s.SetStart(make_point(x_iu - half, y_iu - half))
            s.SetEnd(make_point(x_iu + half,   y_iu + half))
            s.SetLayer(pcbnew.Margin)
            s.SetWidth(lw)
            board.Add(s)
            self.error_markers.append(s)

        pcbnew.Refresh()

    # ------------------------------------------------------------------
    def _log(self, text, colour=None):
        if colour:
            self.log_box.SetDefaultStyle(wx.TextAttr(colour))
        self.log_box.AppendText(text)

    ACTION_MAP = {
        "RPD":           "Fix split plane or add GND via near this location.",
        "Split-Plane":   "Fix split plane or add GND via near this location.",
        "Z-Axis":        "Add a GND stitching via next to this signal via.",
        "Edge Fringing": "Move trace inward ~3 mm from board edge.",
        "Crosstalk":     "Space parallel traces apart by at least 3x their width.",
        "Stub":          "Delete the dangling trace segment (prevents RF reflection).",
        "Floating":      "Drop a GND via into this pour, or delete it.",
        "Hot-Loop":      "Move Inductor, IC, and bypass cap closer together.",
        "Skew":          "Re-route diff-pair to equalise lengths.",
        "Bypassing":     "Add a 100 nF cap within 3 mm of the IC power pin.",
        "ESL":           "Replace with 0402 or 0603 footprint to cut loop inductance.",
        "Antenna":       "Add a Ferrite Bead or TVS diode on this connector net.",
    }

    def _action_for(self, text):
        for keyword, action in self.ACTION_MAP.items():
            if keyword in text:
                return action
        return "Review layout near flagged location."

    def _update_log(self):
        self.log_box.Clear()

        if self.current_bom_suggestions:
            self._log(f"COMPONENT & BOM WARNINGS "
                      f"({len(self.current_bom_suggestions)}):\n\n",
                      wx.Colour(220, 120, 0))
            for b in self.current_bom_suggestions:
                self._log(f"■ {b['ref']}  [{b['type']}]\n", wx.Colour(220, 120, 0))
                self._log(f"  → {b['message']}\n\n",        wx.Colour(60, 140, 220))

        if self.current_violators:
            self._log(f"LAYOUT VIOLATIONS "
                      f"({len(self.current_violators)}):\n\n", wx.Colour(210, 40, 40))
            seen = set()
            for v in self.current_violators:
                key = v['net'] + v['violation']
                if key in seen:
                    continue
                seen.add(key)
                risk     = v.get('risk', 0)
                severity = "HIGH" if risk > 100 else "MED" if risk > 40 else "LOW"
                self._log(f"■ [{severity}] Net: {v['net']}\n", wx.Colour(210, 40, 40))
                for clause in v['violation'].split(" & "):
                    self._log(f"  • {clause.strip()}\n", wx.Colour(210, 40, 40))
                self._log(f"  → ACTION: {self._action_for(v['violation'])}\n\n",
                          wx.Colour(60, 140, 220))

        if not self.current_violators and not self.current_bom_suggestions:
            self._log("✓  NO VIOLATIONS DETECTED\n\nNo major EMI or BOM issues found.",
                      wx.Colour(0, 180, 60))


# ===================================================================
# Top-level frame
# ===================================================================
class OpenEMIGUI(wx.Frame):
    def __init__(self):
        super().__init__(None,
                         title="OpenEMI – Professional SI/EMI Analyzer",
                         size=(1300, 780))
        self.SetBackgroundColour(
            wx.SystemSettings.GetColour(wx.SYS_COLOUR_FRAMEBK))
        panel = MainEMIPanel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.Centre()
