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

_VENDOR_CFDOF_DIR = os.path.join(_ADDON_DIR, "vendor", "CfdOF")
if os.path.isdir(_VENDOR_CFDOF_DIR) and _VENDOR_CFDOF_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_CFDOF_DIR)


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

        from commands.parametric_study_cmd import BendPipeParametricCommand
        from commands.verification_cmd import VerificationCommand

        FreeCADGui.addCommand("AI4CFD_GeometryContainer", GeometryContainerCmd())
        FreeCADGui.addCommand("AI4CFD_BendPipeParametric", BendPipeParametricCommand())
        FreeCADGui.addCommand("AI4CFD_Verification", VerificationCommand())

        self.appendToolbar("AI4CFD", ["AI4CFD_GeometryContainer", "AI4CFD_Verification"])

        self.appendMenu("AI4CFD", ["AI4CFD_GeometryContainer"])
        self.appendMenu(["AI4CFD", "Parametric Studies"],
                        ["AI4CFD_BendPipeParametric"])
        self.appendMenu("AI4CFD", ["AI4CFD_Verification"])

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

# Register the bundled CfdOF workbench (vendored under vendor/CfdOF/) so it's
# available with no separate CfdOF install. CfdOF's Init.py/InitGui.py are
# written to be exec()'d by FreeCAD's own Mod-folder loader — which injects
# FreeCAD/FreeCADGui/Workbench as globals — so we replicate that here rather
# than hand-porting their registration logic (keeps the vendored files
# byte-for-byte diffable against upstream).
#
# Skipped entirely if a standalone CfdOF addon is also installed
# (Mod/CfdOF/, e.g. via the Addon Manager) — FreeCAD's own Mod-folder loader
# already execs THAT copy's InitGui.py normally, which calls
# FreeCADGui.addPreferencePage(CfdPreferencePage, "CfdOF"). Loading our
# vendored copy on top would call the same addPreferencePage with a second,
# distinct CfdPreferencePage class object, and FreeCAD doesn't deduplicate
# preference pages by category+title — only by object identity — so the
# "CfdOF" category ends up with two "General" entries instead of one.
_STANDALONE_CFDOF_INITGUI = os.path.join(
    FreeCAD.getUserAppDataDir(), "Mod", "CfdOF", "InitGui.py")

if os.path.isdir(_VENDOR_CFDOF_DIR) and not os.path.isfile(_STANDALONE_CFDOF_INITGUI):
    try:
        _cfdof_ns = {
            "FreeCAD": FreeCAD,
            "FreeCADGui": FreeCADGui,
            "Workbench": FreeCADGui.Workbench,
        }
        for _script in ("Init.py", "InitGui.py"):
            _path = os.path.join(_VENDOR_CFDOF_DIR, _script)
            with open(_path, encoding="utf-8") as _fh:
                exec(compile(_fh.read(), _path, "exec"), _cfdof_ns)
        FreeCAD.Console.PrintMessage("AI4CFD: bundled CfdOF workbench registered.\n")
    except Exception as exc:
        FreeCAD.Console.PrintWarning(f"AI4CFD: bundled CfdOF failed to load: {exc}\n")
elif os.path.isfile(_STANDALONE_CFDOF_INITGUI):
    FreeCAD.Console.PrintMessage(
        "AI4CFD: a standalone CfdOF addon is already installed (Mod/CfdOF) — "
        "skipping the bundled copy to avoid duplicate registration (e.g. two "
        "'CfdOF > General' preference pages).\n")

FreeCADGui.addWorkbench(AI4CFDWorkbench())
