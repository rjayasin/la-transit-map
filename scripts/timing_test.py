"""Check estimated arrivals against fixed timing points and displayed motion."""
import unittest

from schedule_timing import detie, eff_dist, repair_estimates


class TimingTests(unittest.TestCase):
    def test_express_hop_uses_time_before_next_fixed_stop(self):
        times = [0, 60, 120, 600]
        fixed = [True, False, False, True]
        out = repair_estimates(times, fixed, {"d": [0, 250, 260, 300]})
        self.assertEqual(out, [0, 500, 520, 600])
        self.assertEqual(times, [0, 60, 120, 600])

    def test_tied_estimates_use_fixed_interval(self):
        times = [0, 0, 0, 60, 900]
        out = repair_estimates(times, [True, False, False, False, True],
                               {"d": [0, 50, 100, 150, 200]})
        self.assertEqual(out, [0, 225, 450, 675, 900])

    def test_healthy_estimates_and_fixed_times_stay_put(self):
        times = [0, 120, 180, 300, 360, 420, 900]
        fixed = [True, False, False, True, False, False, True]
        out = repair_estimates(times, fixed, {"d": [0, 10, 40, 60, 300, 310, 360]})
        self.assertEqual(out[:4], times[:4])
        self.assertEqual(out[-1], times[-1])
        self.assertEqual(out[4:6], [780, 800])
        self.assertEqual(repair_estimates(times, [True] * 7,
                                         {"d": [0, 10, 40, 60, 300, 310, 360]}), times)

    def test_infeasible_fixed_interval_is_not_retimed(self):
        times = [0, 1, 60]
        self.assertEqual(repair_estimates(times, [True, False, True],
                                         {"d": [0, 200, 300]}), times)

    def test_inset_movement_counts_when_main_map_is_flat(self):
        pat = {"d": [0, 0, 0, 0], "ir": [0, 0, 0, 0],
               "id": [0, 1000, 1050, 1500]}
        times = repair_estimates([0, 60, 120, 600],
                                [True, False, False, True], pat)
        self.assertEqual(times, [0, 400, 420, 600])
        self.assertEqual(detie(list(times), eff_dist(pat)), times)

    def test_trip_endpoints_stay_fixed_across_midnight(self):
        times = [86300, 86360, 86420, 86900]
        out = repair_estimates(times, [False] * 4, {"d": [0, 250, 260, 300]})
        self.assertEqual(out, [86300, 86800, 86820, 86900])


if __name__ == "__main__":
    unittest.main()
