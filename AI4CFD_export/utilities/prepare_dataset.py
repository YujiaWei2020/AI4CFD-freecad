"""Module for preparing ML-ready dataset from OpenFOAM VTK exports.

This script:
1. Merges internal.vtu + wall.vtp + inlet.vtp + outlet.vtp for each case
2. Adds type_flags column (one-hot encoded: [fluid, wall, inlet, outlet])
3. Saves as .npy files with shape (N, 12) for fast training

Columns: [x, y, z, u, v, w, p, wall_dist, is_fluid, is_wall, is_inlet, is_outlet]
"""

import os
import glob
import multiprocessing
import numpy as np
import pyvista as pv
from typing import List, Optional, Tuple
from tqdm import tqdm


def find_case_vtk_dirs(results_dir: str) -> List[str]:
    """Finds all VTK time directories in the results folder.
    
    Args:
        results_dir: Path to the results directory.
        
    Returns:
        List of paths to VTK time directories (e.g., results/case_XXX/VTK/case_XXX_543/).
    """
    # Pattern: results/case_*/VTK/case_*_*/
    pattern = os.path.join(results_dir, "*", "VTK", "*")
    dirs = [d for d in glob.glob(pattern) if os.path.isdir(d)]
    
    # Filter to only keep latest time per case
    case_vtk_map = {}
    for d in dirs:
        case_dir = os.path.dirname(os.path.dirname(d))  # Go up 2 levels to case dir
        if case_dir not in case_vtk_map:
            case_vtk_map[case_dir] = d
        else:
            # Keep the latest (alphabetically last)
            if d > case_vtk_map[case_dir]:
                case_vtk_map[case_dir] = d
    
    return list(case_vtk_map.values())


