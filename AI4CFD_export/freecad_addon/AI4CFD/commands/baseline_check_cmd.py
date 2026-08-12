"""FreeCAD task panel for Baseline Validation.

Two steps:
  Step 1 — Set the baseline model directory (OpenFOAM case or CfdOF output).
  Step 2 — Confirm the model is physically correct and ready for parametric study.
"""

import os
import subprocess

import FreeCAD
import FreeCADGui

try:
    from PySide2 import QtWidgets, QtCore
except ImportError:
    from PySide6 import QtWidgets, QtCore


_STATUS = {
    "idle":    ("●", "#888888"),
    "ok":      ("✓", "#2e7d32"),
}

_CHECKS = [
    ("mesh_ok",    "Mesh quality verified"),
    ("bc_ok",      "Boundary conditions correct"),
    ("converged",  "Solver converged"),
    ("validated",  "Results validated against reference / experiment"),
]


def _read_case_summary(case_dir: str) -> str:
    """Return a short human-readable summary of an OpenFOAM case directory."""
    lines = []
    ctrl = os.path.join(case_dir, "system", "controlDict")
    if not os.path.isfile(ctrl):
        # CfdOF split layout: case/ sub-directory
        ctrl = os.path.join(case_dir, "case", "system", "controlDict")
    if os.path.isfile(ctrl):
        try:
            text = open(ctrl, encoding="utf-8", errors="replace").read()
            for key in ("application", "endTime", "deltaT"):
                for ln in text.splitlines():
                    ln = ln.strip()
                    if ln.startswith(key) and not ln.startswith("//"):
                        val = ln.split()
                        if len(val) >= 2:
                            lines.append(f"{key}: {val[1].rstrip(';')}")
                        break
        except Exception:
            pass
    readme = os.path.join(case_dir, "README.md")
    if os.path.isfile(readme):
        try:
            first = [l.strip() for l in open(readme, encoding="utf-8",
                     errors="replace").readlines() if l.strip()][:3]
            lines.extend(first)
        except Exception:
            pass
    return "\n".join(lines) if lines else "(no summary available)"


