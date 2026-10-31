"""helpers/fcp/fcp_verify.py: SRT parsing, round-trip XML analysis, captions parity (offline)."""
import sys
import tempfile
import unittest
from pathlib import Path

FCP = Path(__file__).parents[1] / "helpers" / "fcp"
if str(FCP) not in sys.path:
    sys.path.insert(0, str(FCP))

from fcp_verify import analyse_roundtrip, captions_parity, parse_srt  # noqa: E402

FADES = ('<adjust-volume amount="0dB"><param name="amount"><fadeIn type="easeIn" duration="1/30s"/>'
         '<fadeOut type="easeOut" duration="1/30s"/></param></adjust-volume>')
GRADE = ('<filter-video ref="r9" name="Color Adjustments"><param name="Control Range" key="19" value="0 (SDR)"/>'
         '<param name="Contrast" key="17" value="12"/></filter-video>')


def caption(text, offset, duration):
    return (f'<caption lane="1" offset="{offset}" name="{text}" duration="{duration}">'
            f'<text><text-style ref="x">{text}</text-style></text></caption>')


def clip(name, offset, start, duration, caps, beat, fades=FADES):
    return (f'<asset-clip ref="r2" name="{name}" offset="{offset}" start="{start}" duration="{duration}">'
            f'{fades}{"".join(caps)}<marker start="{start}" duration="1/30s" value="{beat}"/>{GRADE}</asset-clip>')


def roundtrip_xml(drop_fade_out_on=None):
    clips = [
        clip("A", "0s", "1s", "2s", [caption("HELLO THERE", "11/10s", "7/10s")], "HOOK"),
        clip("B", "2s", "2s", "5/2s", [caption("SECOND SOURCE", "21/10s", "4/5s")], "PROBLEM"),
        clip("C", "9/2s", "5s", "11/5s", [caption("BACK AGAIN", "51/10s", "7/10s")], "PAYOFF"),
    ]
    if drop_fade_out_on is not None:
        clips[drop_fade_out_on] = clips[drop_fade_out_on].replace(
            '<fadeOut type="easeOut" duration="1/30s"/>', "")
    return ('<?xml version="1.0"?><fcpxml version="1.11"><resources/><library><event><project>'
            '<sequence duration="67/10s"><spine>' + "".join(clips) +
            '</spine></sequence></project></event></library></fcpxml>')


PLAN = {
    "fps": "30", "fade_frames": 1, "caption_mode": "native",
    "clips": [{"index": i, "has_audio": True} for i in range(3)],
    "markers": [{"value": v} for v in ("HOOK", "PROBLEM", "PAYOFF")],
    "captions": [{"text": t} for t in ("HELLO THERE", "SECOND SOURCE", "BACK AGAIN")],
    "grade": {"params": {"Contrast": 12}, "auto": False},
}

SRT = """1
00:00:00,100 --> 00:00:00,800
HELLO THERE

2
00:00:02,100 --> 00:00:02,900
SECOND SOURCE

3
00:00:04,600 --> 00:00:05,300
BACK AGAIN
"""


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.srt = self.tmp / "master.srt"
        self.srt.write_text(SRT)

    def rt(self, **kw):
        p = self.tmp / "roundtrip.fcpxml"
        p.write_text(roundtrip_xml(**kw))
        return p

    def test_parse_srt(self):
        cues = parse_srt(self.srt)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[0], (0.1, 0.8, "HELLO THERE"))
        self.assertAlmostEqual(cues[2][0], 4.6)
        multi = self.tmp / "multi.srt"
        multi.write_text("1\n00:01:02.500 --> 00:01:03,000\nTWO\nLINES\n")
        self.assertEqual(parse_srt(multi), [(62.5, 63.0, "TWO LINES")])

    def test_clean_roundtrip(self):
        out = analyse_roundtrip(self.rt(), PLAN)
        self.assertEqual(out["problems"], [])
        self.assertEqual(out["fades"], {"ok": 3, "expected": 3})
        self.assertEqual(out["markers"]["got"], ["HOOK", "PROBLEM", "PAYOFF"])
        self.assertEqual(out["grade_clips"], 3)
        caps = out["_captions"]
        self.assertEqual([c["text"] for c in caps], ["HELLO THERE", "SECOND SOURCE", "BACK AGAIN"])
        self.assertAlmostEqual(caps[0]["out_start_s"], 0.1)
        self.assertAlmostEqual(caps[1]["out_start_s"], 2.1)
        self.assertAlmostEqual(caps[2]["out_start_s"], 4.6)
        self.assertEqual(captions_parity(caps, self.srt, 1 / 30 + 1e-3)["mismatches"], [])

    def test_missing_fade_out_reported(self):
        out = analyse_roundtrip(self.rt(drop_fade_out_on=1), PLAN)
        self.assertEqual(out["fades"], {"ok": 2, "expected": 3})
        self.assertEqual(len(out["problems"]), 1)
        self.assertIn("fadeIn+fadeOut", out["problems"][0])

    def test_marker_and_count_mismatch(self):
        plan = dict(PLAN, markers=[{"value": "HOOK"}, {"value": "X"}, {"value": "PAYOFF"}],
                    clips=PLAN["clips"] + [{"index": 3, "has_audio": False}])
        problems = analyse_roundtrip(self.rt(), plan)["problems"]
        self.assertTrue(any(p.startswith("markers") for p in problems))
        self.assertTrue(any("round-trip spine has 3 clips, plan 4" in p for p in problems))

    def test_captions_parity_text_mismatch(self):
        caps = analyse_roundtrip(self.rt(), PLAN)["_captions"]
        caps[1] = dict(caps[1], text="SECOND SAUCE")
        res = captions_parity(caps, self.srt, 0.05)
        self.assertEqual(len(res["mismatches"]), 1)
        self.assertIn("cue 1", res["mismatches"][0])

    def test_captions_parity_time_tolerance(self):
        caps = analyse_roundtrip(self.rt(), PLAN)["_captions"]
        caps[2] = dict(caps[2], out_start_s=caps[2]["out_start_s"] + 0.02)
        self.assertEqual(captions_parity(caps, self.srt, 1 / 30)["mismatches"], [])
        caps[2] = dict(caps[2], out_start_s=caps[2]["out_start_s"] + 0.1)
        res = captions_parity(caps, self.srt, 1 / 30)
        self.assertEqual(len(res["mismatches"]), 1)
        self.assertIn("FCP start", res["mismatches"][0])

    def test_captions_parity_missing_srt_and_missing_cue(self):
        self.assertEqual(captions_parity([], self.tmp / "none.srt", 0.1)["mismatches"], ["master.srt missing"])
        caps = analyse_roundtrip(self.rt(), PLAN)["_captions"][:2]
        res = captions_parity(caps, self.srt, 0.1)
        self.assertEqual(res["mismatches"], ["srt cue 2 'BACK AGAIN' has no FCP caption"])


if __name__ == "__main__":
    unittest.main()