def load_boundary_mesh(vtk_time_dir: str, boundary_name: str) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Loads a boundary VTP file and extracts data.
    
    Args:
        vtk_time_dir: Path to the VTK time directory.
        boundary_name: Name of the boundary (wall, inlet, outlet).
        
    Returns:
        Tuple of (points, velocity, pressure, wall_distance) or None if file doesn't exist.
    """
    boundary_path = os.path.join(vtk_time_dir, "boundary", f"{boundary_name}.vtp")
    
    if not os.path.exists(boundary_path):
        return None
    
    try:
        mesh = pv.read(boundary_path)
        
        points = mesh.points.astype(np.float32)
        
        # Velocity
        if "U" in mesh.point_data:
            vel = mesh.point_data["U"].astype(np.float32)
        else:
            vel = np.zeros((len(points), 3), dtype=np.float32)
            
        # Pressure
        if "p" in mesh.point_data:
            p = mesh.point_data["p"].astype(np.float32)
        else:
            p = np.zeros(len(points), dtype=np.float32)
            
        # Wall distance
        if "wall_distance" in mesh.point_data:
            dist = mesh.point_data["wall_distance"].astype(np.float32)
        else:
            dist = np.zeros(len(points), dtype=np.float32)
            
        return points, vel, p, dist
        
    except Exception as e:
        print(f"Error loading {boundary_path}: {e}")
        return None


def process_single_case(args: Tuple[str, str]) -> str:
    """Processes a single case and saves as .npy file.
    
    Args:
        args: Tuple of (vtk_time_dir, output_dir).
        
    Returns:
        Status message.
    """
    vtk_time_dir, output_dir = args
    
    case_name = os.path.basename(os.path.dirname(os.path.dirname(vtk_time_dir)))
    output_path = os.path.join(output_dir, f"{case_name}.npy")
    
    # Skip if already processed
    if os.path.exists(output_path):
        return f"[{case_name}] Skipped (already exists)"
    
    try:
        # Load internal mesh
        internal_path = os.path.join(vtk_time_dir, "internal.vtu")
        if not os.path.exists(internal_path):
            return f"[{case_name}] Skipped: internal.vtu not found"
        
        internal_mesh = pv.read(internal_path)
        
        # Extract internal data
        n_internal = internal_mesh.n_points
        internal_points = internal_mesh.points.astype(np.float32)
        
        if "U" in internal_mesh.point_data:
            internal_vel = internal_mesh.point_data["U"].astype(np.float32)
        else:
            return f"[{case_name}] Skipped: No velocity field"
            
        if "p" in internal_mesh.point_data:
            internal_p = internal_mesh.point_data["p"].astype(np.float32)
        else:
            return f"[{case_name}] Skipped: No pressure field"
            
        if "wall_distance" in internal_mesh.point_data:
            internal_dist = internal_mesh.point_data["wall_distance"].astype(np.float32)
        else:
            return f"[{case_name}] Skipped: No wall_distance field"
        
        # Create type flags for internal (all fluid)
        # Columns: [is_fluid, is_wall, is_inlet, is_outlet]
        internal_flags = np.zeros((n_internal, 4), dtype=np.float32)
        internal_flags[:, 0] = 1  # Fluid
        
        # Combine internal data
        all_points = [internal_points]
        all_vel = [internal_vel]
        all_p = [internal_p]
        all_dist = [internal_dist]
        all_flags = [internal_flags]
        
        # Load boundary meshes
        for boundary_name, flag_idx in [("wall", 1), ("inlet", 2), ("outlet", 3)]:
            boundary_data = load_boundary_mesh(vtk_time_dir, boundary_name)
            
            if boundary_data is not None:
                b_points, b_vel, b_p, b_dist = boundary_data
                n_boundary = len(b_points)
                
                # Create flags for this boundary type
                b_flags = np.zeros((n_boundary, 4), dtype=np.float32)
                b_flags[:, flag_idx] = 1
                
                all_points.append(b_points)
                all_vel.append(b_vel)
                all_p.append(b_p)
                all_dist.append(b_dist)
                all_flags.append(b_flags)
        
        # Concatenate all data
        points = np.vstack(all_points)
        vel = np.vstack(all_vel)
        p = np.concatenate(all_p).reshape(-1, 1)
        dist = np.concatenate(all_dist).reshape(-1, 1)
        flags = np.vstack(all_flags)
        
        # Create final array: (N, 12)
        # Columns: [x, y, z, u, v, w, p, wall_dist, is_fluid, is_wall, is_inlet, is_outlet]
        data = np.hstack([points, vel, p, dist, flags]).astype(np.float32)
        
        # Save
        np.save(output_path, data)
        
        n_total = len(points)
        n_boundary = n_total - n_internal
        return f"[{case_name}] Saved: {n_total} points ({n_internal} fluid, {n_boundary} boundary)"
        
    except Exception as e:
        return f"[{case_name}] Error: {str(e)}"


def main():
    from config import RESULTS_DIR, DATASET_DIR, NUM_CORES
    
    # Create output directory
    os.makedirs(DATASET_DIR, exist_ok=True)
    
    print(f"Searching for VTK exports in '{RESULTS_DIR}'...")
    vtk_dirs = find_case_vtk_dirs(RESULTS_DIR)
    
    if not vtk_dirs:
        print("No VTK directories found. Run export_to_vtk.py first.")
        return
    
    print(f"Found {len(vtk_dirs)} cases. Processing with {NUM_CORES} workers...")
    
    # Prepare arguments
    args_list = [(vtk_dir, DATASET_DIR) for vtk_dir in vtk_dirs]
    
    with multiprocessing.Pool(processes=NUM_CORES) as pool:
        results = list(tqdm(pool.imap(process_single_case, args_list), total=len(args_list)))
    
    # Print summary
    print("\n--- Summary ---")
    saved = sum(1 for r in results if "Saved" in r)
    skipped = sum(1 for r in results if "Skipped" in r)
    errors = sum(1 for r in results if "Error" in r)
    
    print(f"Saved: {saved}, Skipped: {skipped}, Errors: {errors}")
    
    # Print errors if any
    for r in results:
        if "Error" in r:
            print(r)


if __name__ == "__main__":
    main()