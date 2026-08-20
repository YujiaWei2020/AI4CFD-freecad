"""Verification panel — mesh-density (grid convergence) study.

Standalone implementation: VerificationPanel does NOT inherit from
CFDParametricPanel — that class in parametric_study_cmd.py is kept exactly
as the original CFD Parametric Study logic and is untouched by this module.
This file follows the same two-phase *concept* (export geometry/mesh cases
on the main thread, then run them in a background-thread batch) but is its
own self-contained class; it only imports the shared, non-panel helper
functions from parametric_study_cmd.py — CfdOF case writing, STL export
fallback, and the batch-run worker — which are equally "just utilities" for
CFD Parametric Study itself, not part of its panel class.

Instead of sweeping a spreadsheet-linked geometry parameter, this sweeps the
CfdOF mesh object's CharacteristicLengthMax around its current value (half →
current → double, in 3 steps by default) with the geometry held fixed — a
mesh-independence check, not a design-space study.
"""

import json
import os
import threading

import numpy as np
import FreeCAD
import FreeCADGui

try:
    from PySide2 import QtWidgets
except ImportError:
    from PySide6 import QtWidgets

from addon_config import _WSL_PROJECT, _win_to_wsl, _wsl_to_win, _ON_WINDOWS
from commands.parametric_study_cmd import (
    _find_cfdof_mesh_obj, _cfdof_mesh_case_dir, _write_case_via_cfdof,
    _read_cfdof_inlet_velocity, _read_cfdof_inlet_flow_rate,
    _read_all_expression_linked_properties,
    _export_stls_for_case, _export_refinement_stls, _patch_names_from_template,
    _find_exportable_shapes, _BatchWorker, _dbl_spin, _right_item,
)

# Default output root inside the project (Windows path) — Verification's own
# subfolder, separate from CFD Parametric Study's "parametric_study".
_DEFAULT_OUT = os.path.join(
    _WSL_PROJECT.replace("/mnt/e/", "E:\\").replace("/", "\\"),
    "verification")

_STATE_PROP = "AI4CFD_VerificationState"


def _read_characteristic_length_max_mm(mesh_obj) -> float:
    """Current CfdOF CharacteristicLengthMax in mm.

    CharacteristicLengthMax defaults to "0 m" (= auto). CfdMeshTools itself
    never writes an auto-computed value back onto the property — it just
    computes one internally each time a mesh is written (2% of the bounding
    box diagonal, capped at 40% of the smallest extent). Replicated here so
    "current" is a real, usable number to build a min/max sweep around even
    when the user has left this on auto rather than setting it explicitly.
    """
    try:
        cl = FreeCAD.Units.Quantity(mesh_obj.CharacteristicLengthMax).Value
    except Exception:
        cl = 0.0
    if cl > 0.0:
        return cl
    part_obj = getattr(mesh_obj, "Part", None)
    if part_obj is not None and hasattr(part_obj, "Shape"):
        bb = part_obj.Shape.BoundBox
        cl_bound_mag = (bb.XLength**2 + bb.YLength**2 + bb.ZLength**2) ** 0.5
        cl_bound_min = min(bb.XLength, bb.YLength, bb.ZLength)
        return min(0.02 * cl_bound_mag, 0.4 * cl_bound_min)
    return 1.0  # last-resort fallback — no Part/Shape to size against


