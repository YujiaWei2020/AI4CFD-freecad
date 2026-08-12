#!/usr/bin/env python3
"""Run one parametric CFD case from a pre-exported geometry.

Supports two template layouts:

1. Single-folder layout (simple cases):
   template_dir/
     system/  constant/  0/  ...

2. CfdOF split layout (meshCase + case):
   template_dir/
     meshCase/   ← blockMesh + snappyHexMesh run here; STL goes here
     case/       ← solver runs here; polyMesh copied here after meshing

The layout is auto-detected from whether template_dir/meshCase/ exists.

In the split layout the exported geometry.stl replaces only the *main*
geometry STL in meshCase/constant/triSurface/ (i.e. the largest .stl that
is not named patch_*.stl).  Patch STLs from the template are left in place.
extendedFeatureEdgeMesh/ and the .eMesh file are cleaned so
surfaceFeatureExtract regenerates them from the new geometry.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys 

_OF_SOURCE = "/usr/lib/openfoam/openfoam2212/etc/bashrc"

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# Standalone postProcess dict that writes grad(U)/grad(p) for the current time
# directory. Mirrors the `gradU` coded functionObject in the training
# controlDict, but as a self-contained dict so it can be run on demand via
# `postProcess -dict <this file> -fields '(U p)' -time 0` (see
# _run_inference_postprocess).
_GRAD_DICT_CONTENT = '''\
FoamFile
{
    version     2.0;
    format      ascii;
    class       dictionary;
    object      AI4CFD_gradDict;
}

functions
{
    gradU
    {
        type            coded;
        libs            (utilityFunctionObjects);
        name            AI4CFDgradU;

        executeControl  writeTime;
        writeControl    writeTime;

        codeInclude
        #{
            #include "fvc.H"
        #};

        codeExecute
        #{
            const volVectorField& U =
                mesh().lookupObject<volVectorField>("U");

            volTensorField gradU
            (
                IOobject
                (
                    "grad(U)",
                    mesh().time().timeName(),
                    mesh(),
                    IOobject::NO_READ,
                    IOobject::AUTO_WRITE
                ),
                fvc::grad(U)
            );
            gradU.write();

            const volScalarField& p =
                mesh().lookupObject<volScalarField>("p");

            volVectorField gradP
            (
                IOobject
                (
                    "grad(p)",
                    mesh().time().timeName(),
                    mesh(),
                    IOobject::NO_READ,
                    IOobject::AUTO_WRITE
                ),
                fvc::grad(p)
            );
            gradP.write();
        #};
    }
}
'''


# ── OpenFOAM runner ───────────────────────────────────────────────────────────

def _run(command: str, cwd: str) -> int:
    print(f"[AI4CFD] $ {command}", flush=True)
    if sys.platform == "win32":
        from utilities.openfoam_env import windows_openfoam_source_cmd
        full = windows_openfoam_source_cmd(command, cwd=cwd)
    else:
        full = f'bash -c "source {_OF_SOURCE} 2>/dev/null && {command}"'
    proc = subprocess.Popen(full, shell=True, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for raw in proc.stdout:
        print(raw.decode("utf-8", errors="replace"), end="", flush=True)
    proc.wait()
    if proc.returncode != 0:
        print(f"[AI4CFD] WARNING: '{command}' exited {proc.returncode}", flush=True)
    return proc.returncode


def _require(command: str, cwd: str) -> None:
    rc = _run(command, cwd)
    if rc != 0:
        print(f"[AI4CFD] FATAL: '{command}' failed (rc={rc}). Aborting.", flush=True)
        sys.exit(rc)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_time_dir(name: str) -> bool:
    try:
        float(name)
        return True
    except ValueError:
        return False


def _clean_solver_dirs(d: str) -> None:
    """Remove stale polyMesh and time directories from a case dir."""
    poly = os.path.join(d, "constant", "polyMesh")
    if os.path.isdir(poly):
        shutil.rmtree(poly)
        print(f"[AI4CFD] Cleaned polyMesh in {os.path.basename(d)}", flush=True)
    for name in list(os.listdir(d)):
        p = os.path.join(d, name)
        if name != "0" and _is_time_dir(name) and os.path.isdir(p):
            shutil.rmtree(p)
        elif name.startswith("processor") and os.path.isdir(p):
            shutil.rmtree(p)


def _read_solver(d: str) -> str:
    for ctrl in ("system/controlDict", "system/controlDict.j2"):
        path = os.path.join(d, ctrl)
        if os.path.isfile(path):
            for line in open(path, encoding="utf-8", errors="replace"):
                s = line.strip()
                if s.startswith("application"):
                    return s.split()[-1].rstrip(";")
    return "simpleFoam"


def _has(d: str, *rel: str) -> bool:
    return os.path.isfile(os.path.join(d, *rel))


def _copy_template_files(src: str, dst: str) -> None:
    """Overwrite dst with contents of src, skipping hidden files and __pycache__."""
    os.makedirs(dst, exist_ok=True)
    for item in os.listdir(src):
        if item.startswith(".") or item == "__pycache__":
            continue
        s = os.path.join(src, item)
        d = os.path.join(dst, item)
        if os.path.isdir(s):
            if os.path.exists(d):
                shutil.rmtree(d)
            shutil.copytree(s, d)
        else:
            shutil.copy2(s, d)


def _find_main_stl(trisurf_dir: str) -> str | None:
    """Return the filename of the main geometry STL (not patch_*.stl).

    CfdOF puts the full geometry in e.g. Body_Geometry.stl and sub-regions
    in patch_1_0.stl, patch_2_0.stl, etc.  We only replace the main one;
    patches come from the template and are left in place.
    """
    if not os.path.isdir(trisurf_dir):
        return None
    candidates = [
        f for f in os.listdir(trisurf_dir)
        if f.lower().endswith(".stl") and not f.lower().startswith("patch_")
    ]
    if not candidates:
        # Fallback: any STL
        candidates = [f for f in os.listdir(trisurf_dir) if f.lower().endswith(".stl")]
    if not candidates:
        return None
    # Pick the largest — the main geometry is always the biggest file
    candidates.sort(
        key=lambda f: os.path.getsize(os.path.join(trisurf_dir, f)),
        reverse=True)
    return candidates[0]


def _stl_bbox(trisurf_dir: str) -> tuple | None:
    """Return (xmin, ymin, zmin, xmax, ymax, zmax) parsed from ASCII STL vertex lines."""
    xmn = ymn = zmn = float('inf')
    xmx = ymx = zmx = float('-inf')
    found = False
    for fname in os.listdir(trisurf_dir):
        if not fname.lower().endswith('.stl'):
            continue
        try:
            with open(os.path.join(trisurf_dir, fname), 'r', errors='replace') as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith('vertex '):
                        p = s.split()
                        if len(p) == 4:
                            x, y, z = float(p[1]), float(p[2]), float(p[3])
                            xmn = min(xmn, x); xmx = max(xmx, x)
                            ymn = min(ymn, y); ymx = max(ymx, y)
                            zmn = min(zmn, z); zmx = max(zmx, z)
                            found = True
        except Exception as exc:
            print(f"[AI4CFD] STL parse error ({fname}): {exc}", flush=True)
    return (xmn, ymn, zmn, xmx, ymx, zmx) if found else None


def _update_blockmesh_bbox(mesh_dir: str, margin_frac: float = 0.3) -> None:
    """Expand blockMeshDict so it fully contains the STL geometry + margin.

    Handles two CfdOF blockMeshDict styles:

    Style A — named scalar variables at the top level (CfdOF default):
        xMin  -0.006;   xMax  0.006;
        yMin  -0.006;   yMax  0.006;
        zMin  -0.011;   zMax  0.001;
    The vertices block then references these as ($xMin $yMin $zMin) …

    Style B — literal coordinates inside the vertices block:
        vertices ( (x0 y0 z0) … (x7 y7 z7) );
    All existing vertices are proportionally remapped so multi-block
    topologies are preserved.

    Only ever expands — never shrinks — so a bbox that is already
    large enough is left untouched.
    """
    import re as _re

    _NUM = r'-?[\d.eE+\-]+'

    trisurf_dir = os.path.join(mesh_dir, "constant", "triSurface")
    bmd_path    = os.path.join(mesh_dir, "system", "blockMeshDict")
    if not os.path.isfile(bmd_path) or not os.path.isdir(trisurf_dir):
        return

    bbox = _stl_bbox(trisurf_dir)
    if bbox is None:
        print("[AI4CFD] No STL vertices found — skipping blockMesh bbox check",
              flush=True)
        return

    sx0, sy0, sz0, sx1, sy1, sz1 = bbox
    print(f"[AI4CFD] STL bbox   x[{sx0:.4f} {sx1:.4f}] "
          f"y[{sy0:.4f} {sy1:.4f}] z[{sz0:.4f} {sz1:.4f}]", flush=True)

    with open(bmd_path) as fh:
        content = fh.read()

    # ── Style A: named variables (xMin / xMax / yMin / yMax / zMin / zMax) ───
    _VAR_KEYS = ('xMin', 'xMax', 'yMin', 'yMax', 'zMin', 'zMax')
    named = {}
    for key in _VAR_KEYS:
        m = _re.search(rf'\b{key}\b\s+({_NUM})\s*;', content)
        if m:
            named[key] = float(m.group(1))

    if len(named) == 6:
        bx0, bx1 = named['xMin'], named['xMax']
        by0, by1 = named['yMin'], named['yMax']
        bz0, bz1 = named['zMin'], named['zMax']
        print(f"[AI4CFD] blockMesh  x[{bx0:.4f} {bx1:.4f}] "
              f"y[{by0:.4f} {by1:.4f}] z[{bz0:.4f} {bz1:.4f}] (named vars)",
              flush=True)

        mx = (sx1 - sx0) * margin_frac or 0.1
        my = (sy1 - sy0) * margin_frac or 0.1
        mz = (sz1 - sz0) * margin_frac or 0.1

        new_vals = {
            'xMin': min(bx0, sx0 - mx), 'xMax': max(bx1, sx1 + mx),
            'yMin': min(by0, sy0 - my), 'yMax': max(by1, sy1 + my),
            'zMin': min(bz0, sz0 - mz), 'zMax': max(bz1, sz1 + mz),
        }

        if all(abs(new_vals[k] - named[k]) < 1e-12 for k in _VAR_KEYS):
            print("[AI4CFD] blockMesh bbox sufficient — no update needed", flush=True)
            return

        for key in _VAR_KEYS:
            content = _re.sub(
                rf'(\b{key}\b\s+){_NUM}(\s*;)',
                lambda mo, v=new_vals[key]: f'{mo.group(1)}{v:.6g}{mo.group(2)}',
                content,
            )

        print(f"[AI4CFD] Updated blockMesh (named vars) "
              f"x[{new_vals['xMin']:.4f} {new_vals['xMax']:.4f}] "
              f"y[{new_vals['yMin']:.4f} {new_vals['yMax']:.4f}] "
              f"z[{new_vals['zMin']:.4f} {new_vals['zMax']:.4f}]", flush=True)
        with open(bmd_path, 'w') as fh:
            fh.write(content)
        return

    # ── Style B: literal coordinate tuples inside the vertices block ──────────
    m = _re.search(r'(vertices\s*\()(.*?)(\)\s*;)', content, _re.DOTALL)
    if not m:
        print("[AI4CFD] Cannot locate vertices block in blockMeshDict", flush=True)
        return

    vert_block = m.group(2)
    raw = _re.findall(
        rf'\(\s*({_NUM})\s+({_NUM})\s+({_NUM})\s*\)', vert_block)
    if not raw:
        print("[AI4CFD] No literal vertex tuples in blockMeshDict "
              "(variable refs not expanded — check named-var fallback)", flush=True)
        return

    coords = [(float(a), float(b), float(c)) for a, b, c in raw]
    bx0 = min(v[0] for v in coords); bx1 = max(v[0] for v in coords)
    by0 = min(v[1] for v in coords); by1 = max(v[1] for v in coords)
    bz0 = min(v[2] for v in coords); bz1 = max(v[2] for v in coords)
    print(f"[AI4CFD] blockMesh  x[{bx0:.4f} {bx1:.4f}] "
          f"y[{by0:.4f} {by1:.4f}] z[{bz0:.4f} {bz1:.4f}] (literal verts)",
          flush=True)

    mx = (sx1 - sx0) * margin_frac or 0.1
    my = (sy1 - sy0) * margin_frac or 0.1
    mz = (sz1 - sz0) * margin_frac or 0.1

    nx0 = min(bx0, sx0 - mx); nx1 = max(bx1, sx1 + mx)
    ny0 = min(by0, sy0 - my); ny1 = max(by1, sy1 + my)
    nz0 = min(bz0, sz0 - mz); nz1 = max(bz1, sz1 + mz)

    if (nx0, ny0, nz0, nx1, ny1, nz1) == (bx0, by0, bz0, bx1, by1, bz1):
        print("[AI4CFD] blockMesh bbox sufficient — no update needed", flush=True)
        return

    print(f"[AI4CFD] Updating blockMesh (literal verts) "
          f"x[{nx0:.4f} {nx1:.4f}] y[{ny0:.4f} {ny1:.4f}] z[{nz0:.4f} {nz1:.4f}]",
          flush=True)

    def _remap(v, old0, old1, new0, new1):
        if abs(old1 - old0) < 1e-12:
            return (new0 + new1) / 2
        return new0 + (v - old0) / (old1 - old0) * (new1 - new0)

    new_coords = [
        (_remap(x, bx0, bx1, nx0, nx1),
         _remap(y, by0, by1, ny0, ny1),
         _remap(z, bz0, bz1, nz0, nz1))
        for x, y, z in coords
    ]
    it = iter(new_coords)

    def _replacer(mo):
        x, y, z = next(it)
        return f'({x:.6g} {y:.6g} {z:.6g})'

    new_vert_block = _re.sub(
        rf'\(\s*{_NUM}\s+{_NUM}\s+{_NUM}\s*\)', _replacer, vert_block)

    new_content = content[:m.start(2)] + new_vert_block + content[m.end(2):]
    with open(bmd_path, 'w') as fh:
        fh.write(new_content)
    print("[AI4CFD] blockMeshDict updated", flush=True)


def _stl_solid_count(path: str) -> int:
    """Count named solids in an ASCII STL; returns 1 for binary STL."""
    import re
    try:
        with open(path, "r", encoding="ascii", errors="strict") as f:
            content = f.read()
        return len(re.findall(r"^solid\s", content, re.MULTILINE))
    except (UnicodeDecodeError, OSError):
        return 1  # binary = single unnamed region


def _get_all_snappy_stl_regions(mesh_dir: str, stl_name: str) -> list:
    """Return ALL region names listed for stl_name in snappyHexMeshDict."""
    import re
    snappy = os.path.join(mesh_dir, "system", "snappyHexMeshDict")
    if not os.path.isfile(snappy):
        return []
    try:
        content = open(snappy, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.DOTALL)
    content = re.sub(r"//[^\n]*", "", content)
    m = re.search(rf'["\']?{re.escape(stl_name)}["\']?\s*\{{', content)
    if not m:
        return []
    start, depth, i = m.end(), 1, m.end()
    while i < len(content) and depth > 0:
        if content[i] == "{": depth += 1
        elif content[i] == "}": depth -= 1
        i += 1
    block = content[start: i - 1]
    rm = re.search(r"\bregions\s*\{", block)
    if not rm:
        return []
    r_start, depth, i = rm.end(), 1, rm.end()
    while i < len(block) and depth > 0:
        if block[i] == "{": depth += 1
        elif block[i] == "}": depth -= 1
        i += 1
    return re.findall(r"\b(\w+)\s*\{", block[r_start: i - 1])


def _classify_stl_to_multi_solid(
    src_path: str, region_names: list, out_path: str, scale: float = 0.001
) -> bool:
    """Read a single-solid STL, classify triangles by z-centroid, write multi-solid.

    Triangles near z_max  → region_names[0]  (top face / inlet)
    Triangles near z_min  → region_names[1]  (bottom face / outlet)
    Everything in between → region_names[-1] (lateral wall)

    scale: applied to all vertex coordinates when writing (0.001 = mm → metres).
    Returns False if any group would be empty or parsing fails.
    """
    import struct, re as _re

    with open(src_path, "rb") as f:
        raw = f.read()

    triangles = []  # [(nx,ny,nz, (x1,y1,z1), (x2,y2,z2), (x3,y3,z3))]

    def _parse_binary(data):
        if len(data) < 84:
            return False
        n = struct.unpack_from("<I", data, 80)[0]
        if len(data) != 84 + n * 50:
            return False
        off = 84
        for _ in range(n):
            nx, ny, nz = struct.unpack_from("<fff", data, off); off += 12
            v1 = struct.unpack_from("<fff", data, off); off += 12
            v2 = struct.unpack_from("<fff", data, off); off += 12
            v3 = struct.unpack_from("<fff", data, off); off += 12
            off += 2
            triangles.append((nx, ny, nz, v1, v2, v3))
        return bool(triangles)

    def _parse_ascii(data):
        try:
            text = data.decode("ascii", errors="replace")
        except Exception:
            return False
        pts = _re.findall(
            r"vertex\s+(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)", text)
        nms = _re.findall(
            r"facet\s+normal\s+(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)", text)
        if not nms or len(pts) != len(nms) * 3:
            return False
        for i, n in enumerate(nms):
            v1, v2, v3 = pts[i * 3], pts[i * 3 + 1], pts[i * 3 + 2]
            triangles.append((
                float(n[0]), float(n[1]), float(n[2]),
                tuple(float(x) for x in v1),
                tuple(float(x) for x in v2),
                tuple(float(x) for x in v3),
            ))
        return bool(triangles)

    # Try binary detection first, then ASCII
    parsed = False
    if len(raw) >= 84:
        n_try = struct.unpack_from("<I", raw, 80)[0]
        if len(raw) == 84 + n_try * 50:
            parsed = _parse_binary(raw)
    if not parsed:
        parsed = _parse_ascii(raw)
    if not parsed or not triangles:
        print("[AI4CFD] classify_stl: could not parse triangles", flush=True)
        return False

    # z range
    z_vals = [v[2] for _, _, _, v1, v2, v3 in triangles for v in (v1, v2, v3)]
    z_min, z_max = min(z_vals), max(z_vals)
    z_range = z_max - z_min
    if z_range < 1e-12:
        return False
    tol = z_range * 0.05   # 5 % band at each end

    top  = region_names[0]
    bot  = region_names[1] if len(region_names) > 1 else region_names[-1]
    wall = region_names[-1]
    groups = {n: [] for n in region_names}

    for tri in triangles:
        _, _, _, v1, v2, v3 = tri
        z_c = (v1[2] + v2[2] + v3[2]) / 3.0
        if z_c > z_max - tol:
            groups[top].append(tri)
        elif z_c < z_min + tol:
            groups[bot].append(tri)
        else:
            groups[wall].append(tri)

    if any(not g for g in groups.values()):
        print(f"[AI4CFD] classify_stl: empty group — "
              f"{[(k, len(v)) for k, v in groups.items()]}",
              flush=True)
        return False

    lines = []
    for name in region_names:
        lines.append(f"solid {name}\n")
        for nx, ny, nz, v1, v2, v3 in groups[name]:
            lines.append(f"  facet normal {nx:.6e} {ny:.6e} {nz:.6e}\n"
                         "    outer loop\n")
            for v in (v1, v2, v3):
                lines.append(
                    f"      vertex {v[0]*scale:.6e} {v[1]*scale:.6e} {v[2]*scale:.6e}\n"
                )
            lines.append("    endloop\n  endfacet\n")
        lines.append(f"endsolid {name}\n")

    with open(out_path, "wb") as f:
        f.write("".join(lines).encode("ascii"))

    counts = ", ".join(f"{k}:{len(groups[k])}" for k in region_names)
    print(f"[AI4CFD] Multi-solid STL written ({counts}) "
          f"scale×{scale} — {os.path.basename(out_path)}", flush=True)
    return True


def _build_multi_solid_from_patch_stls(
    trisurf_dir: str, region_names: list, out_name: str
) -> bool:
    """Build a multi-solid STL by concatenating individual patch_N_0.stl files.

    These come from the template (ASCII, metres) and are correct for
    snappyHexMesh.  Used as a fallback when z-centroid classification fails.
    """
    lines = []
    for name in region_names:
        path = os.path.join(trisurf_dir, f"{name}.stl")
        if not os.path.isfile(path):
            print(f"[AI4CFD] patch STL missing: {name}.stl", flush=True)
            return False
        try:
            raw_lines = open(path, "r", encoding="ascii", errors="replace").readlines()
        except Exception as exc:
            print(f"[AI4CFD] cannot read {name}.stl: {exc}", flush=True)
            return False
        # Strip first (solid ...) and last (endsolid ...) lines; keep facets
        if raw_lines and raw_lines[0].strip().startswith("solid"):
            raw_lines = raw_lines[1:]
        if raw_lines and raw_lines[-1].strip().startswith("endsolid"):
            raw_lines = raw_lines[:-1]
        lines.append(f"solid {name}\n")
        lines.extend(raw_lines)
        lines.append(f"endsolid {name}\n")

    out_path = os.path.join(trisurf_dir, out_name)
    with open(out_path, "wb") as f:
        f.write("".join(lines).encode("ascii"))
    counts = ", ".join(region_names)
    print(f"[AI4CFD] Built multi-solid '{out_name}' from template patches: {counts}",
          flush=True)
    return True


def _get_snappy_stl_region(mesh_dir: str, stl_name: str) -> str | None:
    """Return the first region name listed for stl_name in snappyHexMeshDict."""
    import re
    snappy = os.path.join(mesh_dir, "system", "snappyHexMeshDict")
    if not os.path.isfile(snappy):
        return None
    try:
        content = open(snappy, encoding="utf-8", errors="replace").read()
    except Exception:
        return None
    # Strip comments
    content = re.sub(r'/\*.*?\*/', '', content, flags=re.DOTALL)
    content = re.sub(r'//[^\n]*', '', content)

    # Find the block for this STL filename
    m = re.search(rf'["\']?{re.escape(stl_name)}["\']?\s*\{{', content)
    if not m:
        return None

    # Brace-match to extract the block
    start, depth, i = m.end(), 1, m.end()
    while i < len(content) and depth > 0:
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
        i += 1
    block = content[start:i - 1]

    # Find regions { ... } and return the first region name
    rm = re.search(r'\bregions\s*\{', block)
    if not rm:
        return None
    r_start, depth, i = rm.end(), 1, rm.end()
    while i < len(block) and depth > 0:
        if block[i] == '{':
            depth += 1
        elif block[i] == '}':
            depth -= 1
        i += 1
    nm = re.search(r'\b(\w+)\s*\{', block[r_start:i - 1])
    return nm.group(1) if nm else None


def _fix_stl_solid_name(stl_path: str, new_name: str) -> None:
    """Set the solid name in an STL file so snappyHexMesh finds the region.

    ASCII STL: rename the existing solid line in-place.
    Binary STL: convert to ASCII with the correct solid name — binary STLs
                store no region name and snappyHexMesh cannot use them for
                named-region references.
    """
    import re
    import struct

    with open(stl_path, "rb") as f:
        raw = f.read()

    # Detect binary: must be >= 84 bytes and triangle count matches file size
    is_binary = False
    if len(raw) >= 84:
        try:
            n_tri = struct.unpack_from("<I", raw, 80)[0]
            if len(raw) == 84 + n_tri * 50:
                is_binary = True
        except Exception:
            pass
    # Also treat as binary if it doesn't start with "solid"
    if not raw.lstrip()[:5] == b"solid":
        is_binary = True

    if is_binary:
        # Parse binary STL and emit ASCII with the required solid name
        print(f"[AI4CFD] Converting binary STL to ASCII "
              f"(solid '{new_name}') — {os.path.basename(stl_path)}", flush=True)
        try:
            n_tri  = struct.unpack_from("<I", raw, 80)[0]
            lines  = [f"solid {new_name}\n"]
            offset = 84
            for _ in range(n_tri):
                nx, ny, nz        = struct.unpack_from("<fff", raw, offset); offset += 12
                v1x, v1y, v1z     = struct.unpack_from("<fff", raw, offset); offset += 12
                v2x, v2y, v2z     = struct.unpack_from("<fff", raw, offset); offset += 12
                v3x, v3y, v3z     = struct.unpack_from("<fff", raw, offset); offset += 12
                offset += 2  # attribute byte count
                lines.append(f"  facet normal {nx:.6e} {ny:.6e} {nz:.6e}\n")
                lines.append("    outer loop\n")
                lines.append(f"      vertex {v1x:.6e} {v1y:.6e} {v1z:.6e}\n")
                lines.append(f"      vertex {v2x:.6e} {v2y:.6e} {v2z:.6e}\n")
                lines.append(f"      vertex {v3x:.6e} {v3y:.6e} {v3z:.6e}\n")
                lines.append("    endloop\n")
                lines.append("  endfacet\n")
            lines.append(f"endsolid {new_name}\n")
            with open(stl_path, "wb") as f:
                f.write("".join(lines).encode("ascii"))
            print(f"[AI4CFD] Converted {n_tri} triangles to ASCII STL", flush=True)
        except Exception as exc:
            print(f"[AI4CFD] Binary→ASCII conversion failed: {exc}", flush=True)
        return

    # ASCII STL — rename solid / endsolid lines
    content  = raw.decode("ascii", errors="replace")
    old_m    = re.search(r"solid\s*(\S*)", content.lstrip())
    old_name = old_m.group(1) if old_m else ""
    if old_name == new_name:
        return
    content = re.sub(r"^solid\s*\S*",    f"solid {new_name}",    content,
                     count=1, flags=re.MULTILINE)
    content = re.sub(r"^endsolid\s*\S*", f"endsolid {new_name}", content,
                     count=1, flags=re.MULTILINE)
    with open(stl_path, "wb") as f:
        f.write(content.encode("ascii"))
    print(f"[AI4CFD] STL solid renamed '{old_name}' → '{new_name}' "
          f"({os.path.basename(stl_path)})", flush=True)


def _find_vtk_time_dir(vtk_base: str) -> str | None:
    """Return the single VTK time directory produced by foamToVTK, or None."""
    if not os.path.isdir(vtk_base):
        return None
    subdirs = sorted(
        d for d in os.listdir(vtk_base)
        if os.path.isdir(os.path.join(vtk_base, d))
    )
    return os.path.join(vtk_base, subdirs[-1]) if subdirs else None


def _run_inference_postprocess(case_dir: str, solv_dir: str) -> None:
    """After mesh-only: foamToVTK → wall distance → .npy in INFERENCE_DIR.

    Uses the same Python utilities as the training dataset pipeline so the
    .npy column layout is identical and the trained model can read it directly.
    """
    # 1. Compute grad(U)/grad(p) on the time-0 fields (no solver run needed) so
    #    they end up in 0/ and get picked up by foamToVTK below. The model's
    #    TRAIN_FIELDS include grad(U)/grad(p); without this step DatasetPreparer
    #    skips the case ("Missing fields ['grad(U)', 'grad(p)']") and no .npy
    #    is written.
    #
    #    The built-in `grad` function object (`postProcess -func 'grad(U)'`)
    #    hits an OpenFOAM-2212 bug ("Refuse to store unregistered object:
    #    grad(U)") for vector->tensor gradients, so instead we reuse the same
    #    `coded` function object the training controlDict uses for grad(U)/
    #    grad(p) (which constructs the fields with IOobject::AUTO_WRITE and
    #    calls .write() directly, sidestepping the registry bug), via a
    #    standalone dict run through `postProcess -dict`.
    print("[AI4CFD] Computing grad(U), grad(p) on initial fields...", flush=True)
    grad_dict_path = os.path.join(solv_dir, "system", "AI4CFD_gradDict")
    with open(grad_dict_path, "w") as f:
        f.write(_GRAD_DICT_CONTENT)
    _run("postProcess -dict system/AI4CFD_gradDict -fields '(U p)' -time 0", solv_dir)

    # 2. Export mesh + 0/ initial BCs (+ grad fields) to VTK
    print("[AI4CFD] foamToVTK — exporting mesh and initial BCs...", flush=True)
    if _run("foamToVTK", solv_dir) != 0:
        print("[AI4CFD] WARNING: foamToVTK failed — inference .npy not created",
              flush=True)
        return

    vtk_base     = os.path.join(solv_dir, "VTK")
    vtk_time_dir = _find_vtk_time_dir(vtk_base)
    if vtk_time_dir is None:
        print("[AI4CFD] WARNING: foamToVTK produced no output directory", flush=True)
        return
    print(f"[AI4CFD] VTK time dir: {vtk_time_dir}", flush=True)

    # 3. Make project root importable (helpers/ lives 3 levels deep)
    here         = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.normpath(os.path.join(here, "..", "..", ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    # 4. Compute wall distances via KDTree (same as training pipeline)
    print("[AI4CFD] Computing wall distances from boundary patches...", flush=True)
    try:
        from utilities.calculate_wall_distance import WallDistanceCalculator
        calc = WallDistanceCalculator(solv_dir)
        ok   = calc.process_time_dir(vtk_time_dir)
        if ok:
            print("[AI4CFD] Wall distances written to VTK files", flush=True)
        else:
            print("[AI4CFD] WARNING: wall distance computation skipped "
                  "(no wall patches in VTK?)", flush=True)
    except Exception as exc:
        print(f"[AI4CFD] WARNING: wall distance failed: {exc}", flush=True)

    # 5. Convert VTK → .npy in INFERENCE_DIR
    print("[AI4CFD] Creating inference .npy...", flush=True)
    try:
        from config import INFERENCE_DIR
        from utilities.prepare_npy import DatasetPreparer
        os.makedirs(INFERENCE_DIR, exist_ok=True)
        preparer = DatasetPreparer(
            results_dir = solv_dir,                    # not used — we call process_case directly
            output_dir  = INFERENCE_DIR,
            case_subdir = os.path.basename(solv_dir),  # "case" — for name resolution
        )
        status = preparer.process_case(vtk_time_dir)
        print(f"[AI4CFD] {status}", flush=True)
    except Exception as exc:
        print(f"[AI4CFD] WARNING: .npy creation failed: {exc}", flush=True)


def _clean_feature_edges(mesh_dir: str, main_stl_name: str) -> None:
    """Remove pre-computed edge-mesh files so surfaceFeatureExtract starts fresh."""
    # constant/extendedFeatureEdgeMesh/
    efem = os.path.join(mesh_dir, "constant", "extendedFeatureEdgeMesh")
    if os.path.isdir(efem):
        shutil.rmtree(efem)
        print(f"[AI4CFD] Cleaned extendedFeatureEdgeMesh/", flush=True)

    # constant/triSurface/*.eMesh — remove all stale edge meshes (MeshRefinement etc.)
    trisurf = os.path.join(mesh_dir, "constant", "triSurface")
    if os.path.isdir(trisurf):
        for fname in os.listdir(trisurf):
            if fname.lower().endswith(".emesh"):
                os.remove(os.path.join(trisurf, fname))
                print(f"[AI4CFD] Removed stale {fname}", flush=True)


def _patch_snappy_location(case_dir: str, mesh_dir: str) -> None:
    """Read ai4cfd_meta.json and patch locationInMesh in snappyHexMeshDict.

    The FreeCAD export phase computes a point guaranteed to be inside the pipe
    cavity for each parameterised geometry and stores it as ai4cfd_meta.json.
    We inject it here, after the template is copied, so snappyHexMesh always
    starts from the correct interior point regardless of parameter changes.
    """
    import json as _json
    import re as _re
    meta_path = os.path.join(case_dir, "ai4cfd_meta.json")
    if not os.path.isfile(meta_path):
        return
    try:
        meta = _json.load(open(meta_path, encoding="utf-8"))
        loc  = meta.get("locationInMesh")
        if not loc or len(loc) != 3:
            return
        loc_str = f"( {loc[0]:.6g} {loc[1]:.6g} {loc[2]:.6g} )"
        for d in (mesh_dir, case_dir):
            spath = os.path.join(d, "system", "snappyHexMeshDict")
            if not os.path.isfile(spath):
                continue
            text = open(spath, encoding="utf-8", errors="replace").read()
            new_text = _re.sub(
                r"locationInMesh\s*\([^)]*\)\s*;",
                f"locationInMesh {loc_str};",
                text)
            if new_text != text:
                with open(spath, "w", encoding="utf-8") as fh:
                    fh.write(new_text)
                print(f"[AI4CFD] locationInMesh → {loc_str}  ({spath})", flush=True)
            else:
                print(f"[AI4CFD] locationInMesh pattern not found in {spath}", flush=True)
    except Exception as exc:
        print(f"[AI4CFD] Warning: locationInMesh patch failed: {exc}", flush=True)


# Maps a CfdOF boundary-object property name to where it lives in the
# per-case OpenFOAM 0/<field> files. This is the ONE place that needs
# updating to teach the parametric exporter about a new field — the read
# side (parametric_study_cmd.py's _read_all_expression_linked_properties)
# captures ANY expression-linked property automatically, with no per-field
# code there at all. Only the write side needs this table, because
# OpenFOAM's file format gives no way to discover "which file and keyword
# does property X correspond to" from the FreeCAD property name alone.
#
#   (of_file, of_key, kind)
#     of_file: the field file under 0/ (e.g. 'U', 'nut', 'p')
#     of_key:  the dict keyword to rewrite for 'scalar' kind (e.g. 'Ks');
#              None for 'vector' kind, which doesn't use a fixed keyword.
#     kind:
#       'vector' — rescale every non-zero 'uniform (x y z)' found anywhere
#                  in the patch's block to the new magnitude, preserving
#                  each vector's own template direction. Matches how CfdOF
#                  writes velocity BCs, which can duplicate the value into
#                  both 'value' and 'normalVelocity.value'.
#       'scalar' — rewrite the number following '<of_key> uniform '.
#
# Extend this table (and nothing else) to parametrize another field.
_PROPERTY_OF_MAP = {
    "VelocityMag":       ("U",   None,  "vector"),
    "RoughnessHeight":   ("nut", "Ks",  "scalar"),
    "RoughnessConstant": ("nut", "Cs",  "scalar"),
}


def _patch_named_boundary_properties(solv_dir: str, params: dict) -> bool:
    """Generic per-patch property patcher, driven by _PROPERTY_OF_MAP.

    This is the primary mechanism going forward, replacing the older
    field-specific _patch_named_boundary_velocities / _patch_named_wall_
    roughness pair below (both kept only as a fallback for params.json
    written before this existed). params['_boundary_properties'] is
    {patch_name: {property_name: value}}, written by
    parametric_study_cmd.py's _read_all_expression_linked_properties() from
    EVERY property with a live ExpressionEngine binding on EVERY CfdOF
    boundary object — no per-field code needed on that side at all.

    A captured property with no entry in _PROPERTY_OF_MAP is logged loudly
    instead of silently doing nothing, so an unsupported parametrized field
    is visible immediately instead of quietly failing to vary.
    """
    import re as _re

    props_by_patch = params.get("_boundary_properties")
    if not props_by_patch:
        return False

    # of_file -> patch_name -> [(kind, of_key, value), ...]
    by_file: dict = {}
    for patch_name, props in props_by_patch.items():
        for prop_name, value in props.items():
            mapping = _PROPERTY_OF_MAP.get(prop_name)
            if mapping is None:
                print(f"[AI4CFD] No OpenFOAM mapping for property "
                      f"'{prop_name}' on patch '{patch_name}' — value "
                      f"{value:.6g} NOT applied. Add it to _PROPERTY_OF_MAP "
                      f"in run_parametric_sim.py to support it.", flush=True)
                continue
            of_file, of_key, kind = mapping
            by_file.setdefault(of_file, {}).setdefault(patch_name, []) \
                    .append((kind, of_key, value))

    if not by_file:
        return False

    _VEC_RE = _re.compile(
        r'(uniform\s+\(\s*)'
        r'(-?[\d.eE+\-]+)(\s+)(-?[\d.eE+\-]+)(\s+)(-?[\d.eE+\-]+)'
        r'(\s*\))'
    )

    any_patched = False
    for of_file, patches in by_file.items():
        f_path = os.path.join(solv_dir, "0", of_file)
        if not os.path.isfile(f_path):
            print(f"[AI4CFD] 0/{of_file} not found — skipping patch(es) "
                  f"{list(patches)}", flush=True)
            continue

        with open(f_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()

        file_patched = 0
        for patch_name, ops in patches.items():
            m = _re.search(rf'\b{_re.escape(patch_name)}\b\s*\n?\s*\{{', text)
            if not m:
                continue
            start, depth, i = m.end(), 1, m.end()
            while i < len(text) and depth > 0:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                i += 1
            block = text[start: i - 1]
            block_count = 0

            for kind, of_key, value in ops:
                if kind == "vector":
                    def _replace_vec(mo, value=value):
                        vx = float(mo.group(2))
                        vy = float(mo.group(4))
                        vz = float(mo.group(6))
                        vmag = (vx**2 + vy**2 + vz**2) ** 0.5
                        if vmag < 1e-12:
                            return mo.group(0)      # zero vector — leave alone
                        scale = value / vmag
                        return (f"{mo.group(1)}{vx*scale:.6g}{mo.group(3)}"
                                f"{vy*scale:.6g}{mo.group(5)}{vz*scale:.6g}"
                                f"{mo.group(7)}")
                    new_block, n = _VEC_RE.subn(_replace_vec, block)
                elif kind == "scalar":
                    new_block, n = _re.subn(
                        rf'(\b{of_key}\s+uniform\s+)(-?[\d.eE+\-]+)',
                        lambda mo, value=value: f"{mo.group(1)}{value:.6g}",
                        block)
                else:
                    continue
                if n:
                    block = new_block
                    block_count += n

            if block_count == 0:
                continue
            text = text[:start] + block + text[i - 1:]
            file_patched += block_count
            ops_str = ", ".join(f"{of_key or '(vector)'}={value:.6g}"
                                 for _, of_key, value in ops)
            print(f"[AI4CFD] 0/{of_file}: patch '{patch_name}' -> {ops_str} "
                  f"({block_count} value(s))", flush=True)

        if file_patched:
            with open(f_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            any_patched = True

    if not any_patched:
        print("[AI4CFD] _boundary_properties present but nothing matched "
              "any named boundaryField block", flush=True)

    return any_patched


def _patch_named_boundary_velocities(solv_dir: str, params: dict) -> bool:
    """Rewrite each boundaryField block's velocity vectors by patch name.

    Unlike _patch_inlet_velocity() below (which infers "the" inlet direction
    from internalField and only rescales vectors already pointing that way),
    this patches each patch's OWN vectors to its OWN recorded speed while
    preserving that patch's own template direction. That distinction matters
    whenever a case has more than one velocity-driven boundary pointing in
    different directions — e.g. a manifold's main 'inlet' flowing along +X
    plus several 'pumpNN' legs entering along +Y. The direction-matching
    heuristic silently skips every boundary that doesn't happen to align
    with internalField, so pumpNN would stay frozen at the template's value
    no matter what the linked spreadsheet alias is set to.

    params['_boundary_velocities_ms'] is {patch_name: velocity_m_s}, written
    by parametric_study_cmd.py's _read_all_cfdof_boundary_velocities() from
    each CfdOF boundary object's Label (which CfdOF writes verbatim as the
    OpenFOAM patch name) and its expression-linked VelocityMag.

    Returns True if at least one patch was updated — callers use this to
    skip the older global-value fallback below, which would otherwise
    redundantly (if harmlessly) re-scale whichever single patch happens to
    match its direction heuristic.
    """
    import re as _re

    vels = params.get("_boundary_velocities_ms")
    if not vels:
        return False

    u_path = os.path.join(solv_dir, "0", "U")
    if not os.path.isfile(u_path):
        print("[AI4CFD] 0/U not found — named boundary velocity patch skipped",
              flush=True)
        return False

    with open(u_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    _VEC_RE = _re.compile(
        r'(uniform\s+\(\s*)'
        r'(-?[\d.eE+\-]+)(\s+)(-?[\d.eE+\-]+)(\s+)(-?[\d.eE+\-]+)'
        r'(\s*\))'
    )

    patched = 0
    for patch_name, vel_ms in vels.items():
        m = _re.search(rf'\b{_re.escape(patch_name)}\b\s*\n?\s*\{{', text)
        if not m:
            continue
        start, depth, i = m.end(), 1, m.end()
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        block = text[start: i - 1]

        count = [0]

        def _replace(mo, vel_ms=vel_ms):
            vx, vy, vz = float(mo.group(2)), float(mo.group(4)), float(mo.group(6))
            vmag = (vx**2 + vy**2 + vz**2) ** 0.5
            if vmag < 1e-12:
                return mo.group(0)          # zero vector (wall) — leave alone
            scale = vel_ms / vmag
            count[0] += 1
            return (f"{mo.group(1)}{vx*scale:.6g}{mo.group(3)}"
                    f"{vy*scale:.6g}{mo.group(5)}{vz*scale:.6g}{mo.group(7)}")

        new_block = _VEC_RE.sub(_replace, block)
        if count[0] == 0:
            continue

        text = text[:start] + new_block + text[i - 1:]
        patched += count[0]
        print(f"[AI4CFD] 0/U: patch '{patch_name}' -> {vel_ms:.6g} m/s "
              f"({count[0]} vector(s))", flush=True)

    if patched == 0:
        print("[AI4CFD] 0/U: no named boundaryField blocks matched "
              "_boundary_velocities_ms", flush=True)
        return False

    with open(u_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def _patch_inlet_velocity(solv_dir: str, params: dict) -> None:
    """Scale ALL non-zero velocity vectors in 0/U to the new inlet speed.

    Reads the reference velocity direction from internalField, then replaces
    every 'uniform (Ux Uy Uz)' that points in the same direction — including
    internalField itself, inlet.value, inlet.normalVelocity.value, outlet
    reference value, etc.  Zero vectors (wall no-slip) are left untouched.

    Velocity source (priority order):
      1. '_inlet_velocity_ms' — set by FreeCAD export from expression-linked VelocityMag
      2. Any param key matching vel/velocity/speed regex
         Auto-detects mm/s: if raw value >> internalField magnitude, divides by 1000.
    """
    import re as _re

    # ── 1. Resolve target velocity ────────────────────────────────────────────
    if "_inlet_velocity_ms" in params:
        vel_ms   = float(params["_inlet_velocity_ms"])
        src      = "_inlet_velocity_ms"
        raw_alias = False   # already in m/s from CfdOF getValueAs("m/s")
    else:
        vel_key = next(
            (k for k, v in params.items()
             if isinstance(v, (int, float))
             and _re.search(r'vel(ocity)?|speed', k, _re.IGNORECASE)),
            None,
        )
        if vel_key is None:
            return
        vel_ms    = float(params[vel_key])
        src       = vel_key
        raw_alias = True    # raw spreadsheet value — may be in mm/s

    u_path = os.path.join(solv_dir, "0", "U")
    if not os.path.isfile(u_path):
        print("[AI4CFD] 0/U not found — velocity patch skipped", flush=True)
        return

    with open(u_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    # ── 2. Read reference direction from internalField ────────────────────────
    m_ref = _re.search(
        r'\binternalField\s+uniform\s+\(\s*'
        r'(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)\s*\)',
        text)
    if m_ref is None:
        print("[AI4CFD] 0/U: no internalField vector — velocity patch skipped",
              flush=True)
        return

    rx, ry, rz = float(m_ref.group(1)), float(m_ref.group(2)), float(m_ref.group(3))
    ref_mag = (rx**2 + ry**2 + rz**2) ** 0.5
    if ref_mag < 1e-12:
        print("[AI4CFD] 0/U: internalField is zero — velocity patch skipped", flush=True)
        return

    # Auto-detect mm/s only for raw alias values (NOT for _inlet_velocity_ms which
    # is always already in m/s from CfdOF's getValueAs("m/s")).
    # e.g. spreadsheet stores B4=1250 (mm/s) but 0/U uses 1.25 (m/s).
    if raw_alias and vel_ms > ref_mag * 10.0:
        vel_ms /= 1000.0
        print(f"[AI4CFD] Auto-converted: {vel_ms*1000:.6g} mm/s → {vel_ms:.6g} m/s",
              flush=True)

    print(f"[AI4CFD] Patching 0/U from '{src}': {vel_ms:.6g} m/s  "
          f"(ref dir = ({rx} {ry} {rz}))", flush=True)

    scale = vel_ms / ref_mag
    nx, ny, nz = rx * scale, ry * scale, rz * scale

    # ── 3. Replace every uniform vector with the same direction ───────────────
    _VEC_RE = _re.compile(
        r'(uniform\s+\(\s*)'
        r'(-?[\d.eE+\-]+)(\s+)(-?[\d.eE+\-]+)(\s+)(-?[\d.eE+\-]+)'
        r'(\s*\))'
    )
    count = [0]

    def _replace(mo):
        vx = float(mo.group(2))
        vy = float(mo.group(4))
        vz = float(mo.group(6))
        vmag = (vx**2 + vy**2 + vz**2) ** 0.5
        if vmag < 1e-12:
            return mo.group(0)                       # zero vector — leave wall alone
        dot = (vx*rx + vy*ry + vz*rz) / (vmag * ref_mag)
        if abs(dot - 1.0) > 0.05:
            return mo.group(0)                       # different direction — skip
        count[0] += 1
        return (f"{mo.group(1)}{nx:.6g}{mo.group(3)}"
                f"{ny:.6g}{mo.group(5)}{nz:.6g}{mo.group(7)}")

    new_text = _VEC_RE.sub(_replace, text)

    if count[0] == 0:
        print("[AI4CFD] WARNING: no velocity vectors matched the inlet direction",
              flush=True)
        return

    with open(u_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    print(f"[AI4CFD] 0/U: updated {count[0]} vector(s) → "
          f"({nx:.6g} {ny:.6g} {nz:.6g}) m/s", flush=True)


def _patch_named_wall_roughness(solv_dir: str, params: dict) -> bool:
    """Rewrite each wall patch's Ks/Cs in 0/nut for nutkRoughWallFunction BCs.

    Mirrors _patch_named_boundary_velocities() but for wall roughness: the
    STL-only fallback export copies 0/nut from the template verbatim, and
    (until now) nothing ever read RoughnessHeight/RoughnessConstant off the
    CfdOF boundary objects at all — so a spreadsheet alias expression-linked
    to roughness had zero effect on any exported case, regardless of value.

    params['_wall_roughness'] is {patch_name: {'Ks': height_m, 'Cs': const}},
    written by parametric_study_cmd.py's _read_all_cfdof_wall_roughness()
    from each wall-type CfdOF boundary object's Label (== OpenFOAM patch
    name) and its expression-linked RoughnessHeight/RoughnessConstant.
    """
    import re as _re

    rough = params.get("_wall_roughness")
    if not rough:
        return False

    nut_path = os.path.join(solv_dir, "0", "nut")
    if not os.path.isfile(nut_path):
        print("[AI4CFD] 0/nut not found — wall roughness patch skipped",
              flush=True)
        return False

    with open(nut_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    patched = 0
    for patch_name, vals in rough.items():
        m = _re.search(rf'\b{_re.escape(patch_name)}\b\s*\n?\s*\{{', text)
        if not m:
            continue
        start, depth, i = m.end(), 1, m.end()
        while i < len(text) and depth > 0:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        block = text[start: i - 1]

        block_count = 0
        for key in ("Ks", "Cs"):
            if key not in vals:
                continue
            val = vals[key]
            new_block, n = _re.subn(
                rf'(\b{key}\s+uniform\s+)(-?[\d.eE+\-]+)',
                lambda mo, val=val: f"{mo.group(1)}{val:.6g}",
                block)
            if n:
                block = new_block
                block_count += n

        if block_count == 0:
            continue
        text = text[:start] + block + text[i - 1:]
        patched += block_count
        print(f"[AI4CFD] 0/nut: patch '{patch_name}' -> "
              f"Ks={vals.get('Ks')}, Cs={vals.get('Cs')} "
              f"({block_count} value(s))", flush=True)

    if patched == 0:
        print("[AI4CFD] 0/nut: no named boundaryField blocks matched "
              "_wall_roughness", flush=True)
        return False

    with open(nut_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def _patch_inlet_flow_rate(solv_dir: str, params: dict) -> None:
    """Rewrite the inlet's volumetricFlowRate in 0/U for flowRateInletVelocity BCs.

    CfdOF's 'volumetricFlowRateInlet' subtype writes the inlet patch as:
        type                flowRateInletVelocity;
        volumetricFlowRate  constant 0.001;
        value               $internalField;
    This is a completely different mechanism from a fixed-velocity inlet: the
    driving number is 'volumetricFlowRate', not a 'uniform (Ux Uy Uz)' vector,
    so _patch_inlet_velocity() never touches it. Without this, every case in a
    volumetric-rate parametric sweep silently keeps the template's flow rate.

    Velocity source (priority order), mirroring _patch_inlet_velocity:
      1. '_inlet_volumetric_flow_rate_m3s' — set by FreeCAD export from
         expression-linked VolFlowRate (already in m^3/s).
      2. Any param key matching a flow-rate regex (assumed already m^3/s —
         unlike velocity, there's no reliable reference magnitude in 0/U to
         auto-detect alternate units against).
    """
    import re as _re

    if "_inlet_volumetric_flow_rate_m3s" in params:
        rate_m3s = float(params["_inlet_volumetric_flow_rate_m3s"])
        src = "_inlet_volumetric_flow_rate_m3s"
    else:
        rate_key = next(
            (k for k in params if _re.search(r'flow.?rate|volumetric', k, _re.IGNORECASE)),
            None,
        )
        if rate_key is None:
            return
        rate_m3s = float(params[rate_key])
        src = rate_key

    u_path = os.path.join(solv_dir, "0", "U")
    if not os.path.isfile(u_path):
        print("[AI4CFD] 0/U not found — flow-rate patch skipped", flush=True)
        return

    with open(u_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    _RATE_RE = _re.compile(
        r'(volumetricFlowRate\s+constant\s+)(-?[\d.eE+\-]+)(\s*;)')
    count = [0]

    def _replace(mo):
        count[0] += 1
        return f"{mo.group(1)}{rate_m3s:.6g}{mo.group(3)}"

    new_text = _RATE_RE.sub(_replace, text)

    if count[0] == 0:
        print(f"[AI4CFD] 0/U: no volumetricFlowRate entry found — "
              f"'{src}' not applied (inlet BC may not be flowRateInletVelocity)",
              flush=True)
        return

    with open(u_path, "w", encoding="utf-8") as fh:
        fh.write(new_text)
    print(f"[AI4CFD] Patching 0/U volumetricFlowRate from '{src}': "
          f"{rate_m3s:.6g} m^3/s ({count[0]} patch(es))", flush=True)


def _reset_stale_nonuniform_fields(zero_dir: str) -> None:
    """Collapse baked-in nonuniform 0/<field> data to a representative uniform value.

    A template built by pointing 'OF template' at an already-solved CfdOF case
    (see the panel's own tip: "run your case once via CfdOF, then paste that
    case path here") carries the solved internalField / boundary 'value'
    entries verbatim as 'nonuniform List<...> N (...)'. That N is the face/cell
    count of the ORIGINAL mesh. Copied into a parametric case whose geometry
    (and therefore mesh) differs, it either fails to read (size mismatch) or
    silently seeds the new case with wrong per-cell data — and since it isn't
    a plain 'uniform (x y z)' vector, _patch_inlet_velocity's direction lookup
    on internalField also silently no-ops when this is present.

    Collapsing each nonuniform block to its first data row keeps a reasonable
    representative direction/magnitude (rather than an arbitrary zero) for
    downstream velocity/flow-rate patching and potentialFoam -initialiseUBCs
    to build on, while being valid for any mesh size.
    """
    import re as _re

    if not os.path.isdir(zero_dir):
        return

    _BLOCK_RE = _re.compile(
        r'(internalField|value)(\s+)nonuniform\s+List<(\w+)>\s*'
        r'\d+\s*\(\s*(.*?)\)\s*;',
        _re.DOTALL,
    )
    _FIRST_VEC_RE = _re.compile(
        r'\(\s*(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)\s+(-?[\d.eE+\-]+)\s*\)')
    _FIRST_SCALAR_RE = _re.compile(r'(-?[\d.eE+\-]+)')

    for fname in os.listdir(zero_dir):
        fpath = os.path.join(zero_dir, fname)
        if not os.path.isfile(fpath):
            continue
        try:
            text = open(fpath, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        if "nonuniform" not in text:
            continue

        def _replace(mo):
            keyword, sep, ftype, body = mo.group(1), mo.group(2), mo.group(3), mo.group(4)
            if ftype == "scalar":
                m = _FIRST_SCALAR_RE.search(body)
                repl = m.group(1) if m else "0"
            else:
                m = _FIRST_VEC_RE.search(body)
                repl = f"({m.group(1)} {m.group(2)} {m.group(3)})" if m else "(0 0 0)"
            return f"{keyword}{sep}uniform {repl};"

        new_text = _BLOCK_RE.sub(_replace, text)
        if new_text != text:
            with open(fpath, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            print(f"[AI4CFD] 0/{fname}: collapsed stale nonuniform field(s) to "
                  f"uniform (mesh-size independent)", flush=True)


def _has_potential_flow(solv_dir: str) -> bool:
    """Return True if system/fvSolution has a potentialFlow{} sub-dict.

    CfdOF-generated cases always include this and rely on it via
    'potentialFoam -initialiseUBCs' in their own Allrun script (see e.g.
    cfd_models/bend_pipe_demo/case/Allrun). Gate on its presence so this
    doesn't break plain/non-CfdOF templates that never had it.
    """
    import re as _re
    path = os.path.join(solv_dir, "system", "fvSolution")
    if not os.path.isfile(path):
        return False
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return False
    return bool(_re.search(r'\bpotentialFlow\s*\{', text))


def _patch_meshdict_surface_file(mesh_dir: str) -> None:
    """Generate cfmesh_body.stl and update meshDict to use it.

    cfMesh needs a closed surface formed by the named sub-patches (wall +
    inlet + outlet).  patch_0_* (the full closed body used only by
    snappyHexMesh for geometry snapping) is intentionally excluded —
    including it would duplicate the surface triangles and confuse cfMesh.

    The generated solid names (patch_3_1, patch_1_0, patch_2_0) become the
    cfMesh patch names, which createPatch then renames to wall/inlet/outlet.
    """
    import re as _re
    mdict = os.path.join(mesh_dir, "system", "meshDict")
    if not os.path.isfile(mdict):
        return

    # Build cfmesh_body.stl: all patch_N_M.stl where N != 0
    trisurf = os.path.join(mesh_dir, "constant", "triSurface")
    cfmesh_stl = os.path.join(trisurf, "cfmesh_body.stl")
    if os.path.isdir(trisurf):
        parts = []
        for fname in sorted(os.listdir(trisurf)):
            if not fname.lower().endswith(".stl"):
                continue
            # Include only patch_N_M.stl with N > 0 (skip patch_0_*, non-patch files)
            import re as _re2
            m = _re2.match(r"patch_(\d+)_", fname, _re2.IGNORECASE)
            if not m or int(m.group(1)) == 0:
                continue
            try:
                with open(os.path.join(trisurf, fname),
                          encoding="ascii", errors="replace") as fh:
                    parts.append(fh.read())
            except Exception:
                pass
        if parts:
            with open(cfmesh_stl, "w", encoding="ascii") as fh:
                fh.writelines(parts)
            print(f"[AI4CFD] cfmesh_body.stl generated from {len(parts)} patch STL(s)",
                  flush=True)
        else:
            print("[AI4CFD] WARNING: no patch STLs found for cfmesh_body.stl", flush=True)

    # Patch meshDict to use the generated STL
    try:
        text = open(mdict, encoding="utf-8", errors="replace").read()
        new_text = _re.sub(
            r'surfaceFile\s+"[^"]*";',
            'surfaceFile "constant/triSurface/cfmesh_body.stl";',
            text,
            count=1)
        if new_text != text:
            with open(mdict, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            print("[AI4CFD] meshDict: surfaceFile → constant/triSurface/cfmesh_body.stl",
                  flush=True)
        else:
            print("[AI4CFD] meshDict: surfaceFile already points to cfmesh_body.stl",
                  flush=True)
    except Exception as exc:
        print(f"[AI4CFD] Warning: meshDict surfaceFile patch failed: {exc}", flush=True)


# ── Split layout (CfdOF meshCase + case) ─────────────────────────────────────

def _run_split(case_dir: str, template_dir: str, num_cores: int,
               mesh_only: bool = False, inference_prep: bool = False,
               params: dict = None) -> None:
    tmpl_mesh = os.path.join(template_dir, "meshCase")
    tmpl_case = os.path.join(template_dir, "case")
    mesh_dir  = os.path.join(case_dir, "meshCase")
    solv_dir  = os.path.join(case_dir, "case")

    # ── 1. Snapshot all our exported STL files before template copy ───────────
    # FreeCAD exports to case_NNNN/constant/triSurface/:
    #   • Body_Geometry.stl + patch_1_0.stl ... (if CfdOF boundaries found)
    #   • geometry.stl                          (single-body fallback)
    our_trisurf = os.path.join(case_dir, "constant", "triSurface")
    our_stl_data: dict = {}   # {filename: bytes}
    if os.path.isdir(our_trisurf):
        for fname in os.listdir(our_trisurf):
            if fname.lower().endswith(".stl"):
                with open(os.path.join(our_trisurf, fname), "rb") as fh:
                    our_stl_data[fname] = fh.read()
        print(f"[AI4CFD] Loaded {len(our_stl_data)} STL file(s): "
              f"{sorted(our_stl_data)}", flush=True)
    else:
        print("[AI4CFD] WARNING: no constant/triSurface/ found in case dir", flush=True)

    if not our_stl_data:
        print("[AI4CFD] FATAL: no geometry STL was exported — aborting.", flush=True)
        sys.exit(1)

    # ── 2. Read template's main STL name BEFORE template copy ────────────────
    tmpl_trisurf  = os.path.join(tmpl_mesh, "constant", "triSurface")
    tmpl_main_stl = _find_main_stl(tmpl_trisurf)
    print(f"[AI4CFD] Template main STL: {tmpl_main_stl or '(none found)'}", flush=True)

    # ── 3. Copy template meshCase and case into case_dir ──────────────────────
    print("[AI4CFD] Copying template meshCase…", flush=True)
    _copy_template_files(tmpl_mesh, mesh_dir)
    print("[AI4CFD] Copying template case…", flush=True)
    _copy_template_files(tmpl_case, solv_dir)

    # Template's 0/ may carry a previously-solved case's nonuniform field data
    # (sized for a different mesh) — collapse it before anything downstream
    # (BC patching, potentialFoam) relies on 0/ being mesh-size independent.
    _reset_stale_nonuniform_fields(os.path.join(solv_dir, "0"))

    # Patch locationInMesh so snappyHexMesh starts inside the new geometry
    _patch_snappy_location(case_dir, mesh_dir)

    # ── 4. Write our STLs into meshCase/constant/triSurface/ ──────────────────
    mesh_trisurf = os.path.join(mesh_dir, "constant", "triSurface")
    os.makedirs(mesh_trisurf, exist_ok=True)
    for fname, data in our_stl_data.items():
        dest = os.path.join(mesh_trisurf, fname)
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"[AI4CFD] Placed {fname} → meshCase/constant/triSurface/", flush=True)

    # If only geometry.stl (single-body fallback), also write under the
    # template's expected main STL name so snappyHexMeshDict references work.
    is_single_fallback = (
        len(our_stl_data) == 1 and "geometry.stl" in our_stl_data
    )
    if is_single_fallback and tmpl_main_stl and tmpl_main_stl != "geometry.stl":
        dest = os.path.join(mesh_trisurf, tmpl_main_stl)
        with open(dest, "wb") as fh:
            fh.write(our_stl_data["geometry.stl"])
        print(f"[AI4CFD] Also wrote geometry.stl as '{tmpl_main_stl}' "
              f"(snappyHexMeshDict reference)", flush=True)

    # ── 4b. Ensure Body_Geometry.stl has correct named region(s) ─────────────
    active_main = tmpl_main_stl or "geometry.stl"
    stl_dest    = os.path.join(mesh_trisurf, active_main)
    if os.path.isfile(stl_dest):
        n_solids = _stl_solid_count(stl_dest)
        if n_solids > 1:
            print(f"[AI4CFD] '{active_main}' already has {n_solids} named solids "
                  f"— no rename needed", flush=True)
        else:
            # Read all expected region names from snappyHexMeshDict
            region_names = _get_all_snappy_stl_regions(mesh_dir, active_main)
            if len(region_names) >= 2 and is_single_fallback:
                # Single FreeCAD body export — classify triangles by z-centroid.
                # The STL from _export_stl is already in metres (×0.001 applied
                # in FreeCAD), so scale=1.0 here.
                ok = _classify_stl_to_multi_solid(
                    stl_dest, region_names, stl_dest, scale=1.0)
                if not ok:
                    # Fallback: build multi-solid by concatenating template patch STLs
                    # (ASCII, metres, already in triSurface from template copy in step 3)
                    ok2 = _build_multi_solid_from_patch_stls(
                        mesh_trisurf, region_names, active_main)
                    if not ok2:
                        print("[AI4CFD] patch STL fallback also failed — "
                              "renaming to first region only", flush=True)
                        _fix_stl_solid_name(stl_dest, region_names[0])
            else:
                region = (region_names[0] if region_names else
                          _get_snappy_stl_region(mesh_dir, active_main))
                if region:
                    _fix_stl_solid_name(stl_dest, region)
                else:
                    print(f"[AI4CFD] No regions found for '{active_main}' "
                          f"— solid name left as-is", flush=True)

    # ── 5. Find main STL name for feature-edge cleanup ────────────────────────
    main_stl = tmpl_main_stl or _find_main_stl(mesh_trisurf)
    print(f"[AI4CFD] Main STL: {main_stl or '(none)'}", flush=True)

    # ── 6. Clean pre-computed edge meshes (surfaceFeatureExtract regenerates) ──
    if main_stl:
        _clean_feature_edges(mesh_dir, main_stl)

    # ── 7. Clean stale polyMesh / time dirs ───────────────────────────────────
    _clean_solver_dirs(mesh_dir)
    _clean_solver_dirs(solv_dir)

    # ── 7. Diagnostics ────────────────────────────────────────────────────────
    for label, d in [("meshCase/system", os.path.join(mesh_dir, "system")),
                     ("case/system",     os.path.join(solv_dir, "system"))]:
        if os.path.isdir(d):
            print(f"[AI4CFD] {label}/ contains: {sorted(os.listdir(d))}", flush=True)
    trisurf_out = os.path.join(mesh_dir, "constant", "triSurface")
    if os.path.isdir(trisurf_out):
        print(f"[AI4CFD] meshCase/constant/triSurface/ contains: "
              f"{sorted(os.listdir(trisurf_out))}", flush=True)

    # ── 8. Meshing (runs in meshCase/) ────────────────────────────────────────
    if _has(mesh_dir, "system", "meshDict"):
        # ── cfMesh path ───────────────────────────────────────────────────────
        # Patch the surface file reference (CfdOF writes .fms; we export .stl)
        _patch_meshdict_surface_file(mesh_dir)
        # Try cartesianMesh (hex-dominant); fall back to tetMesh
        if _run("cartesianMesh", mesh_dir) != 0:
            print("[AI4CFD] cartesianMesh failed — trying tetMesh", flush=True)
            _require("tetMesh", mesh_dir)
    else:
        # ── snappyHexMesh path ────────────────────────────────────────────────
        _update_blockmesh_bbox(mesh_dir)   # expand background mesh if geometry grew
        if _has(mesh_dir, "system", "blockMeshDict"):
            _require("blockMesh", mesh_dir)
        else:
            print("[AI4CFD] No blockMeshDict — skipping blockMesh", flush=True)

        if _has(mesh_dir, "system", "surfaceFeatureExtractDict") or \
           _has(mesh_dir, "system", "surfaceFeaturesDict"):
            _require("surfaceFeatureExtract", mesh_dir)
        else:
            print("[AI4CFD] No surfaceFeatureExtractDict — skipping surfaceFeatureExtract",
                  flush=True)

        if _has(mesh_dir, "system", "snappyHexMeshDict"):
            _require("snappyHexMesh -overwrite", mesh_dir)
        else:
            print("[AI4CFD] No snappyHexMeshDict — skipping snappyHexMesh", flush=True)

    # ── 9. Copy polyMesh meshCase → case ─────────────────────────────────────
    src_poly = os.path.join(mesh_dir, "constant", "polyMesh")
    dst_poly = os.path.join(solv_dir, "constant", "polyMesh")
    if os.path.isdir(src_poly):
        if os.path.exists(dst_poly):
            shutil.rmtree(dst_poly)
        shutil.copytree(src_poly, dst_poly)
        print("[AI4CFD] Copied polyMesh: meshCase → case", flush=True)
    else:
        print("[AI4CFD] WARNING: no polyMesh produced — solver may fail", flush=True)

    # ── 10. createPatch — rename snappyHexMesh patches → real BC names ────────
    # (patch_1_0 → inlet, patch_2_0 → outlet, patch_3_0 → wall, etc.)
    # Must run BEFORE the solver reads 0/p or 0/U.
    if _has(solv_dir, "system", "createPatchDict"):
        _require("createPatch -overwrite", solv_dir)
    else:
        print("[AI4CFD] No createPatchDict — skipping createPatch", flush=True)

    # ── 10b. Patch expression-linked CfdOF boundary properties (fallback
    #        path only — the CfdOF full-case write already embeds every
    #        value correctly when that path succeeds). Generic mechanism
    #        first; the field-specific ones are legacy fallbacks for
    #        params.json written before _boundary_properties existed.
    if not _patch_named_boundary_properties(solv_dir, params or {}):
        if not _patch_named_boundary_velocities(solv_dir, params or {}):
            _patch_inlet_velocity(solv_dir, params or {})
        _patch_named_wall_roughness(solv_dir, params or {})
    _patch_inlet_flow_rate(solv_dir, params or {})

    # ── 11. renumberMesh — improve solver convergence ─────────────────────────
    if _has(solv_dir, "system", "decomposeParDict") or True:
        _run("renumberMesh -overwrite", solv_dir)   # non-fatal

    # ── 11b. potentialFoam -initialiseUBCs — initialise U/p from a potential-
    #         flow solve, exactly as CfdOF's own Allrun does (see e.g.
    #         cfd_models/bend_pipe_demo/case/Allrun). Skipped only in pure
    #         mesh-only mode, where nothing downstream reads the field.
    if mesh_only and not inference_prep:
        print("[AI4CFD] mesh-only mode — skipping potentialFoam init.", flush=True)
    elif _has_potential_flow(solv_dir):
        _require("potentialFoam -initialiseUBCs -pName p", solv_dir)
    else:
        print("[AI4CFD] No potentialFlow{} in fvSolution — skipping potentialFoam init.",
              flush=True)

    # ── 12. Solver (runs in case/) ────────────────────────────────────────────
    if mesh_only or inference_prep:
        print("[AI4CFD] mesh-only mode — skipping solver.", flush=True)
        if inference_prep:
            _run_inference_postprocess(case_dir, solv_dir)
        return

    solver = _read_solver(solv_dir)
    print(f"[AI4CFD] Solver: {solver}", flush=True)

    if num_cores > 1:
        _require("decomposePar", solv_dir)
        _require(f"mpirun -np {num_cores} {solver} -parallel", solv_dir)
        _run("reconstructPar", solv_dir)
    else:
        _require(solver, solv_dir)


# ── CfdOF-written case detection + pipeline-only runner ──────────────────────

def _is_cfdof_case_complete(case_dir: str) -> bool:
    """Return True if CfdOF already wrote a complete meshCase+case layout here.

    If constant/triSurface/ exists in the case root, a fresh STL-only export
    was done — always use the template-copy path so the new STL takes effect.
    """
    if os.path.isdir(os.path.join(case_dir, "constant", "triSurface")):
        return False  # fresh STL export present; use _run_split
    return (
        os.path.isdir(os.path.join(case_dir, "meshCase", "system")) and
        os.path.isdir(os.path.join(case_dir, "case", "0")) and
        os.path.isdir(os.path.join(case_dir, "meshCase", "constant", "triSurface"))
    )


def _write_turbulence_lib(d: str) -> None:
    """Write system/turbulenceLib, matching CfdOF's own Allrun detection.

    CfdOF's template controlDict does '#include "turbulenceLib"' (see
    vendor/CfdOF/Data/Templates/case/system/controlDict) — but that file is
    never a static template; CfdOF's own Allrun script writes it at run
    time by checking which turbulence library is actually installed
    (OpenFOAM renamed 'turbulenceModels' to 'momentumTransportModels' in
    some branches; older/ESI Windows builds still ship 'turbulenceModels').
    Any pipeline that invokes OpenFOAM utilities directly instead of
    running Allrun (as this one does) must replicate that one-line
    detection itself, or the very first utility that reads controlDict —
    even something as unrelated as createPatch — fails immediately with a
    missing-include FOAM FATAL IO ERROR, before the solver ever runs.
    """
    ctrl = os.path.join(d, "system", "controlDict")
    if not os.path.isfile(ctrl):
        return
    text = open(ctrl, encoding="utf-8", errors="replace").read()
    if "turbulenceLib" not in text:
        return
    if os.path.isfile(os.path.join(d, "system", "turbulenceLib")):
        return  # already written (e.g. meshCase and case share nothing, but idempotent either way)

    # Ask the sourced OpenFOAM environment for FOAM_LIBBIN rather than
    # guessing a version/platform-specific install path ourselves.
    if sys.platform == "win32":
        from utilities.openfoam_env import windows_openfoam_source_cmd
        probe_cmd = windows_openfoam_source_cmd("echo %FOAM_LIBBIN%", cwd=d)
    else:
        probe_cmd = f'bash -c "source {_OF_SOURCE} 2>/dev/null && echo $FOAM_LIBBIN"'
    lib_bin = ""
    try:
        out = subprocess.run(probe_cmd, shell=True, cwd=d, capture_output=True,
                              text=True, timeout=60).stdout
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if lines:
            lib_bin = lines[-1]
    except Exception as exc:
        print(f"[AI4CFD] WARNING: could not probe FOAM_LIBBIN ({exc})", flush=True)

    momentum = os.path.isdir(lib_bin) and any(
        os.path.isfile(os.path.join(lib_bin, f"libmomentumTransportModels{ext}"))
        for ext in (".so", ".dll"))
    lib_name = "libmomentumTransportModels.so" if momentum else "libturbulenceModels.so"

    with open(os.path.join(d, "system", "turbulenceLib"), "w") as fh:
        fh.write(lib_name + "\n")
    note = "" if lib_bin else " (FOAM_LIBBIN not resolved — defaulted)"
    print(f"[AI4CFD] system/turbulenceLib -> {lib_name}{note}", flush=True)


def _run_cfmesh(mesh_dir: str) -> None:
    """Run cfMesh's cartesianMesh, converting the surface to .fms first.

    Mirrors CfdOF's own Allmesh template for MeshUtility=cfMesh (see
    vendor/CfdOF/Data/Templates/mesh/Allmesh): meshDict's surfaceFile
    references a '<Name>_Geometry.fms' file that CfdOF's own mesh writer
    never actually produces as a static output — its Allmesh script runs
    surfaceFeatureEdges to generate it from the STL immediately before
    invoking cartesianMesh. Any pipeline invoking these utilities directly
    (as this one does) must do the same conversion itself, or cartesianMesh
    fails outright with a missing-surface error.
    """
    import re as _re

    mdict_path = os.path.join(mesh_dir, "system", "meshDict")
    if not os.path.isfile(mdict_path):
        _require("cartesianMesh", mesh_dir)
        return

    text = open(mdict_path, encoding="utf-8", errors="replace").read()
    m = _re.search(r'surfaceFile\s+"([^"]+)"', text)
    if m:
        surface_ref = m.group(1)          # e.g. 'Fusion005_Geometry.fms'
        trisurf  = os.path.join(mesh_dir, "constant", "triSurface")
        fms_path = os.path.join(trisurf, surface_ref)
        if surface_ref.lower().endswith(".fms") and not os.path.isfile(fms_path):
            stl_ref  = surface_ref[:-4] + ".stl"
            stl_path = os.path.join(trisurf, stl_ref)
            if os.path.isfile(stl_path):
                _require(
                    f'surfaceFeatureEdges -angle 60 '
                    f'"constant/triSurface/{stl_ref}" "{surface_ref}"',
                    mesh_dir)
            else:
                print(f"[AI4CFD] WARNING: meshDict expects "
                      f"constant/triSurface/{surface_ref}, and no matching "
                      f"{stl_ref} was found to convert — cartesianMesh will "
                      f"likely fail", flush=True)
    else:
        print("[AI4CFD] meshDict has no surfaceFile entry — running "
              "cartesianMesh as-is", flush=True)

    if _run("cartesianMesh", mesh_dir) != 0:
        print("[AI4CFD] cartesianMesh failed — trying tetMesh", flush=True)
        _require("tetMesh", mesh_dir)


def _run_pipeline_only_split(case_dir: str, num_cores: int,
                             mesh_only: bool = False) -> None:
    """Run meshing + solver for a case CfdOF already wrote — no template copy."""
    mesh_dir = os.path.join(case_dir, "meshCase")
    solv_dir = os.path.join(case_dir, "case")

    mesh_trisurf = os.path.join(mesh_dir, "constant", "triSurface")
    main_stl = _find_main_stl(mesh_trisurf)
    print(f"[AI4CFD] Main STL: {main_stl or '(none)'}", flush=True)

    if main_stl:
        _clean_feature_edges(mesh_dir, main_stl)
    _clean_solver_dirs(mesh_dir)
    _clean_solver_dirs(solv_dir)

    for label, d in [("meshCase/system", os.path.join(mesh_dir, "system")),
                     ("case/system",     os.path.join(solv_dir, "system"))]:
        if os.path.isdir(d):
            print(f"[AI4CFD] {label}/ contains: {sorted(os.listdir(d))}", flush=True)
    if os.path.isdir(mesh_trisurf):
        print(f"[AI4CFD] meshCase/constant/triSurface/ contains: "
              f"{sorted(os.listdir(mesh_trisurf))}", flush=True)

    # CfdOF's template controlDict always '#include's turbulenceLib, which
    # is normally written at run time by Allrun, not by the case writer —
    # write it now so even the FIRST utility below (meshing, createPatch)
    # doesn't fail on a missing include. meshCase and case each carry their
    # own controlDict/system dir, so both need it.
    _write_turbulence_lib(mesh_dir)
    _write_turbulence_lib(solv_dir)

    if _has(mesh_dir, "system", "meshDict"):
        # ── cfMesh path ────────────────────────────────────────────────────
        _run_cfmesh(mesh_dir)
    else:
        # ── snappyHexMesh path ───────────────────────────────────────────────
        _update_blockmesh_bbox(mesh_dir)   # expand background mesh if geometry grew
        if _has(mesh_dir, "system", "blockMeshDict"):
            _require("blockMesh", mesh_dir)
        else:
            print("[AI4CFD] No blockMeshDict — skipping blockMesh", flush=True)

        if _has(mesh_dir, "system", "surfaceFeatureExtractDict") or \
           _has(mesh_dir, "system", "surfaceFeaturesDict"):
            _require("surfaceFeatureExtract", mesh_dir)
        else:
            print("[AI4CFD] No surfaceFeatureExtractDict — skipping", flush=True)

        if _has(mesh_dir, "system", "snappyHexMeshDict"):
            _require("snappyHexMesh -overwrite", mesh_dir)
        else:
            print("[AI4CFD] No snappyHexMeshDict — skipping", flush=True)

    src_poly = os.path.join(mesh_dir, "constant", "polyMesh")
    dst_poly = os.path.join(solv_dir, "constant", "polyMesh")
    if os.path.isdir(src_poly):
        if os.path.exists(dst_poly):
            shutil.rmtree(dst_poly)
        shutil.copytree(src_poly, dst_poly)
        print("[AI4CFD] Copied polyMesh: meshCase → case", flush=True)
    else:
        print("[AI4CFD] WARNING: no polyMesh produced — solver may fail", flush=True)

    if _has(solv_dir, "system", "createPatchDict"):
        _require("createPatch -overwrite", solv_dir)
    else:
        print("[AI4CFD] No createPatchDict — skipping createPatch", flush=True)

    _run("renumberMesh -overwrite", solv_dir)

    if mesh_only:
        print("[AI4CFD] mesh-only mode — skipping potentialFoam init and solver.",
              flush=True)
        return

    if _has_potential_flow(solv_dir):
        _require("potentialFoam -initialiseUBCs -pName p", solv_dir)
    else:
        print("[AI4CFD] No potentialFlow{} in fvSolution — skipping potentialFoam init.",
              flush=True)

    solver = _read_solver(solv_dir)
    print(f"[AI4CFD] Solver: {solver}", flush=True)

    if num_cores > 1:
        _require("decomposePar", solv_dir)
        _require(f"mpirun -np {num_cores} {solver} -parallel", solv_dir)
        _run("reconstructPar", solv_dir)
    else:
        _require(solver, solv_dir)


# ── Single-folder layout ──────────────────────────────────────────────────────

def _run_single(case_dir: str, template_dir: str, num_cores: int,
                mesh_only: bool = False, params: dict = None) -> None:
    our_trisurf = os.path.join(case_dir, "constant", "triSurface")
    geom_bytes  = None
    if os.path.isdir(our_trisurf):
        stls = [f for f in os.listdir(our_trisurf) if f.lower().endswith(".stl")]
        if stls:
            with open(os.path.join(our_trisurf, stls[0]), "rb") as fh:
                geom_bytes = fh.read()

    tmpl_trisurf = os.path.join(template_dir, "constant", "triSurface")
    main_stl     = _find_main_stl(tmpl_trisurf)
    print(f"[AI4CFD] Template main STL: {main_stl or '(none found)'}", flush=True)

    print("[AI4CFD] Copying template config files…", flush=True)
    _copy_template_files(template_dir, case_dir)

    # See _run_split: a template copied from an already-solved case carries
    # mesh-size-dependent nonuniform field data that must not survive into a
    # case with different geometry.
    _reset_stale_nonuniform_fields(os.path.join(case_dir, "0"))

    if geom_bytes:
        name = main_stl or "geometry.stl"
        dest = os.path.join(our_trisurf, name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(geom_bytes)
        print(f"[AI4CFD] Wrote geometry → constant/triSurface/{name}", flush=True)
        # Fix solid name to match snappyHexMeshDict region reference
        region = _get_snappy_stl_region(case_dir, name)
        if region:
            _fix_stl_solid_name(dest, region)
        if main_stl:
            _clean_feature_edges(case_dir, main_stl)

    _clean_solver_dirs(case_dir)

    sys_dir = os.path.join(case_dir, "system")
    if os.path.isdir(sys_dir):
        print(f"[AI4CFD] system/ contains: {sorted(os.listdir(sys_dir))}", flush=True)

    _update_blockmesh_bbox(case_dir)   # expand background mesh if geometry grew
    if _has(case_dir, "system", "blockMeshDict"):
        _require("blockMesh", case_dir)
    else:
        print("[AI4CFD] No blockMeshDict — skipping blockMesh", flush=True)

    if _has(case_dir, "system", "surfaceFeatureExtractDict") or \
       _has(case_dir, "system", "surfaceFeaturesDict"):
        _require("surfaceFeatureExtract", case_dir)
    else:
        print("[AI4CFD] No surfaceFeatureExtractDict — skipping surfaceFeatureExtract",
              flush=True)

    if _has(case_dir, "system", "snappyHexMeshDict"):
        _require("snappyHexMesh -overwrite", case_dir)
    else:
        print("[AI4CFD] No snappyHexMeshDict — skipping snappyHexMesh", flush=True)

    if _has(case_dir, "system", "createPatchDict"):
        _require("createPatch -overwrite", case_dir)

    # Patch expression-linked CfdOF boundary properties (generic mechanism
    # first; the field-specific ones are legacy fallbacks for params.json
    # written before _boundary_properties existed).
    if not _patch_named_boundary_properties(case_dir, params or {}):
        if not _patch_named_boundary_velocities(case_dir, params or {}):
            _patch_inlet_velocity(case_dir, params or {})
        _patch_named_wall_roughness(case_dir, params or {})
    _patch_inlet_flow_rate(case_dir, params or {})

    _run("renumberMesh -overwrite", case_dir)

    if mesh_only:
        print("[AI4CFD] mesh-only mode — skipping potentialFoam init and solver.",
              flush=True)
        return

    if _has_potential_flow(case_dir):
        _require("potentialFoam -initialiseUBCs -pName p", case_dir)
    else:
        print("[AI4CFD] No potentialFlow{} in fvSolution — skipping potentialFoam init.",
              flush=True)

    solver = _read_solver(case_dir)
    print(f"[AI4CFD] Solver: {solver}", flush=True)

    if num_cores > 1:
        _require("decomposePar", case_dir)
        _require(f"mpirun -np {num_cores} {solver} -parallel", case_dir)
        _run("reconstructPar", case_dir)
    else:
        _require(solver, case_dir)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-dir",     required=True)
    parser.add_argument("--template-dir", required=True)
    parser.add_argument("--num-cores",    type=int, default=1)
    parser.add_argument("--params",       default="{}", type=json.loads)
    parser.add_argument("--mesh-only",      action="store_true",
                        help="Run meshing only — skip the solver")
    parser.add_argument("--inference-prep", action="store_true",
                        help="Mesh + foamToVTK + wall distance + .npy for inference")
    args = parser.parse_args()

    case_dir     = args.case_dir
    template_dir = args.template_dir

    print(f"[AI4CFD] Case:     {case_dir}",     flush=True)
    print(f"[AI4CFD] Template: {template_dir}", flush=True)
    if args.params:
        print(f"[AI4CFD] Params:   {args.params}", flush=True)

    mesh_only      = args.mesh_only
    inference_prep = args.inference_prep
    if mesh_only:
        print("[AI4CFD] mesh-only mode — solver will be skipped.", flush=True)
    if inference_prep:
        print("[AI4CFD] inference-prep mode — mesh + foamToVTK + wall dist + .npy",
              flush=True)

    # Priority 1: CfdOF already wrote a complete case — run pipeline only
    if _is_cfdof_case_complete(case_dir):
        print("[AI4CFD] Layout: CfdOF-written (pipeline-only, no template copy)",
              flush=True)
        _run_pipeline_only_split(case_dir, args.num_cores, mesh_only=mesh_only)

    # Priority 2: need template copy
    elif not os.path.isdir(template_dir):
        print(f"[AI4CFD] ERROR: template not found and case is incomplete: "
              f"{template_dir}", flush=True)
        sys.exit(1)

    else:
        split = (os.path.isdir(os.path.join(template_dir, "meshCase")) and
                 os.path.isdir(os.path.join(template_dir, "case")))
        if split:
            print("[AI4CFD] Layout: CfdOF split (meshCase + case)", flush=True)
            _run_split(case_dir, template_dir, args.num_cores,
                       mesh_only=mesh_only, inference_prep=inference_prep,
                       params=args.params)
        else:
            print("[AI4CFD] Layout: single-folder", flush=True)
            _run_single(case_dir, template_dir, args.num_cores, mesh_only=mesh_only,
                        params=args.params)

    print(f"[AI4CFD] DONE: {case_dir}", flush=True)


if __name__ == "__main__":
    main()
