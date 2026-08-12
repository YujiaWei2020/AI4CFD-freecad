"""FreeCAD task panel for CFD Simulation setup.

Two modes:
  1. Load Baseline Model  — pick a pre-built OpenFOAM case from openfoam_base/
  2. Use CfdOF Module     — launch FreeCAD's CfdOF workbench
"""

import os
import glob
import shutil
import subprocess

import FreeCAD
import FreeCADGui

try:
    from PySide2 import QtWidgets, QtCore
except ImportError:
    from PySide6 import QtWidgets, QtCore

from addon_config import _WSL_PROJECT


# ── CfdOF document observer ──────────────────────────────────────────────────

_cfd_observer = None   # registered at most once per session

try:
    from PySide2.QtCore import QTimer
except ImportError:
    from PySide6.QtCore import QTimer


def _is_cfdof_analysis(obj) -> bool:
    """Return True if obj looks like a CfdOF top-level analysis container."""
    tid   = obj.TypeId
    label = getattr(obj, "Label", "")
    return (
        tid == "Fem::FemAnalysis" or
        tid.startswith("CfdOF::") or
        (tid == "App::DocumentObjectGroup" and "CfdAnalysis" in label)
    )


def _place_under_cfd_sim(doc_name: str, obj_name: str) -> None:
    """Place a CfdOF analysis container under AI4CFD_CFDSim."""
    try:
        doc = FreeCAD.getDocument(doc_name)
        if doc is None:
            return
        obj = doc.getObject(obj_name)
        if obj is None:
            return

        cfd_sim = doc.getObject("AI4CFD_CFDSim")
        if cfd_sim is None:
            return
        if not hasattr(cfd_sim, "addObject"):
            FreeCAD.Console.PrintWarning(
                "AI4CFD: AI4CFD_CFDSim has no addObject — "
                "double-click the AI4CFD tree root to rebuild it.\n")
            return
        if obj in getattr(cfd_sim, "Group", []):
            return   # already placed

        # Skip if already inside some non-AI4CFD group container
        for p in getattr(obj, "InList", []):
            if not p.Name.startswith("AI4CFD_") and hasattr(p, "addObject"):
                return

        cfd_sim.addObject(obj)
        doc.recompute()
        FreeCAD.Console.PrintMessage(
            f"AI4CFD: placed '{obj.Label}' under CFD Simulation.\n")
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"AI4CFD: could not place {obj_name} under CFD Simulation: {exc}\n")


def collect_cfdof_from_root(doc) -> None:
    """Move any CfdOF analysis objects sitting at doc root under AI4CFD_CFDSim.

    Called when AI4CFD workbench re-activates so objects added while the user
    was in the CfdOF workbench are captured without needing the real-time observer.
    """
    if doc is None:
        return
    cfd_sim = doc.getObject("AI4CFD_CFDSim")
    if cfd_sim is None or not hasattr(cfd_sim, "addObject"):
        return
    already = set(getattr(cfd_sim, "Group", []))
    moved   = False
    for obj in list(doc.Objects):
        if obj in already or obj.Name.startswith("AI4CFD_"):
            continue
        if not _is_cfdof_analysis(obj):
            continue
        # Only move objects that have no parent outside AI4CFD
        parent_outside = any(
            not p.Name.startswith("AI4CFD_") and hasattr(p, "addObject")
            for p in getattr(obj, "InList", [])
        )
        if parent_outside:
            continue
        try:
            cfd_sim.addObject(obj)
            FreeCAD.Console.PrintMessage(
                f"AI4CFD: moved '{obj.Label}' under CFD Simulation.\n")
            moved = True
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"AI4CFD: could not move {obj.Name}: {exc}\n")
    if moved:
        try:
            doc.recompute()
        except Exception:
            pass


class _CfdSimObserver:
    """Watches for CfdOF analysis containers and nests them under AI4CFD_CFDSim.

    Uses a 300 ms deferred call so CfdOF has time to finish its full command
    (set proxy, add children) before we move the top-level container.
    """

    def slotCreatedObject(self, obj):
        if obj.Name.startswith("AI4CFD_"):
            return
        if _is_cfdof_analysis(obj):
            doc_name, obj_name = obj.Document.Name, obj.Name
            QTimer.singleShot(300, lambda: _place_under_cfd_sim(doc_name, obj_name))


