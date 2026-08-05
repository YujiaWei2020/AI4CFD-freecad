"""Module for exporting OpenFOAM results to VTK format in parallel."""

import os
import subprocess
import multiprocessing
import glob
from typing import List
from utilities.run_case import SimulationRunner

class VTKExporter:
    """Handles the export of OpenFOAM data to VTK format."""

    def __init__(self, case_dir: str = "case") -> None:
        self.case_dir = case_dir

    def run_command(self, command: str, timeout: int = None) -> None:
        """Delegates command execution to the SimulationRunner."""
        if not hasattr(self, 'runner'):
            self.runner = SimulationRunner(self.case_dir)
        self.runner.run_command(command, timeout)

    def export(self) -> None:
        """Runs foamToVTK on the latest time step only (steady-state export)."""
        self.run_command("foamToVTK -latestTime")

    def is_valid_case(self) -> bool:
        """Checks if the case directory is a valid OpenFOAM case."""
        return (
            os.path.isdir(self.case_dir)
            and os.path.exists(os.path.join(self.case_dir, "system", "controlDict"))
        )

    def process(self) -> str:
        """Validates and exports the case.  Returns a human-readable status."""
        name = os.path.basename(self.case_dir)
        if not self.is_valid_case():
            return f"[{name}] Skipped: not a valid OpenFOAM case"
        try:
            self.export()
            return f"[{name}] OK"
        except Exception as exc:
            return f"[{name}] FAILED: {exc}"

    @staticmethod
    def find_cases(results_dir: str = "results",
                   case_subdir: str = None) -> List[str]:
        """Finds all OpenFOAM case directories.

        case_subdir: if set (e.g. 'case'), the actual OF case lives at
                     results_dir/<entry>/<case_subdir>/ (parametric-study layout).
        """
        if case_subdir:
            pattern = os.path.join(results_dir, "*", case_subdir)
        else:
            pattern = os.path.join(results_dir, "*")
        return [d for d in glob.glob(pattern) if os.path.isdir(d)]

    @staticmethod
    def process_single(case_dir: str) -> str:
        """Multiprocessing entry point — never raises; returns a status string."""
        try:
            exporter = VTKExporter(case_dir)
            return exporter.process()
        except Exception as exc:
            return f"[{os.path.basename(case_dir)}] FAILED: {exc}"

    @classmethod
    def batch_export(cls, results_dir: str, num_cores: int,
                     case_subdir: str = None) -> None:
        """Finds all cases and exports them in parallel.

        One failed case never stops the rest.

        Args:
            results_dir: Path to the results directory.
            num_cores: Number of parallel processes.
            case_subdir: nested subdirectory name for parametric-study layout.
        """
        print(f"Searching for cases in '{results_dir}'...")
        case_dirs = cls.find_cases(results_dir, case_subdir)

        if not case_dirs:
            print("No cases found.")
            return

        print(f"Found {len(case_dirs)} cases. "
              f"Starting parallel export on {num_cores} cores...")

        ok = failed = skipped = 0
        if num_cores == 1:
            for cd in case_dirs:
                msg = cls.process_single(cd)
                print(msg, flush=True)
                if "OK"      in msg: ok      += 1
                elif "SKIP"  in msg: skipped += 1
                else:                failed  += 1
        else:
            with multiprocessing.Pool(processes=num_cores) as pool:
                for msg in pool.imap_unordered(cls.process_single, case_dirs):
                    print(msg, flush=True)
                    if "OK"      in msg: ok      += 1
                    elif "SKIP"  in msg: skipped += 1
                    else:                failed  += 1

        print(f"\nBatch export finished — "
              f"{ok} OK, {skipped} skipped, {failed} failed.")


def main():
    from config import RESULTS_DIR, NUM_CORES
    VTKExporter.batch_export(RESULTS_DIR, NUM_CORES)


if __name__ == "__main__":
    main()