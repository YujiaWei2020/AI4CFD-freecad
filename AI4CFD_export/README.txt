AI4CFD FreeCAD workbench -- export package
===========================================

What's in this zip
-------------------
freecad_addon/AI4CFD/   The FreeCAD workbench (commands, helpers, icons, InitGui.py).
config.py               Shared pipeline configuration (paths, field/channel definitions).
utilities/               Post-processing helpers the workbench calls into (wall distance,
                         VTK export, dataset prep, force/turbulence calculators, etc).
install_to_appdata.ps1  Convenience installer for Windows (see below).

NOT included: dataset/, parametric_study/, cfd_models/ (project-specific CFD case data
and results -- large, and not part of the workbench itself). Copy those separately if
the new machine needs to open the same cases.

Install on the new computer
----------------------------
1. Extract this zip somewhere permanent, e.g.:
     C:\AI4CFD_core          (Windows)
     ~/AI4CFD_core           (Linux/macOS)
   This becomes the "project root" -- config.py and utilities/ must stay next to
   freecad_addon/, since the workbench's helper scripts import them via a relative path.

2. Copy freecad_addon/AI4CFD into FreeCAD's per-user Mod folder:
     Windows: %APPDATA%\FreeCAD\Mod\AI4CFD
     Linux:   ~/.local/share/FreeCAD/Mod/AI4CFD
     macOS:   ~/Library/Application Support/FreeCAD/Mod/AI4CFD
   On Windows you can just run install_to_appdata.ps1 from inside the extracted folder --
   it copies freecad_addon/AI4CFD to %APPDATA%\FreeCAD\Mod\AI4CFD automatically.

3. Start FreeCAD, open Edit -> Preferences -> AI4CFD, and set:
     - Project root        -> the folder from step 1
     - Python executable   -> a native Python (NOT FreeCAD's bundled interpreter) with
                               cadquery, numpy, and scipy installed
   (These are stored per-user by FreeCAD itself, not by the files you copied, so they
   must be set again on every new machine/install.)

4. Make sure the CfdOF workbench is installed and a native OpenFOAM install is configured
   under Edit -> Preferences -> CfdOF -- the "CFD Simulation" panel drives CfdOF directly.

5. Restart FreeCAD, switch to the AI4CFD workbench, and run "Geometry Container" to build
   the model tree.
