"""CFD Parametric Study panel.

Two-phase workflow
-----------------
Phase 1 — Geometry export (main thread, sequential):
  For each parameter sample:
    1. Write aliases back into the FreeCAD spreadsheet
    2. Call doc.recompute() so PartDesign / Part geometry updates
    3. Export the selected solid as STL into case_NNNN/constant/triSurface/

Phase 2 — Batch simulation (background threads, parallel):
  For each exported case directory run the OpenFOAM pipeline
  (mesh + solve) via the existing build_sim_cmd infrastructure.

Parameters come from numeric aliases defined in any FreeCAD Spreadsheet
object.  The user sets aliases on cells via right-click → Properties →
Alias in the Spreadsheet workbench, then links geometry dimensions to
them with expressions (e.g. =Spreadsheet.pipe_radius).
"""

import json
import math
import os
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import FreeCAD
import FreeCADGui

try:
    from PySide2 import QtCore, QtWidgets
    from PySide2.QtCore import QObject, Signal
    from PySide2.QtGui import QColor, QFont
except ImportError:
    from PySide6 import QtCore, QtWidgets
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtGui import QColor, QFont

from addon_config import build_parametric_sim_cmd, _WSL_PROJECT, _win_to_wsl, _wsl_to_win, _ON_WINDOWS

# Default output root inside the project (Windows path)
_DEFAULT_OUT = os.path.join(
    _WSL_PROJECT.replace("/mnt/e/", "E:\\").replace("/", "\\"),
    "parametric_study")

# Windows path to openfoam_base/ for template picker
_OPENFOAM_BASE_WIN = _WSL_PROJECT.replace("/mnt/e/", "E:\\").replace("/", "\\") + "\\openfoam_base"


# ── Spreadsheet helpers ───────────────────────────────────────────────────────

_BASE_PROPS = frozenset({
    'ExpressionEngine', 'Label', 'Label2', 'Map', 'State', 'Visibility',
    'Content', 'cells', 'Shape', 'Placement',
})

# Matches raw FreeCAD cell addresses like B1, AA10, Z99 — these are NOT aliases.
# Named aliases always contain lowercase letters or non-digit suffixes.
_CELL_ADDR_RE = re.compile(r'^[A-Z]+\d+$')


def _find_spreadsheets(doc) -> list:
    if doc is None:
        return []
    return [o for o in doc.Objects if o.TypeId == "Spreadsheet::Sheet"]


def _prop_to_float(val):
    """Convert a spreadsheet property value to float, or return None."""
    if isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return float(val)
    # FreeCAD Quantity (e.g. "10 mm/s") — extract the numeric part
    if hasattr(val, 'Value'):
        try:
            return float(val.Value)
        except Exception:
            pass
    return None


def _get_aliases(sheet) -> dict:
    """Return {alias: float_value} for every named numeric alias on the sheet.

    FreeCAD exposes both named aliases (e.g. 'velocity') and raw cell-address
    properties (e.g. 'B1') on the same spreadsheet object.  We prefer named
    aliases; if none are found we fall back to cell addresses so the table is
    never empty for plain-number spreadsheets without explicitly-set aliases.
    """
    named  = {}
    addrs  = {}
    for prop in sheet.PropertiesList:
        if prop in _BASE_PROPS or prop.startswith('_'):
            continue
        try:
            fval = _prop_to_float(getattr(sheet, prop))
        except Exception:
            continue
        if fval is None:
            continue
        if _CELL_ADDR_RE.match(prop):
            addrs[prop] = fval    # raw cell address — keep as fallback
        else:
            named[prop] = fval   # named alias — preferred
    return named if named else addrs


def _set_one(sheet, alias: str, value: float) -> None:
    """Set a spreadsheet alias regardless of whether the cell is typed as int or float."""
    try:
        addr = sheet.getCellFromAlias(alias)
        if addr:
            sheet.set(addr, str(float(value)))
            return
    except Exception:
        pass
    try:
        setattr(sheet, alias, float(value))
        return
    except Exception:
        pass
    try:
        setattr(sheet, alias, int(round(value)))
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            f"AI4CFD param: cannot set {alias}={value}: {exc}\n")


def _set_aliases(sheet, params: dict) -> None:
    for alias, value in params.items():
        _set_one(sheet, alias, value)


# ── CfdOF full-case writer ────────────────────────────────────────────────────

def _find_cfdof_mesh_obj(doc):
    """Return the CfdOF mesh object (has CasePath + Mesh-like proxy)."""
    for obj in doc.Objects:
        if not hasattr(obj, "CasePath"):
            continue
        proxy = getattr(obj, "Proxy", None)
        if proxy is None:
            continue
        pname = type(proxy).__name__
        if any(k in pname for k in ("Mesh", "Cart", "Snappy")):
            return obj
    # Fallback: any object with CasePath
    for obj in doc.Objects:
        if hasattr(obj, "CasePath"):
            return obj
    return None


def _read_cfdof_inlet_velocity(doc) -> float | None:
    """Return the current inlet velocity magnitude in m/s from a CfdOF BC object.

    Works when the user has set an expression like =Spreadsheet.velocity on the
    BC's VelocityMag property via FreeCAD's expression editor — after doc.recompute()
    the property reflects the current spreadsheet value.

    Returns None if no inlet BC object is found.
    """
    for obj in doc.Objects:
        if getattr(obj, "BoundaryType", "") != "inlet":
            continue
        vel = getattr(obj, "VelocityMag", None)
        if vel is None:
            continue
        try:
            return float(vel.getValueAs("m/s"))
        except AttributeError:
            try:
                return float(vel)
            except Exception:
                pass
    return None


def _read_cfdof_inlet_flow_rate(doc) -> float | None:
    """Return the current inlet volumetric flow rate in m^3/s from a CfdOF BC object.

    Mirrors _read_cfdof_inlet_velocity but for the VolFlowRate property, which
    CfdOF uses for the 'volumetricFlowRateInlet' BoundarySubType (written to
    OpenFOAM as 'type flowRateInletVelocity; volumetricFlowRate constant <v>;').
    The property exists on every inlet BC object regardless of which subtype is
    active, so this is safe to read unconditionally — same as VelocityMag.

    Returns None if no inlet BC object is found.
    """
    for obj in doc.Objects:
        if getattr(obj, "BoundaryType", "") != "inlet":
            continue
        rate = getattr(obj, "VolFlowRate", None)
        if rate is None:
            continue
        try:
            return float(rate.getValueAs("m^3/s"))
        except AttributeError:
            try:
                return float(rate)
            except Exception:
                pass
    return None


def _read_all_cfdof_boundary_velocities(doc) -> dict:
    """Return {patch_name: velocity_m_s} for every inlet/outlet BC object.

    _read_cfdof_inlet_velocity() only returns the FIRST inlet-type object it
    finds, which silently drops every other velocity-driven boundary in a
    multi-inlet case (e.g. a manifold with a main 'inlet' plus 'pump01'..
    'pump04' branches, each linked to a different — or the same — spreadsheet
    alias). CfdOF writes each boundary object's Label verbatim as the
    OpenFOAM patch name, so keying by Label lets run_parametric_sim.py patch
    each named boundaryField block in 0/U individually instead of relying on
    a single global value + direction-matching heuristic (which only works
    when every inlet happens to point the same way as internalField).
    """
    out = {}
    for obj in doc.Objects:
        if getattr(obj, "BoundaryType", "") not in ("inlet", "outlet"):
            continue
        vel = getattr(obj, "VelocityMag", None)
        if vel is None:
            continue
        try:
            v = float(vel.getValueAs("m/s"))
        except AttributeError:
            try:
                v = float(vel)
            except Exception:
                continue
        out[obj.Label] = v
    return out


def _import_cfdof(name: str):
    """Import a CfdOF module by trying 'CfdOF.<name>' then '<name>' directly."""
    for full in (f"CfdOF.{name}", name):
        try:
            mod = __import__(full, fromlist=[name])
            return getattr(mod, name)   # return the class, not the module
        except (ImportError, AttributeError):
            pass
    return None


