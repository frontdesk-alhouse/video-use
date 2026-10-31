"""helpers/fcp/fcp_import.py: reconcile() against canned timeline.getDetailedState payloads."""
import sys
import unittest
from pathlib import Path

FCP = Path(__file__).parents[1] / "helpers" / "fcp"
if str(FCP) not in sys.path:
    sys.path.insert(0, str(FCP))

from fcp_import import reconcile, seconds_of  # noqa: E402

PLAN = {
    "fps": "30",
    "total_duration_s": 6.7,
    "clips": [
        {"index": 0, "duration_s": 2.0, "out_start_s": 0.0, "out_end_s": 2.0},
        {"index": 1, "duration_s": 2.5, "out_start_s": 2.0, "out_end_s": 4.5},
        {"index": 2, "duration_s": 2.2, "out_start_s": 4.5, "out_end_s": 6.7},
    ],
}


def t(seconds, timescale=30):
    value = round(seconds * timescale)
    return {"value": value, "seconds": value / timescale, "timescale": timescale}


def coarse(seconds):
    return {"value": int(seconds), "seconds": int(seconds), "timescale": 1}


def item(name, start, dur, end=None, **kw):
    it = {"name": name, "class": "FFAnchoredMediaComponent", "lane": 0,
          "startTime": t(start), "duration": t(dur), "endTime": end or t(start + dur)}
    it.update(kw)
    return it


def state(items, duration=6.7):
    return {"sequenceName": "P", "itemCount": len(items), "duration": t(duration), "items": items}


GOOD = [item("HOOK", 0.0, 2.0), item("PROBLEM", 2.0, 2.5), item("PAYOFF", 4.5, 2.2)]


class ReconcileTest(unittest.TestCase):
    def test_exact_match(self):
        r = reconcile(state(GOOD), PLAN)
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(len(r["clips"]), 3)
        self.assertAlmostEqual(r["timeline_duration_s"], 6.7)

    def test_coarse_end_time_does_not_fail(self):
        items = [item("HOOK", 0.0, 2.0), item("PROBLEM", 2.0, 2.5, end=coarse(4.5)),
                 item("PAYOFF", 4.5, 2.2, end=coarse(6.7))]
        # a 3.2 s clip reported as endTime 3 s @ timescale 1
        plan = {"fps": "30", "total_duration_s": 3.2,
                "clips": [{"index": 0, "duration_s": 3.2, "out_start_s": 0.0, "out_end_s": 3.2}]}
        one = [item("X", 0.0, 3.2, end={"value": 3, "seconds": 3, "timescale": 1})]
        r = reconcile(state(one, duration=3.2), plan)
        self.assertTrue(r["ok"], r["problems"])
        self.assertAlmostEqual(r["clips"][0]["end_s"], 3.2)
        self.assertTrue(reconcile(state(items), PLAN)["ok"])

    def test_within_one_frame_tolerance(self):
        items = [item("HOOK", 0.0, 2.0 + 1 / 30), item("PROBLEM", 2.0 + 1 / 30, 2.5 - 1 / 30),
                 item("PAYOFF", 4.5, 2.2)]
        self.assertTrue(reconcile(state(items), PLAN, tolerance_frames=1)["ok"])
        self.assertFalse(reconcile(state(items), PLAN, tolerance_frames=0)["ok"])

    def test_real_mismatch_fails(self):
        items = [item("HOOK", 0.0, 2.0), item("PROBLEM", 2.0, 2.0), item("PAYOFF", 4.0, 2.2)]
        r = reconcile(state(items, duration=6.2), PLAN)
        self.assertFalse(r["ok"])
        self.assertTrue(any(p.startswith("clip 1") for p in r["problems"]), r["problems"])
        self.assertTrue(any(p.startswith("timeline duration") for p in r["problems"]), r["problems"])
        self.assertEqual([row["ok"] for row in r["clips"]], [True, False, False])

    def test_clip_count_mismatch(self):
        r = reconcile(state(GOOD[:2], duration=4.5), PLAN)
        self.assertFalse(r["ok"])
        self.assertIn("spine has 2 items, plan has 3 clips", r["problems"])

    def test_connected_and_transition_items_ignored(self):
        extra = [dict(item("broll", 0.5, 3.0), lane=1),
                 {"name": "Cross Dissolve", "class": "FFAnchoredTransition", "lane": 0,
                  "startTime": t(1.9), "duration": t(0.2)}]
        r = reconcile(state(GOOD[:1] + extra + GOOD[1:]), PLAN)
        self.assertTrue(r["ok"], r["problems"])

    def test_no_start_time_fallback(self):
        items = [{"name": n, "lane": 0, "duration": t(d)} for n, d in
                 (("HOOK", 2.0), ("PROBLEM", 2.5), ("PAYOFF", 2.2))]
        r = reconcile(state(items), PLAN)
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(set(r["clips"][0]), {"index", "name", "duration_s", "expected_duration_s", "ok"})
        items[1]["duration"] = t(1.5)
        bad = reconcile(state(items), PLAN)
        self.assertFalse(bad["ok"])
        self.assertTrue(any(p.startswith("clip 1 duration") for p in bad["problems"]))

    def test_seconds_of(self):
        self.assertEqual(seconds_of(None), 0.0)
        self.assertEqual(seconds_of({"value": 3003, "timescale": 30000}), 0.1001)
        self.assertEqual(seconds_of({"value": 3, "seconds": 3.5, "timescale": 1}), 3.5)


if __name__ == "__main__":
    unittest.main()


class ImportRouteTest(unittest.TestCase):
    """SpliceKit 3.3.9 reads one 64 KB socket chunk per message: a bigger inline import never
    parses and the call hangs until timeout (measured 2026-10-27: 54.8 KB ok, 68.9 KB hangs)."""

    def test_small_xml_goes_inline(self):
        from fcp_import import import_route
        self.assertEqual(import_route("<fcpxml/>" * 100), "inline")

    def test_xml_over_the_socket_chunk_goes_by_menu(self):
        from fcp_import import import_route
        self.assertEqual(import_route("x" * 70_000), "menu")

    def test_escaped_quotes_count_toward_the_limit(self):
        from fcp_import import import_route, INLINE_LIMIT
        # raw size under the limit, but every quote is escaped in the JSON request
        xml = '"' * (INLINE_LIMIT // 2 + 100)
        self.assertEqual(import_route(xml), "menu")

    def test_force_menu(self):
        from fcp_import import import_route
        self.assertEqual(import_route("<fcpxml/>", force_menu=True), "menu")
