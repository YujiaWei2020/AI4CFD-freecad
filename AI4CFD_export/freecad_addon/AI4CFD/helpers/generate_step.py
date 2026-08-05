#!/usr/bin/env python3
"""Subprocess helper — runs in the project venv to generate a STEP preview file.

Called by the FreeCAD addon:
    python generate_step.py --type bend --params '{"pipe_radius":1.0,...}' --output /tmp/foo.step
"""

import argparse
import json
import os
import sys

# Inject project root so geometry.* imports work
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.join(_HERE, "..", "..", "..")  # freecad_addon/AI4CFD/helpers/../../.. = project root
sys.path.insert(0, os.path.normpath(_PROJECT_ROOT))


def _bend(params: dict, output: str) -> None:
    from geometry.pipe.bendpipe import Bend
    import cadquery as cq

    bend = Bend(
        length_before=params["length_before"],
        pipe_radius=params["pipe_radius"],
        length_after=params["length_after"],
        bend_radius=params["bend_radius"],
        bend_angle=params["bend_angle"],
    )
    bend.generate_shape()
    cq.exporters.export(bend.shape, output)


_GENERATORS = {
    "bend": _bend,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate geometry STEP for FreeCAD preview")
    parser.add_argument("--type",   required=True, choices=list(_GENERATORS))
    parser.add_argument("--params", required=True, type=json.loads, metavar="JSON")
    parser.add_argument("--output", required=True, metavar="PATH")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    _GENERATORS[args.type](args.params, args.output)
    print(f"OK: {args.output}")


if __name__ == "__main__":
    main()
