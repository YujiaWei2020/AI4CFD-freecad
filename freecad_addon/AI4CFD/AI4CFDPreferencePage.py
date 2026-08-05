"""Preferences page: Edit -> Preferences -> AI4CFD.

Lets the user point AI4CFD at its project root ("working directory") and
at the native Python used to run every helper script. Every dataset /
model / results / case path used elsewhere in the addon defaults to a
subfolder of the working directory. OpenFOAM itself is whatever native
install CfdOF's own Preferences page points at (Edit -> Preferences ->
CfdOF) — no separate setting is needed here.
"""

import os

try:
    from PySide2 import QtWidgets
except ImportError:
    from PySide6 import QtWidgets

from addon_config import (
    get_working_dir, set_working_dir,
    get_native_python, set_native_python,
)


class AI4CFDPreferencePage:
    def __init__(self):
        self.form = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(self.form)

        group = QtWidgets.QGroupBox("Working directory")
        group_lay = QtWidgets.QFormLayout(group)

        row = QtWidgets.QHBoxLayout()
        self.le_working_dir = QtWidgets.QLineEdit()
        browse = QtWidgets.QPushButton("Browse…")
        browse.clicked.connect(self._browse_working_dir)
        row.addWidget(self.le_working_dir)
        row.addWidget(browse)
        group_lay.addRow("Project root", row)

        note = QtWidgets.QLabel(
            "AI4CFD's project root. dataset/, results/, best_models/, "
            "openfoam_base/ and the helper scripts are all expected as "
            "subfolders of this directory."
        )
        note.setWordWrap(True)
        group_lay.addRow(note)

        layout.addWidget(group)

        py_group = QtWidgets.QGroupBox("Python")
        py_lay = QtWidgets.QFormLayout(py_group)

        py_row = QtWidgets.QHBoxLayout()
        self.le_python_exe = QtWidgets.QLineEdit()
        py_browse = QtWidgets.QPushButton("Browse…")
        py_browse.clicked.connect(self._browse_python_exe)
        py_row.addWidget(self.le_python_exe)
        py_row.addWidget(py_browse)
        py_lay.addRow("Python executable", py_row)

        py_note = QtWidgets.QLabel(
            "The Python used to run every AI4CFD helper script (geometry "
            "export, CFD runs, AI training/inference) — not FreeCAD's own "
            "bundled interpreter. Needs cadquery, numpy, scipy and torch "
            "installed. Auto-detected via the 'py' launcher if left blank."
        )
        py_note.setWordWrap(True)
        py_lay.addRow(py_note)

        layout.addWidget(py_group)

        restart_note = QtWidgets.QLabel("Changes take effect on the next FreeCAD restart.")
        layout.addWidget(restart_note)
        layout.addStretch()

    def _browse_working_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self.form, "Choose AI4CFD working directory", self.le_working_dir.text()
        )
        if d:
            self.le_working_dir.setText(os.path.normpath(d))

    def _browse_python_exe(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(
            self.form, "Choose Python executable", self.le_python_exe.text(),
            filter="python.exe" if os.name == "nt" else "*"
        )
        if f:
            self.le_python_exe.setText(os.path.normpath(f))

    def loadSettings(self):
        self.le_working_dir.setText(get_working_dir())
        self.le_python_exe.setText(get_native_python())

    def saveSettings(self):
        working_dir = self.le_working_dir.text().strip()
        if working_dir:
            set_working_dir(working_dir)
        python_exe = self.le_python_exe.text().strip()
        if python_exe:
            set_native_python(python_exe)
