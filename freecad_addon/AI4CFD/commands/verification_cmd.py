"""Verification command — a mesh-density (grid convergence) study.

Reuses CFDParametricPanel's export + batch-run machinery as-is, but instead
of sweeping a spreadsheet-linked geometry parameter it sweeps the CfdOF mesh
object's CharacteristicLengthMax around its current value (half → current →
double, in 3 steps by default) with the geometry held fixed — a classic
mesh-independence check, not a design-space study.
"""

import os

import FreeCAD
import FreeCADGui

from commands.parametric_study_cmd import CFDParametricPanel


class VerificationPanel(CFDParametricPanel):
    """CFDParametricPanel configured as a mesh-density sweep.

    No behavior is overridden — this exists only so tree_objects.py's
    doubleClicked (which instantiates panel_cls(obj_name) with a single
    positional arg) and VerificationCommand both get the same configuration
    without needing to special-case the constructor call site.
    """

    def __init__(self, obj_name: str = None) -> None:
        super().__init__(obj_name, title="Verification — AI4CFD",
                          out_subdir="verification", default_steps=3,
                          sampling_heading="2 · Mesh Density",
                          grid_sweep_only=True,
                          param_heading="1 · Mesh Parameter",
                          mesh_param_source=True,
                          default_n_workers=3)


class VerificationCommand:
    def GetResources(self) -> dict:
        icon = os.path.join(
            FreeCAD.getUserAppDataDir(), "Mod", "AI4CFD",
            "icons", "verification.svg",
        )
        return {
            "MenuText": "Verification",
            "ToolTip":  "Mesh-density (grid convergence) study — re-runs the "
                        "same geometry at half/current/double the mesh's "
                        "characteristic length",
            "Pixmap":   icon,
        }

    def IsActive(self) -> bool:
        return True

    def Activated(self) -> None:
        # Ensure the "Verification" tree object exists so the panel has a
        # place to save/restore its configuration (see CFDParametricPanel's
        # _save_state/_restore_state) — matches the object opened when this
        # same tree item is later double-clicked.
        doc = FreeCAD.activeDocument() or FreeCAD.newDocument("AI4CFD")
        from tree_objects import build_tree
        build_tree(doc)
        FreeCADGui.Control.showDialog(VerificationPanel("AI4CFD_Verification"))
