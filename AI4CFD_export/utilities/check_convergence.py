"""Script to check simulation convergence and quarantine failed cases.

This script scans the 'results' directory for OpenFOAM cases. It determines if a case
has reached the maximum number of iterations (EndTime) as specified in its controlDict.
If so, the case is moved to a 'quarantine' directory.
"""

import os
import shutil
import re

from config import RESULTS_DIR, QUARANTINE_DIR

def get_end_time(control_dict_path):
    """Parses the endTime from an OpenFOAM controlDict."""
    try:
        with open(control_dict_path, 'r') as f:
            content = f.read()
            match = re.search(r'endTime\s+(\d+);', content)
            if match:
                return int(match.group(1))
    except Exception as e:
        print(f"Error reading {control_dict_path}: {e}")
    return None

def get_latest_time(case_path):
    """Finds the largest numeric directory name in the case path."""
    try:
        subdirs = [d for d in os.listdir(case_path) if os.path.isdir(os.path.join(case_path, d))]
        numeric_subdirs = []
        for d in subdirs:
            try:
                # Handle standard integer time steps and potential float times if needed
                # For now simpleFoam usually writes integers. simpleFoam might write 1000
                numeric_subdirs.append(float(d)) 
            except ValueError:
                continue
        
        if numeric_subdirs:
            return max(numeric_subdirs)
    except Exception as e:
        print(f"Error scanning {case_path}: {e}")
    return 0

def check_and_quarantine(results_dir: str = None, quarantine_dir: str = None,
                          case_subdir: str = None) -> None:
    """Scan results_dir for non-converged cases and move them to quarantine.

    results_dir:   directory containing the case sub-folders (default: config.RESULTS_DIR)
    quarantine_dir: destination for failed cases (default: config.QUARANTINE_DIR)
    case_subdir:   if set (e.g. 'case'), the actual OF case lives at
                   results_dir/<entry>/<case_subdir>/ (parametric-study layout)
    """
    results_dir   = results_dir   or RESULTS_DIR
    quarantine_dir = quarantine_dir or QUARANTINE_DIR

    if not os.path.exists(results_dir):
        print(f"Directory '{results_dir}' not found.")
        return

    os.makedirs(quarantine_dir, exist_ok=True)

    top_entries = [d for d in os.listdir(results_dir)
                   if os.path.isdir(os.path.join(results_dir, d))]

    print(f"Scanning {len(top_entries)} entries in '{results_dir}'...")
    quarantined_count = 0

    for entry in top_entries:
        entry_path = os.path.join(results_dir, entry)
        # Resolve the actual OF case directory
        if case_subdir:
            case_path = os.path.join(entry_path, case_subdir)
        else:
            case_path = entry_path

        control_dict_path = os.path.join(case_path, "system", "controlDict")
        if not os.path.exists(control_dict_path):
            print(f"Skipping {entry}: controlDict not found.")
            continue

        end_time = get_end_time(control_dict_path)
        if end_time is None:
            print(f"Skipping {entry}: Could not parse endTime.")
            continue

        latest_time = get_latest_time(case_path)

        if latest_time >= end_time:
            print(f"Case {entry} reached max iterations "
                  f"({latest_time} >= {end_time}). Moving to quarantine.")
            dest_path = os.path.join(quarantine_dir, entry)
            try:
                shutil.move(entry_path, dest_path)
                quarantined_count += 1
            except Exception as e:
                print(f"Error moving {entry}: {e}")

    print(f"Done. Quarantined {quarantined_count} cases.")

if __name__ == "__main__":
    check_and_quarantine()
