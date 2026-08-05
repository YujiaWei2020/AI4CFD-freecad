#!/usr/bin/env python3
"""Run a single bend-pipe OpenFOAM simulation from FreeCAD parameters.

Called by the FreeCAD addon:
    python run_simulation.py --params '{"pipe_radius":1.0,...}' [options]
"""

import argparse
import json
import os
import sys
import threading
from datetime import datetime

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--params",            required=True, type=json.loads)
    parser.add_argument("--num-cores",         type=int,   default=4)
    parser.add_argument("--max-iterations",    type=int,   default=1000)
    parser.add_argument("--mesh-scale-factor", type=float, default=0.5)
    parser.add_argument("--module",            default="train")
    parser.add_argument("--results-dir",       default=None)
    parser.add_argument("--case-dir",          default=None,
                        help="Use this exact directory as the case (no timestamp subdir created)")
    args = parser.parse_args()

    from simulators.pipe.bendpipe.bendpipe_simulator import PipeSimulationPipeline

    if args.case_dir:
        case_dir = args.case_dir
    else:
        results_dir = args.results_dir or os.path.join(_PROJECT_ROOT, "results")
        os.makedirs(results_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        case_dir  = os.path.join(results_dir, f"case_freecad_{timestamp}")

    p = args.params
    print(f"[AI4CFD] Case directory: {case_dir}", flush=True)
    print(f"[AI4CFD] Params: {p}", flush=True)

    pipeline = PipeSimulationPipeline(
        pipe_radius       = p["pipe_radius"],
        length_before     = p["length_before"],
        length_after      = p["length_after"],
        bend_radius       = p["bend_radius"],
        bend_angle        = p["bend_angle"],
        inlet_speed       = p["inlet_speed"],
        num_cores         = args.num_cores,
        max_iterations    = args.max_iterations,
        mesh_scale_factor = args.mesh_scale_factor,
        template_dir      = os.path.join(_PROJECT_ROOT, "openfoam_base", "bendpipe"),
        module            = args.module,
        case_dir          = case_dir,
    )

    # Tail simulation.log in a thread so FreeCAD sees OpenFOAM output live
    log_path   = os.path.join(case_dir, "simulation.log")
    _stop_tail = threading.Event()

    def _tail_log():
        import time
        while not os.path.exists(log_path):
            time.sleep(0.3)
        with open(log_path) as fh:
            while True:
                line = fh.readline()
                if line:
                    print(line, end="", flush=True)
                elif _stop_tail.is_set():
                    break
                else:
                    time.sleep(0.2)

    tail = threading.Thread(target=_tail_log, daemon=True)
    tail.start()

    try:
        pipeline.run()
    finally:
        _stop_tail.set()
        tail.join(timeout=2)

    print(f"[AI4CFD] DONE: {case_dir}", flush=True)


if __name__ == "__main__":
    main()
