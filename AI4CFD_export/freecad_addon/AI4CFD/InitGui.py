"""Registers the AI4CFD workbench with FreeCAD's GUI."""

import os
import sys

import FreeCAD
import FreeCADGui

try:
    _ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
except NameError:
    _ADDON_DIR = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "AI4CFD")

if _ADDON_DIR not in sys.path:
    sys.path.insert(0, _ADDON_DIR)


class AI4CFDWorkbench(FreeCADGui.Workbench):
    MenuText = "AI4CFD"
    ToolTip  = "AI-assisted CFD geometry generation and simulation"

    def __init__(self) -> None:
        from AI4CFDPreferencePage import AI4CFDPreferencePage
        icons_dir = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "AI4CFD", "icons")
        FreeCADGui.addIconPath(icons_dir)
        FreeCADGui.addPreferencePage(AI4CFDPreferencePage, "AI4CFD")

    def Initialize(self) -> None:
        # Define the command class locally so it is a local variable — not a
        # module-level name that FreeCAD's exec() loader clears after startup.
        class GeometryContainerCmd:
            def GetResources(self):
                icon = os.path.join(
                    FreeCAD.getUserAppDataDir(), "Mod", "AI4CFD",
                    "icons", "ai4cfd_logo.svg",
                )
                return {
                    "MenuText": "Geometry Container",
                    "ToolTip":  "Initialise the AI4CFD model tree",
                    "Pixmap":   icon,
                }

            def IsActive(self):
                return True

            def Activated(self):
                doc = FreeCAD.activeDocument() or FreeCAD.newDocument("AI4CFD")
                from tree_objects import build_tree
                build_tree(doc)
                FreeCAD.Console.PrintMessage("AI4CFD model tree initialised.\n")

        from commands.bend_pipe_cmd        import BendPipeCommand
        from commands.parametric_study_cmd import BendPipeParametricCommand

        FreeCADGui.addCommand("AI4CFD_GeometryContainer", GeometryContainerCmd())
        FreeCADGui.addCommand("AI4CFD_BendPipe",           BendPipeCommand())
        FreeCADGui.addCommand("AI4CFD_BendPipeParametric", BendPipeParametricCommand())

        self.appendToolbar("AI4CFD", ["AI4CFD_GeometryContainer"])

        self.appendMenu("AI4CFD", ["AI4CFD_GeometryContainer",
                                   "AI4CFD_BendPipe"])
        self.appendMenu(["AI4CFD", "Parametric Studies"],
                        ["AI4CFD_BendPipeParametric"])

    def Activated(self) -> None:
        doc = FreeCAD.activeDocument()
        if doc is None:
            doc = FreeCAD.newDocument("AI4CFD")
        from tree_objects import build_tree
        build_tree(doc)
        # Always register the CfdOF observer and collect any analysis objects
        # that were created while the user was in the CfdOF workbench.
        try:
            from commands.cfd_sim_cmd import _ensure_cfd_observer, collect_cfdof_from_root
            _ensure_cfd_observer()
            collect_cfdof_from_root(doc)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"AI4CFD: CfdOF observer setup: {exc}\n")

    def Deactivated(self) -> None:
        pass

    def GetClassName(self) -> str:
        return "Gui::PythonWorkbench"


# Set Icon at module scope while the exec namespace is still alive.
try:
    AI4CFDWorkbench.Icon = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "icons", "ai4cfd_logo.svg",
    )
except NameError:
    AI4CFDWorkbench.Icon = os.path.join(
        FreeCAD.getUserAppDataDir(), "Mod", "AI4CFD",
        "icons", "ai4cfd_logo.svg",
    )

FreeCADGui.addWorkbench(AI4CFDWorkbench())
