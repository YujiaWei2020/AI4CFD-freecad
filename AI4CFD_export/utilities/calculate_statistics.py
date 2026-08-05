"""Module for calculating dataset-wide statistics for normalization.

Key design decisions:
    - Batch Welford: vectorised over all points per file — no per-point Python loop.
    - Velocity std uses fluid-only non-wall points to avoid the near-zero interior
      bias that makes u_std artificially small and inflates normalised MSE for U.
    - All other scalar fields (p_rgh, alpha.water, k, omega) use global stats.

Fields computed are driven by TRAIN_FIELDS / FIELD_CONFIG in config.py.
"""

import os
import glob
import json
import numpy as np
import pyvista as pv
from typing import List, Dict, Any, Optional
from tqdm import tqdm

from config import TRAIN_FIELDS, FIELD_CONFIG, BOUNDARY_CONFIG


# Fields that should use fluid-only (non-wall) points for std calculation.
# Near-wall velocity is clamped to 0 by BCs, which compresses the std and
# causes inlet/high-velocity points to appear as extreme outliers in norm space.
FLUID_ONLY_STD_FIELDS = {"U"}


class _WelfordAccumulator:
    """Numerically stable online mean/variance via batch Welford update.

    Accepts arbitrary-length 1-D numpy arrays per call, so there is no
    per-point Python loop.
    """

    def __init__(self):
        self.count = 0
        self.mean  = 0.0
        self.m2    = 0.0

    def update(self, values: np.ndarray) -> None:
        """Update with a 1-D float64 array of new observations."""
        values = np.asarray(values, dtype=np.float64).ravel()
        n      = len(values)
        if n == 0:
            return

        # Batch Welford — single pass, no Python loop
        new_count = self.count + n
        new_mean  = self.mean + (values.sum() - n * self.mean) / new_count

        # Parallel variance update (Chan et al.)
        delta_pre  = values - self.mean
        delta_post = values - new_mean
        self.m2   += float((delta_pre * delta_post).sum())

        self.count = new_count
        self.mean  = new_mean

    @property
    def std(self) -> float:
        if self.count < 2:
            return 0.0
        return float(np.sqrt(self.m2 / (self.count - 1)))

    @property
    def mean_val(self) -> float:
        return float(self.mean)


