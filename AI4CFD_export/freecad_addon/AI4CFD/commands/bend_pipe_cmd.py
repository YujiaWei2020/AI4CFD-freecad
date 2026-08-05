"""FreeCAD command + task panel for bend-pipe geometry preview and simulation."""

import json
import os
import subprocess
import threading

import FreeCAD
import FreeCADGui
import Part

try:
    from PySide2 import QtCore, QtWidgets
    from PySide2.QtCore import QObject, Signal
except ImportError:
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtCore import QObject, Signal

from addon_config import PREVIEW_STEP, build_cmd, build_sim_cmd

BEND_BOUNDS = {
    "pipe_radius":   (0.5,   1.5),
    "length_before": (4.0,   8.0),
    "length_after":  (4.0,   8.0),
    "bend_radius":   (2.0,   4.0),
    "bend_angle":    (30.0, 135.0),
    "inlet_speed":   (0.5,   4.0),
}

_LABELS = {
    "pipe_radius":   "Pipe radius (m)",
    "length_before": "Straight length before (m)",
    "length_after":  "Straight length after (m)",
    "bend_radius":   "Bend radius (m)",
    "bend_angle":    "Bend angle (°)",
    "inlet_speed":   "Inlet speed (m/s)",
}


class _SimWorker(QObject):
    """Runs the simulation subprocess on a background thread; emits lines safely."""
    line_ready = Signal(str)
    finished   = Signal(int)

    def __init__(self, cmd: list) -> None:
        super().__init__()
        self._cmd = cmd

    def run(self) -> None:
        proc = subprocess.Popen(
            self._cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        for raw in proc.stdout:
            self.line_ready.emit(raw.decode('utf-8', errors='replace').rstrip())
        proc.wait()
        self.finished.emit(proc.returncode)


class BendPipePanel:
    """Task panel shown in FreeCAD's left dock."""

    def __init__(self, obj_name: str = None) -> None:
        self._obj_name = obj_name
        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Bend Pipe — AI4CFD")
        layout = QtWidgets.QVBoxLayout(self.form)

        # ── Parameter spinboxes ───────────────────────────────────────────────
        form_layout = QtWidgets.QFormLayout()
        self.spins: dict = {}
        for key, label in _LABELS.items():
            lo, hi = BEND_BOUNDS[key]
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(lo, hi)
            spin.setSingleStep(round((hi - lo) / 20, 4))
            spin.setDecimals(3)
            spin.setValue(round((lo + hi) / 2, 3))
            self.spins[key] = spin
            form_layout.addRow(label, spin)
        layout.addLayout(form_layout)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QtWidgets.QHBoxLayout()
        self._preview_btn = QtWidgets.QPushButton("Preview 3D")
        self._sim_btn     = QtWidgets.QPushButton("Run Simulation")
        self._sim_btn.setStyleSheet("background-color: #2e7d32; color: white; font-weight: bold;")
        btn_row.addWidget(self._preview_btn)
        btn_row.addWidget(self._sim_btn)
        layout.addLayout(btn_row)

        # ── Log output ────────────────────────────────────────────────────────
        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(500)
        self._log.setFixedHeight(180)
        self._log.setPlaceholderText("Simulation output appears here…")
        layout.addWidget(self._log)

        self._preview_btn.clicked.connect(self._preview)
        self._sim_btn.clicked.connect(self._run_simulation)

        self._worker = None
        self._thread = None

    # ── helpers ──────────────────────────────────────────────────────────────

    def _all_params(self) -> dict:
        return {k: sp.value() for k, sp in self.spins.items()}

    def _geo_params(self) -> dict:
        return {k: v for k, v in self._all_params().items() if k != "inlet_speed"}

    def _log_line(self, text: str) -> None:
        self._log.appendPlainText(text)
        self._log.verticalScrollBar().setValue(self._log.verticalScrollBar().maximum())

    # ── Preview ──────────────────────────────────────────────────────────────

    def _preview(self) -> None:
        self._log_line("Generating geometry…")
        result = subprocess.run(
            build_cmd("bend", json.dumps(self._geo_params())),
            capture_output=True,
        )
        if result.returncode != 0:
            self._log_line(f"Error:\n{result.stderr.decode('utf-8', errors='replace').strip()[:300]}")
            return

        doc = FreeCAD.activeDocument() or FreeCAD.newDocument("AI4CFD")
        for obj in doc.Objects:
            if obj.Label.startswith("BendPreview"):
                doc.removeObject(obj.Name)

        shape = Part.read(PREVIEW_STEP)
        feat  = doc.addObject("Part::Feature", "BendPreview")
        feat.Shape = shape
        doc.recompute()
        try:
            FreeCADGui.SendMsgToActiveView("ViewFit")
        except Exception:
            pass
        self._log_line("Preview loaded.")

    # ── Simulation ───────────────────────────────────────────────────────────

    def _run_simulation(self) -> None:
        if self._thread and self._thread.is_alive():
            self._log_line("[already running — wait for it to finish]")
            return

        params = self._all_params()
        self._log.clear()
        self._log_line("Starting OpenFOAM simulation…")
        self._sim_btn.setEnabled(False)

        cmd = build_sim_cmd(json.dumps(params))
        self._worker = _SimWorker(cmd)
        self._worker.line_ready.connect(self._log_line)
        self._worker.finished.connect(self._sim_finished)

        self._thread = threading.Thread(target=self._worker.run, daemon=True)
        self._thread.start()

    def _sim_finished(self, returncode: int) -> None:
        if returncode == 0:
            self._log_line("\n✓ Simulation complete.")
            self._mark_complete()
        else:
            self._log_line(f"\n✗ Simulation failed (exit {returncode}).")
        self._sim_btn.setEnabled(True)

    def _mark_complete(self) -> None:
        if self._obj_name:
            try:
                from tree_objects import mark_task_complete
                mark_task_complete(self._obj_name)
            except Exception:
                pass

    # ── FreeCAD task-panel protocol ──────────────────────────────────────────

    def accept(self) -> None:
        FreeCADGui.Control.closeDialog()

    def reject(self) -> None:
        FreeCADGui.Control.closeDialog()


class BendPipeCommand:
    def GetResources(self) -> dict:
        return {
            "MenuText": "Bend Pipe",
            "ToolTip":  "Configure, preview, and simulate a bend-pipe geometry",
        }

    def IsActive(self) -> bool:
        return True

    def Activated(self) -> None:
        FreeCADGui.Control.showDialog(BendPipePanel())
