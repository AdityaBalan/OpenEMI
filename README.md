# OpenEMI: Physics-Informed EMI Risk Analysis for KiCad

OpenEMI is an open-source Python-based KiCad Action Plugin that embeds physics-informed EMI risk analysis directly within the PCB layout editor. It operates entirely on the live PCB database via the KiCad pcbnew API, requiring no geometry export or external solver invocation. 

By providing immediate, actionable EMI feedback during layout iteration, OpenEMI lowers the barrier to EMC-aware PCB design.

## 🚀 Key Features

* **Spatial EMI Risk Heatmap:** Synthesizes a spatially resolved EMI risk heatmap using Gaussian-smoothed grid accumulation. 
* **Live Layout Analysis:** Evaluates signal edge rates, current loop areas, return path continuity, crosstalk coupling, and via-induced discontinuities directly from the board geometry.
* **Advanced Rule Engine:** Detects discrete layout violations including switching hot loops, return path discontinuities, 3W-rule crosstalk violations, via stubs, and floating copper regions.
* **BOM & Component Checks:** Identifies decoupling capacitors placed in physically large footprints (which exhibit elevated ESL), ICs lacking local high-frequency bypass capacitors, and unfiltered I/O connectors.
* **Interactive Cross-Probing:** Supports interactive cross-probing between the heatmap overlay and PCB objects within the KiCad editor.
* **Actionable Diagnostics:** Provides a color-coded textual diagnostic panel with specific remediation actions for each detected violation.
* **Rapid Execution:** The full analysis pipeline executes in under five seconds for a board of moderate complexity, enabling continuous use during the layout phase.

## 🛠️ Installation

OpenEMI relies exclusively on system-installed Python packages and executes within KiCad's embedded Python interpreter.

   Ensure you have the required Python dependencies installed in the environment KiCad uses:
   ```bash
   pip install numpy scipy matplotlib wxPython
