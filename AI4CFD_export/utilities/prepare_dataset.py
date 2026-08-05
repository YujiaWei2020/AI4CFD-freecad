"""Module for preparing ML-ready dataset from OpenFOAM VTK exports.

Column layout (driven by TRAIN_FIELDS / FIELD_CONFIG / BOUNDARY_CONFIG in config.py):

    [x, y, z,  <field channels>,  wall_dist,  is_fluid, is_wall, is_inlet, is_outlet, is_target_patch]

Default (U + alpha.water):
    [x, y, z,  u, v, w,  alpha,  wall_dist,  flags(4),  is_target_patch(1)]  → (N, 13)

is_target_patch:
    1.0 for points whose OpenFOAM patch name matches TARGET_RESULT_NAME
    (e.g. "airfoil"), 0.0 for everything else (fluid, other walls, inlet,
    outlet).  Set to "" or None in config to disable named-patch filtering.

Boundary patch roles (one-hot flags) are driven by BOUNDARY_CONFIG, so no
edits to this file are needed when adding new geometries or patch names.
"""

import os
import glob
import multiprocessing
import numpy as np
import pyvista as pv
from typing import List, Optional, Tuple, Dict
from tqdm import tqdm

from config import TRAIN_FIELDS, FIELD_CONFIG, BOUNDARY_CONFIG, TARGET_RESULT_NAME, TARGET_RESULT_FIELD


