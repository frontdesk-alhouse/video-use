"""FCP caption cues (captions.cues_for_range via plan.json) == render.build_master_srt."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import _fcp_fixture as fx  # puts helpers/ and helpers/fcp on sys.path
from captions import cue_text, cues_for_range  # noqa: E402
from edl_to_fcpxml import BuildOptions, write_outputs  # noqa: E402
from fcp_verify import parse_srt  # noqa: E402

HELPERS = Path(__file__).parents[1] / "helpers"


def load_render():
    # same loader captions.py uses
    spec = importlib.util.spec_from_file_location("vu_render", HELPERS / "render.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("vu_render", mod)
    spec.loader.exec_module(mod)
    return mod


render = load_render()


class CaptionParityTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.edl, self.edit_dir = fx.make_project(Path(self._tmp.name))

    def test_plan_captions_match_master_srt(self):
        opt = BuildOptions(probe_fn=fx.fake_probe, event="E", project="P")
        _, plan_path = write_outputs(self.edl, self.edit_dir, opt)
        plan = json.loads(plan_path.read_text())

        srt_path = self.edit_dir / "reference.srt"
        render.build_master_srt(self.edl, self.edit_dir, srt_path)
        srt = parse_srt(srt_path)

        self.assertEqual([c["text"] for c in plan["captions"]], [t for _, _, t in srt])
        for cap, (a, _b, text) in zip(plan["captions"], srt):
            with self.subTest(text=text):
                self.assertAlmostEqual(cap["srt_start_s"], a, delta=1e-3 + 1e-9)
        # write_outputs's master.srt is the same file
        self.assertEqual(parse_srt(self.edit_dir / "master.srt"), srt)

    def test_cues_for_range_uses_render_chunking(self):
        tr = json.loads((self.edit_dir / "transcripts" / "A.json").read_text())
        cues = cues_for_range(tr, 1.0, 3.0, 10.0)
        chunks = render.chunk_words(render._words_in_range(tr, 1.0, 3.0))
        self.assertEqual([c.text for c in cues], [cue_text(ch) for ch in chunks])
        self.assertAlmostEqual(cues[0].out_start, 10.1)
        self.assertAlmostEqual(cues[0].src_start, 1.1)

    def test_cue_text_normalisation(self):
        chunk = [{"text": " news; "}, {"text": "and  more,"}]
        self.assertEqual(cue_text(chunk), "NEWS; AND MORE")


if __name__ == "__main__":
    unittest.main()
