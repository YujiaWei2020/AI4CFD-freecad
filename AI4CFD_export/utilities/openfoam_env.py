"""Locate the native Windows OpenFOAM install.

Shared by utilities/run_case.py and the FreeCAD addon's
run_parametric_sim.py so both drive the exact OpenFOAM build CfdOF is
configured with, without going through WSL.
"""

import os
import re


def find_windows_openfoam_dir() -> str:
    """Return CfdOF's configured OpenFOAM install dir, or a sane default.

    Reads FreeCAD's user.cfg directly (no `import FreeCAD` needed) so this
    works from standalone scripts as well as from inside FreeCAD.
    """
    cfg_path = os.path.join(os.environ.get("APPDATA", ""), "FreeCAD", "user.cfg")
    if os.path.isfile(cfg_path):
        try:
            text = open(cfg_path, encoding="utf-8", errors="replace").read()
        except OSError:
            text = ""
        m = re.search(
            r'<FCParamGroup Name="CfdOF">.*?<FCText Name="InstallationPath">([^<]*)</FCText>',
            text, re.DOTALL)
        if m and m.group(1).strip():
            return m.group(1).strip()
    appdata = os.environ.get("APPDATA", "")
    default = os.path.join(appdata, "ESI-OpenCFD", "OpenFOAM", "v2212")
    return default if os.path.isdir(default) else ""


def windows_openfoam_source_cmd(command: str, cwd: str = None) -> str:
    """Return a cmd.exe command string that sources OpenFOAM then runs `command`.

    cwd, if given, is re-asserted with `cd /d` *after* sourcing OpenFOAM —
    the ESI-OpenCFD setEnvVariables .bat itself `cd`s into its own install
    directory while setting HOME, so a working directory passed only via
    subprocess's cwd= is silently overridden; every command would run from
    the OpenFOAM install's internal home folder instead of the case dir.
    """
    of_dir = find_windows_openfoam_dir()
    if not of_dir:
        raise RuntimeError(
            "OpenFOAM installation not found. Set it in FreeCAD's "
            "Edit -> Preferences -> CfdOF page, or install the ESI-OpenCFD "
            "Windows build to the default location."
        )
    version = os.path.basename(of_dir.rstrip("\\/")).lstrip("v")
    source = f'call "{of_dir}\\setEnvVariables-v{version}.bat" && '
    cd = f'cd /d "{cwd}" && ' if cwd else ""
    return f"{source}{cd}{command}"
