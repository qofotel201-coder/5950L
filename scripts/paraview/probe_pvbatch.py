"""Verify that this interpreter is a working headless ParaView ``pvbatch``."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback

from paraview import simple


def _paraview_version():
    value = simple.GetParaViewVersion()
    if isinstance(value, (tuple, list)):
        return ".".join(str(component) for component in value)
    return str(value)


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main():
    args = _parse_args()
    output_path = args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "PASS",
        "paraview_version": _paraview_version(),
        "python_executable": str(Path(sys.executable).resolve()),
    }
    output_path.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


def _entry_point():
    try:
        return main()
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_entry_point())
