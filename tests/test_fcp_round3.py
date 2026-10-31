"""Round-3 FCP builder features: per-source rotation, B-roll cut from a take,
gap and title card in the spine, title font params."""
from __future__ import annotations

import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path

import _fcp_fixture as fx  # puts helpers/ and helpers/fcp on sys.path

import edl_to_fcpxml as builder  # noqa: E402
import fcp_import  # noqa: E402
import fcp_verify  # noqa: E402
from captions import render_mod, title_xml  # noqa: E402
from timing import parse_rational  # noqa: E402
from validate import validate  # noqa: E402


def fake_still(calls: list):
    def still(path, width, height, color):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG fake")
        calls.append((Path(path).name, width, height, color))
    return still


CARD = {"lines": [{"text": "LIBRO CERO:", "size": 70},
                  {"text": "NADIE TIENE UNA TRAYECTORIA LINEAL", "size": 110, "position_y": -6}],
        "duration": 2.0}


class Round3Base(unittest.TestCase):
    captions = "both"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        edl, self.edit_dir = fx.make_project(Path(self.tmp.name), grade=None)
        ranges = edl["ranges"]
        # A(2.0) · gap 1.0 · card 2.0 · B(2.5) · A(2.2)
        edl["ranges"] = [ranges[0], {"gap": 1.0}, {"card": CARD, "beat": "TITLE"}, ranges[1], ranges[2]]
        edl["fcp"] = {"transform": {"B": {"rotation": 180}}}
        edl["overlays"] = [
            # B-roll cut from take B, inside the last A clip (out 7.5–9.7)
            {"source": "B", "source_start": 1.0, "start_in_output": 7.6, "duration": 1.5},
            # explicit overrides: not muted, no rotation
            {"source": "B", "source_start": 0.0, "start_in_output": 0.5, "duration": 1.0,
             "mute": False, "rotation": 0},
            # past the media: B is 6.0 s, so 5.0 + 3.0 is clamped to 1.0 s
            {"source": "B", "source_start": 5.0, "start_in_output": 6.0, "duration": 3.0},
        ]
        edl.pop("total_duration_s", None)
        self.edl = edl
        self.still_calls: list = []
        self.opt = builder.BuildOptions(probe_fn=fx.fake_probe, captions=self.captions,
                                        still_fn=fake_still(self.still_calls))

    def tearDown(self):
        self.tmp.cleanup()

    def build(self):
        xml, plan = builder.build(self.edl, self.edit_dir, self.opt)
        return ET.fromstring(xml), plan, xml

    def spine(self, root):
        return list(root.find(".//spine"))

    def asset_id(self, root, name):
        return next(a.get("id") for a in root.iter("asset") if a.get("name") == name)


class RotationTest(Round3Base):
    def test_rotated_source_gets_transform_before_volume(self):
        root, _, _ = self.build()
        b_id = self.asset_id(root, fx.B_NAME)
        b_clips = [c for c in self.spine(root) if c.tag == "asset-clip" and c.get("ref") == b_id]
        self.assertEqual(len(b_clips), 1)
        kids = [k.tag for k in b_clips[0]]
        self.assertEqual(kids[:2], ["adjust-transform", "adjust-volume"])
        self.assertEqual(b_clips[0].find("adjust-transform").get("rotation"), "180")

    def test_other_sources_untouched(self):
        root, plan, _ = self.build()
        a_id = self.asset_id(root, fx.A_NAME)
        for c in self.spine(root):
            if c.get("ref") == a_id:
                self.assertIsNone(c.find("adjust-transform"))
        self.assertEqual([c.get("rotation") for c in plan.clips if c["kind"] == "media"], [0.0, 180.0, 0.0])


