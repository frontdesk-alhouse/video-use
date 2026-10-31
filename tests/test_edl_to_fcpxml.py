"""helpers/fcp/edl_to_fcpxml.py: EDL → FCPXML with a fake probe (no media, no FCP)."""
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from urllib.parse import unquote, urlparse

import _fcp_fixture as fx  # also puts helpers/ and helpers/fcp on sys.path
from edl_to_fcpxml import BuildOptions, build, write_outputs  # noqa: E402
from timing import parse_rational  # noqa: E402
from validate import validate  # noqa: E402

XMLLINT_MISSING = "xmllint not found"
FRAME = 1 / 30


def anchored(clip):
    return [c for c in clip if c.tag in {"asset-clip", "caption", "title"}]


class BuilderTestBase(unittest.TestCase):
    grade = "warm_cinematic"
    captions = "native"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.edl, self.edit_dir = fx.make_project(self.tmp, grade=self.grade)

    def build(self, edl=None, **kw):
        kw.setdefault("captions", self.captions)
        opt = BuildOptions(probe_fn=fx.fake_probe, event="E", project="P", **kw)
        xml, plan = build(edl or self.edl, self.edit_dir, opt)
        return xml, plan, ET.fromstring(xml)

    def spine(self, root):
        return list(root.find(".//sequence/spine"))


class StructureTest(BuilderTestBase):
    def setUp(self):
        super().setUp()
        self.xml, self.plan, self.root = self.build()

    def test_well_formed_and_valid(self):
        path = self.tmp / "project.fcpxml"
        path.write_text(self.xml, encoding="utf-8")
        errors = validate(path, allow_missing_media=True)
        errors = [e for e in errors if not e.startswith(XMLLINT_MISSING)]
        self.assertEqual(errors, [])

    def test_spine_contiguous_and_sums_to_sequence(self):
        clips = self.spine(self.root)
        self.assertEqual([c.tag for c in clips], ["asset-clip"] * 3)
        cursor = Fraction(0)
        for c in clips:
            self.assertEqual(parse_rational(c.get("offset")), cursor)
            cursor += parse_rational(c.get("duration"))
        seq = self.root.find(".//sequence")
        self.assertEqual(parse_rational(seq.get("duration")), cursor)

    def test_child_order_in_first_clip(self):
        first = self.spine(self.root)[0]
        tags = [c.tag for c in first]
        self.assertEqual(tags[0], "adjust-volume")
        self.assertEqual(tags[-2:], ["marker", "filter-video"])
        middle = tags[1:-2]
        self.assertEqual(middle[0], "asset-clip")
        self.assertTrue(middle[1:] and all(t == "caption" for t in middle[1:]))
        self.assertEqual(first[1].get("lane"), "1")

    def test_overlay_offset_on_source_grid(self):
        first = self.spine(self.root)[0]
        ov = first.find("asset-clip")
        clip_start = parse_rational(first.get("start"))
        clip_out = parse_rational(first.get("offset"))
        self.assertEqual(parse_rational(ov.get("offset")), clip_start + (Fraction(1, 2) - clip_out))
        self.assertEqual(parse_rational(ov.get("duration")), 3)
        self.assertEqual(len(self.plan.overlays), 1)
        self.assertEqual(self.plan.overlays[0]["clip_index"], 0)

    def test_caption_names_and_no_overlap(self):
        expected = [c["text"] for c in self.plan.captions]
        self.assertEqual(expected, ["HELLO THERE", "FRIENDS.", "THIS IS IT.",
                                    "SECOND SOURCE", "TALKS HERE.", "YES INDEED.",
                                    "BACK AGAIN", "WITH MORE", "NEWS", "BYE."])
        got = []
        for clip in self.spine(self.root):
            caps = clip.findall("caption")
            spans = []
            for cap in caps:
                self.assertEqual(cap.get("name"), cap.get("name").upper())
                self.assertEqual("".join(cap.find("text").itertext()), cap.get("name"))
                self.assertTrue(cap.get("role").startswith("iTT?captionFormat=ITT.en"))
                off = parse_rational(cap.get("offset"))
                spans.append((off, off + parse_rational(cap.get("duration"))))
                got.append(cap.get("name"))
            spans.sort()
            for (_, a1), (b0, _) in zip(spans, spans[1:]):
                self.assertLessEqual(a1, b0)
        self.assertEqual(got, expected)

    def test_markers_are_beats_in_order(self):
        values = [m.get("value") for c in self.spine(self.root) for m in c.findall("marker")]
        self.assertEqual(values, ["HOOK", "PROBLEM", "PAYOFF"])
        self.assertEqual([m["value"] for m in self.plan.markers], values)
        first_marker = self.spine(self.root)[0].find("marker")
        self.assertEqual(first_marker.get("note"), "opens strong")

    def test_total_duration_within_one_frame(self):
        self.assertLessEqual(abs(self.plan.total_duration_s - self.edl["total_duration_s"]), FRAME + 1e-9)
        self.assertEqual(self.plan.notes, [])

    def test_media_rep_percent_encoded(self):
        reps = self.root.findall(".//media-rep")
        self.assertEqual(len(reps), 3)
        for rep in reps:
            src = rep.get("src")
            self.assertTrue(src.startswith("file:///"), src)
            self.assertNotIn(" ", src)
            self.assertIn("%20", src)
            self.assertTrue(Path(unquote(urlparse(src).path)).exists())

    def test_ntsc_source_in_30fps_sequence(self):
        b = self.spine(self.root)[1]
        start = parse_rational(b.get("start"))
        self.assertEqual(b.get("start"), "59059/30000s")      # floor(2.0 / (1001/30000)) = 59 frames
        self.assertEqual((start / Fraction(1001, 30000)).denominator, 1)
        dur = parse_rational(b.get("duration"))
        self.assertEqual((dur / Fraction(1, 30)).denominator, 1)  # sequence grid
        self.assertEqual(dur, Fraction(5, 2))
        fmt_id = b.get("format")
        fmt = self.root.find(f".//format[@id='{fmt_id}']")
        self.assertEqual(fmt.get("frameDuration"), "1001/30000s")
        self.assertEqual((fmt.get("width"), fmt.get("height")), ("3840", "2160"))

    def test_fades_on_every_audio_clip(self):
        for clip in self.spine(self.root):
            self.assertIsNotNone(clip.find("./adjust-volume/param/fadeIn"))
            self.assertIsNotNone(clip.find("./adjust-volume/param/fadeOut"))


