"""AI4CFD FreeCAD addon configuration.

The project root ("working directory") is set via FreeCAD's
Edit -> Preferences -> AI4CFD page, not by editing this file. Every other
path (dataset/, results/, best_models/, openfoam_base/, the helper
scripts, …) is derived from that one directory.

Everything runs as a native Windows/Linux process — no WSL. On Windows the
helper scripts run under a native Python (also set on the Preferences page)
and OpenFOAM commands are sourced from whatever native install CfdOF is
configured with (see utilities/openfoam_env.py).
"""

import os
import subprocess
import sys

_ON_WINDOWS = sys.platform == "win32"

# FreeCAD parameter group where addon settings are persisted.
_PREF_GROUP = "User parameter:BaseApp/Preferences/Mod/AI4CFD"

# Used only until the user sets a working directory in Preferences.
_FALLBACK_PROJECT_WIN = r"E:\AI\3d_DL_prediction\AI4CFD_core"
_FALLBACK_PROJECT_POSIX = "/mnt/e/AI/3d_DL_prediction/AI4CFD_core"


def _win_to_wsl(path: str) -> str:
    """No-op — kept for backward compatibility with command files that
    still call this before/after passing paths to subprocess commands.
    Everything is native now, so paths need no translation."""
    return path


def _wsl_to_win(path: str) -> str:
    """No-op — see _win_to_wsl."""
    return path


def get_working_dir() -> str:
    """Return the working directory as a native OS path (what the
    Preferences page displays/edits), falling back to a default if the
    user hasn't set one yet."""
    try:
        import FreeCAD
        stored = FreeCAD.ParamGet(_PREF_GROUP).GetString("WorkingDir", "")
    except ImportError:
        stored = ""
    if stored:
        return stored
    return _FALLBACK_PROJECT_WIN if _ON_WINDOWS else _FALLBACK_PROJECT_POSIX


def set_working_dir(native_path: str) -> None:
    """Persist the working directory (a native OS path) to FreeCAD Preferences."""
    import FreeCAD
    FreeCAD.ParamGet(_PREF_GROUP).SetString("WorkingDir", native_path)


def _autodetect_native_python() -> str:
    """Best-effort discovery of a native Python (not FreeCAD's own
    bundled interpreter)."""
    if not _ON_WINDOWS:
        return sys.executable

    # 1. Try the `py` launcher — only works if it's reachable from FreeCAD's
    #    own process PATH, which is not always the case.
    try:
        out = subprocess.check_output(
            ["py", "-3", "-c", "import sys; print(sys.executable)"],
            text=True, timeout=5, stderr=subprocess.DEVNULL)
        candidate = out.strip()
        if candidate and os.path.isfile(candidate):
            return candidate
    except Exception:
        pass

    # 2. Fall back to scanning common install locations directly.
    import glob
    patterns = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python", "Python3*", "python.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Python3*", "python.exe"),
        r"C:\Python3*\python.exe",
    ]
    candidates = [c for p in patterns for c in glob.glob(p)]
    # Skip the Microsoft Store app-execution-alias stub — it isn't a real interpreter.
    candidates = [c for c in candidates if "WindowsApps" not in c]
    if candidates:
        candidates.sort(reverse=True)   # highest Python3XX first
        return candidates[0]

    return ""


def get_native_python() -> str:
    """Return the native Python executable used to run all helper scripts."""
    try:
        import FreeCAD
        stored = FreeCAD.ParamGet(_PREF_GROUP).GetString("PythonExe", "")
    except ImportError:
        stored = ""
    if stored:
        return stored
    return _autodetect_native_python()


def set_native_python(path: str) -> None:
    """Persist the native Python executable path to FreeCAD Preferences."""
    import FreeCAD
    FreeCAD.ParamGet(_PREF_GROUP).SetString("PythonExe", path)


_WORKING_DIR = get_working_dir()

