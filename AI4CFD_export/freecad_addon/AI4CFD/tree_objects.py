"""Builds the AI4CFD document object tree (Model panel).

Tree structure:
  AI4CFD  (DocumentObjectGroup)
  ├── Geometry Import                      (FeaturePython)
  └── Physical Model                       (DocumentObjectGroup)
      ├── CFD Simulation                   (FeaturePython)
      │   └── Baseline Validation          (FeaturePython)
      └── CFD Parametric Study             (FeaturePython)

Double-clicking any leaf item opens the corresponding task panel.
"""

import os as _os

import FreeCAD
import FreeCADGui

try:
    from PySide2 import QtCore
except ImportError:
    from PySide6 import QtCore

try:
    _ADDON_DIR = _os.path.dirname(_os.path.abspath(__file__))
except NameError:
    _ADDON_DIR = _os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "AI4CFD")

_ICON_DEFAULT  = ":/icons/Part_Feature.svg"
_ICON_COMPLETE = _os.path.join(_ADDON_DIR, "icons", "task_complete.svg")

# ---------------------------------------------------------------------------
# Map: object Name → (module, class [, extra_args…])
# ---------------------------------------------------------------------------
_PANEL_MAP = {
    # Geometry Import
    "AI4CFD_GeoImport":          ("commands.geometry_import_cmd", "GeometryImportPanel"),

    # Physical Model
    # CFDSim uses floating=True: shown as a standalone window so CfdOF child
    # objects can still open their own task dialogs without conflict.
    "AI4CFD_CFDSim":             ("commands.cfd_sim_cmd",          "CFDSimulationPanel",
                                  {"floating": True}),
    "AI4CFD_BaselineCheck":      ("commands.baseline_check_cmd",   "BaselineCheckPanel"),
    "AI4CFD_CFDParam":           ("commands.parametric_study_cmd", "CFDParametricPanel"),
}

# Keeps references to floating windows alive (prevents garbage collection)
_floating_refs = {}

# Object Names from older tree layouts — removed automatically on rebuild
_LEGACY_NAMES = frozenset({
    "AI4CFD_BendPipeTool", "AI4CFD_BendPipeParamTool",
    "AI4CFD_ParamStudies", "AI4CFD_Training",
    "AI4CFD_Prediction",   "AI4CFD_PredictBendPipe",
    "AI4CFD_PredictAirfoil",
    # Individual training step items replaced by AI4CFD_TrainPipeline
    "AI4CFD_Train_VTK", "AI4CFD_Train_WallDist", "AI4CFD_Train_Stats",
    "AI4CFD_Train_Convergence", "AI4CFD_Train_Dataset",
    # Physical AI Training / Physical AI Prediction — feature removed
    "AI4CFD_AITraining", "AI4CFD_TrainPipeline", "AI4CFD_Train_AI",
    "AI4CFD_AIPrediction", "AI4CFD_InferPipeline", "AI4CFD_AIPredict",
    "AI4CFD_ExploreDesign",
})

# Prefixes whose numbered instances (e.g. "AI4CFD_ExploreDesign001") should
# also be swept up by _remove_legacy, not just the exact base name.
_LEGACY_PREFIXES = ("AI4CFD_ExploreDesign",)


# ---------------------------------------------------------------------------
# Proxy classes for FeaturePython tool objects
# ---------------------------------------------------------------------------

class _ToolProxy:
    def __init__(self, obj) -> None:
        obj.Proxy = self

    def execute(self, obj) -> None:
        pass

    def dumps(self):
        return None

    def loads(self, _):
        return None


class _ToolViewProxy:
    def __init__(self, vobj) -> None:
        vobj.Proxy = self  # FreeCAD calls attach(vobj) after this

    def attach(self, vobj) -> None:
        self._vobj = vobj  # called at creation and on document restore

    def doubleClicked(self, vobj) -> bool:
        entry = _PANEL_MAP.get(vobj.Object.Name)
        if entry is None:
            # Numbered instances (e.g. "AI4CFD_ExploreDesign001") created via
            # "New Design Space" share the base task's panel.
            for key, val in _PANEL_MAP.items():
                suffix = vobj.Object.Name[len(key):]
                if vobj.Object.Name.startswith(key) and suffix.isdigit():
                    entry = val
                    break
        if entry is None:
            return False
        mod_name, cls_name = entry[0], entry[1]
        opts = entry[2] if len(entry) > 2 else {}
        import importlib
        mod       = importlib.import_module(mod_name)
        panel_cls = getattr(mod, cls_name)
        panel     = panel_cls(vobj.Object.Name)

        if opts.get("floating"):
            # Show as a standalone window — leaves FreeCADGui.Control free for
            # CfdOF (or other) task dialogs that open on child objects.
            w = panel.form
            w.setWindowFlags(w.windowFlags() | QtCore.Qt.Window)
            w.setAttribute(QtCore.Qt.WA_DeleteOnClose, False)
            _floating_refs[vobj.Object.Name] = panel   # prevent GC
            w.show()
            w.raise_()
            w.activateWindow()
        else:
            # Standard task-panel path — close any lingering dialog first.
            try:
                FreeCADGui.Control.closeDialog()
            except Exception:
                pass
            FreeCADGui.Control.showDialog(panel)
        return True

    def getIcon(self) -> str:
        try:
            if self._vobj.Object.Completed:
                return _ICON_COMPLETE
        except Exception:
            pass
        return _ICON_DEFAULT

    def dumps(self):
        return None

    def loads(self, _):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_or_create_group(doc, name: str, label: str, parent=None):
    for obj in doc.Objects:
        if obj.Name == name:
            return obj
    grp = doc.addObject("App::DocumentObjectGroup", name)
    grp.Label = label
    if parent is not None:
        parent.addObject(grp)
    return grp