class BrollTest(Round3Base):
    def overlays(self, root):
        b_id = self.asset_id(root, fx.B_NAME)
        return [o for c in self.spine(root) for o in c.findall("asset-clip") if o.get("ref") == b_id]

    def test_broll_reuses_source_asset_with_in_point_mute_and_rotation(self):
        root, plan, _ = self.build()
        first = next(o for o in self.overlays(root) if o.get("lane") == "1"
                     and o.find("adjust-volume") is not None and o.get("start") != "0s"
                     and parse_rational(o.get("duration")) == Fraction(3, 2))
        # B is 30000/1001: 1.0 s floors to frame 29
        self.assertEqual(parse_rational(first.get("start")), Fraction(29 * 1001, 30000))
        self.assertEqual([k.tag for k in first], ["adjust-transform", "adjust-volume"])
        self.assertEqual(first.find("adjust-transform").get("rotation"), "180")
        self.assertEqual(first.find("adjust-volume").get("amount"), "-96dB")
        ov = plan.overlays[0] if plan.overlays[0]["overlay_index"] == 0 else \
            next(o for o in plan.overlays if o["overlay_index"] == 0)
        self.assertTrue(ov["muted"])
        self.assertEqual(ov["rotation"], 180.0)

    def test_broll_offset_is_in_parent_source_time(self):
        root, plan, _ = self.build()
        ov = next(o for o in plan.overlays if o["overlay_index"] == 0)
        self.assertEqual(ov["clip_index"], 4)          # last A clip: out 7.5–9.7, src 5.0–7.2
        # 7.6 out → 0.1 s into the clip → A source 5.1 s
        self.assertAlmostEqual(float(parse_rational(ov["offset_in_clip"])), 5.1, places=6)

    def test_overrides(self):
        root, plan, _ = self.build()
        ov = next(o for o in plan.overlays if o["overlay_index"] == 1)
        self.assertFalse(ov["muted"])
        self.assertEqual(ov["rotation"], 0.0)
        el = next(o for o in self.overlays(root) if o.get("start") == "0s")
        self.assertEqual(len(list(el)), 0)

    def test_duration_clamped_to_media(self):
        _, plan, _ = self.build()
        ov = next(o for o in plan.overlays if o["overlay_index"] == 2)
        self.assertLessEqual(ov["duration_s"], 1.0 + 1e-9)
        self.assertTrue(any("clamped to the media" in n for n in plan.notes))

    def test_unknown_source_raises(self):
        self.edl["overlays"] = [{"source": "Z", "start_in_output": 0.5, "duration": 1}]
        with self.assertRaises(KeyError):
            self.build()