class VerificationPanel:
    """Mesh-density sweep: export N cases at different CharacteristicLengthMax
    values (geometry fixed), then run them via the same batch-simulation
    machinery CFD Parametric Study uses.
    """

    def __init__(self, obj_name: str = None) -> None:
        self._obj_name = obj_name
        self._worker  = None
        self._thread  = None
        self._exported_cases: list = []   # [(case_dir, params_dict)]
        self._exporting = False
        self._cur_mm = None   # current mesh CharacteristicLengthMax (mm)

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("Verification — AI4CFD")
        root = QtWidgets.QVBoxLayout(self.form)
        root.setSpacing(5)

        # ── Mesh parameter ───────────────────────────────────────────────────
        grp_param = QtWidgets.QGroupBox("1 · Mesh Parameter")
        lay_param = QtWidgets.QVBoxLayout(grp_param)
        lay_param.addWidget(QtWidgets.QLabel(
            "<i>Sweeps the current CfdOF mesh object's "
            "<b>Characteristic Length Max</b> — same geometry every case, "
            "mesh density only.</i>"))

        form_param = QtWidgets.QFormLayout()
        self._cur_lbl = QtWidgets.QLabel("—")
        form_param.addRow("Current (mm):", self._cur_lbl)

        self._min_spin = _dbl_spin(0.0)
        self._min_spin.valueChanged.connect(self._refresh_count)
        form_param.addRow("Min (mm):", self._min_spin)

        self._max_spin = _dbl_spin(0.0)
        self._max_spin.valueChanged.connect(self._refresh_count)
        form_param.addRow("Max (mm):", self._max_spin)

        self._steps_spin = QtWidgets.QSpinBox()
        self._steps_spin.setRange(2, 200)
        self._steps_spin.setValue(3)
        self._steps_spin.valueChanged.connect(self._refresh_count)
        form_param.addRow("Steps:", self._steps_spin)
        lay_param.addLayout(form_param)

        refresh_btn = QtWidgets.QPushButton("↻ Re-read current mesh value")
        refresh_btn.clicked.connect(self._populate_mesh_param)
        lay_param.addWidget(refresh_btn)
        root.addWidget(grp_param)

        # ── Mesh Density (sampling) ──────────────────────────────────────────
        grp_samp = QtWidgets.QGroupBox("2 · Mesh Density")
        lay_samp = QtWidgets.QVBoxLayout(grp_samp)
        lay_samp.addWidget(QtWidgets.QLabel("Grid sweep (min → max, evenly spaced)"))

        count_row = QtWidgets.QHBoxLayout()
        self._count_lbl = QtWidgets.QLabel("Total simulations: —")
        count_row.addWidget(self._count_lbl, 1)
        prev_btn = QtWidgets.QPushButton("Preview…")
        prev_btn.clicked.connect(self._preview_samples)
        count_row.addWidget(prev_btn)
        lay_samp.addLayout(count_row)
        root.addWidget(grp_samp)

        # ── Phase 1: Export geometry cases ───────────────────────────────────
        grp_exp = QtWidgets.QGroupBox("3 · Export geometry cases (main thread)")
        lay_exp = QtWidgets.QFormLayout(grp_exp)

        body_row = QtWidgets.QHBoxLayout()
        self._body_combo = QtWidgets.QComboBox()
        self._body_combo.setToolTip("Solid body whose STL is written for each case")
        body_row.addWidget(self._body_combo, 1)
        body_ref = QtWidgets.QPushButton("↻")
        body_ref.setFixedWidth(28)
        body_ref.clicked.connect(self._populate_body_combo)
        body_row.addWidget(body_ref)
        lay_exp.addRow("Body:", body_row)

        out_row = QtWidgets.QHBoxLayout()
        self._out_edit = QtWidgets.QLineEdit(_DEFAULT_OUT)
        out_row.addWidget(self._out_edit, 1)
        browse_btn = QtWidgets.QPushButton("…")
        browse_btn.setFixedWidth(28)
        browse_btn.clicked.connect(self._browse_out)
        out_row.addWidget(browse_btn)
        lay_exp.addRow("Output dir:", out_row)

        tmpl_row = QtWidgets.QHBoxLayout()
        self._tmpl_edit = QtWidgets.QLineEdit()
        self._tmpl_edit.setPlaceholderText(
            "Path to a rendered OpenFOAM case  (e.g. your CfdOF case directory)")
        self._tmpl_edit.setToolTip(
            "A fully-rendered OpenFOAM case (no .j2 files) used as the config template.\n"
            "Its system/, 0/, and constant/ (except triSurface/) files are copied\n"
            "into each batch case. Point this at the case CfdOF last wrote to disk.")
        tmpl_row.addWidget(self._tmpl_edit, 1)
        tmpl_browse = QtWidgets.QPushButton("…")
        tmpl_browse.setFixedWidth(28)
        tmpl_browse.clicked.connect(self._browse_template)
        tmpl_row.addWidget(tmpl_browse)
        lay_exp.addRow("OF template:", tmpl_row)

        self._export_btn = QtWidgets.QPushButton("Export All Cases")
        self._export_btn.setStyleSheet(
            "background-color:#2e7d32;color:white;font-weight:bold;padding:5px;")
        self._export_btn.clicked.connect(self._export_cases)
        lay_exp.addRow(self._export_btn)

        self._export_progress = QtWidgets.QProgressBar()
        self._export_progress.setValue(0)
        lay_exp.addRow(self._export_progress)

        self._export_status = QtWidgets.QLabel("No cases exported yet.")
        self._export_status.setStyleSheet("color:#555;")
        lay_exp.addRow(self._export_status)
        root.addWidget(grp_exp)

        # ── Phase 2: Run simulations ─────────────────────────────────────────
        grp_run = QtWidgets.QGroupBox("4 · Run batch simulations")
        lay_run = QtWidgets.QFormLayout(grp_run)

        self._n_workers = QtWidgets.QSpinBox()
        self._n_workers.setRange(1, 32)
        self._n_workers.setValue(3)
        lay_run.addRow("Parallel workers:", self._n_workers)

        self._cores_per = QtWidgets.QSpinBox()
        self._cores_per.setRange(1, 32)
        self._cores_per.setValue(1)
        lay_run.addRow("Cores per sim:", self._cores_per)

        self._run_mode = QtWidgets.QComboBox()
        self._run_mode.addItem("Train  (run CFD solver → dataset)")
        self._run_mode.addItem("Inference  (mesh only → .npy for prediction)")
        lay_run.addRow("Run mode:", self._run_mode)

        btn_row = QtWidgets.QHBoxLayout()
        self._run_btn = QtWidgets.QPushButton("Run Batch")
        self._run_btn.setStyleSheet(
            "background-color:#1565c0;color:white;font-weight:bold;padding:5px;")
        self._run_btn.clicked.connect(self._run_batch)
        btn_row.addWidget(self._run_btn)

        self._stop_btn = QtWidgets.QPushButton("Stop")
        self._stop_btn.setStyleSheet(
            "background-color:#e65100;color:white;font-weight:bold;padding:5px;")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self._stop_btn)
        lay_run.addRow(btn_row)

        self._sim_progress = QtWidgets.QProgressBar()
        self._sim_progress.setValue(0)
        lay_run.addRow(self._sim_progress)

        self._log = QtWidgets.QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumBlockCount(3000)
        self._log.setFixedHeight(130)
        lay_run.addRow(self._log)
        root.addWidget(grp_run)

        # ── Init ─────────────────────────────────────────────────────────────
        self._populate_mesh_param()
        self._populate_body_combo()
        self._auto_populate_template()
        self._restore_state()

    # ── Mesh parameter ───────────────────────────────────────────────────────

    def _populate_mesh_param(self) -> None:
        doc = FreeCAD.activeDocument()
        mesh_obj = _find_cfdof_mesh_obj(doc) if doc else None
        if mesh_obj is None or not hasattr(mesh_obj, "CharacteristicLengthMax"):
            self._cur_lbl.setText("— (no CfdOF mesh object found)")
            self._cur_mm = None
            self._refresh_count()
            return
        self._cur_mm = _read_characteristic_length_max_mm(mesh_obj)
        self._cur_lbl.setText(f"{self._cur_mm:.4g}")
        self._min_spin.setValue(self._cur_mm / 2.0)
        self._max_spin.setValue(self._cur_mm * 2.0)
        self._refresh_count()

    # ── Body / paths ─────────────────────────────────────────────────────────

    def _populate_body_combo(self) -> None:
        doc = FreeCAD.activeDocument()
        shapes = _find_exportable_shapes(doc)
        prev = self._body_combo.currentData()
        self._body_combo.clear()
        for label, name in shapes:
            self._body_combo.addItem(label, name)
        idx = self._body_combo.findData(prev)
        self._body_combo.setCurrentIndex(max(idx, 0))

    def _auto_populate_template(self) -> None:
        """Pre-fill the template field from the CfdOF mesh case directory."""
        if self._tmpl_edit.text().strip():
            return  # user already set it
        doc = FreeCAD.activeDocument()
        if doc is None:
            return
        case_path = _cfdof_mesh_case_dir(doc)
        if _ON_WINDOWS and case_path.startswith("/mnt/"):
            case_path = _wsl_to_win(case_path)
        if case_path and os.path.isdir(case_path):
            self._tmpl_edit.setText(case_path)
            FreeCAD.Console.PrintMessage(
                f"AI4CFD: template auto-set from CfdOF mesh case dir: {case_path}\n")

    def _browse_template(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self.form, "Select OpenFOAM template case directory",
            self._tmpl_edit.text() or "")
        if d:
            self._tmpl_edit.setText(d)

    def _template_wsl(self) -> str:
        """Return WSL path of the template directory."""
        path = self._tmpl_edit.text().strip()
        if not path:
            return ""
        if _ON_WINDOWS and len(path) > 1 and path[1] == ":":
            return _win_to_wsl(path)
        return path

    def _browse_out(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self.form, "Select output directory", self._out_edit.text())
        if d:
            self._out_edit.setText(d)

    # ── Task generation ──────────────────────────────────────────────────────

    def _generate_tasks(self) -> list:
        """Grid sweep over the single CharacteristicLengthMax value — always
        min → max, evenly spaced (no fixed/vary toggle needed: a mesh-density
        study is inherently a sweep, unlike CFD Parametric Study's per-alias
        table)."""
        if self._cur_mm is None:
            return []
        lo, hi = self._min_spin.value(), self._max_spin.value()
        steps  = self._steps_spin.value()
        return [
            {"characteristic_length_max": float(v)}
            for v in np.linspace(lo, hi, steps)
        ]

    def _refresh_count(self) -> None:
        try:
            n = len(self._generate_tasks())
            self._count_lbl.setText(f"Total simulations: <b>{n}</b>")
        except Exception:
            self._count_lbl.setText("Total simulations: —")

    # ── Preview dialog ────────────────────────────────────────────────────────

    def _preview_samples(self) -> None:
        tasks = self._generate_tasks()
        if not tasks:
            QtWidgets.QMessageBox.information(
                self.form, "Preview",
                "No mesh parameter found — click the refresh button in section 1.")
            return

        dlg = QtWidgets.QDialog(self.form)
        dlg.setWindowTitle(f"Sample preview  ({len(tasks)} total)")
        dlg.resize(420, 350)
        lay = QtWidgets.QVBoxLayout(dlg)

        tbl = QtWidgets.QTableWidget(len(tasks), 2)
        tbl.setHorizontalHeaderLabels(["#", "characteristic_length_max (mm)"])
        for r, params in enumerate(tasks):
            tbl.setItem(r, 0, _right_item(str(r + 1)))
            tbl.setItem(r, 1, _right_item(f"{params['characteristic_length_max']:.4g}"))
        tbl.resizeColumnsToContents()
        lay.addWidget(tbl)
        btn = QtWidgets.QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec_()

    # ── Phase 1: Export geometry ─────────────────────────────────────────────

    def _export_cases(self) -> None:
        if self._exporting:
            return

        doc = FreeCAD.activeDocument()
        if doc is None:
            self._export_status.setText("No active document.")
            return
        mesh_obj = _find_cfdof_mesh_obj(doc)
        if mesh_obj is None or not hasattr(mesh_obj, "CharacteristicLengthMax"):
            self._export_status.setText(
                "No CfdOF mesh object found — set up meshing (CfdOF) first.")
            return

        body_name = self._body_combo.currentData()
        if not body_name:
            self._export_status.setText("Select a body to export.")
            return
        body = doc.getObject(body_name)
        if body is None or not hasattr(body, "Shape"):
            self._export_status.setText("Body not found or has no Shape.")
            return

        # Body/geometry never changes for a mesh-density sweep — validate once
        # up front rather than re-checking every iteration like a
        # geometry-varying study needs to.
        try:
            shape = body.Shape
            if shape.isNull() or not shape.Solids:
                self._export_status.setText(
                    "Body has no valid solid — aborting.")
                return
        except Exception as exc:
            self._export_status.setText(f"Body shape invalid: {exc}")
            return

        tasks   = self._generate_tasks()
        out_dir = self._out_edit.text().strip()
        if not out_dir:
            self._export_status.setText("Set an output directory.")
            return
        if not tasks:
            self._export_status.setText("No mesh values to sweep — check section 1.")
            return

        self._exporting = True
        self._export_btn.setEnabled(False)
        self._exported_cases.clear()
        self._export_progress.setMaximum(len(tasks))
        self._export_progress.setValue(0)

        # Save the original value to restore after export
        original_mm = _read_characteristic_length_max_mm(mesh_obj)

        try:
            for i, params in enumerate(tasks):
                # 1. Push this mesh-density value onto the mesh object (no
                # spreadsheet involved — geometry stays fixed the whole run).
                mesh_obj.CharacteristicLengthMax = \
                    f"{params['characteristic_length_max']:.6g} mm"
                doc.recompute(None, True, True)   # force=True, checkExternal=True
                QtWidgets.QApplication.processEvents()

                # 2. Export case for this mesh-density value.
                # Primary path: ask CfdOF to write the full case (meshCase/ + case/)
                # using the current document state.
                # Fallback: STL-only export; run_parametric_sim.py copies template.
                case_dir = os.path.join(out_dir, f"case_{i+1:04d}")
                os.makedirs(case_dir, exist_ok=True)

                location = None
                cfdof_ok = _write_case_via_cfdof(doc, case_dir)
                if cfdof_ok:
                    mode = "cfdof-full"
                    self._export_status.setText(
                        f"Exported {i+1}/{len(tasks)}: "
                        f"{os.path.basename(case_dir)} [CfdOF full case]"
                    )
                else:
                    stl_dir  = os.path.join(case_dir, "constant", "triSurface")
                    tmpl_win = self._tmpl_edit.text().strip()
                    if not tmpl_win:
                        tmpl_win = _cfdof_mesh_case_dir(doc)
                    if _ON_WINDOWS and tmpl_win.startswith("/mnt/"):
                        tmpl_win = _wsl_to_win(tmpl_win)
                    tmpl_tri = None
                    if tmpl_win:
                        for subpath in (
                            os.path.join(tmpl_win, "meshCase", "constant", "triSurface"),
                            os.path.join(tmpl_win, "constant", "triSurface"),
                        ):
                            if os.path.isdir(subpath):
                                tmpl_tri = subpath
                                break
                    mode, location = _export_stls_for_case(doc, body, stl_dir, tmpl_tri)
                    _export_refinement_stls(
                        doc, body, stl_dir, tmpl_tri,
                        _patch_names_from_template(tmpl_tri))
                    self._export_status.setText(
                        f"Exported {i+1}/{len(tasks)}: "
                        f"{os.path.basename(case_dir)} [{mode}]"
                    )

                # 3. Read expression-linked CfdOF inlet velocity / flow rate /
                #    boundary properties for params.json tracking (and for
                #    run_parametric_sim.py's fallback 0/U patching when the
                #    CfdOF full-case write above did not happen).
                cfdof_vel = _read_cfdof_inlet_velocity(doc)
                if cfdof_vel is not None:
                    params["_inlet_velocity_ms"] = cfdof_vel

                all_props = _read_all_expression_linked_properties(doc)
                if all_props:
                    params["_boundary_properties"] = all_props

                cfdof_flow_rate = _read_cfdof_inlet_flow_rate(doc)
                if cfdof_flow_rate is not None and abs(cfdof_flow_rate) > 1e-12:
                    params["_inlet_volumetric_flow_rate_m3s"] = cfdof_flow_rate

                # 4. Save parameter JSON and mesh metadata alongside
                with open(os.path.join(case_dir, "params.json"), "w") as fh:
                    json.dump(params, fh, indent=2)
                if location is not None:
                    with open(os.path.join(case_dir, "ai4cfd_meta.json"), "w") as fh:
                        json.dump({"locationInMesh": list(location)}, fh)

                self._exported_cases.append((case_dir, params))
                self._export_progress.setValue(i + 1)
                QtWidgets.QApplication.processEvents()

        except Exception as exc:
            self._export_status.setText(f"Export failed: {exc}")
            FreeCAD.Console.PrintError(f"AI4CFD verification export: {exc}\n")
        finally:
            # Restore the original mesh density
            try:
                mesh_obj.CharacteristicLengthMax = f"{original_mm:.6g} mm"
                doc.recompute(None, True, True)
            except Exception:
                pass
            self._exporting = False
            self._export_btn.setEnabled(True)

        n = len(self._exported_cases)
        self._export_status.setText(
            f"✓ {n} case{'s' if n != 1 else ''} exported to {out_dir}")

    # ── Phase 2: Batch simulation ─────────────────────────────────────────────

    def _log_line(self, text: str) -> None:
        self._log.appendPlainText(text)
        self._log.verticalScrollBar().setValue(
            self._log.verticalScrollBar().maximum())

    def _on_sim_started(self, idx: int, total: int) -> None:
        self._sim_progress.setMaximum(total)
        self._sim_progress.setValue(idx - 1)

    def _on_sim_finished(self, idx: int, total: int, rc: int) -> None:
        status = "✓" if rc == 0 else f"✗ rc={rc}"
        self._log_line(f"[{idx}/{total}] {status}")
        self._sim_progress.setValue(idx)

    def _on_all_done(self, successes: int, total: int) -> None:
        self._sim_progress.setValue(total)
        stopped = self._worker and self._worker._stopped
        msg = (f"⬛ Stopped — {successes}/{total} completed."
               if stopped else
               f"✓ Done — {successes}/{total} succeeded.")
        self._log_line(f"\n{msg}")
        if successes == total and not stopped:
            self._mark_complete()
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    def _mark_complete(self) -> None:
        if self._obj_name:
            try:
                from tree_objects import mark_task_complete
                mark_task_complete(self._obj_name)
            except Exception:
                pass

    def _stop(self) -> None:
        if self._worker:
            self._worker.stop()
            self._log_line("[Stop requested…]")

    def _run_batch(self) -> None:
        if self._thread and self._thread.is_alive():
            self._log_line("[Already running]")
            return

        if not self._exported_cases:
            self._log_line("No exported cases — click 'Export All Cases' first.")
            return

        tmpl = self._template_wsl()
        if not tmpl:
            # Auto-detect from CfdOF mesh case directory
            doc = FreeCAD.activeDocument()
            cfdof_path = _cfdof_mesh_case_dir(doc) if doc else ""
            if cfdof_path:
                tmpl = _win_to_wsl(cfdof_path) if _ON_WINDOWS else cfdof_path
        if not tmpl:
            self._log_line("Set the OpenFOAM template directory first (OF template field).")
            return

        inference_prep = (self._run_mode.currentIndex() == 1)

        self._log.clear()
        self._sim_progress.setValue(0)
        self._sim_progress.setMaximum(len(self._exported_cases))
        mode_label = "Inference prep (mesh → .npy)" if inference_prep else "Train (CFD solver)"
        self._log_line(f"Starting — {len(self._exported_cases)} cases  [{mode_label}]…")
        self._log_line(f"Template:  {tmpl}")
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._worker = _BatchWorker(
            self._exported_cases,
            n_workers      = self._n_workers.value(),
            cores_per_sim  = self._cores_per.value(),
            template_wsl   = tmpl,
            inference_prep = inference_prep,
        )
        self._worker.line_ready.connect(self._log_line)
        self._worker.sim_started.connect(self._on_sim_started)
        self._worker.sim_finished.connect(self._on_sim_finished)
        self._worker.all_done.connect(self._on_all_done)

        self._thread = threading.Thread(target=self._worker.run, daemon=True)
        self._thread.start()

    # ── State persistence (tied to the FreeCAD tree object) ────────────────────
    # Saved on accept() (the task-panel OK button), restored here on next open.
    # No-op whenever obj_name is None (there is no tree object to read/write).

    def _serialize_state(self) -> dict:
        return {
            "body_name": self._body_combo.currentData(),
            "out_dir":   self._out_edit.text(),
            "template":  self._tmpl_edit.text(),
            "min_mm":    self._min_spin.value(),
            "max_mm":    self._max_spin.value(),
            "steps":     self._steps_spin.value(),
        }

    def _save_state(self) -> None:
        if not self._obj_name:
            return
        doc = FreeCAD.activeDocument()
        obj = doc.getObject(self._obj_name) if doc else None
        if obj is None:
            return
        if not hasattr(obj, _STATE_PROP):
            obj.addProperty(
                "App::PropertyString", _STATE_PROP, "AI4CFD",
                "Saved panel configuration (JSON)")
        setattr(obj, _STATE_PROP, json.dumps(self._serialize_state()))

    def _restore_state(self) -> None:
        if not self._obj_name:
            return
        doc = FreeCAD.activeDocument()
        obj = doc.getObject(self._obj_name) if doc else None
        if obj is None or not hasattr(obj, _STATE_PROP):
            return
        raw = getattr(obj, _STATE_PROP, "")
        if not raw:
            return
        try:
            state = json.loads(raw)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"AI4CFD: could not parse saved Verification state: {exc}\n")
            return

        body_idx = self._body_combo.findData(state.get("body_name"))
        if body_idx >= 0:
            self._body_combo.setCurrentIndex(body_idx)
        if state.get("out_dir"):
            self._out_edit.setText(state["out_dir"])
        if state.get("template"):
            self._tmpl_edit.setText(state["template"])
        if "min_mm" in state:
            self._min_spin.setValue(state["min_mm"])
        if "max_mm" in state:
            self._max_spin.setValue(state["max_mm"])
        if "steps" in state:
            self._steps_spin.setValue(state["steps"])
        self._refresh_count()

    # ── FreeCAD protocol ──────────────────────────────────────────────────────

    def accept(self) -> None:
        self._save_state()
        FreeCADGui.Control.closeDialog()

    def reject(self) -> None:
        FreeCADGui.Control.closeDialog()


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
        # place to save/restore its configuration (see _save_state/
        # _restore_state above) — matches the object opened when this same
        # tree item is later double-clicked.
        doc = FreeCAD.activeDocument() or FreeCAD.newDocument("AI4CFD")
        from tree_objects import build_tree
        build_tree(doc)
        FreeCADGui.Control.showDialog(VerificationPanel("AI4CFD_Verification"))
