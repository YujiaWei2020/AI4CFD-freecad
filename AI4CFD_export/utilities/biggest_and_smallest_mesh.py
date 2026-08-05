"""Utility script to analyze mesh sizes across simulation results.

This script parses meshing log files from OpenFOAM simulations and identifies
the cases with the largest and smallest cell counts. Useful for understanding
mesh variation across a batch of simulations.

Usage:
    python biggest_and_smallest_mesh.py
"""

import glob
import re


def parse_meshes() -> None:
    """Analyzes meshing logs to find cases with largest and smallest cell counts.
    
    Scans all meshing.log files in the results directory, extracts the final
    cell count from each, and reports the extremes. Uses the last occurrence
    of "Layer mesh : cells:NNNN" pattern as the final mesh size.
    """
    from config import RESULTS_DIR
    log_pattern = f"{RESULTS_DIR}/*/meshing.log"
    files = glob.glob(log_pattern)
    
    if not files:
        print("No meshing logs found in results/*/meshing.log")
        return

    max_cells = -1
    max_file = ""
    
    min_cells = float('inf')
    min_file = ""
    
    # Regex to capture the cell count
    # Line format: "Layer mesh : cells:54122  faces:168176  points:60841"
    regex = re.compile(r"Layer mesh\s*:\s*cells:(\d+)")
    
    count = 0
    for log_file in files:
        try:
            with open(log_file, 'r') as f:
                content = f.read()
                
            matches = regex.findall(content)
            if matches:
                # Take the last occurrence as the final mesh
                cells = int(matches[-1])
                
                if cells > max_cells:
                    max_cells = cells
                    max_file = log_file
                    
                if cells < min_cells:
                    min_cells = cells
                    min_file = log_file
                
                count += 1
        except Exception as e:
            print(f"Error reading {log_file}: {e}")

    if count == 0:
        print("No valid mesh counts found in logs.")
    else:
        print("--- Mesh Analysis Results ---")
        print(f"analyzed {count} logs.")
        print("-" * 30)
        print(f"Smallest Mesh:")
        print(f"  File: {min_file}")
        print(f"  Cells: {min_cells}")
        print("-" * 30)
        print(f"Biggest Mesh:")
        print(f"  File: {max_file}")
        print(f"  Cells: {max_cells}")

if __name__ == "__main__":
    parse_meshes()
