"""
utilities/calculate_forces.py

Compute aerodynamic lift and drag forces from a VTP point-cloud inference result.

Method (pressure integration):
    F = -∫∫ p n̂ dA  ≈  -Σ_i p_i n̂_i A_i

    where n̂_i is the outward surface normal and A_i is the area of the i-th
    triangle after Delaunay triangulation of the point cloud.

    Viscous forces require wall shear stress (τ_w), which is not available
    from this inference — only p and U are predicted.  The viscous contribution
    is typically 5-15 % of total drag for a smooth airfoil; pressure forces
    dominate lift.

Usage (CLI):
    python utilities/calculate_forces.py [vtp_path]
        --aoa   <degrees>   angle of attack (default from config)
        --chord <m>         chord length    (default from config)
        --span  <m>         span            (default from config)
        --speed <m/s>       freestream speed
        --rho   <kg/m3>     air density (default 1.225)

Usage (import):
    from utilities.calculate_forces import compute_forces
    results = compute_forces("inference_output/inference_output_prediction_airfoil.vtp",
                             angle_of_attack=5.0, chord_length=1.0, span=0.2,
                             freestream_speed=10.0)
"""

import os
import sys
import argparse
import numpy as np
import pyvista as pv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import (
    TARGET_RESULT_NAME, INLET_PATCH_NAME,
    INFERENCE_DIR,
)


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def compute_forces(
    vtp_path: str,
    angle_of_attack: float = 0.0,
    chord_length: float    = 1.0,
    span: float            = 0.2,
    freestream_speed: float = 10.0,
    rho: float             = 1000,
    flip_normals: bool     = False,
) -> dict:
    """Compute pressure-based lift and drag from an airfoil surface VTP file.

    Args:
        vtp_path:         Path to the VTP file produced by the inference script.
        angle_of_attack:  Angle of attack in degrees.
        chord_length:     Chord length in metres (for Cl/Cd normalisation).
        span:             Spanwise extent in metres (for Cl/Cd normalisation).
        freestream_speed: Free-stream velocity magnitude in m/s.
        rho:              Air density in kg/m³.
        flip_normals:     Set True if the computed lift/drag signs are inverted.

    Returns:
        dict with keys: lift_N, drag_N, Cl, Cd, LD_ratio, F_vector, n_cells.
    """
    # ── Load ────────────────────────────────────────────────────────────────
    if not os.path.exists(vtp_path):
        raise FileNotFoundError(f"VTP file not found: {vtp_path}")

    mesh = pv.read(vtp_path)

    if "p" not in mesh.point_data:
        raise ValueError(
            f"Pressure field 'p' not found in {vtp_path}. "
            f"Available arrays: {list(mesh.point_data.keys())}"
        )

    # ── Triangulate point cloud → surface with areas and normals ────────────
    surface = mesh.delaunay_2d()
    surface = surface.compute_normals(
        cell_normals=True, point_normals=False, flip_normals=flip_normals
    )
    surface = surface.compute_cell_sizes(length=False, area=True, volume=False)

    # Map point-based pressure to cell-centroid values (triangle average)
    surface = surface.point_data_to_cell_data()

    p_cells = surface.cell_data["p"]          # (n_cells,)  scalar or (n_cells, 1)
    if p_cells.ndim > 1:
        p_cells = p_cells[:, 0]
    areas   = surface.cell_data["Area"]       # (n_cells,)
    normals = surface.cell_data["Normals"]    # (n_cells, 3)  outward unit normals

    # ── Pressure force integration ───────────────────────────────────────────
    # Fluid pressure acts inward on the surface:
    #   F_body = -∫ p n̂_outward dA
    F_cells = -(p_cells[:, np.newaxis] * normals * areas[:, np.newaxis])
    F_total = F_cells.sum(axis=0)   # (3,)

    # ── Project onto lift / drag axes ────────────────────────────────────────
    # Convention: freestream along +X, body rotated by AoA.
    #   drag_dir = freestream direction  = [cos α,  sin α, 0]
    #   lift_dir = ⊥ to freestream, +up = [-sin α, cos α, 0]
    alpha    = np.radians(angle_of_attack)
    drag_dir = np.array([ np.cos(alpha),  np.sin(alpha), 0.0])
    lift_dir = np.array([-np.sin(alpha),  np.cos(alpha), 0.0])

    lift = float(np.dot(F_total, lift_dir))
    drag = float(np.dot(F_total, drag_dir))

    # ── Non-dimensionalise ──────────────────────────────────────────────────
    q_inf = 0.5 * rho * freestream_speed ** 2
    S_ref = chord_length * span

    denom = q_inf * S_ref
    Cl    = lift / denom if denom > 0 else float("nan")
    Cd    = drag / denom if denom > 0 else float("nan")
    LD    = Cl / Cd if (not np.isnan(Cd) and Cd != 0.0) else float("nan")

    return {
        "lift_N":  lift,
        "drag_N":  drag,
        "Cl":      Cl,
        "Cd":      Cd,
        "LD_ratio": LD,
        "F_vector": F_total.tolist(),
        "n_cells":  surface.n_cells,
        "q_inf":    q_inf,
        "S_ref":    S_ref,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_vtp() -> str:
    return os.path.join(
        INFERENCE_DIR,
        f"inference_output_prediction_{TARGET_RESULT_NAME}.vtp",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Compute aerodynamic lift/drag from inference VTP output."
    )
    parser.add_argument(
        "vtp_path", nargs="?", default=_default_vtp(),
        help=f"Path to airfoil VTP file (default: {_default_vtp()})",
    )
    parser.add_argument("--aoa",   type=float, default=5.0,   help="Angle of attack in degrees")
    parser.add_argument("--chord", type=float, default=1.0,   help="Chord length in metres")
    parser.add_argument("--span",  type=float, default=0.2,   help="Span in metres")
    parser.add_argument("--speed", type=float, default=10.0,  help="Freestream speed in m/s")
    parser.add_argument("--rho",   type=float, default=1.225, help="Air density in kg/m³")
    parser.add_argument("--flip-normals", action="store_true",
                        help="Flip surface normals (use if lift/drag signs are wrong)")
    args = parser.parse_args()

    print(f"Loading: {args.vtp_path}")
    results = compute_forces(
        vtp_path         = args.vtp_path,
        angle_of_attack  = args.aoa,
        chord_length     = args.chord,
        span             = args.span,
        freestream_speed = args.speed,
        rho              = args.rho,
        flip_normals     = args.flip_normals,
    )

    print(f"\n--- Aerodynamic Forces (pressure only) ---")
    print(f"  AoA          : {args.aoa} deg")
    print(f"  Chord × Span : {args.chord} m × {args.span} m")
    print(f"  q_inf        : {results['q_inf']:.4f} Pa")
    print(f"  Surface cells: {results['n_cells']}")
    print()
    print(f"  Lift   : {results['lift_N']:+.4f} N")
    print(f"  Drag   : {results['drag_N']:+.4f} N")
    print(f"  Cl     : {results['Cl']:+.4f}")
    print(f"  Cd     : {results['Cd']:+.4f}")
    print(f"  L/D    : {results['LD_ratio']:+.2f}")
    print(f"  F_vec  : [{', '.join(f'{v:+.4f}' for v in results['F_vector'])}] N")
    print()
    print("  Note: viscous (friction) drag not included — requires wall shear stress.")


if __name__ == "__main__":
    main()