def _ensure_cfd_observer() -> None:
    global _cfd_observer
    if _cfd_observer is None:
        _cfd_observer = _CfdSimObserver()
        FreeCAD.addDocumentObserver(_cfd_observer)


# ── Path helpers ──────────────────────────────────────────────────────────────

def _wsl_to_win(path: str) -> str:
    if path.startswith("/mnt/") and len(path) > 6:
        return f"{path[5].upper()}:{path[6:].replace('/', os.sep)}"
    return path


_BASE_WIN = _wsl_to_win(_WSL_PROJECT + "/openfoam_base")   # Windows path
_BASE_WSL = _WSL_PROJECT + "/openfoam_base"                # WSL path


# ── Case metadata reader ──────────────────────────────────────────────────────

def _list_cases() -> list[str]:
    """Return subdirectory names inside openfoam_base (Windows path)."""
    if not os.path.isdir(_BASE_WIN):
        return []
    return sorted(
        d for d in os.listdir(_BASE_WIN)
        if os.path.isdir(os.path.join(_BASE_WIN, d)) and not d.startswith(".")
    )


def _case_info(case_name: str) -> dict:
    """Read basic metadata from an openfoam_base case directory."""
    case_dir = os.path.join(_BASE_WIN, case_name)
    info = {"solver": "—", "fields": [], "description": ""}

    # Fields: files in 0/ (strip .j2 suffix)
    zero_dir = os.path.join(case_dir, "0")
    if os.path.isdir(zero_dir):
        info["fields"] = sorted(
            os.path.splitext(f)[0]
            for f in os.listdir(zero_dir)
            if not f.startswith(".")
        )

    # Solver: first line matching "application" in system/controlDict(.j2)
    for ctrl in ("system/controlDict", "system/controlDict.j2"):
        ctrl_path = os.path.join(case_dir, ctrl)
        if os.path.isfile(ctrl_path):
            try:
                for line in open(ctrl_path, encoding="utf-8", errors="replace"):
                    stripped = line.strip()
                    if stripped.startswith("application"):
                        # "application  simpleFoam;" → "simpleFoam"
                        info["solver"] = stripped.split()[-1].rstrip(";")
                        break
            except Exception:
                pass
            break

    # README (optional)
    for readme in ("README.md", "README.txt", "README"):
        rp = os.path.join(case_dir, readme)
        if os.path.isfile(rp):
            try:
                info["description"] = open(rp, encoding="utf-8", errors="replace").read(400)
            except Exception:
                pass
            break

    return info


# ── ParaView launcher ──────────────────────────────────────────────────────

def _find_paraview() -> str | None:
    candidates = []
    for root in [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 r"C:\Program Files", r"D:\Program Files"]:
        if root:
            candidates += glob.glob(os.path.join(root, "ParaView*", "bin", "paraview.exe"))
    hit = shutil.which("paraview") or shutil.which("paraview.exe")
    if hit:
        candidates.insert(0, hit)
    candidates = [c for c in candidates if os.path.isfile(c)]
    return sorted(candidates, reverse=True)[0] if candidates else None


# ── Panel ─────────────────────────────────────────────────────────────────────