class DatasetPreparer:
    """Prepares ML-ready .npy files from OpenFOAM VTK exports.

    Merges internal.vtu + all boundary .vtp files per case, attaches
    one-hot type flags and an is_target_patch flag, and saves as .npy.
    Column layout and boundary roles are both driven by config.py.

    Attributes:
        results_dir: Root directory containing case sub-folders.
        output_dir:  Directory to save .npy files.
    """

    def __init__(self, results_dir: str, output_dir: str,
                 case_subdir: str = None) -> None:
        self.results_dir = results_dir
        self.output_dir  = output_dir
        self.case_subdir = case_subdir

        # wall_distance is always required in addition to TRAIN_FIELDS
        self.required_fields = TRAIN_FIELDS + ["wall_distance"]

        # Total field channels, e.g. U=3 + alpha.water=1 → 4
        self.n_field_channels = sum(
            FIELD_CONFIG[f]["channels"] for f in TRAIN_FIELDS
        )

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def find_vtk_dirs(self) -> List[str]:
        """Finds the latest VTK time directory per case."""
        if self.case_subdir:
            pattern = os.path.join(self.results_dir, "*", self.case_subdir, "VTK", "*")
        else:
            pattern = os.path.join(self.results_dir, "*", "VTK", "*")
        dirs = [d for d in glob.glob(pattern) if os.path.isdir(d)]

        case_vtk_map: Dict[str, str] = {}
        for d in dirs:
            case_dir = os.path.dirname(os.path.dirname(d))
            if case_dir not in case_vtk_map or d > case_vtk_map[case_dir]:
                case_vtk_map[case_dir] = d

        return list(case_vtk_map.values())

    # ------------------------------------------------------------------
    # Field extraction helpers
    # ------------------------------------------------------------------

    def _extract_fields(self, mesh: pv.DataSet) -> np.ndarray:
        """Extracts TRAIN_FIELDS from a mesh into a (N, n_field_channels) array.

        Missing fields default to zeros so boundary meshes that lack some
        fields still produce the correct shape.

        Args:
            mesh: PyVista dataset.

        Returns:
            (N, n_field_channels) float32 array in TRAIN_FIELDS order.
        """
        n      = mesh.n_points
        arrays = []

        for field in TRAIN_FIELDS:
            channels = FIELD_CONFIG[field]["channels"]
            if field in mesh.point_data:
                arr = mesh.point_data[field].astype(np.float32)
                if arr.ndim == 1:
                    arr = arr.reshape(-1, 1)
            else:
                arr = np.zeros((n, channels), dtype=np.float32)
            arrays.append(arr)

        return np.hstack(arrays)  # (N, n_field_channels)

    def _extract_mesh_data(
        self, mesh: pv.DataSet
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Extracts points, fields, and wall distance from a mesh.

        Returns:
            points    : (N, 3)  float32
            fields    : (N, n_field_channels)  float32
            wall_dist : (N, 1)  float32
        """
        n         = mesh.n_points
        points    = mesh.points.astype(np.float32)
        fields    = self._extract_fields(mesh)
        wall_dist = (
            mesh.point_data["wall_distance"].astype(np.float32).reshape(-1, 1)
            if "wall_distance" in mesh.point_data
            else np.zeros((n, 1), dtype=np.float32)
        )
        return points, fields, wall_dist

    def _load_boundary(
        self, vtk_time_dir: str, patch_name: str
    ) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
        """Loads a boundary .vtp file and extracts data.

        Args:
            vtk_time_dir: Path to the VTK time directory.
            patch_name:   Boundary patch name (e.g. 'wall', 'inlet').

        Returns:
            (points, fields, wall_dist) tuple, or None if file is absent.
        """
        path = os.path.join(vtk_time_dir, "boundary", f"{patch_name}.vtp")
        if not os.path.exists(path):
            return None
        try:
            mesh = pv.read(path)
            if mesh.n_points == 0:
                return None
            return self._extract_mesh_data(mesh)
        except Exception as e:
            print(f"  Warning: could not load {path}: {e}")
            return None

    # ------------------------------------------------------------------
    # Case processing
    # ------------------------------------------------------------------

    def _case_name_from_vtk_dir(self, vtk_time_dir: str) -> str:
        """Derive a unique case name from the VTK time directory.

        Flat layout:        results/case_0001/VTK/time  → 'case_0001'
        Parametric layout:  study/case_0001/case/VTK/time → 'case_0001'
        """
        of_case_dir = os.path.dirname(os.path.dirname(vtk_time_dir))  # …/case or …/case_0001
        name = os.path.basename(of_case_dir)
        if self.case_subdir and name == self.case_subdir:
            # Step up one more level past the 'case/' subdir
            name = os.path.basename(os.path.dirname(of_case_dir))
        return name

    def process_case(self, vtk_time_dir: str) -> str:
        """Processes one case and saves it as a .npy file.

        Output shape: (N, 3 + n_field_channels + 1 + 4 + 1)
                       coords + fields + wall_dist + flags(4) + is_target_patch(1)

        One-hot flag columns : [is_fluid, is_wall, is_inlet, is_outlet]
        is_target_patch      : 1.0 where patch_name == TARGET_RESULT_NAME

        Args:
            vtk_time_dir: Path to the VTK time directory.

        Returns:
            Human-readable status string.
        """
        case_name   = self._case_name_from_vtk_dir(vtk_time_dir)
        output_path = os.path.join(self.output_dir, f"{case_name}.npy")

        if os.path.exists(output_path):
            return f"[{case_name}] Skipped (already exists)"

        try:
            internal_path = os.path.join(vtk_time_dir, "internal.vtu")
            if not os.path.exists(internal_path):
                return f"[{case_name}] Skipped: internal.vtu not found"

            internal_mesh = pv.read(internal_path)

            missing = [
                f for f in self.required_fields
                if f not in internal_mesh.point_data
            ]
            if missing:
                return f"[{case_name}] Skipped: Missing fields {missing}"

            # --- Internal (fluid) points --------------------------------
            pts, flds, dist = self._extract_mesh_data(internal_mesh)
            n_internal = len(pts)

            # Flag column 0 = is_fluid; is_target_patch = 0 for fluid
            internal_flags          = np.zeros((n_internal, 4), dtype=np.float32)
            internal_flags[:, 0]    = 1
            internal_target         = np.zeros((n_internal, 1), dtype=np.float32)

            all_pts    = [pts]
            all_flds   = [flds]
            all_dist   = [dist]
            all_flags  = [internal_flags]
            all_target = [internal_target]

            # --- Boundary patches (roles from BOUNDARY_CONFIG) ----------
            for patch_name, cfg in BOUNDARY_CONFIG.items():
                b_data = self._load_boundary(vtk_time_dir, patch_name)
                if b_data is None:
                    continue
                b_pts, b_flds, b_dist = b_data
                n_b = len(b_pts)

                b_flags                 = np.zeros((n_b, 4), dtype=np.float32)
                b_flags[:, cfg["flag"]] = 1

                # is_target_patch: 1 if this patch is the named target.
                # Only meaningful in Boundary mode; zeroed out for Full-Field.
                b_target = np.ones((n_b, 1), dtype=np.float32) \
                           if (TARGET_RESULT_FIELD == "Boundary"
                               and TARGET_RESULT_NAME
                               and patch_name == TARGET_RESULT_NAME) \
                           else np.zeros((n_b, 1), dtype=np.float32)

                all_pts.append(b_pts)
                all_flds.append(b_flds)
                all_dist.append(b_dist)
                all_flags.append(b_flags)
                all_target.append(b_target)

            # --- Assemble final array -----------------------------------
            # Shape: (N, 3 + n_field_channels + 1 + 4 + 1)
            points = np.vstack(all_pts)
            fields = np.vstack(all_flds)
            dists  = np.vstack(all_dist)
            flags  = np.vstack(all_flags)
            target = np.vstack(all_target)

            result = np.hstack([points, fields, dists, flags, target]).astype(np.float32)
            np.save(output_path, result)

            n_total      = len(points)
            n_cols       = result.shape[1]
            n_target_pts = int(target.sum())
            return (
                f"[{case_name}] Saved: {n_total} points "
                f"({n_internal} fluid, {n_total - n_internal} boundary, "
                f"{n_target_pts} on '{TARGET_RESULT_NAME}') "
                f"— {n_cols} columns {TRAIN_FIELDS}"
            )

        except Exception as e:
            return f"[{case_name}] Error: {str(e)}"

    # ------------------------------------------------------------------
    # Batch / inference entry points
    # ------------------------------------------------------------------

    @staticmethod
    def _process_single(args: Tuple) -> str:
        """Multiprocessing wrapper."""
        vtk_time_dir, results_dir, output_dir, case_subdir = args
        preparer = DatasetPreparer(results_dir, output_dir, case_subdir=case_subdir)
        return preparer.process_case(vtk_time_dir)

    def inference_process_single(self) -> str:
        """Processes a single inference case (latest VTK time dir).

        Returns:
            Human-readable status string.
        """
        os.makedirs(self.output_dir, exist_ok=True)
        vtk_base  = os.path.join(self.output_dir, "VTK")
        time_dirs = sorted(
            d for d in glob.glob(os.path.join(vtk_base, "*"))
            if os.path.isdir(d)
        )
        if not time_dirs:
            raise FileNotFoundError(
                f"No VTK time directories found in {vtk_base}"
            )
        result = self.process_case(time_dirs[-1])
        print(result)

        # Copy as the fixed filename expected by inference_pointnet_geo.py.
        # The file must land in INFERENCE_DIR (= inference_output/) not in
        # inference_output/case/, so also copy one level up for split layout.
        import shutil
        case_name = self._case_name_from_vtk_dir(time_dirs[-1])
        src = os.path.join(self.output_dir, f"{case_name}.npy")
        if os.path.isfile(src):
            for dst_dir in {self.output_dir, os.path.dirname(self.output_dir)}:
                dst = os.path.join(dst_dir, "inference_output.npy")
                if dst != src:
                    shutil.copy2(src, dst)
                    print(f"[dataset] Copied → {dst}", flush=True)

        return result

    def clean_output_dir(self) -> None:
        """Deletes all .npy files in the output directory."""
        npy_files = glob.glob(os.path.join(self.output_dir, "*.npy"))
        if not npy_files:
            print("Output directory is already clean.")
            return
        print(f"Cleaning {len(npy_files)} .npy file(s) from '{self.output_dir}'...")
        for f in npy_files:
            os.remove(f)
        print("Done cleaning.")

    def batch_process(self, num_cores: int, clean: bool = True) -> None:
        """Finds all cases and prepares datasets in parallel.

        Args:
            num_cores: Number of worker processes.
            clean:     If True, deletes existing .npy files before processing.
        """
        os.makedirs(self.output_dir, exist_ok=True)

        if clean:
            self.clean_output_dir()

        print(f"Searching for VTK exports in '{self.results_dir}'...")
        print(f"Fields         : {TRAIN_FIELDS} ({self.n_field_channels} channels)")
        print(f"Boundary roles : { {p: c['flag'] for p, c in BOUNDARY_CONFIG.items()} }")
        print(f"Target patch   : '{TARGET_RESULT_NAME}' (is_target_patch column)")

        vtk_dirs = self.find_vtk_dirs()
        if not vtk_dirs:
            print("No VTK directories found. Run export_to_vtk.py first.")
            return

        print(f"Found {len(vtk_dirs)} cases. Processing with {num_cores} workers...")
        args_list = [(d, self.results_dir, self.output_dir, self.case_subdir)
                     for d in vtk_dirs]

        if num_cores == 1:
            results = [self._process_single(a) for a in tqdm(args_list)]
        else:
            with multiprocessing.Pool(processes=num_cores) as pool:
                results = list(tqdm(
                    pool.imap(self._process_single, args_list),
                    total=len(args_list),
                ))

        saved   = sum(1 for r in results if "Saved"   in r)
        skipped = sum(1 for r in results if "Skipped" in r)
        errors  = sum(1 for r in results if "Error"   in r)

        print(f"\n--- Summary ---")
        print(f"Saved: {saved}, Skipped: {skipped}, Errors: {errors}")
        for r in results:
            if "Error" in r or "Skipped" in r:
                print(r)


def main():
    from config import RESULTS_DIR, DATASET_DIR, NUM_CORES
    preparer = DatasetPreparer(RESULTS_DIR, DATASET_DIR)
    preparer.batch_process(NUM_CORES)


if __name__ == "__main__":
    main()