class GapAndCardTest(Round3Base):
    def test_spine_shape(self):
        root, plan, _ = self.build()
        tags = [c.tag for c in self.spine(root)]
        self.assertEqual(tags, ["asset-clip", "gap", "video", "asset-clip", "asset-clip"])
        gap = self.spine(root)[1]
        self.assertEqual(gap.get("start"), "3600s")
        self.assertEqual(parse_rational(gap.get("offset")), Fraction(2))
        self.assertEqual(parse_rational(gap.get("duration")), Fraction(1))
        self.assertEqual([c["kind"] for c in plan.clips], ["media", "gap", "card", "media", "media"])
        self.assertFalse(plan.clips[1]["has_audio"])
        self.assertAlmostEqual(plan.total_duration_s, 2.0 + 1.0 + 2.0 + 2.5 + 2.2, places=6)

    def test_card_still_and_titles(self):
        root, plan, _ = self.build()
        card = self.spine(root)[2]
        still = next(a for a in root.iter("asset") if a.get("id") == card.get("ref"))
        fmt = next(f for f in root.iter("format") if f.get("id") == still.get("format"))
        self.assertEqual(fmt.get("name"), "FFVideoFormatRateUndefined")
        self.assertEqual((fmt.get("width"), fmt.get("height")), ("1920", "1080"))
        self.assertEqual(self.still_calls, [("card_white_1920x1080.png", 1920, 1080, "white")])
        titles = card.findall("title")
        self.assertEqual([t.get("lane") for t in titles], ["1", "2"])
        self.assertEqual([t.find("text/text-style").text for t in titles],
                         ["LIBRO CERO:", "NADIE TIENE UNA TRAYECTORIA LINEAL"])
        styles = [t.find("text-style-def/text-style") for t in titles]
        self.assertEqual({s.get("font") for s in styles}, {"Herculanum"})
        self.assertEqual([s.get("fontSize") for s in styles], ["70", "110"])
        self.assertEqual({s.get("fontColor") for s in styles}, {"0 0 0 1"})
        self.assertIsNone(styles[0].get("strokeColor"))
        self.assertEqual(titles[0].find("adjust-transform").get("position"), "0 4.5")   # spread default
        self.assertEqual(titles[1].find("adjust-transform").get("position"), "0 -6")
        self.assertEqual(titles[0].get("offset"), "3600s")
        self.assertEqual(plan.markers[2]["value"], "TITLE")

    def test_card_validation(self):
        self.edl["ranges"][2] = {"card": {"lines": []}}
        with self.assertRaises(ValueError):
            self.build()

    def test_gap_only_edl_rejected(self):
        self.edl["ranges"] = [{"gap": 1.0}]
        with self.assertRaises(ValueError):
            self.build()

    def test_dtd_and_semantic_validation(self):
        xml_path, _ = builder.write_outputs(self.edl, self.edit_dir, self.opt)
        errs = validate(xml_path)
        self.assertEqual([e for e in errs if "xmllint not found" not in e], [])

    def test_captions_skip_gap_and_card_and_keep_srt_parity(self):
        xml_path, plan_path = builder.write_outputs(self.edl, self.edit_dir, self.opt)
        plan = json.loads(plan_path.read_text())
        self.assertNotIn(1, {c["clip_index"] for c in plan["captions"]})
        self.assertNotIn(2, {c["clip_index"] for c in plan["captions"]})
        for c in plan["captions"]:
            self.assertAlmostEqual(c["out_start_s"], c["srt_start_s"], delta=1 / 30 + 1e-3)
        after = [c for c in plan["captions"] if c["clip_index"] == 3]
        self.assertTrue(after and after[0]["srt_start_s"] >= 5.0)
        # the round-trip analysis of our own XML: card titles are not counted as captions
        rt = fcp_verify.analyse_roundtrip(xml_path, plan)
        self.assertEqual(rt["problems"], [])
        parity = fcp_verify.captions_parity(rt["_captions"], self.edit_dir / "master.srt", 1 / 30 + 1e-3)
        self.assertEqual(parity["mismatches"], [])

    def test_reconcile_tolerates_gap_missing_from_state(self):
        _, plan, _ = self.build()
        plan_d = {"fps": plan.fps, "total_duration_s": plan.total_duration_s, "clips": plan.clips}

        def t(s):
            v = round(s * 30)
            return {"value": v, "seconds": v / 30, "timescale": 30}
        items = [{"name": c["beat"], "lane": 0, "class": "X", "startTime": t(c["out_start_s"]),
                  "duration": t(c["duration_s"])} for c in plan.clips if c["kind"] != "gap"]
        r = fcp_import.reconcile({"items": items, "duration": t(plan.total_duration_s)}, plan_d)
        self.assertTrue(r["ok"], r["problems"])


class RenderRangeTest(unittest.TestCase):
    def test_range_kind_and_duration(self):
        r = render_mod()
        self.assertEqual(r.range_kind({"gap": 1}), "gap")
        self.assertEqual(r.range_duration({"gap": 1.5}), 1.5)
        self.assertEqual(r.range_duration({"card": {}}), r.DEFAULT_CARD_S)
        self.assertEqual(r.range_duration({"source": "A", "start": 1, "end": 3.5}), 2.5)


class TitleParamsTest(unittest.TestCase):
    def test_defaults_unchanged(self):
        el = ET.fromstring(title_xml("HI", "1s", "1s", "t1", "r9"))
        st = el.find("text-style-def/text-style")
        self.assertEqual((st.get("font"), st.get("fontSize"), st.get("fontColor")), ("Helvetica", "54", "1 1 1 1"))
        self.assertEqual(el.find("adjust-transform").get("position"), "0 -19")

    def test_font_size_color_position(self):
        el = ET.fromstring(title_xml("HI", "1s", "1s", "t1", "r9", font="Herculanum", size=96,
                                     color="0 0 0 1", position_y=12.5))
        st = el.find("text-style-def/text-style")
        self.assertEqual((st.get("font"), st.get("fontSize"), st.get("fontColor")), ("Herculanum", "96", "0 0 0 1"))
        self.assertEqual(el.find("adjust-transform").get("position"), "0 12.5")
        self.assertEqual(st.get("strokeColor"), "0 0 0 0.5")    # rest of the style kept


if __name__ == "__main__":
    unittest.main()
