AI4CFD FreeCAD workbench -- export package
===========================================

What's in this zip
-------------------
freecad_addon/AI4CFD/   The FreeCAD workbench (commands, helpers, icons, InitGui.py).
config.py               Shared pipeline configuration (paths, field/channel definitions).
utilities/               Post-processing helpers the workbench calls into (wall distance,
                         VTK export, dataset prep, force/turbulence calculators, etc).
install.bat / install.ps1  One-click installer for Windows (see below).

NOT included: dataset/, parametric_study/, cfd_models/ (project-specific CFD case data
and results -- large, and not part of the workbench itself). Copy those separately if
the new machine needs to open the same cases.

Install on a Windows machine that already has FreeCAD + CfdOF + OpenFOAM set up
---------------------------------------------------------------------------------
1. Extract this zip somewhere permanent (this folder becomes the "project root" --
   config.py and utilities/ must stay next to freecad_addon/, since the workbench's
   helper scripts import them via a relative path).

2. Double-click install.bat.
   It will automatically:
     - copy freecad_addon/AI4CFD into %APPDATA%\FreeCAD\Mod\AI4CFD
     - find a native Python (not FreeCAD's bundled one) and pip install
       cadquery, numpy, scipy into it
     - find FreeCAD and write the AI4CFD preferences (project root + python exe)
       for you, so nothing needs to be set by hand in Edit -> Preferences

3. Start FreeCAD, switch to the AI4CFD workbench, and run "Geometry Container"
   to build the model tree.

If install.bat can't auto-detect Python or FreeCAD, it will tell you exactly what
to fill in manually. If python is not detected, download 3.12.X and add it to path.
Reinstall everything.

Install on macOS/Linux, or if you'd rather do it by hand
-----------------------------------------------------------
1. Extract this zip to a permanent project folder.
2. Copy freecad_addon/AI4CFD into FreeCAD's per-user Mod folder:
     Linux:   ~/.local/share/FreeCAD/Mod/AI4CFD
     macOS:   ~/Library/Application Support/FreeCAD/Mod/AI4CFD
3. Start FreeCAD, open Edit -> Preferences -> AI4CFD, and set:
     - Project root      -> the folder from step 1
     - Python executable -> a native Python with cadquery, numpy, scipy installed
4. Restart FreeCAD, switch to the AI4CFD workbench, run "Geometry Container".
