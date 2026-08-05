#!/usr/bin/env python3
"""Generic CFD simulation runner — routes to the right simulator pipeline.

Dispatches to the existing project simulator classes (AirfoilSimulationPipeline,
PipeSimulationPipeline, etc.) based on --case-name.  Parameters from the
FreeCAD spreadsheet are passed directly as constructor kwargs, so alias names
must match the simulator's parameter names.

Expected alias names per case
------------------------------
airfoil:   chord_length, angle_of_attack, freestream_speed, span, naca_digits,
           upstream_factor, downstream_factor, lateral_factor, spanwise_factor
bendpipe:  pipe_radius, length_before, length_after, bend_radius, bend_angle,
           inlet_speed
manifold:  (see ManifoldSimulationPipeline)
conicalTank, contactTank: (see respective simulators)

Usage:
    python run_generic_sim.py \
        --case-name airfoil \
        --params '{"chord_length":1.0,"angle_of_attack":5.0,"freestream_speed":20.0}' \
        [--num-cores 4] [--max-iterations 500] [--module train]
"""

import argparse
import importlib
import json
import os
import sys
from datetime import datetime

_HERE         = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)

# Map case name → (module, class)
_SIMULATOR_MAP = {
    "airfoil":     ("simulators.airfoil.airfoil_simulator",
                    "AirfoilSimulationPipeline"),
    "bendpipe":    ("simulators.pipe.bendpipe.bendpipe_simulator",
                    "PipeSimulationPipeline"),
    "twobendpipe": ("simulators.pipe.twobendpipe.twobendpipe_simulator",
                    "TwobendPipeSimulationPipeline"),
    "manifold":    ("simulators.manifold.manifold_simulator",
                    "ManifoldSimulationPipeline"),
    "conicalTank": ("simulators.tank.ConicalTank_simulator",
                    "ConicalTankSimulationPipeline"),
    "contactTank": ("simulators.tank.contactTank_simulator",
                    "ContactTankSimulationPipeline"),
}


def _expected_params(case_name: str) -> list[str]:
    """Return a best-effort list of expected param names for a given case."""
    _HINTS = {
        "airfoil":     ["chord_length", "angle_of_attack", "freestream_speed",
                        "span", "naca_digits"],
        "bendpipe":    ["pipe_radius", "length_before", "length_after",
                        "bend_radius", "bend_angle", "inlet_speed"],
        "twobendpipe": ["pipe_radius", "length_before", "length_between",
                        "length_after", "bend_radius", "bend_angle", "inlet_speed"],
        "manifold":    ["pipe_radius", "inlet_speed"],
        "conicalTank": ["tank_radius_bottom", "tank_radius_top",
                        "tank_height", "inlet_speed"],
        "contactTank": ["tank_length", "tank_width", "tank_height",
                        "baffle_height", "inlet_speed"],
    }
    return _HINTS.get(case_name, [])


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generic CFD parametric simulation runner")
    parser.add_argument("--case-name",      required=True,
                        help="Name of the OpenFOAM base case (e.g. airfoil, bendpipe)")
    parser.add_argument("--params",         required=True, type=json.loads,
                        help="JSON dict of simulation parameters")
    parser.add_argument("--num-cores",      type=int,   default=1)
    parser.add_argument("--max-iterations", type=int,   default=1000)
    parser.add_argument("--module",         default="train",
                        help="'train' runs solver; 'test' skips it")
    parser.add_argument("--results-dir",    default=None)
    parser.add_argument("--case-dir",       default=None,
                        help="Use this exact directory (no timestamp subdir)")
    args = parser.parse_args()

    case_name = args.case_name.rstrip("/").split("/")[-1]   # basename only
    p         = args.params

    # ── Resolve case directory ─────────────────────────────────────────────────
    if args.case_dir:
        case_dir = args.case_dir
    else:
        results_dir = args.results_dir or os.path.join(_PROJECT_ROOT, "results",
                                                        case_name)
        os.makedirs(results_dir, exist_ok=True)
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        case_dir = os.path.join(results_dir, f"case_{ts}")

    template_dir = os.path.join(_PROJECT_ROOT, "openfoam_base", case_name)

    print(f"[AI4CFD] Case name:   {case_name}", flush=True)
    print(f"[AI4CFD] Case dir:    {case_dir}", flush=True)
    print(f"[AI4CFD] Template:    {template_dir}", flush=True)
    print(f"[AI4CFD] Params:      {p}", flush=True)

    # ── Look up and instantiate the simulator ──────────────────────────────────
    entry = _SIMULATOR_MAP.get(case_name)
    if entry is None:
        print(f"[AI4CFD] ERROR: no simulator registered for '{case_name}'.", flush=True)
        print(f"[AI4CFD] Available: {sorted(_SIMULATOR_MAP)}", flush=True)
        expected = _expected_params(case_name)
        if expected:
            print(f"[AI4CFD] Expected params: {expected}", flush=True)
        sys.exit(1)

    mod_name, cls_name = entry
    try:
        mod = importlib.import_module(mod_name)
        SimClass = getattr(mod, cls_name)
    except Exception as exc:
        print(f"[AI4CFD] ERROR importing {cls_name}: {exc}", flush=True)
        sys.exit(1)

    # Merge user params with fixed infrastructure params
    kwargs = {
        **p,
        "num_cores":    args.num_cores,
        "max_iterations": args.max_iterations,
        "module":       args.module,
        "case_dir":     case_dir,
        "template_dir": template_dir,
    }

    try:
        pipeline = SimClass(**kwargs)
    except TypeError as exc:
        print(f"[AI4CFD] ERROR creating {cls_name}: {exc}", flush=True)
        expected = _expected_params(case_name)
        if expected:
            print(f"[AI4CFD] Expected spreadsheet aliases: {expected}", flush=True)
        sys.exit(1)

    # ── Run ────────────────────────────────────────────────────────────────────
    try:
        pipeline.run()
    except Exception as exc:
        print(f"[AI4CFD] Simulation failed: {exc}", flush=True)
        sys.exit(1)

    print(f"[AI4CFD] DONE: {case_dir}", flush=True)


if __name__ == "__main__":
    main()
