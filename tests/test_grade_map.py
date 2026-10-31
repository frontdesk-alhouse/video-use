"""helpers/fcp/grade_map.py: ffmpeg grade strings → Color Adjustments params."""
import importlib.util
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

HELPERS = Path(__file__).parents[1] / "helpers"
if str(HELPERS / "fcp") not in sys.path:
    sys.path.insert(0, str(HELPERS / "fcp"))

from grade_map import PARAM_KEYS, effect_resource_xml, ffmpeg_filter_to_params, filter_video_xml  # noqa: E402

SPEC = importlib.util.spec_from_file_location("video_use_grade", HELPERS / "grade.py")
assert SPEC and SPEC.loader
grade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(grade)

EXPECTED = {
    "subtle": {"Contrast": 3, "Saturation": -2},
    "neutral_punch": {"Contrast": 6, "Shadows": -2, "Highlights": 2},
    "warm_cinematic": {
        "Contrast": 12, "Saturation": -12, "Brightness": -2,
        "Shadows Warmth": 5, "Midtones Warmth": 6, "Midtones Tint": 1,
        "Highlights Warmth": 13, "Highlights Tint": 2, "Shadows": -3, "Highlights": 3,
    },
    "none": {},
}


class PresetMappingTest(unittest.TestCase):
    def test_every_preset_has_an_expectation(self):
        self.assertEqual(set(grade.PRESETS), set(EXPECTED))

    def test_presets(self):
        for name, flt in grade.PRESETS.items():
            with self.subTest(preset=name):
                params, unmapped = ffmpeg_filter_to_params(flt)
                self.assertEqual(unmapped, [])
                self.assertEqual(set(params), set(EXPECTED[name]))
                for k, v in EXPECTED[name].items():
                    self.assertAlmostEqual(params[k], v, places=6, msg=k)


class ParserTest(unittest.TestCase):
    def test_gamma_maps_to_brightness(self):
        params, _ = ffmpeg_filter_to_params("eq=gamma=1.10")
        self.assertAlmostEqual(params["Brightness"], 5.0)

    def test_contrast_only(self):
        self.assertEqual(ffmpeg_filter_to_params("eq=contrast=1.05")[0], {"Contrast": 5.0})

    def test_unknown_filter_and_eq_key_reported(self):
        params, unmapped = ffmpeg_filter_to_params("eq=contrast=1.1:gamma_r=1.2,hue=h=10,unsharp")
        self.assertAlmostEqual(params["Contrast"], 10.0)
        self.assertIn("hue", unmapped)
        self.assertIn("unsharp", unmapped)
        self.assertIn("eq:gamma_r", unmapped)

    def test_clamping(self):
        params, _ = ffmpeg_filter_to_params("eq=contrast=3.5:saturation=-1.5")
        self.assertEqual(params["Contrast"], 100)
        self.assertEqual(params["Saturation"], -100)

    def test_tiny_values_dropped_and_empty(self):
        self.assertEqual(ffmpeg_filter_to_params("eq=contrast=1.0001"), ({}, []))
        self.assertEqual(ffmpeg_filter_to_params(""), ({}, []))


class XmlTest(unittest.TestCase):
    def test_filter_video_xml(self):
        params = {"Contrast": 12, "Saturation": -12, "Shadows Warmth": 5.5}
        el = ET.fromstring(filter_video_xml("r9", params))
        self.assertEqual(el.tag, "filter-video")
        self.assertEqual(el.get("ref"), "r9")
        self.assertEqual(el.get("name"), "Color Adjustments")
        ps = el.findall("param")
        self.assertEqual((ps[0].get("name"), ps[0].get("key"), ps[0].get("value")),
                         ("Control Range", "19", "0 (SDR)"))
        got = {p.get("name"): (p.get("key"), p.get("value")) for p in ps[1:]}
        self.assertEqual(got, {"Contrast": ("17", "12"), "Saturation": ("16", "-12"),
                               "Shadows Warmth": ("14", "5.5")})
        for p in ps[1:]:
            self.assertEqual(int(p.get("key")), PARAM_KEYS[p.get("name")])

    def test_hdr_control_range(self):
        el = ET.fromstring(filter_video_xml("r1", {"Contrast": 1}, sdr=False))
        self.assertEqual(el.find("param").get("value"), "1 (HDR)")

    def test_effect_resource(self):
        el = ET.fromstring(effect_resource_xml("r3"))
        self.assertEqual((el.tag, el.get("id"), el.get("name")), ("effect", "r3", "Color Adjustments"))
        self.assertTrue(el.get("uid").startswith("FxPlug:"))


if __name__ == "__main__":
    unittest.main()
