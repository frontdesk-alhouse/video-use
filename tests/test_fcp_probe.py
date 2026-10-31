"""helpers/fcp/probe.py: parse_ffprobe on canned ffprobe JSON (no subprocess)."""
import sys
import unittest
from pathlib import Path

FCP = Path(__file__).parents[1] / "helpers" / "fcp"
if str(FCP) not in sys.path:
    sys.path.insert(0, str(FCP))

import probe  # noqa: E402
from probe import parse_ffprobe  # noqa: E402

P = Path("/media/clip.mov")


def video(**kw):
    s = {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1",
         "r_frame_rate": "30/1", "color_primaries": "bt709", "color_transfer": "bt709",
         "color_space": "bt709"}
    s.update(kw)
    return s


def audio(channels=2, rate="48000"):
    return {"codec_type": "audio", "channels": channels, "sample_rate": rate}


def data(*streams, duration="12.5"):
    return {"streams": list(streams), "format": {"duration": duration}}


class ParseFfprobeTest(unittest.TestCase):
    def test_landscape_30_rec709(self):
        i = parse_ffprobe(P, data(video(), audio()))
        self.assertEqual((i.width, i.height, i.fps, i.rotation), (1920, 1080, "30/1", 0))
        self.assertEqual(i.duration, 12.5)
        self.assertTrue(i.has_video and i.has_audio)
        self.assertEqual((i.audio_streams, i.audio_channels, i.audio_rate), (1, 2, 48000))
        self.assertFalse(i.portrait)
        self.assertIsNone(i.hdr)
        self.assertEqual(i.colorspace, "1-1-1 (Rec. 709)")

    def test_portrait_via_rotation(self):
        v = video(side_data_list=[{"side_data_type": "Display Matrix", "rotation": -90}])
        i = parse_ffprobe(P, data(v, audio()))
        self.assertEqual(i.rotation, 270)
        self.assertEqual(i.display_size, (1080, 1920))
        self.assertTrue(i.portrait)
        i2 = parse_ffprobe(P, data(video(side_data_list=[{"rotation": 90}])))
        self.assertEqual(i2.rotation, 90)
        self.assertTrue(i2.portrait)

    def test_hdr_pq(self):
        i = parse_ffprobe(P, data(video(color_primaries="bt2020", color_transfer="smpte2084",
                                        color_space="bt2020nc")))
        self.assertEqual(i.hdr, "PQ")
        self.assertEqual(i.colorspace, "9-16-9 (Rec. 2020 PQ)")

    def test_hdr_hlg(self):
        i = parse_ffprobe(P, data(video(color_primaries="bt2020", color_transfer="arib-std-b67")))
        self.assertEqual(i.hdr, "HLG")
        self.assertEqual(i.colorspace, "9-18-9 (Rec. 2020 HLG)")

    def test_bt2020_sdr(self):
        i = parse_ffprobe(P, data(video(color_primaries="bt2020", color_transfer="bt2020-10")))
        self.assertIsNone(i.hdr)
        self.assertEqual(i.colorspace, "9-1-9 (Rec. 2020)")

    def test_audio_only(self):
        i = parse_ffprobe(P, data(audio(1, "44100"), audio()))
        self.assertFalse(i.has_video)
        self.assertTrue(i.has_audio)
        self.assertEqual((i.audio_streams, i.audio_channels, i.audio_rate), (2, 1, 44100))
        self.assertIsNone(i.fps)
        self.assertEqual(i.colorspace, "1-1-1 (Rec. 709)")

    def test_no_audio(self):
        i = parse_ffprobe(P, data(video()))
        self.assertTrue(i.has_video)
        self.assertFalse(i.has_audio)
        self.assertEqual(i.audio_streams, 0)

    def test_fps_fallbacks_and_ntsc(self):
        i = parse_ffprobe(P, data(video(avg_frame_rate="0/0", r_frame_rate="60000/1001")))
        self.assertEqual(i.fps, "60000/1001")
        i = parse_ffprobe(P, data(video(avg_frame_rate="2997/100")))
        self.assertEqual(i.fps, "2997/100")

    def test_iphone_jittery_average_snaps_to_nominal(self):
        # iPhone takes report avg 3706800/123563 (29.999…) with r_frame_rate 30/1: FCP needs the grid
        i = parse_ffprobe(P, data(video(avg_frame_rate="3706800/123563", r_frame_rate="30/1")))
        self.assertEqual(i.fps, "30/1")
        i = parse_ffprobe(P, data(video(avg_frame_rate="30700/1023", r_frame_rate="30/1")))
        self.assertEqual(i.fps, "30/1")
        # a real variable/converted rate far from nominal keeps the average
        i = parse_ffprobe(P, data(video(avg_frame_rate="25/1", r_frame_rate="30/1")))
        self.assertEqual(i.fps, "25/1")

    def test_duration_from_stream_when_format_missing(self):
        i = parse_ffprobe(P, {"streams": [video(duration="4.25")], "format": {}})
        self.assertEqual(i.duration, 4.25)

    def test_format_name(self):
        from fractions import Fraction
        self.assertEqual(probe.format_name(1920, 1080, Fraction(30)), "FFVideoFormat1080p30")
        self.assertEqual(probe.format_name(3840, 2160, Fraction(30000, 1001)),
                         "FFVideoFormat3840x2160p2997")


if __name__ == "__main__":
    unittest.main()