def _write_case_via_cfdof(doc, case_dir: str) -> bool:
    """Write a complete OpenFOAM case for the current geometry using CfdOF.

    CfdOF handles all STL generation (with correct patch names & metre units),
    blockMeshDict, snappyHexMeshDict, 0/ BCs — everything.  We only need to
    run the OpenFOAM pipeline afterwards.

    case_dir: Windows path to the per-parameter-set output directory.
    Returns True on success.
    """
    try:
        # Modern CfdOF's analysis container is an App::DocumentObjectGroupPython
        # whose Proxy class is CfdAnalysis.CfdAnalysis — it is NOT a
        # Fem::FemAnalysis object (that TypeId belongs to the FEM workbench).
        # CfdOF itself identifies it via isinstance(obj.Proxy, CfdAnalysis)
        # (see CfdTools.getActiveAnalysis); mirror that here by proxy class
        # name so this keeps working across CfdOF versions/object types.
        analysis = next(
            (o for o in doc.Objects
             if type(getattr(o, "Proxy", None)).__name__ == "CfdAnalysis"),
            None)
        mesh_obj = _find_cfdof_mesh_obj(doc)

        if analysis is None or mesh_obj is None:
            FreeCAD.Console.PrintMessage(
                "AI4CFD: CfdOF write — no analysis or mesh object; "
                "falling back to STL export\n")
            return False

        orig_mesh_path   = getattr(mesh_obj, "CasePath", "")
        orig_output_path = getattr(analysis, "OutputPath", "")
        # Both CfdMeshTools and CfdCaseWriterFoam read CfdTools.getOutputPath(analysis),
        # which returns analysis.OutputPath — NOT mesh_obj.CasePath.  Set both so
        # expression-linked BC values (e.g. =Spreadsheet.velocity on VelocityMag)
        # propagate correctly to the per-case 0/U after doc.recompute().
        mesh_obj.CasePath   = case_dir
        analysis.OutputPath = case_dir
        FreeCAD.Console.PrintMessage(
            f"AI4CFD: writing CfdOF case → {case_dir}\n")

        try:
            # ── mesh case (meshCase/ with STLs, dicts) ───────────────────────
            CfdMeshToolsCls = _import_cfdof("CfdMeshTools")
            if CfdMeshToolsCls is None:
                FreeCAD.Console.PrintWarning(
                    "AI4CFD: CfdMeshTools not importable — falling back\n")
                return False
            CfdMeshToolsCls(mesh_obj).writeMesh()
            FreeCAD.Console.PrintMessage("AI4CFD: CfdOF wrote meshCase ✓\n")

            # ── solver case (case/ with 0/, system/, constant/) ──────────────
            CfdCaseWriterCls = _import_cfdof("CfdCaseWriterFoam")
            if CfdCaseWriterCls is None:
                FreeCAD.Console.PrintWarning(
                    "AI4CFD: CfdCaseWriterFoam not importable — falling back\n")
                return False
            CfdCaseWriterCls(analysis).writeCase()
            FreeCAD.Console.PrintMessage("AI4CFD: CfdOF wrote case ✓\n")
            return True

        finally:
            mesh_obj.CasePath   = orig_mesh_path
            analysis.OutputPath = orig_output_path

    except Exception as exc:
        import traceback
        FreeCAD.Console.PrintWarning(
            f"AI4CFD: CfdOF write failed ({exc}) — falling back to STL export\n")
        FreeCAD.Console.PrintWarning(traceback.format_exc() + "\n")
        return False


# ── CfdOF patch export ────────────────────────────────────────────────────────

def _refs_prop_name(obj) -> str | None:
    """Return the name of obj's CfdOF face-reference property, or None.

    Modern CfdOF (see CfdFluidBoundary.py) renamed the boundary object's
    face-reference property from 'References' to 'ShapeRefs' — old code
    (and old documents) still migrate 'References' into 'ShapeRefs' on load,
    but never leave the legacy name behind. Check the current name first so
    parametric export actually finds each boundary's referenced faces
    instead of silently falling back to a much weaker per-face guess.
    """
    if hasattr(obj, "ShapeRefs"):
        return "ShapeRefs"
    if hasattr(obj, "References"):
        return "References"
    return None


def _get_faces_from_refs(references, doc) -> list:
    """Extract Part.Face elements from CfdOF-style References/ShapeRefs."""
    faces = []
    for ref in references:
        try:
            if not isinstance(ref, (list, tuple)) or len(ref) < 2:
                continue
            ref_obj, face_names = ref[0], ref[1]
            if isinstance(ref_obj, str):
                ref_obj = doc.getObject(ref_obj)
            if ref_obj is None or not hasattr(ref_obj, "Shape"):
                continue
            if isinstance(face_names, str):
                face_names = [face_names]
            for name in face_names:
                elem = ref_obj.Shape.getElement(name)
                if elem:
                    faces.append(elem)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(f"AI4CFD: face ref error {ref}: {exc}\n")
    return faces


def _shape_to_ascii_stl(shape, solid_name: str, deflection: float = 0.5) -> str:
    """Tessellate a Part shape and return ASCII STL with the given solid name.

    FreeCAD internal units are mm; OpenFOAM expects metres, so every vertex
    coordinate is multiplied by 0.001.
    """
    _S = 0.001   # mm → m
    verts, facets = shape.tessellate(deflection)
    lines = [f"solid {solid_name}"]
    for tri in facets:
        v = [(verts[i][0]*_S, verts[i][1]*_S, verts[i][2]*_S) for i in tri]
        ax, ay, az = v[1][0]-v[0][0], v[1][1]-v[0][1], v[1][2]-v[0][2]
        bx, by, bz = v[2][0]-v[0][0], v[2][1]-v[0][1], v[2][2]-v[0][2]
        nx, ny, nz = ay*bz-az*by, az*bx-ax*bz, ax*by-ay*bx
        mag = (nx*nx + ny*ny + nz*nz) ** 0.5
        if mag > 1e-12:
            nx, ny, nz = nx/mag, ny/mag, nz/mag
        lines += [
            f"  facet normal {nx:.6e} {ny:.6e} {nz:.6e}",
            "    outer loop",
            f"      vertex {v[0][0]:.6e} {v[0][1]:.6e} {v[0][2]:.6e}",
            f"      vertex {v[1][0]:.6e} {v[1][1]:.6e} {v[1][2]:.6e}",
            f"      vertex {v[2][0]:.6e} {v[2][1]:.6e} {v[2][2]:.6e}",
            "    endloop",
            "  endfacet",
        ]
    lines.append(f"endsolid {solid_name}")
    return "\n".join(lines) + "\n"


def _patch_names_from_template(tmpl_trisurf: str) -> list:
    """Return sorted patch_N_0 names found in the template's triSurface dir."""
    if not tmpl_trisurf or not os.path.isdir(tmpl_trisurf):
        return []
    return sorted(
        f[:-4] for f in os.listdir(tmpl_trisurf)
        if f.lower().startswith("patch_") and f.lower().endswith(".stl")
    )


def _wall_patch_from_createpatch(tmpl_base: str) -> str | None:
    """Return the patch_N prefix that maps to 'wall' in createPatchDict.

    tmpl_base: the root template directory (containing case/system/ or system/).
    Searches inside each patch definition block for 'name wall;' and extracts
    the corresponding patch_N number from its 'patches (...)' field.
    """
    import re
    for rel in ("case/system/createPatchDict", "system/createPatchDict"):
        path = os.path.join(tmpl_base, rel)
        if not os.path.isfile(path):
            continue
        try:
            content = open(path, encoding="utf-8", errors="replace").read()
            content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
            content = re.sub(r"//[^\n]*", "", content)

            # Locate the patches ( ... ) list
            pm = re.search(r"\bpatches\s*\(", content)
            if not pm:
                continue
            start, depth, i = pm.end(), 1, pm.end()
            while i < len(content) and depth > 0:
                if content[i] == "(":
                    depth += 1
                elif content[i] == ")":
                    depth -= 1
                i += 1
            patches_body = content[start: i - 1]

            # Walk each { ... } sub-block
            j = 0
            while j < len(patches_body):
                brace = patches_body.find("{", j)
                if brace == -1:
                    break
                depth2, k = 1, brace + 1
                while k < len(patches_body) and depth2 > 0:
                    if patches_body[k] == "{":
                        depth2 += 1
                    elif patches_body[k] == "}":
                        depth2 -= 1
                    k += 1
                block = patches_body[brace + 1: k - 1]
                j = k

                nm = re.search(r"\bname\s+(\w+)\s*;", block)
                if nm and nm.group(1) == "wall":
                    pm2 = re.search(r'"?patch_(\d+)', block)
                    if pm2:
                        return f"patch_{pm2.group(1)}"
        except Exception:
            pass
    return None


