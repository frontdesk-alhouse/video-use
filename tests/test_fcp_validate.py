"""helpers/fcp/validate.py: semantic checks the DTD cannot express."""
import sys
import tempfile
import unittest
from pathlib import Path

FCP = Path(__file__).parents[1] / "helpers" / "fcp"
if str(FCP) not in sys.path:
    sys.path.insert(0, str(FCP))

from validate import semantic_checks, validate  # noqa: E402

TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.11">
<resources>
<format id="r1" frameDuration="1/30s" width="1920" height="1080"/>
<asset id="r2" name="a" start="0s" duration="10s" hasVideo="1" format="r1"><media-rep kind="original-media" src="{src}"/></asset>
</resources>
<library><event name="E"><project name="P">
<sequence format="r1" duration="{seq_dur}">
<spine>
<asset-clip ref="r2" offset="0s" start="0s" duration="2s" format="r1">
<caption lane="1" offset="0s" name="ONE" duration="1s"><text><text-style ref="ts1">ONE</text-style></text><text-style-def id="ts1"><text-style font="Helvetica"/></text-style-def></caption>
<caption lane="1" offset="{cap2}" name="TWO" duration="1s"><text><text-style ref="ts2">TWO</text-style></text><text-style-def id="ts2"><text-style font="Helvetica"/></text-style-def></caption>
</asset-clip>
<asset-clip ref="{ref2}" offset="{off2}" start="2s" duration="3s" format="r1"/>
</spine>
</sequence>
</project></event></library>
</fcpxml>
"""


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.media = self.tmp / "media file.mov"
        self.media.touch()

    def write(self, **kw):
        vals = {"src": self.media.as_uri(), "seq_dur": "5s", "cap2": "1s", "ref2": "r2", "off2": "2s"}
        vals.update(kw)
        p = self.tmp / "t.fcpxml"
        p.write_text(TEMPLATE.format(**vals))
        return p

    def test_clean(self):
        self.assertEqual(semantic_checks(self.write()), [])
        self.assertEqual(validate(self.write(), dtd=False), [])

    def test_spine_gap(self):
        errs = semantic_checks(self.write(off2="3s", seq_dur="6s"))
        self.assertEqual(len(errs), 1)
        self.assertIn("gap or overlap", errs[0])

    def test_wrong_sequence_duration(self):
        errs = semantic_checks(self.write(seq_dur="6s"))
        self.assertEqual(len(errs), 1)
        self.assertIn("sequence duration", errs[0])

    def test_unknown_ref(self):
        errs = semantic_checks(self.write(ref2="r99"))
        self.assertTrue(any("unknown resource 'r99'" in e for e in errs), errs)

    def test_missing_media(self):
        missing = (self.tmp / "gone clip.mov").as_uri()
        errs = semantic_checks(self.write(src=missing))
        self.assertEqual(len(errs), 1)
        self.assertIn("does not exist", errs[0])
        self.assertIn("gone clip.mov", errs[0])          # decoded, not %20
        self.assertEqual(semantic_checks(self.write(src=missing), allow_missing_media=True), [])

    def test_allow_missing_media_does_not_silence_other_errors(self):
        missing = (self.tmp / "gone.mov").as_uri()
        errs = semantic_checks(self.write(src=missing, seq_dur="9s"), allow_missing_media=True)
        self.assertEqual(len(errs), 1)
        self.assertIn("sequence duration", errs[0])

    def test_caption_overlap_same_lane(self):
        errs = semantic_checks(self.write(cap2="1/2s"))
        self.assertEqual(len(errs), 1)
        self.assertIn("overlap", errs[0])
        self.assertIn("lane 1", errs[0])

    def test_not_well_formed(self):
        p = self.tmp / "bad.fcpxml"
        p.write_text("<fcpxml><resources></fcpxml>")
        errs = validate(p, dtd=False)
        self.assertEqual(len(errs), 1)
        self.assertIn("not well-formed", errs[0])


if __name__ == "__main__":
    unittest.main()
