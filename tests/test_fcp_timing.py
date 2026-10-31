"""helpers/fcp/timing.py: frame grids, snapping, rational strings."""
import random
import sys
import unittest
from fractions import Fraction
from pathlib import Path

FCP = Path(__file__).parents[1] / "helpers" / "fcp"
if str(FCP) not in sys.path:
    sys.path.insert(0, str(FCP))

from timing import Timebase, parse_rational, rational  # noqa: E402


class FromFpsTest(unittest.TestCase):
    def test_common_rates(self):
        cases = {
            "24": "1/24s", "25": "1/25s", "30": "1/30s", "60": "1/60s",
            "30000/1001": "1001/30000s", "60000/1001": "1001/60000s",
            "24000/1001": "1001/24000s", "30/1": "1/30s",
        }
        for fps, expected in cases.items():
            with self.subTest(fps=fps):
                self.assertEqual(Timebase.from_fps(fps).frame_duration, expected)

    def test_float_2997(self):
        tb = Timebase.from_fps(29.97)
        self.assertEqual(tb.frame_duration, "100/2997s")
        self.assertEqual(tb.fps, Fraction(2997, 100))

    def test_numeric_and_fraction_inputs(self):
        self.assertEqual(Timebase.from_fps(30).frame, Fraction(1, 30))
        self.assertEqual(Timebase.from_fps(Fraction(30000, 1001)).frame, Fraction(1001, 30000))

    def test_invalid(self):
        for bad in ("0", -24, 0.0):
            with self.subTest(fps=bad), self.assertRaises((ValueError, ZeroDivisionError)):
                Timebase.from_fps(bad)


class FramesTest(unittest.TestCase):
    def setUp(self):
        self.tb = Timebase.from_fps("30")

    def test_modes(self):
        self.assertEqual(self.tb.frames(1.01, "floor"), 30)
        self.assertEqual(self.tb.frames(1.01, "ceil"), 31)
        self.assertEqual(self.tb.frames(1.02, "round"), 31)
        self.assertEqual(self.tb.frames(1.01, "round"), 30)

    def test_float_noise_on_a_frame_boundary(self):
        # 0.1 * 3 = 0.30000000000000004; 2.3 - 0.0000000001 still frame 69
        self.assertEqual(self.tb.frames(0.1 * 3, "floor"), 9)
        self.assertEqual(self.tb.frames(0.1 * 3, "ceil"), 9)
        self.assertEqual(self.tb.frames(2.3 - 1e-10, "floor"), 69)

    def test_ntsc_floor(self):
        tb = Timebase.from_fps("30000/1001")
        self.assertEqual(tb.frames(2.42), 72)
        self.assertEqual(tb.t(72), "3003/1250s")  # 72 * 1001/30000, reduced
        self.assertEqual(parse_rational(tb.t(72)), Fraction(72072, 30000))

    def test_seconds_roundtrip(self):
        self.assertAlmostEqual(self.tb.seconds(45), 1.5)


class RationalTest(unittest.TestCase):
    def test_t_strings(self):
        tb = Timebase.from_fps("30")
        self.assertEqual(tb.t(0), "0s")
        self.assertEqual(tb.t(30), "1s")
        self.assertEqual(tb.t(45), "3/2s")
        self.assertEqual(tb.t(1), "1/30s")
        self.assertEqual(rational(Fraction(7, 1)), "7s")

    def test_parse_rational(self):
        self.assertEqual(parse_rational("7260/3000s"), Fraction(121, 50))
        self.assertEqual(parse_rational("0s"), 0)
        self.assertEqual(parse_rational("1s"), 1)
        self.assertEqual(parse_rational(" 1001/30000s "), Fraction(1001, 30000))
        with self.assertRaises(ValueError):
            parse_rational("12")


class SnapRangeTest(unittest.TestCase):
    def test_min_one_frame(self):
        tb = Timebase.from_fps("30")
        self.assertEqual(tb.snap_range(1.0, 1.0), (30, 1))
        self.assertEqual(tb.snap_range(1.0, 1.001), (30, 1))

    def test_regular(self):
        tb = Timebase.from_fps("30")
        self.assertEqual(tb.snap_range(1.0, 3.0), (30, 60))
        self.assertEqual(tb.snap_range(1.01, 2.0), (30, 30))

    def test_cumulative_offsets_equal_sum_of_durations(self):
        rng = random.Random(1234)
        for fps in ("24", "25", "30000/1001", "60"):
            tb = Timebase.from_fps(fps)
            ranges = []
            for _ in range(50):
                s = rng.uniform(0, 600)
                ranges.append((s, s + rng.uniform(0.01, 8)))
            offset = 0
            offsets, durations = [], []
            for s, e in ranges:
                _, d = tb.snap_range(s, e)
                offsets.append(offset)
                durations.append(d)
                offset += d
            with self.subTest(fps=fps):
                self.assertEqual(offsets[0], 0)
                for i in range(1, len(offsets)):
                    self.assertEqual(offsets[i], offsets[i - 1] + durations[i - 1])
                total = sum(parse_rational(tb.t(d)) for d in durations)
                self.assertEqual(parse_rational(tb.t(offset)), total)
                self.assertEqual((parse_rational(tb.t(offset)) / tb.frame).denominator, 1)


if __name__ == "__main__":
    unittest.main()
