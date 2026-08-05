"""Module for calculating wall distance for OpenFOAM VTK files using KDTree.

Boundary patch roles are driven entirely by BOUNDARY_CONFIG in config.py.
No edits to this file are needed when adding new geometries or patch names.
"""

import os
import glob
import multiprocessing
import numpy as np
import pyvista as pv
from scipy.spatial import cKDTree
from typing import List

from config import BOUNDARY_CONFIG


class WallDistanceCalculator:
    """Calculates wall distance for OpenFOAM VTK exports using KDTree.

    Wall distance is computed from all patches marked wall_for_distance=True
    in BOUNDARY_CONFIG (e.g. both 'wall' and 'open' for the conical tank).
    Results are written back into the original .vtu / .vtp files in-place.

    Attributes:
        case_dir:  Path to the OpenFOAM case directory.
        case_name: Basename of the case directory.
    """

    def __init__(self, case_dir: str) -> None:
        self.case_dir  = case_dir
        self.case_name = os.path.basename(case_dir)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def find_time_dirs(self) -> List[str]:
        """Finds all VTK time directories for this case, sorted."""
        vtk_dir = os.path.join(self.case_dir, "VTK")
        if not os.path.exists(vtk_dir):
            return []
        return sorted(
            d for d in glob.glob(os.path.join(vtk_dir, "*"))
            if os.path.isdir(d)
        )

    # ------------------------------------------------------------------
    # Core processing
    # ------------------------------------------------------------------

    def process_time_dir(self, time_dir: str) -> bool:
        """Calculates and writes wall_distance for one VTK time directory.

        Patches with wall_for_distance=True are merged into the KDTree.
        Their own wall_distance is set to 0. All other patches (inlet,
        outlet, etc.) get their true nearest-wall distance.

        Returns:
            True if files were updated, False if skipped.
        """
        internal_path = os.path.join(time_dir, "internal.vtu")
        boundary_dir  = os.path.join(time_dir, "boundary")

        if not os.path.exists(internal_path):
            return False

        # --- Build KDTree from all wall-type patches --------------------
        wall_points = []
        for patch_name, cfg in BOUNDARY_CONFIG.items():
            if not cfg["wall_for_distance"]:
                continue
            path = os.path.join(boundary_dir, f"{patch_name}.vtp")
            if not os.path.exists(path):
                continue
            mesh = pv.read(path)
            if mesh.n_points > 0:
                wall_points.append(mesh.points)

        if not wall_points:
            return False  # no wall geometry found — skip

        tree = cKDTree(np.vstack(wall_points))

        # --- Internal (fluid) mesh --------------------------------------
        internal_mesh = pv.read(internal_path)
        dist, _ = tree.query(internal_mesh.points, k=1)
        internal_mesh.point_data["wall_distance"] = dist.astype(np.float32)
        internal_mesh.save(internal_path)

        # --- Boundary patches -------------------------------------------
        for patch_name, cfg in BOUNDARY_CONFIG.items():
            path = os.path.join(boundary_dir, f"{patch_name}.vtp")
            if not os.path.exists(path):
                continue

            mesh = pv.read(path)
            if mesh.n_points == 0:
                continue

            if cfg["wall_for_distance"]:
                # Wall patches are distance 0 from themselves
                wall_dist = np.zeros(mesh.n_points, dtype=np.float32)
            else:
                wall_dist, _ = tree.query(mesh.points, k=1)
                wall_dist = wall_dist.astype(np.float32)

            mesh.point_data["wall_distance"] = wall_dist
            mesh.save(path)

        return True

    def process(self) -> str:
        """Calculates wall distance for all time directories in this case.

        Returns:
            Human-readable status string.
        """
        try:
            time_dirs = self.find_time_dirs()
            if not time_dirs:
                return f"[{self.case_name}] skipped: No VTK time directories found"

            updated = 0
            skipped = 0
            for time_dir in time_dirs:
                if self.process_time_dir(time_dir):
                    updated += 1
                else:
                    skipped += 1

            return f"[{self.case_name}] Updated {updated} time dirs, skipped {skipped}."

        except Exception as e:
            return f"[{self.case_name}] Failed: {str(e)}"

    # ------------------------------------------------------------------
    # Static / class helpers for batch use
    # ------------------------------------------------------------------

    @staticmethod
    def process_single(case_dir: str) -> str:
        """Multiprocessing entry point — never raises; returns a status string."""
        try:
            calculator = WallDistanceCalculator(case_dir)
            return calculator.process()
        except Exception as exc:
            return f"[{os.path.basename(case_dir)}] Failed: {exc}"

    @staticmethod
    def find_cases(results_dir: str = "results",
                   case_subdir: str = None) -> List[str]:
        """Finds all OpenFOAM case directories inside results_dir.

        case_subdir: if set (e.g. 'case'), the actual OF case lives at
                     results_dir/<entry>/<case_subdir>/ (parametric-study layout).
        """
        if case_subdir:
            pattern = os.path.join(results_dir, "*", case_subdir)
        else:
            pattern = os.path.join(results_dir, "*")
        return [d for d in glob.glob(pattern) if os.path.isdir(d)]

    @classmethod
    def batch_process(cls, results_dir: str, num_cores: int,
                      case_subdir: str = None) -> None:
        """Calculates wall distance for all cases in parallel.

        Args:
            results_dir: Root directory containing case sub-folders.
            num_cores:   Number of worker processes.
            case_subdir: nested subdirectory name for parametric-study layout.
        """
        print(f"Searching for cases in '{results_dir}'...")
        case_dirs = cls.find_cases(results_dir, case_subdir)

        if not case_dirs:
            print("No cases found.")
            return

        print(f"Found {len(case_dirs)} cases. "
              f"Calculating wall distance on {num_cores} cores...")
        print(f"Wall patches (distance=0): "
              f"{[p for p, c in BOUNDARY_CONFIG.items() if c['wall_for_distance']]}")

        if num_cores == 1:
            results = [cls.process_single(d) for d in case_dirs]
        else:
            with multiprocessing.Pool(processes=num_cores) as pool:
                results = list(pool.imap_unordered(cls.process_single, case_dirs))

        updated = sum(1 for r in results if "Updated" in r)
        failed  = sum(1 for r in results if "Failed"  in r)
        skipped = sum(1 for r in results if "skipped" in r)

        print(f"\n--- Summary ---")
        print(f"Updated: {updated}, Skipped: {skipped}, Failed: {failed}")
        for r in results:
            if "Failed" in r or "skipped" in r:
                print(r)

        print("\nBatch processing finished.")


def main():
    from config import RESULTS_DIR, NUM_CORES
    WallDistanceCalculator.batch_process(RESULTS_DIR, NUM_CORES)


if __name__ == "__main__":
    main()