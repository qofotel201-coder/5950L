from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from cfdpipe.interface_coincidence import (
    InterfaceCoincidenceError,
    inspect_interface_coincidence,
)


class _Occ:
    def __init__(self, owner):
        self.owner = owner

    def importShapes(self, path):
        self.owner.calls.append(("importShapes", path))
        return [(3, 1), (3, 2)]

    def synchronize(self):
        self.owner.calls.append(("synchronize",))

    def getMass(self, dimension, tag):
        return 1.0


class _Model:
    def __init__(self, owner, *, fail=False):
        self.owner = owner
        self.fail = fail
        self.occ = _Occ(owner)

    def add(self, name):
        self.owner.calls.append(("add", name))

    def getParametrizationBounds(self, dimension, tag):
        return ([0.0, 0.0], [1.0, 1.0]) if dimension == 2 else ([0.0], [1.0])

    def getValue(self, dimension, tag, parameters):
        if dimension == 2:
            return [0.0, float(parameters[0]), float(parameters[1])]
        values = []
        edge = (tag - 1) % 4
        for parameter in parameters:
            if edge == 0:
                values.extend([0.0, float(parameter), 0.0])
            elif edge == 1:
                values.extend([0.0, 1.0, float(parameter)])
            elif edge == 2:
                values.extend([0.0, 1.0 - float(parameter), 1.0])
            else:
                values.extend([0.0, 0.0, 1.0 - float(parameter)])
        return values

    def isInside(self, dimension, tag, point, parametric):
        return True

    def getClosestPoint(self, dimension, tag, point):
        if self.fail:
            raise RuntimeError("probe boom")
        return list(point), [float(point[1]), float(point[2])]

    def getNormal(self, tag, uv):
        return [1.0, 0.0, 0.0] if tag == 1 else [-1.0, 0.0, 0.0]

    def getBoundary(self, dimtags, combined, oriented, recursive):
        surface = dimtags[0][1]
        start = 1 if surface == 1 else 5
        return [(1, start + offset) for offset in range(4)]


class _Option:
    def __init__(self, owner):
        self.owner = owner

    def setString(self, name, value):
        self.owner.calls.append(("setString", name, value))


class _FakeGmsh:
    __version__ = "test"
    __file__ = "fake_gmsh.py"

    def __init__(self, *, fail=False):
        self.calls = []
        self.model = _Model(self, fail=fail)
        self.option = _Option(self)

    def initialize(self):
        self.calls.append(("initialize",))

    def finalize(self):
        self.calls.append(("finalize",))


class InterfaceCoincidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.step = self.root / "model.step"
        self.step.write_text("STEP", encoding="ascii")
        os.chmod(self.step, stat.S_IREAD)
        self.addCleanup(os.chmod, self.step, stat.S_IWRITE | stat.S_IREAD)
        source_hash = hashlib.sha256(self.step.read_bytes()).hexdigest()
        self.topology = self.root / "topology.json"
        self.topology.write_text(
            json.dumps(
                {
                    "source_sha256": source_hash,
                    "strong_contact_candidates": [{"surface_tags": [1, 2]}],
                }
            ),
            encoding="utf-8",
        )

    def test_probe_proves_faces_and_boundaries_and_finalizes(self):
        gmsh = _FakeGmsh()
        output = self.root / "probe.json"

        report = inspect_interface_coincidence(
            self.step,
            self.topology,
            output,
            grid_size=3,
            boundary_sample_count=5,
            gmsh_module=gmsh,
        )

        self.assertEqual(report["status"], "PASS")
        self.assertTrue(report["source_unchanged"])
        self.assertEqual(report["pairs"][0]["status"], "FULL_GEOMETRIC_COINCIDENCE")
        self.assertTrue(report["pairs"][0]["boundary_curve_matching"]["bijective"])
        self.assertEqual(gmsh.calls[0], ("initialize",))
        self.assertEqual(gmsh.calls[-1], ("finalize",))
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)

    def test_exception_still_finalizes_and_preserves_original_traceback(self):
        gmsh = _FakeGmsh(fail=True)

        with self.assertRaisesRegex(InterfaceCoincidenceError, "probe boom"):
            inspect_interface_coincidence(
                self.step,
                self.topology,
                grid_size=3,
                boundary_sample_count=5,
                gmsh_module=gmsh,
            )

        self.assertEqual(gmsh.calls[-1], ("finalize",))

    def test_stale_topology_stops_before_initialize(self):
        document = json.loads(self.topology.read_text(encoding="utf-8"))
        document["source_sha256"] = "0" * 64
        self.topology.write_text(json.dumps(document), encoding="utf-8")
        gmsh = _FakeGmsh()

        with self.assertRaisesRegex(InterfaceCoincidenceError, "stale"):
            inspect_interface_coincidence(
                self.step, self.topology, gmsh_module=gmsh
            )

        self.assertEqual(gmsh.calls, [])


if __name__ == "__main__":
    unittest.main()