def _rebuild_body_geometry_stl(stl_dir: str, patch_names: list) -> None:
    """Concatenate individual patch STL files into Body_Geometry.stl."""
    sections = []
    for pname in patch_names:
        path = os.path.join(stl_dir, f"{pname}.stl")
        if os.path.isfile(path):
            sections.append(open(path, "r", encoding="ascii", errors="replace").read())
    if sections:
        with open(os.path.join(stl_dir, "Body_Geometry.stl"), "w") as fh:
            fh.writelines(sections)
        FreeCAD.Console.PrintMessage(
            f"AI4CFD: Body_Geometry.stl rebuilt from {len(sections)} patches\n")


def _stl_approx_centroid(stl_path: str):
    """Return (x, y, z) mean vertex position from an ASCII STL, or None."""
    import re as _re
    xs, ys, zs = [], [], []
    try:
        with open(stl_path, "r", errors="replace") as fh:
            for line in fh:
                m = _re.match(
                    r"\s*vertex\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)", line)
                if m:
                    xs.append(float(m.group(1)))
                    ys.append(float(m.group(2)))
                    zs.append(float(m.group(3)))
    except Exception:
        return None
    if not xs:
        return None
    n = len(xs)
    return (sum(xs) / n, sum(ys) / n, sum(zs) / n)


def _stl_has_curved_surface(stl_path: str) -> bool:
    """Return True if the STL contains any non-axis-aligned facet normal.

    A background-domain box has all normals along ±X/Y/Z (axis-aligned).
    A pipe body or aerofoil has at least one angled normal.
    This distinguishes static background boxes from body geometry patches.
    """
    import re as _re
    try:
        with open(stl_path, "r", errors="replace") as fh:
            for line in fh:
                m = _re.match(
                    r"\s*facet normal\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)",
                    line)
                if m:
                    nx = abs(float(m.group(1)))
                    ny = abs(float(m.group(2)))
                    nz = abs(float(m.group(3)))
                    if nx < 0.99 and ny < 0.99 and nz < 0.99:
                        return True
    except Exception:
        pass
    return False


def _stl_curved_fraction(stl_path: str):
    """Return fraction [0,1] of facet normals that are non-axis-aligned, or None on error."""
    import re as _re
    total = curved = 0
    try:
        with open(stl_path, "r", errors="replace") as fh:
            for line in fh:
                m = _re.match(
                    r"\s*facet normal\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)\s+([\d.eE+\-]+)",
                    line)
                if m:
                    total += 1
                    nx = abs(float(m.group(1)))
                    ny = abs(float(m.group(2)))
                    nz = abs(float(m.group(3)))
                    if nx < 0.99 and ny < 0.99 and nz < 0.99:
                        curved += 1
    except Exception:
        return None
    return curved / total if total > 0 else None


def _patch_role_map_from_createpatch(tmpl_base: str) -> dict:
    """Parse createPatchDict and return {semantic_name: patch_N_prefix}.

    e.g. {"wall": "patch_3", "inlet": "patch_1", "outlet": "patch_2",
          "defaultFaces": "patch_0"}
    """
    import re as _re
    result = {}
    for rel in ("case/system/createPatchDict", "system/createPatchDict"):
        path = os.path.join(tmpl_base, rel)
        if not os.path.isfile(path):
            continue
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
            text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
            text = _re.sub(r"//[^\n]*", "", text)
            pm = _re.search(r"\bpatches\s*\(", text)
            if not pm:
                continue
            start, depth, i = pm.end(), 1, pm.end()
            while i < len(text) and depth > 0:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            patches_body = text[start: i - 1]
            j = 0
            while j < len(patches_body):
                brace = patches_body.find("{", j)
                if brace == -1:
                    break
                d2, k = 1, brace + 1
                while k < len(patches_body) and d2 > 0:
                    if patches_body[k] == "{":
                        d2 += 1
                    elif patches_body[k] == "}":
                        d2 -= 1
                    k += 1
                block = patches_body[brace + 1: k - 1]
                j = k
                nm = _re.search(r"\bname\s+(\w+)\s*;", block)
                pm2 = _re.search(r'"?patch_(\d+)', block)
                if nm and pm2:
                    result[nm.group(1)] = f"patch_{pm2.group(1)}"
            if result:
                return result
        except Exception:
            pass
    return result