def _get_or_create_tool(doc, name: str, label: str, parent):
    for obj in doc.Objects:
        if obj.Name == name:
            if not hasattr(obj, "Completed"):
                obj.addProperty("App::PropertyBool", "Completed", "AI4CFD", "Task completed")
                obj.Completed = False
            return obj
    obj = doc.addObject("App::FeaturePython", name)
    obj.Label = label
    obj.addProperty("App::PropertyBool", "Completed", "AI4CFD", "Task completed")
    obj.Completed = False
    _ToolProxy(obj)
    if hasattr(obj, "ViewObject") and obj.ViewObject is not None:
        _ToolViewProxy(obj.ViewObject)
    parent.addObject(obj)
    return obj


def _get_or_create_geo_tool(doc, name: str, label: str, parent):
    """FeaturePython with GroupExtension — has addObject() AND double-click panel."""
    for obj in doc.Objects:
        if obj.Name == name:
            if not hasattr(obj, "Completed"):
                obj.addProperty("App::PropertyBool", "Completed", "AI4CFD", "Task completed")
                obj.Completed = False
            # Migration: add group extension if the object was created without it
            if not hasattr(obj, "addObject"):
                try:
                    obj.addExtension("App::GroupExtensionPython")
                    if obj.ViewObject:
                        obj.ViewObject.addExtension(
                            "Gui::ViewProviderGroupExtensionPython")
                except Exception:
                    pass
            return obj
    obj = doc.addObject("App::FeaturePython", name)
    obj.Label = label
    obj.addProperty("App::PropertyBool", "Completed", "AI4CFD", "Task completed")
    obj.Completed = False
    obj.addExtension("App::GroupExtensionPython")
    _ToolProxy(obj)
    if hasattr(obj, "ViewObject") and obj.ViewObject is not None:
        obj.ViewObject.addExtension("Gui::ViewProviderGroupExtensionPython")
        _ToolViewProxy(obj.ViewObject)
    parent.addObject(obj)
    return obj


def _remove_legacy(doc) -> None:
    """Remove objects from older tree layouts so they don't clutter the tree."""
    def _is_legacy(name: str) -> bool:
        if name in _LEGACY_NAMES:
            return True
        for prefix in _LEGACY_PREFIXES:
            suffix = name[len(prefix):]
            if name.startswith(prefix) and suffix.isdigit():
                return True
        return False

    to_remove = [o for o in doc.Objects if _is_legacy(o.Name)]
    for obj in to_remove:
        try:
            doc.removeObject(obj.Name)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def mark_task_complete(obj_name: str) -> None:
    """Called by panels when their task finishes successfully."""
    doc = FreeCAD.activeDocument()
    if doc is None:
        return
    obj = doc.getObject(obj_name)
    if obj is None or not hasattr(obj, "Completed"):
        return
    obj.Completed = True
    if hasattr(obj, "ViewObject") and obj.ViewObject is not None:
        try:
            obj.ViewObject.signalChangeIcon()
        except Exception:
            pass
    try:
        doc.recompute()
    except Exception:
        pass


def build_tree(doc) -> None:
    """Idempotent — safe to call every time the workbench activates."""

    _remove_legacy(doc)

    root = _get_or_create_group(doc, "AI4CFD_Root", "AI4CFD")

    # ── Geometry Import (group-capable so user sketches nest inside it) ───────
    _get_or_create_geo_tool(doc, "AI4CFD_GeoImport", "Geometry Import", root)

    # ── Physical Model ────────────────────────────────────────────────────────
    model_grp = _get_or_create_group(doc, "AI4CFD_PhysModel", "Physical Model", root)
    cfd_sim = _get_or_create_geo_tool(doc, "AI4CFD_CFDSim",   "CFD Simulation",       model_grp)
    _get_or_create_tool(doc, "AI4CFD_BaselineCheck", "Baseline Validation",  cfd_sim)
    _get_or_create_tool(doc, "AI4CFD_CFDParam",      "CFD Parametric Study", model_grp)

    doc.recompute()
