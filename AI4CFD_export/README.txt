AI4CFD FreeCAD workbench -- export package
===========================================

What's in this zip
-------------------
freecad_addon/AI4CFD/   The FreeCAD workbench (commands, helpers, icons, InitGui.py).
freecad_addon/AI4CFD/vendor/CfdOF/  A bundled copy of the CfdOF workbench. AI4CFD's
                         InitGui.py registers it automatically on startup, so you do
                         NOT need CfdOF separately installed or on the FreeCAD Addon
                         Manager -- it just works out of the box.
config.py               Shared pipeline configuration (paths, field/channel definitions).
utilities/               Post-processing helpers the workbench calls into (wall distance,
                         VTK export, dataset prep, force/turbulence calculators, etc).
install.bat / install.ps1  One-click installer for Windows (see below).

NOT included: dataset/, parametric_study/, cfd_models/ (project-specific CFD case data
and results -- large, and not part of the workbench itself). Copy those separately if
the new machine needs to open the same cases.

Prerequisites: FreeCAD, and a working OpenFOAM install that CfdOF can find (see CfdOF's
own docs for supported OpenFOAM versions/platforms). CfdOF itself is bundled -- you do
not need to install it separately.

Install on Windows
---------------------------------------------------------------------------------
1. Extract this zip somewhere permanent (this folder becomes the "project root" --
   config.py and utilities/ must stay next to freecad_addon/, since the workbench's
   helper scripts import them via a relative path).

2. Double-click install.bat.
   It will automatically:
     - copy freecad_addon/AI4CFD (including the bundled CfdOF) into
       %APPDATA%\FreeCAD\Mod\AI4CFD
     - find a native Python (not FreeCAD's bundled one) and pip install
       cadquery, numpy, scipy into it
     - find FreeCAD and write the AI4CFD preferences (project root + python exe)
       for you, so nothing needs to be set by hand in Edit -> Preferences

3. Start FreeCAD, switch to the AI4CFD workbench, and run "Geometry Container"
   to build the model tree.

If install.bat can't auto-detect Python or FreeCAD, it will tell you exactly what
to fill in manually (it never guesses silently).

Install on macOS/Linux, or if you'd rather do it by hand
-----------------------------------------------------------
1. Extract this zip to a permanent project folder.
2. Copy freecad_addon/AI4CFD (including its vendor/CfdOF subfolder) into FreeCAD's
   per-user Mod folder:
     Linux:   ~/.local/share/FreeCAD/Mod/AI4CFD
     macOS:   ~/Library/Application Support/FreeCAD/Mod/AI4CFD
3. Start FreeCAD, open Edit -> Preferences -> AI4CFD, and set:
     - Project root      -> the folder from step 1
     - Python executable -> a native Python with cadquery, numpy, scipy installed
4. Restart FreeCAD, switch to the AI4CFD workbench, run "Geometry Container".