class CFDSimulationPanel:
    """Task panel: choose between a baseline OpenFOAM case or the CfdOF workbench."""

    def __init__(self, obj_name: str = None) -> None:
        self._obj_name = obj_name

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("CFD Simulation — AI4CFD")
        self.form.setMinimumWidth(420)
        layout = QtWidgets.QVBoxLayout(self.form)

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_baseline_tab(), "Load Baseline Model")
        tabs.addTab(self._build_cfdof_tab(),    "Use CfdOF Module")
        layout.addWidget(tabs)

    # ── Tab 1: Baseline model ─────────────────────────────────────────────────

    def _build_baseline_tab(self) -> QtWidgets.QWidget:
        w   = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setSpacing(8)

        lay.addWidget(QtWidgets.QLabel(
            "<b>Select an OpenFOAM baseline case</b><br>"
            "<small>Pre-built Jinja2-template cases from <tt>openfoam_base/</tt>. "
            "Pick a case to inspect its fields and solver, then open the "
            "directory or launch ParaView.</small>"
        ))

        # ── case list ────────────────────────────────────────────────────────
        list_row = QtWidgets.QHBoxLayout()

        self._case_list = QtWidgets.QListWidget()
        self._case_list.setFixedHeight(110)
        cases = _list_cases()
        for c in cases:
            self._case_list.addItem(c)
        if cases:
            self._case_list.setCurrentRow(0)
        self._case_list.currentTextChanged.connect(self._on_case_selected)
        list_row.addWidget(self._case_list)

        refresh_btn = QtWidgets.QPushButton("↻")
        refresh_btn.setFixedWidth(28)
        refresh_btn.setToolTip("Refresh case list")
        refresh_btn.clicked.connect(self._refresh_cases)
        list_row.addWidget(refresh_btn, 0, QtCore.Qt.AlignTop)
        lay.addLayout(list_row)

        # ── info panel ────────────────────────────────────────────────────────
        info_box = QtWidgets.QGroupBox("Case details")
        info_lay = QtWidgets.QFormLayout(info_box)

        self._lbl_solver = QtWidgets.QLabel("—")
        self._lbl_solver.setStyleSheet("font-weight: bold;")
        info_lay.addRow("Solver:", self._lbl_solver)

        self._lbl_fields = QtWidgets.QLabel("—")
        self._lbl_fields.setWordWrap(True)
        info_lay.addRow("Fields:", self._lbl_fields)

        self._lbl_path = QtWidgets.QLabel("—")
        self._lbl_path.setWordWrap(True)
        self._lbl_path.setStyleSheet("color: #555; font-size: 9px;")
        info_lay.addRow("Path:", self._lbl_path)

        self._desc_edit = QtWidgets.QPlainTextEdit()
        self._desc_edit.setReadOnly(True)
        self._desc_edit.setMaximumHeight(60)
        self._desc_edit.setPlaceholderText("No README found.")
        info_lay.addRow("README:", self._desc_edit)

        lay.addWidget(info_box)

        # ── action buttons ────────────────────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()

        open_btn = QtWidgets.QPushButton("Open Directory")
        open_btn.setToolTip("Open the case folder in Windows Explorer")
        open_btn.clicked.connect(self._open_dir)
        btn_row.addWidget(open_btn)

        pv_btn = QtWidgets.QPushButton("Open in ParaView")
        pv_btn.setToolTip("Create a .foam sentinel and launch ParaView")
        pv_btn.clicked.connect(self._open_paraview)
        btn_row.addWidget(pv_btn)

        lay.addLayout(btn_row)

        # populate details for the first item
        if cases:
            self._on_case_selected(cases[0])

        lay.addStretch()
        return w

    def _on_case_selected(self, case_name: str) -> None:
        if not case_name:
            return
        info = _case_info(case_name)
        self._lbl_solver.setText(info["solver"])
        self._lbl_fields.setText("  ".join(info["fields"]) or "—")
        self._lbl_path.setText(os.path.join(_BASE_WIN, case_name))
        self._desc_edit.setPlainText(info["description"])

    def _refresh_cases(self) -> None:
        current = (self._case_list.currentItem() or QtWidgets.QListWidgetItem("")).text()
        self._case_list.clear()
        cases = _list_cases()
        for c in cases:
            self._case_list.addItem(c)
        # restore selection
        items = self._case_list.findItems(current, QtCore.Qt.MatchExactly)
        if items:
            self._case_list.setCurrentItem(items[0])
        elif cases:
            self._case_list.setCurrentRow(0)

    def _selected_case_dir(self) -> str | None:
        item = self._case_list.currentItem()
        if item is None:
            return None
        return os.path.join(_BASE_WIN, item.text())

    def _open_dir(self) -> None:
        d = self._selected_case_dir()
        if d and os.path.isdir(d):
            os.startfile(d)
        else:
            FreeCAD.Console.PrintWarning("AI4CFD: no case selected or directory not found.\n")

    def _open_paraview(self) -> None:
        d = self._selected_case_dir()
        if not d or not os.path.isdir(d):
            FreeCAD.Console.PrintWarning("AI4CFD: no case selected.\n")
            return
        foam_file = os.path.join(d, "case.foam")
        open(foam_file, "a").close()     # create sentinel if missing
        pv = _find_paraview()
        if not pv:
            FreeCAD.Console.PrintError(
                "AI4CFD: ParaView not found. Install it or add it to PATH.\n")
            return
        subprocess.Popen([pv, foam_file])
        FreeCAD.Console.PrintMessage(f"AI4CFD: ParaView launched for {foam_file}\n")

    # ── Tab 2: CfdOF ─────────────────────────────────────────────────────────

    def _build_cfdof_tab(self) -> QtWidgets.QWidget:
        w   = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(w)
        lay.setSpacing(10)

        lay.addWidget(QtWidgets.QLabel(
            "<b>FreeCAD CfdOF Workbench</b><br>"
            "<small>CfdOF is a FreeCAD workbench that integrates directly with "
            "OpenFOAM. It lets you assign boundary conditions, mesh settings, "
            "and solver parameters directly on the 3D geometry before running "
            "an OpenFOAM case — without editing any text files.</small>"
        ))

        # availability check
        self._cfdof_status = QtWidgets.QLabel()
        self._cfdof_status.setWordWrap(True)
        self._check_cfdof()
        lay.addWidget(self._cfdof_status)

        info_box = QtWidgets.QGroupBox("How it works")
        ib_lay   = QtWidgets.QVBoxLayout(info_box)
        steps = (
            "1. Import or sketch geometry in <b>Geometry Import</b>.",
            "2. Click <b>Launch CfdOF</b> to switch to the CfdOF workbench.",
            "3. Use CfdOF tools to set up the mesh, BCs, and solver.",
            "4. Run the solver directly from CfdOF.",
            "5. Post-process results in ParaView.",
        )
        for step in steps:
            lbl = QtWidgets.QLabel(step)
            lbl.setWordWrap(True)
            ib_lay.addWidget(lbl)
        lay.addWidget(info_box)

        btn_row = QtWidgets.QHBoxLayout()
        launch_btn = QtWidgets.QPushButton("Launch CfdOF Workbench")
        launch_btn.setStyleSheet(
            "background-color: #1565c0; color: white; font-weight: bold; padding: 7px;")
        launch_btn.clicked.connect(self._launch_cfdof)
        btn_row.addWidget(launch_btn)

        install_btn = QtWidgets.QPushButton("CfdOF Install Guide…")
        install_btn.setToolTip("Open the CfdOF GitHub page")
        install_btn.clicked.connect(self._open_cfdof_docs)
        btn_row.addWidget(install_btn)

        lay.addLayout(btn_row)

        collect_btn = QtWidgets.QPushButton("↳  Collect CfdOF Analysis into Tree")
        collect_btn.setToolTip(
            "If a CfdOF analysis container is sitting at the document root, "
            "this button moves it under Physical Model → CFD Simulation.")
        collect_btn.setStyleSheet("margin-top: 6px;")
        collect_btn.clicked.connect(self._collect_cfdof)
        lay.addWidget(collect_btn)

        lay.addStretch()
        return w

    def _collect_cfdof(self) -> None:
        doc = FreeCAD.activeDocument()
        collect_cfdof_from_root(doc)

    def _check_cfdof(self) -> None:
        wbs = FreeCADGui.listWorkbenches()
        if "CfdOF" in wbs or "CfdOFWorkbench" in wbs:
            self._cfdof_status.setText(
                "<span style='color:#2e7d32'>✓  CfdOF is installed.</span>")
            self._cfdof_wb_name = "CfdOF" if "CfdOF" in wbs else "CfdOFWorkbench"
        else:
            self._cfdof_status.setText(
                "<span style='color:#b71c1c'>✗  CfdOF workbench not found. "
                "Install it via the FreeCAD Addon Manager or from GitHub.</span>")
            self._cfdof_wb_name = None

    def _launch_cfdof(self) -> None:
        if self._cfdof_wb_name is None:
            self._check_cfdof()   # re-check in case it was installed since panel opened
        if self._cfdof_wb_name is None:
            FreeCAD.Console.PrintError(
                "AI4CFD: CfdOF not installed. "
                "Use the FreeCAD Addon Manager to install it.\n")
            return
        _ensure_cfd_observer()   # watch for CfdOF objects to nest under CFD Simulation
        self.form.close()        # close floating window before switching workbench
        FreeCADGui.Selection.clearSelection()
        try:
            FreeCADGui.activateWorkbench(self._cfdof_wb_name)
        except Exception as exc:
            FreeCAD.Console.PrintError(f"AI4CFD: cannot launch CfdOF: {exc}\n")

    def _open_cfdof_docs(self) -> None:
        import webbrowser
        webbrowser.open("https://github.com/jaheyns/CfdOF")

    # ── Close (floating window — not a task panel) ────────────────────────────

    def accept(self) -> None:
        self.form.close()

    def reject(self) -> None:
        self.form.close()