class BaselineCheckPanel:
    """Task panel: Baseline Validation — two-step directory + confirmation."""

    def __init__(self, obj_name: str = None):
        self._obj_name = obj_name

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Baseline Validation — AI4CFD")
        self.form.setMinimumWidth(400)

        layout = QtWidgets.QVBoxLayout(self.form)
        layout.setSpacing(10)

        # ── Step 1: Baseline directory ────────────────────────────────────────
        s1_box = QtWidgets.QGroupBox("Step 1 — Baseline Model Directory")
        s1_lay = QtWidgets.QVBoxLayout(s1_box)

        dir_row = QtWidgets.QHBoxLayout()
        self._dir_edit = QtWidgets.QLineEdit()
        self._dir_edit.setPlaceholderText("Path to baseline OpenFOAM case…")
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.setFixedWidth(72)
        browse_btn.clicked.connect(self._browse)
        dir_row.addWidget(self._dir_edit, 1)
        dir_row.addWidget(browse_btn)
        s1_lay.addLayout(dir_row)

        self._summary = QtWidgets.QPlainTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setMaximumHeight(80)
        self._summary.setPlaceholderText("Case summary appears here after selecting a directory.")
        self._summary.setStyleSheet("font-size: 9px; color: #444;")
        s1_lay.addWidget(self._summary)

        s1_btn_row = QtWidgets.QHBoxLayout()
        self._s1_status = QtWidgets.QLabel("●")
        self._s1_status.setStyleSheet(f"color: {_STATUS['idle'][1]}; font-size: 16px;")
        self._s1_status.setFixedWidth(22)
        set_btn = QtWidgets.QPushButton("Set Directory")
        set_btn.setStyleSheet(
            "background-color: #1565c0; color: white; font-weight: bold; padding: 4px 10px;")
        set_btn.clicked.connect(self._set_directory)
        open_btn = QtWidgets.QPushButton("Open in Explorer")
        open_btn.clicked.connect(self._open_explorer)
        s1_btn_row.addWidget(self._s1_status)
        s1_btn_row.addWidget(set_btn)
        s1_btn_row.addWidget(open_btn)
        s1_btn_row.addStretch()
        s1_lay.addLayout(s1_btn_row)

        layout.addWidget(s1_box)

        # ── Step 2: Physics confirmation ──────────────────────────────────────
        s2_box = QtWidgets.QGroupBox("Step 2 — Physics Confirmation")
        s2_lay = QtWidgets.QVBoxLayout(s2_box)

        self._checkboxes = {}
        for key, label in _CHECKS:
            cb = QtWidgets.QCheckBox(label)
            s2_lay.addWidget(cb)
            self._checkboxes[key] = cb

        notes_lbl = QtWidgets.QLabel("Notes (optional):")
        notes_lbl.setStyleSheet("font-size: 9px; color: #555;")
        s2_lay.addWidget(notes_lbl)
        self._notes = QtWidgets.QPlainTextEdit()
        self._notes.setMaximumHeight(60)
        self._notes.setPlaceholderText("Any validation notes or comments…")
        s2_lay.addWidget(self._notes)

        s2_btn_row = QtWidgets.QHBoxLayout()
        self._s2_status = QtWidgets.QLabel("●")
        self._s2_status.setStyleSheet(f"color: {_STATUS['idle'][1]}; font-size: 16px;")
        self._s2_status.setFixedWidth(22)
        confirm_btn = QtWidgets.QPushButton("Confirm & Mark Ready")
        confirm_btn.setStyleSheet(
            "background-color: #2e7d32; color: white; font-weight: bold; padding: 4px 10px;")
        confirm_btn.clicked.connect(self._confirm)
        s2_btn_row.addWidget(self._s2_status)
        s2_btn_row.addWidget(confirm_btn)
        s2_btn_row.addStretch()
        s2_lay.addLayout(s2_btn_row)

        layout.addWidget(s2_box)
        layout.addStretch()

        # Restore saved state from the FreeCAD object
        self._load_saved_state()

    # ── Step 1 ────────────────────────────────────────────────────────────────

    def _browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self.form, "Select Baseline Model Directory",
            self._dir_edit.text() or "")
        if d:
            self._dir_edit.setText(d)
            self._summary.setPlainText(_read_case_summary(d))

    def _set_directory(self):
        d = self._dir_edit.text().strip()
        if not d or not os.path.isdir(d):
            QtWidgets.QMessageBox.warning(
                self.form, "Invalid Directory",
                "Please select a valid directory first.")
            return
        self._summary.setPlainText(_read_case_summary(d))
        self._s1_status.setText("✓")
        self._s1_status.setStyleSheet(f"color: {_STATUS['ok'][1]}; font-size: 16px;")
        self._save_dir(d)

    def _open_explorer(self):
        d = self._dir_edit.text().strip()
        if d and os.path.isdir(d):
            try:
                subprocess.Popen(["explorer", os.path.normpath(d)])
            except Exception:
                pass

    def _save_dir(self, path: str):
        if not self._obj_name:
            return
        doc = FreeCAD.activeDocument()
        if doc is None:
            return
        obj = doc.getObject(self._obj_name)
        if obj is None:
            return
        if not hasattr(obj, "BaselineDir"):
            obj.addProperty("App::PropertyString", "BaselineDir",
                            "AI4CFD", "Baseline model directory")
        obj.BaselineDir = path

    # ── Step 2 ────────────────────────────────────────────────────────────────

    def _confirm(self):
        unchecked = [label for key, label in _CHECKS
                     if not self._checkboxes[key].isChecked()]
        if unchecked:
            msg = "The following items are not yet confirmed:\n\n" + \
                  "\n".join(f"  • {l}" for l in unchecked) + \
                  "\n\nConfirm anyway?"
            reply = QtWidgets.QMessageBox.question(
                self.form, "Incomplete Checklist", msg,
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
            if reply != QtWidgets.QMessageBox.Yes:
                return

        self._s2_status.setText("✓")
        self._s2_status.setStyleSheet(f"color: {_STATUS['ok'][1]}; font-size: 16px;")
        self._mark_complete()
        QtWidgets.QMessageBox.information(
            self.form, "Baseline Validated",
            "Baseline model confirmed and marked ready for parametric study.")

    def _mark_complete(self):
        if self._obj_name:
            try:
                from tree_objects import mark_task_complete
                mark_task_complete(self._obj_name)
            except Exception:
                pass

    # ── State persistence ─────────────────────────────────────────────────────

    def _load_saved_state(self):
        if not self._obj_name:
            return
        doc = FreeCAD.activeDocument()
        if doc is None:
            return
        obj = doc.getObject(self._obj_name)
        if obj is None:
            return
        saved_dir = getattr(obj, "BaselineDir", "")
        if saved_dir and os.path.isdir(saved_dir):
            self._dir_edit.setText(saved_dir)
            self._summary.setPlainText(_read_case_summary(saved_dir))
            self._s1_status.setText("✓")
            self._s1_status.setStyleSheet(f"color: {_STATUS['ok'][1]}; font-size: 16px;")
        if getattr(obj, "Completed", False):
            self._s2_status.setText("✓")
            self._s2_status.setStyleSheet(f"color: {_STATUS['ok'][1]}; font-size: 16px;")

    # ── FreeCAD panel protocol ────────────────────────────────────────────────

    def accept(self):
        FreeCADGui.Control.closeDialog()

    def reject(self):
        FreeCADGui.Control.closeDialog()