class GradeTest(BuilderTestBase):
    def effects(self, root, name):
        return root.findall(f".//resources/effect[@name='{name}']")

    def test_warm_cinematic(self):
        _, plan, root = self.build()
        self.assertEqual(len(self.effects(root, "Color Adjustments")), 1)
        for clip in self.spine(root):
            fv = clip.findall("filter-video")
            self.assertEqual(len(fv), 1)
            vals = {p.get("name"): p.get("value") for p in fv[0].findall("param")}
            self.assertEqual(vals["Contrast"], "12")
            self.assertEqual(vals["Saturation"], "-12")
        self.assertEqual(plan.grade["params"]["Contrast"], 12)

    def test_none(self):
        edl = dict(self.edl, grade="none")
        _, plan, root = self.build(edl)
        self.assertEqual(self.effects(root, "Color Adjustments"), [])
        self.assertEqual(root.findall(".//filter-video"), [])

    def test_raw_ffmpeg_filter(self):
        edl = dict(self.edl, grade="eq=contrast=1.05")
        _, plan, root = self.build(edl)
        self.assertEqual(len(self.effects(root, "Color Adjustments")), 1)
        for clip in self.spine(root):
            vals = {p.get("name"): p.get("value") for p in clip.find("filter-video").findall("param")}
            self.assertEqual(vals["Contrast"], "5")
            self.assertNotIn("Saturation", vals)


class CaptionModesTest(BuilderTestBase):
    def test_titles(self):
        xml, plan, root = self.build(captions="titles")
        effect = root.findall(".//resources/effect[@name='Basic Title']")
        self.assertEqual(len(effect), 1)
        titles = root.findall(".//spine/asset-clip/title")
        self.assertEqual(len(titles), len(plan.captions))
        self.assertTrue(titles)
        for t in titles:
            self.assertEqual(t.get("ref"), effect[0].get("id"))
        self.assertEqual(root.findall(".//caption"), [])
        path = self.tmp / "titles.fcpxml"
        path.write_text(xml)
        errors = [e for e in validate(path, allow_missing_media=True) if not e.startswith(XMLLINT_MISSING)]
        self.assertEqual(errors, [])

    def test_none(self):
        _, plan, root = self.build(captions="none")
        self.assertEqual(root.findall(".//caption") + root.findall(".//title"), [])
        self.assertEqual(plan.captions, [])
        opt = BuildOptions(probe_fn=fx.fake_probe, captions="none", event="E", project="P")
        write_outputs(self.edl, self.edit_dir, opt)
        self.assertFalse((self.edit_dir / "master.srt").exists())
        self.assertTrue((self.edit_dir / "fcp" / "project.fcpxml").exists())

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            self.build(captions="burned")


class ClampAndOutputsTest(BuilderTestBase):
    def test_range_past_source_end_is_clamped(self):
        edl = json.loads(json.dumps(self.edl))
        edl["ranges"][1]["end"] = 9.0          # B is 6.0 s long
        _, plan, root = self.build(edl)
        self.assertTrue(any("range 1" in n and "clamped" in n for n in plan.notes), plan.notes)
        b = self.spine(root)[1]
        src_end = parse_rational(b.get("start")) + parse_rational(b.get("duration"))
        self.assertLessEqual(src_end, Fraction(6))
        asset = root.find(f".//asset[@id='{b.get('ref')}']")
        self.assertLessEqual(src_end, parse_rational(asset.get("duration")))
        self.assertEqual(plan.clips[1]["edl_end_s"], 6.0)

    def test_write_outputs_locations(self):
        opt = BuildOptions(probe_fn=fx.fake_probe, event="E", project="P")
        xml_path, plan_path = write_outputs(self.edl, self.edit_dir, opt)
        self.assertEqual(xml_path, self.edit_dir / "fcp" / "project.fcpxml")
        self.assertEqual(plan_path, self.edit_dir / "fcp" / "plan.json")
        self.assertTrue(xml_path.exists())
        plan = json.loads(plan_path.read_text())
        self.assertEqual(len(plan["clips"]), 3)
        self.assertEqual(plan["project"], "P")
        srt = self.edit_dir / "master.srt"
        self.assertTrue(srt.exists())
        self.assertIn("HELLO THERE", srt.read_text())

    def test_missing_source_raises(self):
        edl = json.loads(json.dumps(self.edl))
        edl["sources"]["A"] = str(self.tmp / "nope.mp4")
        with self.assertRaises(FileNotFoundError):
            self.build(edl)


if __name__ == "__main__":
    unittest.main()
