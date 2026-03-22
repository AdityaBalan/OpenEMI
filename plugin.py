import pcbnew
import os
from .ui.main_window import OpenEMIGUI

class EMIPlugin(pcbnew.ActionPlugin):
    def defaults(self):
        self.name = "OpenEMI"
        self.category = "EMI Analysis"
        self.description = "Automated EMI Heatmap, SI Checks & PCB DRC"
        self.show_toolbar_button = True
        
        # Tell KiCad where the icon is
        self.icon_file_name = os.path.join(os.path.dirname(__file__), 'icon.png')

    def Run(self):
        frame = OpenEMIGUI()
        frame.Show()