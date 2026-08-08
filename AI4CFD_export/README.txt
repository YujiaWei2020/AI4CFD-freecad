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


Install on Windows
---------------------------------------------------------------------------------
1. Extract this zip somewhere (this folder becomes the "project root" --
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


