"""Test cache invalidation, source distances, and schedule assembly."""
import copy
import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

import build_data as B
from build_cache import FeedCache, atomic_json
from geometry_check import corridor_error
from schedule_check import validate


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.art = self.root / "art"
        self.code = self.root / "code.py"
        self.inputs = self.root / "shapes.txt"
        self.art.write_text("drawing")
        self.code.write_text("OVERRIDE_PATHS = {('a','1'): [1]}\ndef fit():\n    return 1\n")
        self.inputs.write_text("shape")
        self.tables = {"OVERRIDE_PATHS": {("a","1"): [1], ("b","2"): [2]}}

    def cache(self):
        return FeedCache(self.root / "cache", [self.art], [self.code], {}, self.tables)

    def test_inputs_artwork_code_and_tables_invalidate(self):
        cache = self.cache()
        a, b = cache.key("a", [self.inputs]), cache.key("b", [self.inputs])
        self.tables["OVERRIDE_PATHS"][("a","1")] = [3]
        self.code.write_text("OVERRIDE_PATHS = {('a','1'): [3]}\ndef fit():\n    return 1\n")
        changed = self.cache()
        self.assertNotEqual(a, changed.key("a", [self.inputs]))
        self.assertEqual(b, changed.key("b", [self.inputs]))
        self.code.write_text(self.code.read_text().replace("return 1", "return 2"))
        self.assertNotEqual(b, self.cache().key("b", [self.inputs]))
        self.art.write_text("other drawing")
        self.assertNotEqual(cache.shared["artwork"], self.cache().shared["artwork"])
        before = changed.key("b", [self.inputs])
        self.inputs.write_text("other shape")
        self.assertNotEqual(before, changed.key("b", [self.inputs]))

    def test_corruption_and_stale_keys_are_cache_misses(self):
        c = self.cache()
        c.write("a", "key", {"x": [1,2,3]})
        self.assertEqual(c.read("a", "key"), {"x": [1,2,3]})
        self.assertIsNone(c.read("a", "other"))
        path = self.root / "cache/a.json"
        blob = json.loads(path.read_text()); blob["data"]["x"][0] = 4
        path.write_text(json.dumps(blob))
        self.assertIsNone(c.read("a", "key"))
        path.write_text("{")
        self.assertIsNone(c.read("a", "key"))

    def test_atomic_failure_preserves_existing_output(self):
        out = self.root / "schedule.json"
        atomic_json(out, {"old": 1})
        with self.assertRaises(ValueError):
            atomic_json(out, {"bad": float("nan")})
        self.assertEqual(json.loads(out.read_text()), {"old": 1})
        self.assertEqual(list(self.root.glob("schedule.json.*")), [])


class GeometryTests(unittest.TestCase):
    def test_override_can_target_shape_variants(self):
        full = [(0, 0), (5, 0), (10, 0)]
        spec = {"shape_ids": ("a",), "box": (0, -1, 10, 1),
                "path": [(0, 1), (10, 1)]}
        self.assertEqual(B.apply_override(full, full, spec, "b"), full)
        self.assertEqual(B.apply_override(full, full, spec, "a"),
                         [(0.0, 1.0), (5.0, 1.0), (10.0, 1.0)])

    def test_measures_disambiguate_retraced_legs(self):
        ll = [[-118,34],[-117.99,34],[-118,34]]
        stops = [[-117.995,34],[-117.995,34]]
        u = B.measured_positions(ll,[0,100,200],stops,[50,150])
        np.testing.assert_allclose(u, [0.25,0.75])
        self.assertIsNone(B.measured_positions(ll,[0,100,200],stops,[150,50]))
        self.assertIsNone(B.measured_positions(ll,[0,100,200],stops,[500,1500]))
        self.assertIsNone(B.measured_positions(ll,[0,0,200],stops,[50,150]))
        self.assertIsNone(B.measured_positions(ll,[0,100,200],stops,[0,200]))
        self.assertIsNone(B.measured_positions(ll,[0,None,200],stops,[50,150]))

    def test_trim_preserves_original_distance_offset(self):
        pts = [(0,0),(200,0)]
        trimmed, offset = B.trim_terminus(pts, [(40,0)], with_offset=True)
        self.assertEqual(offset, 40)
        self.assertEqual(trimmed[0], (40,0))
        self.assertEqual(trimmed[-1], (200,0))

    def test_inset_stop_membership_uses_source_pass(self):
        P = np.array([[0.,0.],[10.,0.]])
        runs = [{"pts":P,"icum":np.array([0.,10.]),"u0":a,"u1":b}
                for a,b in [(0.1,0.3),(0.7,0.9)]]
        with patch.object(B, "INSET_RECT", (-1,-1,11,1)):
            ir, d = B.inset_stop_map(runs,[0.2,0.5,0.8],[(5,0)]*3)
        self.assertEqual(ir, [0,-1,1])
        self.assertEqual(d, [5,0,5])

    def test_reference_rejects_shortcut_and_wrong_parallel_street(self):
        ref = [[0,0],[0,20],[20,20],[20,0]]
        self.assertEqual(corridor_error([v for p in ref for v in p],ref,3), (0,0))
        off, cover = corridor_error([0,0,20,0],ref,3)
        self.assertGreater(cover, 15)
        wrong = [0,0,5,0,5,20,20,20,20,0]
        self.assertGreater(max(corridor_error(wrong,ref,3)), 3)

    def test_merge_remaps_fallback_shapes_patterns_and_trips(self):
        part = {"systems":["A"], "routes":[{"sy":0,"n":"1","id":"one"}],
                "shapes":[[0,0,10,0],[0,0,0,10]], "insets":[None,None],
                "insetRanges":[None,None], "_nativeShapes":1,
                "patterns":[{"s":0,"d":[0,10]},{"s":1,"d":[0,10]}],
                "trips":[[0,0,0,10],[0,1,20,10]],"tripDays":[4,8]}
        other = copy.deepcopy(part); other["systems"] = ["B"]
        merged = B.merge_schedules([part,other])
        validate(merged)
        self.assertEqual([p["s"] for p in merged["patterns"]], [0,2,1,3])
        self.assertEqual(merged["trips"][2], [1,2,0,10])
        subset = B.select_route(merged,"one")
        validate(subset)
        self.assertEqual(len(subset["trips"]),4)

    def test_missing_feed_and_missing_inset_fail_before_writing(self):
        with tempfile.TemporaryDirectory() as td, patch.object(B,"GTFS",td):
            with self.assertRaises(ValueError):
                B.check_inputs(["missing"])
        with patch.object(B,"TR_INSET",None):
            with self.assertRaises(ValueError):
                B.check_inputs([])

    def test_subset_cannot_replace_full_schedule(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                B.main(["--only", "gtfs_bus:76", "--out", "schedule.json"])
        self.assertEqual(e.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