# Project root — same variable name kept for the command files that import
# it, but it is now always a native OS path (no WSL translation).
_WSL_PROJECT = _WORKING_DIR

# Native Python used to run every helper script. "-X utf8" forces UTF-8
# mode so stdout — piped, not a real console — doesn't fall back to the
# Windows codepage (cp1252) and crash on any non-ASCII character the
# helper scripts print (arrows, checkmarks, …).
_NATIVE_PYTHON = get_native_python()
_PY_BASE = [_NATIVE_PYTHON, "-X", "utf8"]
if not _NATIVE_PYTHON:
    try:
        import FreeCAD as _FreeCAD
        _FreeCAD.Console.PrintError(
            "AI4CFD: no native Python found automatically. Set it in "
            "Edit -> Preferences -> AI4CFD -> Python executable.\n")
    except ImportError:
        pass

PREVIEW_STEP = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "ai4cfd_preview.step") \
    if _ON_WINDOWS else os.path.expanduser("~/.ai4cfd_preview.step")
_wsl_step_out = PREVIEW_STEP

# Always use the original project path — not the AppData copy — so relative imports work
_helper_path = _WSL_PROJECT + "/freecad_addon/AI4CFD/helpers/generate_step.py"

_SIM_HELPER            = _WSL_PROJECT + "/freecad_addon/AI4CFD/helpers/run_simulation.py"
_PARAMETRIC_SIM_HELPER = _WSL_PROJECT + "/freecad_addon/AI4CFD/helpers/run_parametric_sim.py"
_GENERIC_SIM_HELPER    = _WSL_PROJECT + "/freecad_addon/AI4CFD/helpers/run_generic_sim.py"


def build_cmd(geo_type: str, params_json: str) -> list:
    """Return the subprocess command list for geometry generation."""
    return [
        *_PY_BASE, _helper_path,
        "--type",   geo_type,
        "--params", params_json,
        "--output", _wsl_step_out,
    ]


def build_generic_sim_cmd(case_name: str, params_json: str,
                          num_cores: int = 1, max_iterations: int = 1000,
                          module: str = "train",
                          case_dir: str = None) -> list:
    """Run the named simulator (airfoil, bendpipe, …) with JSON params."""
    base = [
        *_PY_BASE, _GENERIC_SIM_HELPER,
        "--case-name",      case_name,
        "--params",         params_json,
        "--num-cores",      str(num_cores),
        "--max-iterations", str(max_iterations),
        "--module",         module,
    ]
    if case_dir:
        base += ["--case-dir", case_dir]
    return base


def build_parametric_sim_cmd(case_dir: str, template_dir: str,
                             params_json: str = "{}",
                             num_cores: int = 1,
                             mesh_only: bool = False,
                             inference_prep: bool = False) -> list:
    """Run one geometry-driven parametric case: copy template + new STL + remesh.

    mesh_only=True     — skip solver only (debug / template testing).
    inference_prep=True — mesh + foamToVTK + wall distance + .npy (no solver).
    """
    base = [
        *_PY_BASE, _PARAMETRIC_SIM_HELPER,
        "--case-dir",     case_dir,
        "--template-dir", template_dir,
        "--params",       params_json,
        "--num-cores",    str(num_cores),
    ]
    if inference_prep:
        base += ["--inference-prep"]
    elif mesh_only:
        base += ["--mesh-only"]
    return base


def build_sim_cmd(params_json: str, num_cores: int = 4,
                  max_iterations: int = 1000, mesh_scale_factor: float = 0.5,
                  module: str = "train", case_dir: str = None) -> list:
    """Return the subprocess command list for running an OpenFOAM simulation."""
    base = [
        *_PY_BASE, _SIM_HELPER,
        "--params",            params_json,
        "--num-cores",         str(num_cores),
        "--max-iterations",    str(max_iterations),
        "--mesh-scale-factor", str(mesh_scale_factor),
        "--module",            module,
    ]
    if case_dir:
        base += ["--case-dir", case_dir]
    return base
