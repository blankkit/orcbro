import unittest

from bridge import preview
from helpers import GCODE


class PreviewTests(unittest.TestCase):
    def test_layers_features_and_purge(self):
        p = preview.parse_gcode(GCODE.splitlines())
        self.assertEqual([round(l.z, 2) for l in p.layers], [0.2, 0.4])
        self.assertEqual([round(l.height, 2) for l in p.layers], [0.2, 0.2])
        self.assertEqual(p.features, ["Outer wall", "Sparse infill"])
        first = p.layers[0]
        # Omit the pre-layer purge line; retraction and travel separate paths.
        self.assertEqual(first.paths[0], (0, [(10.0, 10.0), (20.0, 10.0), (20.0, 20.0)]))
        infill = first.paths[1]
        self.assertEqual(infill[0], 1)
        self.assertEqual(infill[1][0], (30.0, 30.0))
        self.assertEqual(infill[1][-1], (50.0, 40.0))
        self.assertGreater(len(infill[1]), 4)  # Arc split into segments.

    def test_absolute_extrusion_and_generic_markers(self):
        gcode = """M82
G92 E0
;LAYER_CHANGE
;Z:0.3
;HEIGHT:0.3
;TYPE:External perimeter
G1 X0 Y0
G1 X5 Y0 E1.0
G1 X5 Y5 E0.8
G1 X0 Y5 E1.5
""".splitlines()
        p = preview.parse_gcode(gcode)
        self.assertEqual(len(p.layers), 1)
        self.assertAlmostEqual(p.layers[0].height, 0.3)
        # Absolute E 1.0 → 0.8 retracts, producing two paths.
        self.assertEqual([pts for _, pts in p.layers[0].paths], [[(0.0, 0.0), (5.0, 0.0)], [(5.0, 5.0), (0.0, 5.0)]])

    def test_roundtrip_and_simplify(self):
        p = preview.reduce(preview.parse_gcode(GCODE.splitlines()))
        decoded = preview.decode(preview.encode(p))
        self.assertEqual(decoded.features, p.features)
        self.assertEqual(len(decoded.layers), 2)
        self.assertEqual(decoded.layers[0].paths[0][1][0], (10.0, 10.0))
        self.assertEqual(preview.simplify([(0, 0), (1, 0.001), (2, 0)], 0.01), [(0, 0), (2, 0)])
        self.assertEqual(preview.simplify([(0, 0), (1, 1), (2, 0)], 0.01), [(0, 0), (1, 1), (2, 0)])

    def test_limit_increases_tolerance(self):
        points = [(i * 0.01, (i % 2) * 0.03) for i in range(2000)]
        layer = preview.Layer(z=0.2, height=0.2, paths=[(0, points)])
        reduced = preview.reduce(preview.Preview(bed=(256, 256), features=["x"], layers=[layer]), limit=100)
        self.assertLessEqual(reduced.point_count, 100)


if __name__ == "__main__":
    unittest.main()