def _split_body_by_topology(shape, patch_names: list) -> list:
    """Split body faces into groups matching template patch names.

    Strategy:
      • Planar faces  → individual BC patches (inlet, outlet, …)
      • Curved faces  → grouped as the wall patch

    Returns [(patch_name, Part.Shape), …] in the same order as patch_names.
    """
    import Part

    planar, curved = [], []
    for face in shape.Faces:
        try:
            planar.append(face) if isinstance(face.Surface, Part.Plane) else curved.append(face)
        except Exception:
            curved.append(face)

    n = len(patch_names)
    groups = []

    if not patch_names:
        return groups

    if n == 1:
        groups = [(patch_names[0], shape)]

    elif n == 2:
        if len(planar) >= 1:
            groups = [
                (patch_names[0], Part.makeCompound(planar)),
                (patch_names[1], Part.makeCompound(curved or [shape])),
            ]
        else:
            mid = len(shape.Faces) // 2
            groups = [
                (patch_names[0], Part.makeCompound(shape.Faces[:mid] or [shape.Faces[0]])),
                (patch_names[1], Part.makeCompound(shape.Faces[mid:] or [shape.Faces[-1]])),
            ]

    elif n == 3:
        # Typical: patch_1_0=inlet, patch_2_0=outlet, patch_3_0=wall
        if len(planar) >= 2:
            # Sort planar faces by centroid Z (or area) so ordering is stable
            planar.sort(key=lambda f: f.CenterOfMass.z)
            groups = [
                (patch_names[0], Part.makeCompound([planar[0]])),
                (patch_names[1], Part.makeCompound([planar[-1]])),
                (patch_names[2], Part.makeCompound(curved or planar[1:-1] or [shape])),
            ]
        elif len(planar) == 1:
            groups = [
                (patch_names[0], Part.makeCompound(planar)),
                (patch_names[1], Part.makeCompound(curved[:len(curved)//2] or [shape])),
                (patch_names[2], Part.makeCompound(curved[len(curved)//2:] or [shape])),
            ]
        else:
            # No planar faces — split curved by thirds
            f = shape.Faces
            t = len(f) // 3 or 1
            groups = [
                (patch_names[0], Part.makeCompound(f[:t])),
                (patch_names[1], Part.makeCompound(f[t:2*t])),
                (patch_names[2], Part.makeCompound(f[2*t:])),
            ]

    else:
        # Generic: one face per patch (best effort)
        all_f = shape.Faces
        for i, pname in enumerate(patch_names):
            face = all_f[i] if i < len(all_f) else all_f[-1]
            groups.append((pname, face))

    return groups


def _write_patches(stl_dir: str, groups: list) -> None:
    """Write individual patch_N_0.stl files and a merged Body_Geometry.stl."""
    body_sections = []
    for pname, shape in groups:
        stl_str = _shape_to_ascii_stl(shape, pname)
        with open(os.path.join(stl_dir, f"{pname}.stl"), "w") as fh:
            fh.write(stl_str)
        body_sections.append(stl_str)
        FreeCAD.Console.PrintMessage(f"AI4CFD: exported {pname}.stl\n")
    if body_sections:
        with open(os.path.join(stl_dir, "Body_Geometry.stl"), "w") as fh:
            fh.writelines(body_sections)
        FreeCAD.Console.PrintMessage(
            f"AI4CFD: exported Body_Geometry.stl ({len(body_sections)} solids)\n")


def _export_stls_for_case(doc, body, stl_dir: str,
                           tmpl_trisurf: str = None) -> tuple:
    """Export geometry STLs for one case.

    Priority:
    1. CfdOF fluid-boundary objects (BoundaryType + References) → exact patch export
    2. Template-guided wall-only export — copy patch_0/1/2 (domain boundaries) from
       the template and replace only the wall patch (patch_3) with the new body.
       This is the correct strategy for external-aerodynamics parametric studies where
       the outer domain box and inlet/outlet faces are fixed; only the body surface
       (aerofoil wall) changes.
    3. Topology split for cases without a patch_0 in the template.
    4. Single geometry.stl fallback.

    All STLs are ASCII and scaled mm → m.
    """
    import Part
    import shutil

    os.makedirs(stl_dir, exist_ok=True)

    # ── Method 1: CfdOF boundary objects ─────────────────────────────────────
    boundaries = []
    for obj in doc.Objects:
        refs_prop = _refs_prop_name(obj)
        if refs_prop is None:
            continue
        has_bt = hasattr(obj, "BoundaryType")
        proxy = getattr(obj, "Proxy", None)
        ptype = (getattr(proxy, "Type", "") or type(proxy).__name__) if proxy else ""
        has_proxy = "Boundary" in ptype
        if (has_bt or has_proxy) and getattr(obj, refs_prop, []):
            boundaries.append((obj, refs_prop))

    if boundaries:
        body_sections = []
        for i, (bc, refs_prop) in enumerate(boundaries, 1):
            pname = f"patch_{i}_0"
            faces = _get_faces_from_refs(getattr(bc, refs_prop), doc)
            if not faces:
                FreeCAD.Console.PrintWarning(
                    f"AI4CFD: no faces resolved for {pname} ({bc.Label})\n")
                continue
            try:
                stl_str = _shape_to_ascii_stl(Part.makeCompound(faces), pname)
                with open(os.path.join(stl_dir, f"{pname}.stl"), "w") as fh:
                    fh.write(stl_str)
                body_sections.append(stl_str)
                FreeCAD.Console.PrintMessage(f"AI4CFD: exported {pname}.stl\n")
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    f"AI4CFD: {pname} export failed: {exc}\n")
        if body_sections:
            with open(os.path.join(stl_dir, "Body_Geometry.stl"), "w") as fh:
                fh.writelines(body_sections)
            FreeCAD.Console.PrintMessage(
                f"AI4CFD: Body_Geometry.stl ({len(body_sections)} regions from CfdOF)\n")
            return "cfdof-patches", None

    # ── Discover template patch names ─────────────────────────────────────────
    patch_names = _patch_names_from_template(tmpl_trisurf)
    FreeCAD.Console.PrintMessage(
        f"AI4CFD: template patches: {patch_names}\n")

    # ── Method 2: smart per-patch export ─────────────────────────────────────
    # Uses createPatchDict to understand every patch role, then exports:
    #   patch_0_* with curved surface → full closed body (internal-flow pipe)
    #   patch_0_* with axis-only normals → static background box (external flow)
    #   patch_1_* (inlet)  → planar face closest to template inlet centroid
    #   patch_2_* (outlet) → the other planar end-cap face
    #   patch_3_* (wall)   → all non-planar (curved) body faces only
    domain_patches = [p for p in patch_names if p.startswith("patch_0")]
    if domain_patches and tmpl_trisurf and patch_names:
        try:
            tmpl_base = os.path.dirname(        # triSurface/ → constant/
                            os.path.dirname(    # constant/   → meshCase/
                                os.path.dirname(tmpl_trisurf)))  # meshCase/ → root
            role_map      = _patch_role_map_from_createpatch(tmpl_base)
            wall_prefix   = role_map.get("wall",         "patch_3")
            inlet_prefix  = role_map.get("inlet",        "patch_1")
            outlet_prefix = role_map.get("outlet",       "patch_2")
            body_prefix   = role_map.get("defaultFaces", "patch_0")

            FreeCAD.Console.PrintMessage(
                f"AI4CFD: patch roles  wall={wall_prefix}  inlet={inlet_prefix}"
                f"  outlet={outlet_prefix}  body/default={body_prefix}\n")

            # Split body faces: planar end-caps (inlet/outlet) vs curved wall
            planar_faces = []
            wall_faces   = []
            for face in body.Shape.Faces:
                try:
                    (planar_faces if isinstance(face.Surface, Part.Plane)
                     else wall_faces).append(face)
                except Exception:
                    wall_faces.append(face)

            FreeCAD.Console.PrintMessage(
                f"AI4CFD: body faces — {len(planar_faces)} planar,"
                f" {len(wall_faces)} curved\n")

            # Identify which planar face is inlet vs outlet using template centroid
            inlet_face = outlet_face = None
            if len(planar_faces) >= 2:
                i_patch = next(
                    (p for p in patch_names if p.startswith(inlet_prefix + "_")), None)
                tmpl_c = (_stl_approx_centroid(
                              os.path.join(tmpl_trisurf, f"{i_patch}.stl"))
                          if i_patch else None)
                if tmpl_c:
                    def _d(f):
                        c = f.CenterOfMass   # FreeCAD internal units = mm
                        return ((c.x * 0.001 - tmpl_c[0]) ** 2 +
                                (c.y * 0.001 - tmpl_c[1]) ** 2 +
                                (c.z * 0.001 - tmpl_c[2]) ** 2) ** 0.5
                    planar_faces.sort(key=_d)    # closest to template inlet first
                else:
                    planar_faces.sort(key=lambda f: f.CenterOfMass.z, reverse=True)
                inlet_face  = planar_faces[0]
                outlet_face = planar_faces[-1]
            elif planar_faces:
                inlet_face = outlet_face = planar_faces[0]

            # Export or copy each patch
            for pname in patch_names:
                stl_out  = os.path.join(stl_dir,     f"{pname}.stl")
                tmpl_src = os.path.join(tmpl_trisurf, f"{pname}.stl")

                if pname.startswith(body_prefix + "_"):
                    # Background box (axis-aligned normals) → copy; pipe body → regenerate
                    if os.path.isfile(tmpl_src) and not _stl_has_curved_surface(tmpl_src):
                        shutil.copy2(tmpl_src, stl_out)
                        FreeCAD.Console.PrintMessage(
                            f"AI4CFD:  {pname}.stl ← template (background box)\n")
                    else:
                        stl = _shape_to_ascii_stl(body.Shape, pname)
                        with open(stl_out, "w") as fh:
                            fh.write(stl)
                        FreeCAD.Console.PrintMessage(
                            f"AI4CFD:  {pname}.stl ← full body (closed solid)\n")

                elif pname.startswith(wall_prefix + "_"):
                    faces = wall_faces if wall_faces else body.Shape.Faces
                    stl = _shape_to_ascii_stl(Part.makeCompound(faces), pname)
                    with open(stl_out, "w") as fh:
                        fh.write(stl)
                    FreeCAD.Console.PrintMessage(
                        f"AI4CFD:  {pname}.stl ← {len(faces)} wall face(s)\n")

                elif pname.startswith(inlet_prefix + "_") and inlet_face is not None:
                    stl = _shape_to_ascii_stl(Part.makeCompound([inlet_face]), pname)
                    with open(stl_out, "w") as fh:
                        fh.write(stl)
                    FreeCAD.Console.PrintMessage(f"AI4CFD:  {pname}.stl ← inlet face\n")

                elif pname.startswith(outlet_prefix + "_") and outlet_face is not None:
                    stl = _shape_to_ascii_stl(Part.makeCompound([outlet_face]), pname)
                    with open(stl_out, "w") as fh:
                        fh.write(stl)
                    FreeCAD.Console.PrintMessage(f"AI4CFD:  {pname}.stl ← outlet face\n")

                else:
                    if os.path.isfile(tmpl_src):
                        shutil.copy2(tmpl_src, stl_out)
                        FreeCAD.Console.PrintMessage(
                            f"AI4CFD:  {pname}.stl ← template (unknown role)\n")

            _rebuild_body_geometry_stl(stl_dir, patch_names)

            # Compute locationInMesh: step inward from inlet centroid by half pipe radius
            location_in_mesh = None
            if inlet_face is not None:
                import math as _math
                c = inlet_face.CenterOfMass        # FreeCAD mm
                n = inlet_face.normalAt(0, 0)      # outward unit normal
                r_mm = _math.sqrt(max(inlet_face.Area, 1.0) / _math.pi)
                off = r_mm * 0.5                   # half pipe radius, inward
                location_in_mesh = (
                    (c.x - n.x * off) * 0.001,
                    (c.y - n.y * off) * 0.001,
                    (c.z - n.z * off) * 0.001,
                )
                FreeCAD.Console.PrintMessage(
                    f"AI4CFD: locationInMesh = "
                    f"({location_in_mesh[0]:.5g}, {location_in_mesh[1]:.5g},"
                    f" {location_in_mesh[2]:.5g}) m\n")

            return "smart-patch-export", location_in_mesh

        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"AI4CFD: smart patch export failed ({exc}) — falling back\n")
            import traceback
            FreeCAD.Console.PrintWarning(traceback.format_exc() + "\n")

    # ── Method 3: topology split (no outer-domain patch in template) ──────────
    if patch_names:
        try:
            groups = _split_body_by_topology(body.Shape, patch_names)
            if groups:
                _write_patches(stl_dir, groups)
                return "topology-split", None
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"AI4CFD: topology split failed ({exc}) — falling back to single STL\n")

    # ── Method 4: single body fallback ───────────────────────────────────────
    FreeCAD.Console.PrintMessage("AI4CFD: exporting single body STL\n")
    stl_str = _shape_to_ascii_stl(body.Shape, "geometry")
    with open(os.path.join(stl_dir, "geometry.stl"), "w") as fh:
        fh.write(stl_str)
    return "single", None


def _export_refinement_stls(doc, body, stl_dir: str, tmpl_trisurf: str,
                             patch_names: list) -> None:
    """Regenerate surface-refinement and other non-patch STLs in triSurface.

    Strategy 1 — FreeCAD References: find the object whose label matches the
    filename, resolve its References faces after recompute, tessellate them.

    Strategy 2 — geometric match: compare the curved-normal fraction of the
    template refinement STL against each template patch STL; copy the newly-
    generated patch STL whose fraction is closest.  For a wall-surface
    refinement (all curved normals) this reliably selects the wall patch.

    Final fallback: copy unchanged from template.
    """
    import Part
    import shutil
    if not tmpl_trisurf or not os.path.isdir(tmpl_trisurf):
        return
    os.makedirs(stl_dir, exist_ok=True)

    known = set(patch_names) | {"Body_Geometry"}
    for fname in sorted(os.listdir(tmpl_trisurf)):
        if not fname.lower().endswith(".stl"):
            continue
        stem = fname[:-4]
        if stem in known:
            continue
        stl_out  = os.path.join(stl_dir, fname)
        tmpl_src = os.path.join(tmpl_trisurf, fname)

        # ── Strategy 1: FreeCAD object label/name → References/ShapeRefs ─────
        match = None
        match_refs_prop = None
        stem_clean = stem.replace("_", "").lower()
        for obj in doc.Objects:
            obj_clean = obj.Label.replace(" ", "").replace("_", "").lower()
            if (obj.Label.replace(" ", "_") == stem
                    or obj.Label == stem
                    or obj.Name == stem
                    or obj_clean == stem_clean):
                refs_prop = _refs_prop_name(obj)
                if refs_prop is not None and getattr(obj, refs_prop, []):
                    match = obj
                    match_refs_prop = refs_prop
                    break

        if match is not None:
            try:
                faces = _get_faces_from_refs(getattr(match, match_refs_prop), doc)
                if faces:
                    stl_str = _shape_to_ascii_stl(Part.makeCompound(faces), stem)
                    with open(stl_out, "w") as fh:
                        fh.write(stl_str)
                    FreeCAD.Console.PrintMessage(
                        f"AI4CFD:  {fname} regenerated from {match.Label} References\n")
                    continue
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    f"AI4CFD:  {fname} via References failed ({exc}) — trying geometry match\n")

        # ── Strategy 2: curved-normal fraction matching against patch STLs ────
        # Compare the template refinement STL's curved-normal fraction to each
        # newly-exported patch STL and copy the closest match.
        # If the template is binary (ref_frac=None), fall back to the patch with
        # the highest curved fraction — that is always the curved wall surface,
        # which is exactly what a wall-surface refinement region needs.
        ref_frac = _stl_curved_fraction(tmpl_src)
        best_patch, best_score = None, float("inf")
        for pname in patch_names:
            new_stl = os.path.join(stl_dir, f"{pname}.stl")
            if not os.path.isfile(new_stl):
                continue
            pf = _stl_curved_fraction(new_stl)
            if pf is None:
                continue
            if ref_frac is not None:
                # Minimise |fraction difference|
                score = abs(pf - ref_frac)
            else:
                # Template unreadable (binary) — pick the most-curved patch
                score = 1.0 - pf
            if score < best_score:
                best_score, best_patch = score, pname

        if best_patch is not None:
            new_stl = os.path.join(stl_dir, f"{best_patch}.stl")
            shutil.copy2(new_stl, stl_out)
            tag = f"ref={ref_frac:.2f}, " if ref_frac is not None else "template binary, "
            FreeCAD.Console.PrintMessage(
                f"AI4CFD:  {fname} ← {best_patch}.stl "
                f"({tag}score={best_score:.3f})\n")
            continue

        # ── Final fallback: copy unchanged from template ──────────────────────
        shutil.copy2(tmpl_src, stl_out)
        FreeCAD.Console.PrintMessage(
            f"AI4CFD:  {fname} ← template (fallback — no exported patch matched)\n")


def _find_exportable_shapes(doc) -> list:
    """Return (label, Name) pairs for objects that have an exportable Shape."""
    result = []
    if doc is None:
        return result
    for obj in doc.Objects:
        if obj.Name.startswith("AI4CFD_"):
            continue
        shape = getattr(obj, "Shape", None)
        if shape is None:
            continue
        try:
            if not shape.isNull() and shape.Solids:
                result.append((obj.Label, obj.Name))
        except Exception:
            pass
    return result


# ── Sampling ──────────────────────────────────────────────────────────────────

def _lhs(n: int, bounds: dict) -> list:
    """Latin Hypercube Sampling."""
    keys = list(bounds)
    d    = len(keys)
    if d == 0:
        return [{}] * n
    u = np.zeros((n, d))
    for j in range(d):
        perm    = np.random.permutation(n)
        u[:, j] = (perm + np.random.uniform(size=n)) / n
    return [
        {k: float(bounds[k][0] + u[i, j] * (bounds[k][1] - bounds[k][0]))
         for j, k in enumerate(keys)}
        for i in range(n)
    ]


# ── Phase-2 batch simulation worker ──────────────────────────────────────────

class _BatchWorker(QObject):
    line_ready   = Signal(str)
    sim_started  = Signal(int, int)
    sim_finished = Signal(int, int, int)
    all_done     = Signal(int, int)

    def __init__(self, tasks: list, n_workers: int, cores_per_sim: int,
                 template_wsl: str = "",
                 inference_prep: bool = False) -> None:
        super().__init__()
        self._tasks          = tasks        # list of (case_dir_win, params_dict)
        self._n_workers      = n_workers
        self._cores_per      = cores_per_sim
        self._template_wsl   = template_wsl  # WSL path to template case
        self._inference_prep = inference_prep
        self._stopped        = False
        self._lock           = threading.Lock()
        self._procs: list    = []

    def stop(self) -> None:
        self._stopped = True
        with self._lock:
            for p in self._procs:
                try:
                    if p.poll() is None:
                        p.kill()
                except Exception:
                    pass

    def _run_one(self, idx: int, case_dir: str, params: dict) -> tuple:
        if self._stopped:
            return idx, -1
        total = len(self._tasks)
        self.sim_started.emit(idx + 1, total)
        self.line_ready.emit(
            f"\n─── Sim {idx+1}/{total}  [{os.path.basename(case_dir)}] ───\n" +
            "\n".join(
                f"  {k} = {v:.4g}" if isinstance(v, (int, float)) else f"  {k} = {v}"
                for k, v in params.items()
            )
        )
        wsl_case = (_win_to_wsl(case_dir)
                    if _ON_WINDOWS and len(case_dir) > 1 and case_dir[1] == ":"
                    else case_dir)
        cmd = build_parametric_sim_cmd(
            case_dir       = wsl_case,
            template_dir   = self._template_wsl,
            params_json    = json.dumps(params),
            num_cores      = self._cores_per,
            inference_prep = self._inference_prep,
        )
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        with self._lock:
            self._procs.append(proc)
        if self._stopped:
            proc.kill()
        for raw in proc.stdout:
            self.line_ready.emit(
                f"[{idx+1}] {raw.decode('utf-8', errors='replace').rstrip()}")
        proc.wait()
        import ctypes
        return idx, ctypes.c_int32(proc.returncode).value

    def run(self) -> None:
        total     = len(self._tasks)
        successes = 0
        with ThreadPoolExecutor(max_workers=self._n_workers) as exe:
            futures = {exe.submit(self._run_one, i, cd, p): i
                       for i, (cd, p) in enumerate(self._tasks)}
            for fut in as_completed(futures):
                idx, rc = fut.result()
                self.sim_finished.emit(idx + 1, total, rc)
                if rc == 0:
                    successes += 1
        self.all_done.emit(successes, total)


# ── Main panel ────────────────────────────────────────────────────────────────

_VARY_BG  = QColor("#e3f2fd")
_FIXED_BG = QColor("#ffffff")


class CFDParametricPanel:
    """Two-phase parametric study: export geometry cases then run batch CFD."""

    def __init__(self, obj_name: str = None) -> None:
        self._obj_name  = obj_name
        self._sheet_obj = None
        self._rows: dict  = {}
        self._varied: set = set()
        self._exported_cases: list = []   # [(case_dir, params_dict)]
        self._worker  = None
        self._thread  = None
        self._exporting = False

        self.form = QtWidgets.QWidget()
        self.form.setWindowTitle("CFD Parametric Study — AI4CFD")
        root = QtWidgets.QVBoxLayout(self.form)
        root.setSpacing(5)

        # ── Spreadsheet selector ──────────────────────────────────────────────
        grp_ss = QtWidgets.QGroupBox("1 · Parameters (from spreadsheet)")
        lay_ss = QtWidgets.QVBoxLayout(grp_ss)

        sel_row = QtWidgets.QHBoxLayout()
        sel_row.addWidget(QtWidgets.QLabel("Spreadsheet:"))
        self._sheet_combo = QtWidgets.QComboBox()
        self._sheet_combo.setToolTip(
            "Numeric aliases in this spreadsheet become the study parameters.\n"
            "Link geometry dimensions to aliases with expressions like  =Spreadsheet.pipe_radius")
        self._sheet_combo.currentIndexChanged.connect(self._on_sheet_changed)
        sel_row.addWidget(self._sheet_combo, 1)
        ref_btn = QtWidgets.QPushButton("↻")
        ref_btn.setFixedWidth(28)
        ref_btn.clicked.connect(self._populate_sheet_combo)
        sel_row.addWidget(ref_btn)
        lay_ss.addLayout(sel_row)

        # Parameter table
        self._table = QtWidgets.QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Alias", "Current", "Fixed", "Min", "Max", "Steps"])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        self._table.setColumnWidth(0, 120)
        self._table.setColumnWidth(1,  68)
        self._table.setColumnWidth(2,  72)
        self._table.setColumnWidth(3,  72)
        self._table.setColumnWidth(4,  72)
        self._table.setColumnWidth(5,  50)
        self._table.cellDoubleClicked.connect(self._toggle_vary)
        self._table.setMinimumHeight(140)
        lay_ss.addWidget(self._table)
        lay_ss.addWidget(QtWidgets.QLabel(
            "<i>Double-click a row to toggle <b>Fixed ↔ Vary</b> mode.</i>"))
        root.addWidget(grp_ss)

        # ── Sampling ──────────────────────────────────────────────────────────
        grp_samp  = QtWidgets.QGroupBox("2 · Sampling")
        samp_vlay = QtWidgets.QVBoxLayout(grp_samp)   # owns the group box

        lay_samp = QtWidgets.QHBoxLayout()             # no parent yet
        self._rb_sweep = QtWidgets.QRadioButton("Grid sweep")
        self._rb_rand  = QtWidgets.QRadioButton("Random")
        self._rb_lhs   = QtWidgets.QRadioButton("Latin Hypercube")
        self._rb_sweep.setChecked(True)
        for rb in (self._rb_sweep, self._rb_rand, self._rb_lhs):
            lay_samp.addWidget(rb)
            rb.toggled.connect(self._refresh_count)
            rb.toggled.connect(self._sync_steps_enabled)
        lay_samp.addWidget(QtWidgets.QLabel("  N ="))
        self._n_spin = QtWidgets.QSpinBox()
        self._n_spin.setRange(2, 2000)
        self._n_spin.setValue(20)
        self._n_spin.setEnabled(False)
        self._n_spin.valueChanged.connect(self._refresh_count)
        lay_samp.addWidget(self._n_spin)
        samp_vlay.addLayout(lay_samp)

        count_row = QtWidgets.QHBoxLayout()            # no parent yet
        self._count_lbl = QtWidgets.QLabel("Total simulations: —")
        count_row.addWidget(self._count_lbl, 1)
        prev_btn = QtWidgets.QPushButton("Preview…")
        prev_btn.setToolTip("Show first 50 parameter sets")
        prev_btn.clicked.connect(self._preview_samples)
        count_row.addWidget(prev_btn)
        samp_vlay.addLayout(count_row)

        root.addWidget(grp_samp)

        # ── Phase 1: Export geometry ──────────────────────────────────────────
        grp_exp = QtWidgets.QGroupBox("3 · Export geometry cases (main thread)")
        lay_exp = QtWidgets.QFormLayout(grp_exp)

        # Body to export
        body_row = QtWidgets.QHBoxLayout()
        self._body_combo = QtWidgets.QComboBox()
        self._body_combo.setToolTip("Solid body whose STL is written for each parameter set")
        body_row.addWidget(self._body_combo, 1)
        body_ref = QtWidgets.QPushButton("↻")
        body_ref.setFixedWidth(28)
        body_ref.clicked.connect(self._populate_body_combo)
        body_row.addWidget(body_ref)
        lay_exp.addRow("Body:", body_row)

        # Output directory
        out_row = QtWidgets.QHBoxLayout()
        self._out_edit = QtWidgets.QLineEdit(_DEFAULT_OUT)
        out_row.addWidget(self._out_edit, 1)
        browse_btn = QtWidgets.QPushButton("…")
        browse_btn.setFixedWidth(28)
        browse_btn.clicked.connect(self._browse_out)
        out_row.addWidget(browse_btn)
        lay_exp.addRow("Output dir:", out_row)

        # OpenFOAM template case directory
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

        hint = QtWidgets.QLabel(
            "<i>Tip: run your case once via CfdOF, then paste that case path here.<br>"
            "Spreadsheet aliases can be named anything — only the geometry changes per run.</i>")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555; font-size:9px;")
        lay_exp.addRow(hint)

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

        # ── Phase 2: Run simulations ──────────────────────────────────────────
        grp_run = QtWidgets.QGroupBox("4 · Run batch simulations")
        lay_run = QtWidgets.QFormLayout(grp_run)

        self._n_workers = QtWidgets.QSpinBox()
        self._n_workers.setRange(1, 32)
        self._n_workers.setValue(4)
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
        self._populate_sheet_combo()
        self._populate_body_combo()
        self._auto_populate_template()

    # ── Spreadsheet / body population ────────────────────────────────────────

    def _populate_sheet_combo(self) -> None:
        doc    = FreeCAD.activeDocument()
        sheets = _find_spreadsheets(doc)
        self._sheet_combo.blockSignals(True)
        prev = self._sheet_combo.currentText()
        self._sheet_combo.clear()
        for s in sheets:
            self._sheet_combo.addItem(s.Label, s.Name)
        idx = self._sheet_combo.findText(prev)
        self._sheet_combo.setCurrentIndex(max(idx, 0))
        self._sheet_combo.blockSignals(False)
        self._on_sheet_changed()

    def _on_sheet_changed(self) -> None:
        doc = FreeCAD.activeDocument()
        name = self._sheet_combo.currentData()
        self._sheet_obj = doc.getObject(name) if (doc and name) else None
        self._build_param_table()
        self._refresh_count()

    def _auto_populate_template(self) -> None:
        """Pre-fill the template field from the CfdOF mesh object's CasePath."""
        if self._tmpl_edit.text().strip():
            return  # user already set it
        doc = FreeCAD.activeDocument()
        if doc is None:
            return
        mesh_obj = _find_cfdof_mesh_obj(doc)
        if mesh_obj is None:
            return
        case_path = getattr(mesh_obj, "CasePath", "")
        if _ON_WINDOWS and case_path.startswith("/mnt/"):
            case_path = _wsl_to_win(case_path)
        if case_path and os.path.isdir(case_path):
            self._tmpl_edit.setText(case_path)
            FreeCAD.Console.PrintMessage(
                f"AI4CFD: template auto-set from CfdOF CasePath: {case_path}\n")

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

    def _populate_body_combo(self) -> None:
        doc = FreeCAD.activeDocument()
        shapes = _find_exportable_shapes(doc)
        prev = self._body_combo.currentData()
        self._body_combo.clear()
        for label, name in shapes:
            self._body_combo.addItem(label, name)
        idx = self._body_combo.findData(prev)
        self._body_combo.setCurrentIndex(max(idx, 0))

    def _browse_out(self) -> None:
        d = QtWidgets.QFileDialog.getExistingDirectory(
            self.form, "Select output directory", self._out_edit.text())
        if d:
            self._out_edit.setText(d)

    # ── Parameter table ───────────────────────────────────────────────────────

    def _build_param_table(self) -> None:
        self._table.setRowCount(0)
        self._rows.clear()
        self._varied.clear()

        if self._sheet_obj is None:
            return
        aliases = _get_aliases(self._sheet_obj)
        if not aliases:
            return

        for alias, cur_val in sorted(aliases.items()):
            row = self._table.rowCount()
            self._table.insertRow(row)

            name_item = QtWidgets.QTableWidgetItem(alias)
            name_item.setFlags(QtCore.Qt.ItemIsEnabled)
            f = QFont(); f.setBold(True)
            name_item.setFont(f)
            self._table.setItem(row, 0, name_item)

            cur_item = QtWidgets.QTableWidgetItem(f"{cur_val:.4g}")
            cur_item.setFlags(QtCore.Qt.ItemIsEnabled)
            cur_item.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            self._table.setItem(row, 1, cur_item)

            fixed = _dbl_spin(cur_val)
            fixed.valueChanged.connect(self._refresh_count)
            self._table.setCellWidget(row, 2, fixed)

            margin  = abs(cur_val) * 0.5 or 1.0
            mn = _dbl_spin(cur_val - margin)
            mn.setEnabled(False)
            mn.valueChanged.connect(self._refresh_count)
            self._table.setCellWidget(row, 3, mn)

            mx = _dbl_spin(cur_val + margin)
            mx.setEnabled(False)
            mx.valueChanged.connect(self._refresh_count)
            self._table.setCellWidget(row, 4, mx)

            steps = QtWidgets.QSpinBox()
            steps.setRange(2, 200)
            steps.setValue(5)
            steps.setEnabled(False)
            steps.valueChanged.connect(self._refresh_count)
            self._table.setCellWidget(row, 5, steps)

            self._rows[alias] = {
                "row": row, "fixed": fixed,
                "min": mn, "max": mx, "steps": steps,
            }

    def _toggle_vary(self, row: int, col: int) -> None:
        item = self._table.item(row, 0)
        if item is None:
            return
        key = item.text()
        if key not in self._rows:
            return
        w = self._rows[key]
        if key in self._varied:
            self._varied.discard(key)
            w["fixed"].setEnabled(True)
            w["min"].setEnabled(False)
            w["max"].setEnabled(False)
            w["steps"].setEnabled(False)
            for c in range(6):
                it = self._table.item(row, c)
                if it:
                    it.setBackground(_FIXED_BG)
        else:
            self._varied.add(key)
            w["fixed"].setEnabled(False)
            w["min"].setEnabled(True)
            w["max"].setEnabled(True)
            w["steps"].setEnabled(self._rb_sweep.isChecked())
            for c in range(6):
                it = self._table.item(row, c)
                if it:
                    it.setBackground(_VARY_BG)
        self._refresh_count()

    def _sync_steps_enabled(self) -> None:
        sweep = self._rb_sweep.isChecked()
        self._n_spin.setEnabled(not sweep)
        for key in self._varied:
            self._rows[key]["steps"].setEnabled(sweep)

    # ── Task generation ───────────────────────────────────────────────────────

    def _generate_tasks(self) -> list:
        fixed  = {k: w["fixed"].value()
                  for k, w in self._rows.items() if k not in self._varied}
        bounds = {k: (self._rows[k]["min"].value(), self._rows[k]["max"].value())
                  for k in self._varied}

        if not bounds:
            return [fixed.copy()] if fixed else []

        if self._rb_rand.isChecked():
            n = self._n_spin.value()
            return [
                {**fixed,
                 **{k: float(np.random.uniform(lo, hi))
                    for k, (lo, hi) in bounds.items()}}
                for _ in range(n)
            ]

        if self._rb_lhs.isChecked():
            return [{**fixed, **pt} for pt in _lhs(self._n_spin.value(), bounds)]

        # Grid sweep — independent 1-D per varied param
        mids  = {k: (lo + hi) / 2 for k, (lo, hi) in bounds.items()}
        tasks = []
        for key, (lo, hi) in bounds.items():
            steps = self._rows[key]["steps"].value()
            base  = {**fixed, **mids}
            for v in np.linspace(lo, hi, steps):
                p = base.copy(); p[key] = float(v)
                tasks.append(p)
        return tasks

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
                "No tasks — vary at least one parameter first.")
            return

        dlg = QtWidgets.QDialog(self.form)
        dlg.setWindowTitle(f"Sample preview  ({len(tasks)} total)")
        dlg.resize(680, 350)
        lay = QtWidgets.QVBoxLayout(dlg)

        shown = tasks[:50]
        keys  = list(shown[0].keys())
        tbl   = QtWidgets.QTableWidget(len(shown), len(keys) + 1)
        tbl.setHorizontalHeaderLabels(["#"] + keys)
        for r, params in enumerate(shown):
            tbl.setItem(r, 0, _right_item(str(r + 1)))
            for c, k in enumerate(keys):
                tbl.setItem(r, c + 1, _right_item(f"{params.get(k, 0):.4g}"))
        tbl.resizeColumnsToContents()
        lay.addWidget(tbl)
        if len(tasks) > 50:
            lay.addWidget(QtWidgets.QLabel(
                f"<i>Showing 50 of {len(tasks)}.</i>"))
        btn = QtWidgets.QPushButton("Close")
        btn.clicked.connect(dlg.accept)
        lay.addWidget(btn)
        dlg.exec_()

    # ── Phase 1: Export geometry ──────────────────────────────────────────────

    def _export_cases(self) -> None:
        if self._exporting:
            return

        doc = FreeCAD.activeDocument()
        if doc is None:
            self._export_status.setText("No active document.")
            return
        if self._sheet_obj is None:
            self._export_status.setText("Select a spreadsheet first.")
            return

        body_name = self._body_combo.currentData()
        if not body_name:
            self._export_status.setText("Select a body to export.")
            return
        body = doc.getObject(body_name)
        if body is None or not hasattr(body, "Shape"):
            self._export_status.setText("Body not found or has no Shape.")
            return

        tasks   = self._generate_tasks()
        out_dir = self._out_edit.text().strip()
        if not out_dir:
            self._export_status.setText("Set an output directory.")
            return

        self._exporting = True
        self._export_btn.setEnabled(False)
        self._exported_cases.clear()
        self._export_progress.setMaximum(len(tasks))
        self._export_progress.setValue(0)

        # Save original alias values to restore after export
        original = _get_aliases(self._sheet_obj)

        try:
            for i, params in enumerate(tasks):
                # 1. Push parameter values into spreadsheet
                _set_aliases(self._sheet_obj, params)

                # 2. Force full recompute.
                # doc.recompute() without flags only recomputes objects that
                # FreeCAD has already marked as touched.  After programmatic
                # spreadsheet changes the dependency chain may not be marked,
                # so we explicitly touch the sheet and pass force=True so
                # every object in the DAG is recomputed.
                try:
                    self._sheet_obj.recompute()   # flush sheet alias values first
                except Exception:
                    pass
                try:
                    self._sheet_obj.touch()       # mark dependents as dirty
                except Exception:
                    pass
                doc.recompute(None, True, True)   # force=True, checkExternal=True
                QtWidgets.QApplication.processEvents()

                # Re-fetch body after recompute to guarantee fresh Shape reference
                body = doc.getObject(body_name)
                if body is None or not hasattr(body, "Shape"):
                    self._export_status.setText(
                        f"Case {i+1}: body lost after recompute — skipping")
                    continue

                # Validate geometry: check for closed solid and no recompute errors
                shape_ok = True
                try:
                    shape = body.Shape
                    if shape.isNull():
                        raise ValueError("shape is null")
                    if not shape.Solids:
                        raise ValueError("no solids — geometry may be open/invalid")
                    # Check FreeCAD object state for recompute errors
                    state = getattr(body, "State", [])
                    if isinstance(state, (list, tuple)) and "Invalid" in state:
                        raise ValueError(f"object state = {state}")
                except Exception as exc:
                    shape_ok = False
                    FreeCAD.Console.PrintWarning(
                        f"AI4CFD param case {i+1}: geometry invalid ({exc}) "
                        f"— params={params} — SKIPPING\n"
                    )
                    self._export_status.setText(
                        f"Case {i+1}: geometry invalid — {exc} (skipped)")

                # Diagnostic: log bounding box to confirm geometry changed
                if shape_ok:
                    try:
                        bb = body.Shape.BoundBox
                        FreeCAD.Console.PrintMessage(
                            f"AI4CFD param case {i+1}: params={params} "
                            f"bbox x[{bb.XMin:.4f} {bb.XMax:.4f}] "
                            f"y[{bb.YMin:.4f} {bb.YMax:.4f}] "
                            f"z[{bb.ZMin:.4f} {bb.ZMax:.4f}]\n"
                        )
                    except Exception:
                        pass

                if not shape_ok:
                    continue

                # 3. Export case for this parameter set.
                # Primary path: ask CfdOF to write the full case (meshCase/ + case/)
                # using the current document state.  Because doc.recompute() above has
                # already refreshed expression-linked properties (e.g. VelocityMag =
                # =Spreadsheet.velocity on the inlet BC), CfdCaseWriterFoam will embed
                # the correct per-case velocity in 0/U — no manual patching needed.
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
                    # Fallback: STL-only; run_parametric_sim.py copies the template
                    stl_dir  = os.path.join(case_dir, "constant", "triSurface")
                    tmpl_win = self._tmpl_edit.text().strip()
                    if not tmpl_win:
                        mesh_obj = _find_cfdof_mesh_obj(doc)
                        tmpl_win = getattr(mesh_obj, "CasePath", "") if mesh_obj else ""
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

                # 4. Read expression-linked CfdOF inlet velocity / flow rate for
                #    params.json tracking (and for run_parametric_sim.py's
                #    fallback 0/U patching when the CfdOF full-case write above
                #    did not happen).
                cfdof_vel = _read_cfdof_inlet_velocity(doc)
                if cfdof_vel is not None:
                    params["_inlet_velocity_ms"] = cfdof_vel
                    FreeCAD.Console.PrintMessage(
                        f"AI4CFD: CfdOF inlet velocity = {cfdof_vel:.4g} m/s "
                        f"(case {i+1})\n")

                # Per-patch velocities — needed whenever a case has more than
                # one velocity-driven boundary (e.g. a manifold's main inlet
                # plus several pump legs pointing in different directions).
                # _inlet_velocity_ms above only ever captures ONE of them.
                all_vels = _read_all_cfdof_boundary_velocities(doc)
                if all_vels:
                    params["_boundary_velocities_ms"] = all_vels
                    FreeCAD.Console.PrintMessage(
                        f"AI4CFD: CfdOF boundary velocities = {all_vels} "
                        f"(case {i+1})\n")

                # VolFlowRate exists (defaulted to 0) on every inlet BC object
                # regardless of subtype, so only record it when non-zero —
                # otherwise every velocity-based study would pick up a spurious
                # "_inlet_volumetric_flow_rate_m3s": 0 entry.
                cfdof_flow_rate = _read_cfdof_inlet_flow_rate(doc)
                if cfdof_flow_rate is not None and abs(cfdof_flow_rate) > 1e-12:
                    params["_inlet_volumetric_flow_rate_m3s"] = cfdof_flow_rate
                    FreeCAD.Console.PrintMessage(
                        f"AI4CFD: CfdOF inlet volumetric flow rate = "
                        f"{cfdof_flow_rate:.6g} m^3/s (case {i+1})\n")

                # 5. Save parameter JSON and mesh metadata alongside
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
            FreeCAD.Console.PrintError(f"AI4CFD parametric export: {exc}\n")
        finally:
            # Restore original spreadsheet values
            _set_aliases(self._sheet_obj, original)
            try:
                self._sheet_obj.recompute()
                self._sheet_obj.touch()
            except Exception:
                pass
            doc.recompute(None, True, True)
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
            # Auto-detect from CfdOF mesh object
            doc = FreeCAD.activeDocument()
            mesh_obj = _find_cfdof_mesh_obj(doc) if doc else None
            cfdof_path = getattr(mesh_obj, "CasePath", "") if mesh_obj else ""
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

    # ── FreeCAD protocol ──────────────────────────────────────────────────────

    def accept(self) -> None:
        FreeCADGui.Control.closeDialog()

    def reject(self) -> None:
        FreeCADGui.Control.closeDialog()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dbl_spin(val: float) -> QtWidgets.QDoubleSpinBox:
    s = QtWidgets.QDoubleSpinBox()
    s.setRange(-1e9, 1e9)
    s.setDecimals(4)
    s.setValue(val)
    s.setSingleStep(max(abs(val) * 0.05, 0.001))
    return s


def _right_item(text: str) -> QtWidgets.QTableWidgetItem:
    it = QtWidgets.QTableWidgetItem(text)
    it.setTextAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
    return it


# Keep alias so existing tree items that reference the old name still work
BendPipeParametricPanel = CFDParametricPanel


# ── Command ───────────────────────────────────────────────────────────────────

class BendPipeParametricCommand:
    def GetResources(self) -> dict:
        return {
            "MenuText": "CFD Parametric Study",
            "ToolTip":  "Sample a spreadsheet parameter space and run batch simulations",
        }

    def IsActive(self) -> bool:
        return True

    def Activated(self) -> None:
        FreeCADGui.Control.showDialog(CFDParametricPanel())
