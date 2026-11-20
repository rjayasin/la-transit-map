"""Test cache invalidation, source distances, and schedule assembly."""
import copy
import json
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import numpy as np

import build_data as B
import table_check
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

    def test_table_keys_are_the_same_in_every_process(self):
        # A set iterates in an order set by the per-process string hash seed.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from build_cache import FeedCache; "
                "rows = {('a', '1'): {'shape_ids': {f's{i}' for i in range(20)}}}; "
                "print(FeedCache('.', [], [], {}, {'OVERRIDE_PATHS': rows}).key('a', []))")
        keys = {subprocess.run([sys.executable, "-c", code, str(Path(__file__).resolve().parent)],
                               env={**os.environ, "PYTHONHASHSEED": str(seed)},
                               capture_output=True, text=True, check=True).stdout
                for seed in (1, 2)}
        self.assertEqual(len(keys), 1)

    def test_mask_stamp_covers_what_the_mask_build_reads(self):
        # unfade reads FADE_MIN and calls glyphs; rival_palette reads LEGEND_SEEDS.
        # Both are called from below mask_pixels.
        stamp = B.code_stamp(B.mask_pixels)
        with patch.object(B, "FADE_MIN", B.FADE_MIN + 0.01):
            self.assertNotEqual(B.code_stamp(B.mask_pixels), stamp)
        with patch.dict(B.LEGEND_SEEDS, {"probe": [(1, 2, 3)]}):
            self.assertNotEqual(B.code_stamp(B.mask_pixels), stamp)
        with patch.object(B, "glyphs", lambda sub: sub):
            self.assertNotEqual(B.code_stamp(B.mask_pixels), stamp)
        self.assertEqual(B.code_stamp(B.mask_pixels), stamp)

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


class ScheduleTests(unittest.TestCase):
    def test_block_chain_keeps_one_trip_across_multiple_splits(self):
        def stop(seq, time, sid, measure):
            return (seq, time, time, sid, measure, True)
        times = {
            "a": [stop(1, 10, "x", 0), stop(2, 20, "y", 10)],
            "b": [stop(1, 20, "y", 0), stop(2, 30, "z", 10)],
            "c": [stop(1, 30, "z", 0), stop(2, 40, "w", 10)],
            "reverse": [stop(1, 40, "w", 0), stop(2, 50, "x", 10)],
            "loop": [stop(1, 50, "x", 0), stop(2, 60, "x", 10)],
            "next-loop": [stop(1, 60, "x", 0), stop(2, 70, "x", 10)],
        }
        info = {ti: ("route", "shape") for ti in times}
        info["reverse"] = ("route", "other-shape")
        blocks = {ti: ("service", "block") for ti in times}
        self.assertEqual(B.chain_block_trips(times, info, blocks), 2)
        self.assertEqual(set(times), {"a", "reverse", "loop", "next-loop"})
        self.assertEqual([s[3] for s in times["a"]], ["x", "y", "z", "w"])
        self.assertEqual([s[0] for s in times["a"]], [1, 2, 3, 4])


class GeometryTests(unittest.TestCase):
    def test_override_waypoints_keep_junction_stops_on_their_street(self):
        prior = [(0, 0), (2, 0), (3, 0), (10, 0)]
        spec = {"box": (0, -1, 10, 1), "forward": True,
                "path": [(0, 0), (0, 10), (10, 10)],
                "waypoints": [((2, 0), (0, 10))]}
        result = B.apply_override(prior, prior, spec)
        np.testing.assert_allclose(result, [(0, 0), (0, 10), (1.25, 10), (10, 10)])
        spec["waypoints"] = [((8, 0), (0, 5)), ((2, 0), (5, 10))]
        with self.assertRaisesRegex(ValueError, "waypoints must follow"):
            B.apply_override(prior, prior, spec)

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


class TableCheckTests(unittest.TestCase):
    def test_rows_the_build_would_pass_over_are_reported(self):
        spec = {"box": (0, 0, 1, 1), "path": [(0, 0), (1, 1)]}
        T = {"FEEDS": ["f", "gone"], "FEED_NAMES": {"f": "F", "gone": "G"},
             "OVERRIDE_PATHS": {
                 ("f", "1"): {**spec, "shape_ids": ("a",)},
                 ("f", "1-7"): spec,
                 ("f", "3"): spec,
                 ("f", "2"): [{**spec, "exat": True},
                              {**spec, "shape_ids": ("a", "z")},
                              {**spec, "shape_ids": "b"}]}}
        with tempfile.TemporaryDirectory() as td, patch.object(B, "GTFS", td):
            with zipfile.ZipFile(f"{td}/f.zip", "w") as z:
                z.writestr("routes.txt", "route_id\n1-7\n2\n")
                z.writestr("trips.txt", "route_id,shape_id\n1-7,a\n2,b\n")
            problems = table_check.check(T, table_check.load(T))
        expected = ["no archive", "('f', '1-7'): never read", "has no route '3'",
                    "unknown field 'exat'", "shape 'a' belongs to another route",
                    "shape 'z' is not in the feed", "is a string"]
        self.assertEqual(len(problems), len(expected), problems)
        for text in expected:
            self.assertTrue(any(text in p for p in problems), text)


class StrokeNetworkTests(unittest.TestCase):
    def test_walk_keeps_loop_and_source_distance(self):
        ink = np.asarray(B.densify([(100, 100), (100, 130),
                                   (120, 130), (120, 100)], 0.5))
        prior = np.array([[100., 100.], [120., 100.]])
        base = np.array([[200., 200.], [220., 200.]])
        with patch.object(B, "maskable", return_value=np.ones(2, bool)):
            path, source = B.follow_stroke_network(prior, base, ink)
        self.assertGreater(path[:, 1].max(), 129)
        self.assertGreater(np.hypot(*np.diff(path, axis=0).T).sum(), 75)
        np.testing.assert_allclose(source[[0, -1]], base)
        self.assertAlmostEqual(np.hypot(*np.diff(source, axis=0).T).sum(), 20)
        self.assertTrue(np.all(np.diff(source[:, 0]) >= 0))
        self.assertEqual(len(path), len(source))

    def test_disconnected_network_does_not_emit_a_chord(self):
        ink = np.array([[100., 100.], [100.5, 100.],
                        [120., 100.], [120.5, 100.]])
        prior = ink[[0, 2]]
        with patch.object(B, "maskable", return_value=np.ones(2, bool)):
            with self.assertRaisesRegex(ValueError, "disconnected"):
                B.follow_stroke_network(prior, prior, ink)

    def test_western_exit_stays_on_ink_until_the_sheet_edge(self):
        ink = np.asarray(B.densify([(0.4, 100), (10, 100)], 0.5))
        prior = np.array([[-2., 98.], [2., 100.]])
        with patch.object(B, "maskable", return_value=np.array([False, True])):
            path, source = B.follow_stroke_network(prior, prior, ink)
        np.testing.assert_allclose(path[:, 1], 100)
        self.assertLess(path[0, 0], 0)
        np.testing.assert_allclose(source[[0, -1]], prior)


if __name__ == "__main__":
    unittest.main()
