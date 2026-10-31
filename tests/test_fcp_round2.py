"""Round-2 FCP features: caption locale, Camera LUT, start timecode, captions=both, contact sheet."""
from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import _fcp_fixture as fx  # puts helpers/ and helpers/fcp on sys.path

import edl_to_fcpxml as builder  # noqa: E402
import fcp_verify  # noqa: E402
from probe import LOG_MODES, SourceInfo, lut_override, parse_ffprobe, timecode_to_frames  # noqa: E402
from fractions import Fraction  # noqa: E402


class CaptionLocaleTest(unittest.TestCase):
    def test_bare_codes_get_a_region(self):
        # FCP 12.4 rewrites bare ITT.es / ITT.fr to ITT.en on import, keeps es-MX
        self.assertEqual(builder.caption_locale("es"), "es-MX")
        self.assertEqual(builder.caption_locale("fr"), "fr-FR")

    def test_region_passes_through(self):
        self.assertEqual(builder.caption_locale("es-419"), "es-419")
        self.assertEqual(builder.caption_locale("es_AR"), "es-AR")

    def test_unknown_bare_code_unchanged(self):
        self.assertEqual(builder.caption_locale("eu"), "eu")


class LogProfileTest(unittest.TestCase):
    def _info(self, **kw):
        base = dict(width=3840, height=2160, fps="30/1", duration=5.0, has_video=True)
        base.update(kw)
        return SourceInfo(path=Path("x.mov"), **base)

    def test_apple_log_detected(self):
        i = self._info(make="Apple", color_primaries="bt2020", color_transfer="unknown")
        self.assertEqual(i.log_profile, "Apple_Log")
        self.assertEqual(lut_override(i.log_profile), "112 (Apple_Log)")

    def test_iphone_hlg_is_not_log(self):
        i = self._info(make="Apple", color_primaries="bt2020", color_transfer="arib-std-b67")
        self.assertIsNone(i.log_profile)

    def test_non_apple_unknown_transfer_is_not_assumed(self):
        i = self._info(make="", color_primaries="bt2020", color_transfer="unknown")
        self.assertIsNone(i.log_profile)

    def test_lut_override_table(self):
        self.assertEqual(LOG_MODES["Panasonic_VLog_VGamut"], 64)   # the DTD's own example
        self.assertEqual(lut_override("12 (Custom)"), "12 (Custom)")
        with self.assertRaises(KeyError):
            lut_override("Nope_Log")

    def test_make_and_timecode_parsed(self):
        data = {"format": {"duration": "9.7", "tags": {"com.apple.quicktime.make": "Apple"}},
                "streams": [{"codec_type": "video", "width": 3840, "height": 2160, "avg_frame_rate": "30/1",
                             "color_primaries": "bt2020", "tags": {"timecode": "14:02:48:02"}}]}
        i = parse_ffprobe(Path("t.mov"), data)
        self.assertEqual((i.make, i.timecode, i.tc_start_frames), ("Apple", "14:02:48:02", 1517042))

    def test_drop_frame_timecode(self):
        self.assertEqual(timecode_to_frames("00:01:00;02", Fraction(30000, 1001)), 1800)
        self.assertEqual(timecode_to_frames("00:10:00;00", Fraction(30000, 1001)), 17982)


def _probe_with(extra: dict):
    def probe(path):
        info = fx.fake_probe(path)
        for k, v in extra.get(Path(path).name, {}).items():
            setattr(info, k, v)
        return info
    return probe


class BuilderRound2Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.edl, self.edit_dir = fx.make_project(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, edl=None, **opt):
        xml, plan = builder.build(edl or self.edl, self.edit_dir,
                                  builder.BuildOptions(probe_fn=opt.pop("probe_fn", fx.fake_probe), **opt))
        return ET.fromstring(xml), plan

    def test_default_captions_is_both(self):
        root, plan = self.build()
        self.assertEqual(plan.caption_mode, "both")
        self.assertEqual(len(root.findall(".//caption")), len(plan.captions))
        self.assertEqual(len(root.findall(".//title")), len(plan.captions))
        # titles sit above the overlay lane (Hard Rule 1)
        self.assertTrue(all(t.get("lane") == "2" for t in root.findall(".//title")))

    def test_edl_block_selects_caption_mode(self):
        edl = {**self.edl, "fcp": {"captions": "native"}}
        root, plan = self.build(edl)
        self.assertEqual(plan.caption_mode, "native")
        self.assertEqual(root.findall(".//title"), [])

    def test_caption_role_has_region(self):
        root, _ = self.build(captions="native", lang="es")
        roles = {c.get("role") for c in root.findall(".//caption")}
        self.assertEqual(roles, {"iTT?captionFormat=ITT.es-MX"})

    def test_start_timecode_shifts_source_times(self):
        from timing import parse_rational
        root, plan = self.build(captions="none",
                                probe_fn=_probe_with({fx.A_NAME: {"timecode": "01:00:00:00"}}))
        a_asset = next(a for a in root.iter("asset") if a.get("name") == fx.A_NAME)
        self.assertEqual(parse_rational(a_asset.get("start")), 3600)
        first = root.find(".//spine/asset-clip")          # range 0 is source A
        clip_src = parse_rational(first.get("start")) - 3600
        self.assertEqual(float(clip_src), plan.clips[0]["src_start_s"])
        self.assertEqual(first.find("marker").get("start"), first.get("start"))
        self.assertEqual(plan.clips[0]["tc_start_s"], 3600.0)

    def test_camera_lut_auto_and_override(self):
        log = {"make": "Apple", "color_primaries": "bt2020", "color_transfer": "unknown"}
        root, plan = self.build(captions="none", probe_fn=_probe_with({fx.A_NAME: log}))
        a_asset = next(a for a in root.iter("asset") if a.get("name") == fx.A_NAME)
        self.assertEqual(a_asset.get("customLUTOverride"), "112 (Apple_Log)")
        edl = {**self.edl, "fcp": {"camera_lut": "none"}}
        root, _ = self.build(edl, captions="none", probe_fn=_probe_with({fx.A_NAME: log}))
        a_asset = next(a for a in root.iter("asset") if a.get("name") == fx.A_NAME)
        self.assertIsNone(a_asset.get("customLUTOverride"))

    def test_rotated_source_declares_display_size(self):
        root, _ = self.build(captions="none", probe_fn=_probe_with({fx.B_NAME: {"rotation": 90}}))
        b_asset = next(a for a in root.iter("asset") if a.get("name") == fx.B_NAME)
        fmt = next(f for f in root.iter("format") if f.get("id") == b_asset.get("format"))
        self.assertEqual((fmt.get("width"), fmt.get("height")), ("2160", "3840"))


class ContactSheetTest(unittest.TestCase):
    def test_grid_cells(self):
        self.assertEqual([fcp_verify._cell(i, 2) for i in range(4)], ["0_0", "w0_0", "0_h0", "w0_h0"])
        self.assertEqual(fcp_verify._cell(5, 2), "w0_h0+h0")


if __name__ == "__main__":
    unittest.main()
