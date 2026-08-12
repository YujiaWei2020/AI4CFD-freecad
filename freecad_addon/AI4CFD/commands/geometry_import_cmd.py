"""FreeCAD task panel for geometry import / creation.

Provides entry points into FreeCAD's native geometry tools and file import.
The resulting geometry serves as a visual reference; CFD meshing is handled
separately by OpenFOAM via the Physical Model panels.
"""

import FreeCAD
import FreeCADGui

try:
    from PySide2 import QtWidgets
except ImportError:
    from PySide6 import QtWidgets

# ── Document observer — moves new geometry under AI4CFD_GeoImport ─────────────

_geo_observer = None   # registered at most once per session

# Object types to capture.  PartDesign features live inside a Body automatically,
# so only Body itself is moved.  Part:: covers primitives, booleans, imports, etc.
_GEO_TYPE_PREFIXES = ("Sketcher::", "Part::")
_GEO_TYPE_EXACT    = frozenset({"PartDesign::Body"})


class _GeoImportObserver:
    """Watches for newly created geometry and moves it under AI4CFD_GeoImport."""

    def slotCreatedObject(self, obj):
        # Ignore AI4CFD's own internal objects
        if obj.Name.startswith("AI4CFD_"):
            return
        tid = obj.TypeId
        if not (any(tid.startswith(p) for p in _GEO_TYPE_PREFIXES) or
                tid in _GEO_TYPE_EXACT):
            return
        doc = obj.Document
        if doc is None:
            return
        geo_grp = doc.getObject("AI4CFD_GeoImport")
        if geo_grp is None:
            return
        # Skip if the object is already inside some group (e.g. a PartDesign Body)
        already_grouped = any(
            p.TypeId in ("App::DocumentObjectGroup", "App::Part",
                         "App::FeaturePython", "PartDesign::Body")
            for p in getattr(obj, "InList", [])
        )
        if already_grouped:
            return
        try:
            geo_grp.addObject(obj)
            doc.recompute()
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"AI4CFD: could not place {obj.Name} under Geometry Import: {exc}\n")


def _ensure_observer() -> None:
    global _geo_observer
    if _geo_observer is None:
        _geo_observer = _GeoImportObserver()
        FreeCAD.addDocumentObserver(_geo_observer)


# ── Task panel ────────────────────────────────────────────────────────────────

class GeometryImportPanel:
    """Task panel: import or sketch geometry for CFD reference."""

    def __init__(self, obj_name: str = None) -> None:
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Geometry Import — AI4CFD")
        layout = QtWidgets.QVBoxLayout(self.form)

        layout.addWidget(QtWidgets.QLabel(
            "<b>Import or create geometry for CFD simulation</b><br>"
            "<small>Use FreeCAD's native tools to build a reference geometry, "
            "then configure OpenFOAM parameters in the Physical Model panels.</small>"
        ))
        layout.addSpacing(8)

        # ── Import existing file ──────────────────────────────────────────────
        grp_import = QtWidgets.QGroupBox("Import from file")
        lay_import  = QtWidgets.QVBoxLayout(grp_import)
        lay_import.addWidget(QtWidgets.QLabel(
            "<small>STEP · IGES · STL · BREP · OBJ and other formats supported "
            "by the installed FreeCAD file handlers.</small>"
        ))
        btn_import = QtWidgets.QPushButton("Import Geometry File…")
        btn_import.setStyleSheet("padding: 5px;")
        btn_import.clicked.connect(self._import_file)
        lay_import.addWidget(btn_import)
        layout.addWidget(grp_import)

        # ── Sketch / CAD tools ────────────────────────────────────────────────
        grp_cad = QtWidgets.QGroupBox("Create geometry in FreeCAD")
        lay_cad  = QtWidgets.QVBoxLayout(grp_cad)
        lay_cad.addWidget(QtWidgets.QLabel(
            "<small>Open FreeCAD's parametric-modelling workbenches to draw "
            "or extrude your own geometry.<br>"
            "New objects are automatically placed under <b>Geometry Import</b> "
            "in the tree.</small>"
        ))

        btn_row = QtWidgets.QHBoxLayout()

        btn_part = QtWidgets.QPushButton("Part Design")
        btn_part.setToolTip("Solid modelling with sketches and features")
        btn_part.clicked.connect(lambda: self._activate_wb("PartDesignWorkbench"))
        btn_row.addWidget(btn_part)

        btn_sketch = QtWidgets.QPushButton("Sketcher")
        btn_sketch.setToolTip("Draw a 2-D sketch (profile, cross-section …)")
        btn_sketch.clicked.connect(lambda: self._activate_wb("SketcherWorkbench"))
        btn_row.addWidget(btn_sketch)

        btn_part_wb = QtWidgets.QPushButton("Part")
        btn_part_wb.setToolTip("Primitive shapes, booleans, fillets …")
        btn_part_wb.clicked.connect(lambda: self._activate_wb("PartWorkbench"))
        btn_row.addWidget(btn_part_wb)

        lay_cad.addLayout(btn_row)
        layout.addWidget(grp_cad)

        # ── Note ─────────────────────────────────────────────────────────────
        note = QtWidgets.QLabel(
            "<i>Note: the geometry created here is a visual reference only. "
            "CFD mesh generation is driven by the geometry parameters in "
            "<b>Physical Model → CFD Simulation</b>.</i>"
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #555; font-size: 10px; margin-top: 6px;")
        layout.addWidget(note)

        layout.addStretch()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _import_file(self) -> None:
        _ensure_observer()       # catch imported objects too
        FreeCADGui.Control.closeDialog()
        FreeCADGui.runCommand("Std_Import")

    def _activate_wb(self, wb_name: str) -> None:
        _ensure_observer()       # register before the workbench creates objects
        FreeCADGui.Control.closeDialog()
        try:
            # Clear any AI4CFD selection so Sketcher doesn't try to map onto it
            FreeCADGui.Selection.clearSelection()
            FreeCADGui.activateWorkbench(wb_name)
            if wb_name == "SketcherWorkbench":
                # Open the "New Sketch" dialog — user picks XY / XZ / YZ plane
                FreeCADGui.runCommand("Sketcher_NewSketch")
        except Exception as exc:
            FreeCAD.Console.PrintError(f"Cannot activate {wb_name}: {exc}\n")

    def accept(self) -> None:
        FreeCADGui.Control.closeDialog()

    def reject(self) -> None:
        FreeCADGui.Control.closeDialog()
