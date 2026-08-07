"""Module for executing OpenFOAM simulation commands.

This module defines the SimulationRunner class, which handles the execution of
OpenFOAM solvers (e.g., simpleFoam) within a specified case directory.
"""

import os
import subprocess
import time


class SimulationRunner:
    """Handles the execution of OpenFOAM simulations.

    Attributes:
        case_dir: Directory path for the OpenFOAM case.
    """

    def __init__(self, case_dir: str = "case") -> None:
        self.case_dir = case_dir


    def run_command(self, command: str, timeout: int = None) -> None:
        """Executes a shell command in the case directory.

        Args:
            command: The command string to execute.
            timeout: Maximum execution time in seconds.

        Raises:
            subprocess.CalledProcessError: If the command fails.
            subprocess.TimeoutExpired: If the command times out.
        """
        log_file = os.path.join(self.case_dir, "simulation.log")
        full_command = f'bash -c "source /usr/lib/openfoam/openfoam2212/etc/bashrc && {command}"'
        
        with open(log_file, "a") as f:
            f.write(f"\n[{time.ctime()}] Running: {command}\n")
            f.flush()
            
            # Using subprocess.run with stdout/stderr redirection to file
            # raising exception on error/timeout instead of exit(1)
            subprocess.run(
                full_command, 
                check=True, 
                shell=True, 
                cwd=self.case_dir,
                stdout=f,
                stderr=subprocess.STDOUT,
                timeout=timeout
            )

    def run_simulation(self) -> None:
        """Runs the simpleFoam solver."""
        self.run_command("simpleFoam")


def main():
    runner = SimulationRunner()
    runner.run_simulation()


if __name__ == "__main__":
    main()