class StatisticsCalculator:
    """Calculates dataset-wide normalisation statistics from VTK files.

    Attributes:
        results_dir: Root directory containing case sub-folders.
        output_file: Path to save the statistics JSON.
    """

    def __init__(self, results_dir: str, output_file: Optional[str] = None,
                 case_subdir: str = None) -> None:
        self.results_dir = results_dir
        self.case_subdir = case_subdir
        self.output_file = output_file or os.path.join(
            results_dir, "dataset_statistics.json"
        )

    # ------------------------------------------------------------------
    # File discovery
    # ------------------------------------------------------------------

    def find_final_timestep_files(self) -> List[str]:
        """Finds the internal.vtu for the final VTK timestep of each case."""
        if self.case_subdir:
            pattern = os.path.join(self.results_dir, "*", self.case_subdir, "VTK")
        else:
            pattern = os.path.join(self.results_dir, "*", "VTK")
        vtk_dirs    = glob.glob(pattern)
        final_files = []

        for vtk_dir in sorted(vtk_dirs):
            try:
                subdirs = [
                    d for d in os.listdir(vtk_dir)
                    if os.path.isdir(os.path.join(vtk_dir, d))
                ]
            except OSError:
                continue

            timesteps = []
            for d in subdirs:
                try:
                    ts = float(d.rsplit("_", 1)[-1])
                    timesteps.append((ts, d))
                except (ValueError, IndexError):
                    continue

            if not timesteps:
                continue

            _, last_dir = max(timesteps, key=lambda x: x[0])
            candidate   = os.path.join(vtk_dir, last_dir, "internal.vtu")
            if os.path.isfile(candidate):
                final_files.append(candidate)

        return final_files

    # ------------------------------------------------------------------
    # Wall mask helper
    # ------------------------------------------------------------------

    @staticmethod
    def _wall_patch_points(vtk_time_dir: str) -> Optional[np.ndarray]:
        """Returns wall patch point coords merged across all wall_for_distance patches.

        Used to build a wall mask on the internal mesh via KDTree lookup.
        Returns None if no wall patches found.
        """
        from scipy.spatial import cKDTree

        wall_points = []
        boundary_dir = os.path.join(vtk_time_dir, "boundary")
        for patch_name, cfg in BOUNDARY_CONFIG.items():
            if not cfg["wall_for_distance"]:
                continue
            path = os.path.join(boundary_dir, f"{patch_name}.vtp")
            if os.path.exists(path):
                m = pv.read(path)
                if m.n_points > 0:
                    wall_points.append(m.points)

        return np.vstack(wall_points) if wall_points else None

    # ------------------------------------------------------------------
    # Core calculation
    # ------------------------------------------------------------------

    def calculate(self, file_list: List[str]) -> Dict[str, Any]:
        """Calculates dataset-wide statistics using batch Welford.

        For fields in FLUID_ONLY_STD_FIELDS (i.e. U), two accumulators are
        maintained:
            - global mean  (all points)
            - fluid std    (non-wall points only — avoids near-zero BC bias)

        All other fields use a single global accumulator.

        Args:
            file_list: Paths to internal.vtu files (one per case).

        Returns:
            Statistics dict compatible with DatasetSamplingAllWalls.
        """
        # --- Build accumulators ---------------------------------------
        # mean_key → WelfordAccumulator (global, for mean)
        global_acc: Dict[str, _WelfordAccumulator] = {}
        # mean_key → WelfordAccumulator (fluid-only, for std of velocity fields)
        fluid_acc:  Dict[str, _WelfordAccumulator] = {}

        for field in TRAIN_FIELDS:
            for key in FIELD_CONFIG[field]["stats_keys"]:
                if key.endswith("_mean"):
                    global_acc[key] = _WelfordAccumulator()
                    if field in FLUID_ONLY_STD_FIELDS:
                        fluid_acc[key] = _WelfordAccumulator()

        wall_dist_min  = float("inf")
        wall_dist_max  = float("-inf")
        coords_abs_max = float("-inf")
        n_total        = 0
        n_files        = 0

        required_vtk = ["wall_distance"] + TRAIN_FIELDS

        print(f"Calculating statistics for fields : {TRAIN_FIELDS}")
        print(f"Fluid-only std fields             : {sorted(FLUID_ONLY_STD_FIELDS)}")

        for file_path in tqdm(file_list, desc="Processing files"):
            try:
                mesh = pv.read(file_path)

                missing = [f for f in required_vtk if f not in mesh.point_data]
                if missing:
                    print(f"  Skipping {file_path} — missing: {missing}")
                    continue

                # --- Coords / wall dist ---
                coords    = np.asarray(mesh.points,                dtype=np.float64)
                wall_dist = np.asarray(mesh.point_data["wall_distance"], dtype=np.float64)

                coords_abs_max = max(coords_abs_max, float(np.abs(coords).max()))
                wall_dist_min  = min(wall_dist_min,  float(wall_dist.min()))
                wall_dist_max  = max(wall_dist_max,  float(wall_dist.max()))

                n_pts   = coords.shape[0]
                n_total += n_pts

                # --- Fluid mask (non-wall points for velocity std) ---
                # Wall points have wall_distance ≈ 0; use a small threshold.
                # This avoids loading boundary VTP files for every case.
                fluid_mask = wall_dist > 1e-6   # True = not on a wall surface

                # --- Accumulate per field ---
                for field in TRAIN_FIELDS:
                    arr  = np.asarray(mesh.point_data[field], dtype=np.float64)
                    keys = FIELD_CONFIG[field]["stats_keys"]

                    if arr.ndim == 1:
                        mean_keys = [k for k in keys if k.endswith("_mean")]
                        channels  = [arr]
                    else:
                        mean_keys = [k for k in keys if k.endswith("_mean")]
                        channels  = [arr[:, i] for i in range(arr.shape[1])]

                    for mean_key, channel in zip(mean_keys, channels):
                        # Global accumulator — mean (and std for non-velocity fields)
                        global_acc[mean_key].update(channel)

                        # Fluid-only accumulator — std for velocity fields
                        if mean_key in fluid_acc:
                            fluid_acc[mean_key].update(channel[fluid_mask])

                n_files += 1

            except Exception as e:
                print(f"  Error processing {file_path}: {e}")
                continue

        if n_total < 2:
            raise ValueError("Not enough data points to calculate statistics.")

        # --- Assemble output ------------------------------------------
        stats: Dict[str, Any] = {}

        for field in TRAIN_FIELDS:
            for key in FIELD_CONFIG[field]["stats_keys"]:
                if not key.endswith("_mean"):
                    continue
                std_key = key.replace("_mean", "_std")
                g_acc   = global_acc[key]

                stats[key] = g_acc.mean_val  # always use global mean

                if key in fluid_acc and fluid_acc[key].count >= 2:
                    # Use fluid-only std to avoid near-zero BC compression
                    stats[std_key] = fluid_acc[key].std
                    print(
                        f"  {key:<25} mean={g_acc.mean_val:.6f}  "
                        f"std(fluid-only)={fluid_acc[key].std:.6f}  "
                        f"[was global std={g_acc.std:.6f}]"
                    )
                else:
                    stats[std_key] = g_acc.std

        stats.update({
            "wall_dist_min": float(wall_dist_min),
            "wall_dist_max": float(wall_dist_max),
            "coords_max":    float(coords_abs_max),
            "n_points":      n_total,
            "n_files":       n_files,
            "train_fields":  TRAIN_FIELDS,
        })

        return stats

    # ------------------------------------------------------------------
    # Save / print
    # ------------------------------------------------------------------

    def save(self, stats: Dict[str, Any]) -> None:
        """Saves statistics to JSON."""
        with open(self.output_file, "w") as f:
            json.dump(stats, f, indent=2)
        print(f"\nStatistics saved to: {self.output_file}")

    def print_summary(self, stats: Dict[str, Any]) -> None:
        """Prints a human-readable summary."""
        print(f"\n--- Summary ---")
        print(f"Total points : {stats['n_points']:,}")
        print(f"Cases        : {stats['n_files']}")
        print(f"Fields       : {stats.get('train_fields', '?')}")
        print()
        for field in TRAIN_FIELDS:
            for key in FIELD_CONFIG[field]["stats_keys"]:
                if key.endswith("_mean"):
                    std_key = key.replace("_mean", "_std")
                    fluid   = "* " if field in FLUID_ONLY_STD_FIELDS else "  "
                    print(
                        f"{fluid}{key:<25} "
                        f"mean={stats[key]:.6f},  std={stats[std_key]:.6f}"
                    )
        print(f"\n  wall_dist   min={stats['wall_dist_min']:.6f},"
              f"  max={stats['wall_dist_max']:.6f}")
        print(f"  coords_max  {stats['coords_max']:.6f}")
        print(f"\n  (* std computed from fluid-only non-wall points)")

    def run(self) -> Dict[str, Any]:
        """Full pipeline: find files → calculate → save → print summary."""
        print(f"Searching for VTK files in '{self.results_dir}'...")
        file_list = self.find_final_timestep_files()

        if not file_list:
            print("No VTK files found.")
            return {}

        print(f"Found {len(file_list)} cases.\n")
        stats = self.calculate(file_list)
        self.save(stats)
        self.print_summary(stats)
        return stats


def main():
    from config import RESULTS_DIR
    calculator = StatisticsCalculator(RESULTS_DIR)
    calculator.run()


if __name__ == "__main__":
    main()