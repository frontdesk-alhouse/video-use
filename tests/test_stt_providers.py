"""STT providers: FCP word normalisation, phrase parity with Scribe, provider selection.

Offline: the SpliceKit bridge is a fake newline-delimited JSON-RPC server on an
ephemeral port, and ffprobe/ffmpeg are patched out.
"""
import importlib.util
import json
import os
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HELPERS = Path(__file__).parents[1] / "helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))  # helpers are plain scripts: `stt`, `fcp`, `transcribe`

import stt  # noqa: E402
from fcp.bridge import Bridge  # noqa: E402
from fcp.probe import SourceInfo  # noqa: E402
from stt import fcp as stt_fcp  # noqa: E402
from stt.normalize import fcp_words_to_scribe  # noqa: E402

SPEC = importlib.util.spec_from_file_location("video_use_pack", HELPERS / "pack_transcripts.py")
assert SPEC and SPEC.loader
pack = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pack)


def fw(i, text, start, end, speaker=0):
    return {"index": i, "text": text, "startTime": start, "endTime": end,
            "duration": end - start, "confidence": 0.9, "speaker": speaker}


def sw(text, start, end, speaker="speaker_0"):
    return {"type": "word", "text": text, "start": start, "end": end, "speaker_id": speaker}


def sp(start, end):
    return {"type": "spacing", "text": " ", "start": start, "end": end}


class FcpWordsToScribeTest(unittest.TestCase):
    def test_shape_and_spacing(self):
        out = fcp_words_to_scribe([fw(0, "Hello", 0.0, 0.4), fw(1, "world.", 0.6, 1.0)])
        self.assertEqual(out, [
            sw("Hello", 0.0, 0.4), sp(0.4, 0.6), sw("world.", 0.6, 1.0),
        ])

    def test_no_spacing_when_contiguous(self):
        out = fcp_words_to_scribe([fw(0, "a", 0.0, 0.5), fw(1, "b", 0.5, 0.9)])
        self.assertEqual([w["type"] for w in out], ["word", "word"])

    def test_speaker_normalisation(self):
        words = [fw(0, "a", 0, 1, 2), fw(1, "b", 1, 2, "Speaker 1"),
                 fw(2, "c", 2, 3, "speaker_3"), fw(3, "d", 3, 4, None),
                 fw(4, "e", 4, 5, "Unknown")]
        self.assertEqual([w["speaker_id"] for w in fcp_words_to_scribe(words)],
                         ["speaker_2", "speaker_1", "speaker_3", None, None])

    def test_offset_subtracted(self):
        out = fcp_words_to_scribe([fw(0, "x", 10.25, 10.5), fw(1, "y", 10.75, 11.0)], offset=10.0)
        self.assertEqual([(w["start"], w["end"]) for w in out],
                         [(0.25, 0.5), (0.5, 0.75), (0.75, 1.0)])

    def test_no_audio_events(self):
        out = fcp_words_to_scribe([fw(0, "x", 0, 1), fw(1, "y", 3, 4)])
        self.assertNotIn("audio_event", {w["type"] for w in out})


class PhraseParityTest(unittest.TestCase):
    """Same speech, Scribe-shaped vs FCP-shaped: identical phrases, gap >= 0.5 s breaks."""

    def test_identical_phrases(self):
        scribe_words = [
            sw("We", 0.0, 0.2), sp(0.2, 0.3), sw("ship", 0.3, 0.6), sp(0.6, 0.7),
            sw("today.", 0.7, 1.1), sp(1.1, 1.8),  # 0.7 s pause -> new phrase
            sw("Then", 1.8, 2.0), sp(2.0, 2.1), sw("rest.", 2.1, 2.5),
            sp(2.5, 2.9),  # 0.4 s pause -> same phrase
            sw("Okay.", 2.9, 3.2),
        ]
        fcp_words = [fw(0, "We", 0.0, 0.2), fw(1, "ship", 0.3, 0.6), fw(2, "today.", 0.7, 1.1),
                     fw(3, "Then", 1.8, 2.0), fw(4, "rest.", 2.1, 2.5), fw(5, "Okay.", 2.9, 3.2)]
        a = pack.group_into_phrases(scribe_words)
        b = pack.group_into_phrases(fcp_words_to_scribe(fcp_words))
        self.assertEqual(a, b)
        self.assertEqual([p["text"] for p in b], ["We ship today.", "Then rest. Okay."])


# --- fake SpliceKit bridge ----------------------------------------------------
# Two takes on the inventory timeline: A at [0, 2.0), B at [2.0, 3.5). Words come back in
# timeline seconds, the way Parakeet reports them.

TAKE_A, TAKE_B = 2.0, 1.5
TIMELINE_WORDS = [
    {"index": 0, "text": "Hola", "startTime": 0.1, "endTime": 0.4, "speaker": "Unknown", "confidence": 1},
    {"index": 1, "text": "mundo.", "startTime": 0.5, "endTime": 0.9, "speaker": "Unknown", "confidence": 1},
    {"index": 2, "text": "Otra", "startTime": 2.2, "endTime": 2.5, "speaker": "Unknown", "confidence": 1},
    {"index": 3, "text": "toma.", "startTime": 2.6, "endTime": 3.6, "speaker": "Unknown", "confidence": 1},
]


def _item(name, start, dur):
    return {"name": name, "startTime": {"seconds": start}, "endTime": {"seconds": start + dur},
            "duration": {"seconds": dur}}


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        req = json.loads(line)
        srv = self.server
        method, params = req["method"], req.get("params", {})
        srv.calls.append((method, params))
        if method == "bridge.alive":
            result = {"alive": True}
        elif method == "media.importFile":
            result = {"imported": [{"path": p, "handle": f"obj_{70 + i}", "name": Path(p).stem}
                                   for i, p in enumerate(params["paths"])],
                      "event": "6-10-26"}
        elif method == "fcpxml.import":
            result = {"status": "ok", "importOK": True}
        elif method == "project.open":
            result = {"status": "ok", "name": params["name"]}
        elif method == "browser.appendClip":
            srv.appended.append(params["handle"])
            if len(srv.appended) == 1:  # first take: offset from placementDebug
                result = {"status": "ok", "placementDebug": {
                    "matchedInsertedItem": _item("take_a", 0.0, TAKE_A)}}
            else:  # second take: no placementDebug -> timeline.getDetailedState fallback
                result = {"status": "ok"}
        elif method == "timeline.getDetailedState":
            srv.states += 1
            if getattr(srv, "drop_xml_clips", False) and srv.states == 1:
                result = {"items": []}   # FCP silently dropped the inventory XML's clips
            else:
                result = {"items": [_item("take_a", 0.0, TAKE_A), _item("take_b", TAKE_A, TAKE_B)]}
        elif method == "transcript.setEngine":
            result = {"status": "ok", "engine": params["engine"]}
        elif method == "transcript.getState":
            srv.polls += 1
            result = ({"status": "transcribing", "progress": {"completed": 0, "total": 2}}
                      if srv.polls == 1 else
                      {"status": "ready", "engine": "parakeet", "parakeetModel": "parakeet-tdt-0.6b-v3",
                       "wordCount": len(TIMELINE_WORDS), "words": TIMELINE_WORDS,
                       "speakerDetectionEnabled": False, "speakerDetectionAvailable": True})
        else:
            result = {"status": "ok"}
        # Unsolicited event frame first: the client must skip it.
        self.wfile.write(b'{"jsonrpc":"2.0","method":"event","params":{}}\n')
        self.wfile.write(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": result})
                         .encode() + b"\n")


class FakeBridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.calls: list = []
        self.appended: list = []
        self.polls = 0
        self.states = 0


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fake_probe(path):
    dur = TAKE_A if Path(path).stem == "take_a" else TAKE_B
    return SourceInfo(path=Path(path), width=1920, height=1080, fps="30000/1001",
                      duration=dur, has_video=True, has_audio=True, audio_streams=1)


class BridgeTestCase(unittest.TestCase):
    def setUp(self):
        self.server = FakeBridgeServer()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                                       daemon=True)
        self.thread.start()
        self.live = Bridge("127.0.0.1", self.server.server_address[1], timeout=2.0)
        self.dead = Bridge("127.0.0.1", closed_port(), timeout=0.5)
        env = mock.patch.dict(os.environ, {"VIDEO_USE_STT": "auto"}, clear=False)
        env.start()
        os.environ.pop("VIDEO_USE_STT_PREFER", None)
        os.environ.pop("VIDEO_USE_FCP_ENGINE", None)
        self.addCleanup(env.stop)
        devnull = open(os.devnull, "w")
        self.addCleanup(devnull.close)
        quiet = mock.patch("sys.stderr", new=devnull)
        quiet.start()
        self.addCleanup(quiet.stop)
        probe = mock.patch.object(stt_fcp, "probe", side_effect=fake_probe)
        probe.start()
        self.addCleanup(probe.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.videos_dir = Path(tmp.name).resolve() / "my takes"
        self.videos_dir.mkdir()
        self.take_a = self.videos_dir / "take_a.mp4"
        self.take_b = self.videos_dir / "take_b.mov"
        self.take_a.touch()
        self.take_b.touch()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def methods(self):
        return [m for m, _ in self.server.calls]

    def params(self, method):
        return [p for m, p in self.server.calls if m == method]


class ProviderSelectionTest(BridgeTestCase):
    def test_auto_picks_fcp_when_bridge_up(self):
        with mock.patch.object(stt, "find_api_key", return_value="k"):
            p = stt.resolve_provider(bridge=self.live)
        self.assertEqual(p.name, "fcp")
        self.assertFalse(p.supports_parallel)
        self.assertEqual(p.engine, "parakeet")

    def test_prefer_scribe_env(self):
        with mock.patch.object(stt, "find_api_key", return_value="k"), \
             mock.patch.dict(os.environ, {"VIDEO_USE_STT_PREFER": "scribe"}):
            p = stt.resolve_provider(bridge=self.live)
        self.assertEqual(p.name, "scribe")

    def test_auto_falls_back_to_scribe_when_bridge_down(self):
        with mock.patch.object(stt, "find_api_key", return_value="k"):
            p = stt.resolve_provider(bridge=self.dead)
        self.assertEqual(p.name, "scribe")
        self.assertTrue(p.supports_parallel)

    def test_auto_raises_when_nothing_available(self):
        with mock.patch.object(stt, "find_api_key", return_value=None):
            with self.assertRaises(RuntimeError) as cm:
                stt.resolve_provider(bridge=self.dead)
        msg = str(cm.exception)
        self.assertIn("Final Cut Pro", msg)
        self.assertIn("ELEVENLABS_API_KEY", msg)

    def test_explicit_env_choice(self):
        with mock.patch.dict(os.environ, {"VIDEO_USE_STT": "scribe"}):
            self.assertEqual(stt.resolve_provider(bridge=self.live).name, "scribe")
        with mock.patch.dict(os.environ, {"VIDEO_USE_STT": "fcp", "VIDEO_USE_FCP_ENGINE": "appleSpeech"}):
            p = stt.resolve_provider(bridge=self.dead)
        self.assertEqual((p.name, p.engine), ("fcp", "appleSpeech"))


class FcpProviderTest(BridgeTestCase):
    def provider(self):
        return stt_fcp.FcpProvider(self.live, poll_interval=0.01)

    def test_batch_splits_words_per_take(self):
        done = []
        out = self.provider().transcribe_many([self.take_a, self.take_b],
                                              on_done=lambda v, r: done.append(v))
        self.assertEqual(done, [self.take_a, self.take_b])
        a, b = out[self.take_a], out[self.take_b]

        self.assertEqual(a["text"], "Hola mundo.")
        self.assertEqual([(w["type"], w["start"], w["end"]) for w in a["words"]],
                         [("word", 0.1, 0.4), ("spacing", 0.4, 0.5), ("word", 0.5, 0.9)])
        # B's words shifted back by its 2.0 s offset; the last word clamped to the clip end.
        self.assertEqual(b["text"], "Otra toma.")
        self.assertEqual([(w["type"], w["start"], w["end"]) for w in b["words"]],
                         [("word", 0.2, 0.5), ("spacing", 0.5, 0.6), ("word", 0.6, TAKE_B)])
        for payload in (a, b):
            self.assertTrue(all(w.get("speaker_id") is None for w in payload["words"]))
            prov = payload["provider"]
            self.assertEqual((prov["name"], prov["engine"], prov["model"]),
                             ("fcp", "parakeet", "parakeet-tdt-0.6b-v3"))
            self.assertTrue(prov["inventory_project"].startswith("video-use · inventory my takes "))
        self.assertEqual(b["provider"]["timeline_offset"], TAKE_A)

    def test_rpc_sequence(self):
        """Primary route: the inventory project is one FCPXML with every take on the spine."""
        self.provider().transcribe_many([self.take_a, self.take_b])
        self.assertEqual(self.methods(), [
            "fcpxml.import", "project.open", "timeline.getDetailedState",
            "transcript.setEngine", "transcript.open",
            "transcript.getState", "transcript.getState", "transcript.close",
        ])
        (xml,) = [p["xml"] for p in self.params("fcpxml.import")]
        self.assertEqual(xml.count("<asset-clip "), 2)
        self.assertIn('<event name="video-use · inventory">', xml)
        self.assertIn(self.take_a.as_uri(), xml)
        self.assertIn('frameDuration="1001/30000s"', xml)
        self.assertIn("Rec. 709", xml)
        self.assertNotIn("<caption", xml)

    def test_falls_back_to_media_import_when_fcp_drops_clips(self):
        self.server.drop_xml_clips = True
        out = self.provider().transcribe_many([self.take_a, self.take_b])
        self.assertEqual(self.methods()[:3], ["fcpxml.import", "project.open", "timeline.getDetailedState"])
        self.assertEqual(self.methods()[3:9], [
            "media.importFile", "fcpxml.import", "project.open",
            "browser.appendClip", "browser.appendClip", "timeline.getDetailedState"])
        (imp,) = self.params("media.importFile")
        self.assertEqual(imp, {"paths": [str(self.take_a), str(self.take_b)], "manageFileType": 0})
        self.assertEqual([p["handle"] for p in self.params("browser.appendClip")], ["obj_70", "obj_71"])
        self.assertIn("<spine/>", self.params("fcpxml.import")[1]["xml"])
        self.assertEqual(out[self.take_b]["text"], "Otra toma.")
        # the fallback project gets its own name, so it can't collide with the dropped one
        first, second = [p["name"] for p in self.params("project.open")[:2]]
        self.assertNotEqual(first, second)
        self.assertEqual(self.params("transcript.setEngine"), [{"engine": "parakeet"}])
        self.assertEqual(self.params("transcript.open"), [{"forceRetranscribe": True}])

    def test_error_status_raises_error_message(self):
        provider = self.provider()
        with mock.patch.object(provider.bridge, "call", wraps=provider.bridge.call) as call:
            def fake(method, **kw):
                if method == "transcript.getState":
                    return {"status": "error", "errorMessage": "parakeet model missing"}
                return self.live.__class__.call(provider.bridge, method, **kw)
            call.side_effect = fake
            with self.assertRaises(stt_fcp.BridgeError) as cm:
                provider.transcribe(self.take_a)
        self.assertIn("parakeet model missing", str(cm.exception))
        self.assertEqual(self.methods()[-1], "transcript.close")

    def test_audio_track_not_supported(self):
        with self.assertRaises(ValueError) as cm:
            self.provider().transcribe_many([self.take_a], audio_track=1)
        self.assertIn("--provider scribe", str(cm.exception))
        self.assertEqual(self.methods(), [])

    def test_transcribe_one_saves_provider_key(self):
        import transcribe
        edit = self.videos_dir / "edit"
        path = transcribe.transcribe_one(self.take_a, edit, provider=self.provider(), verbose=False)
        data = json.loads(path.read_text())
        self.assertEqual(path, edit / "transcripts" / "take_a.json")
        self.assertEqual(data["provider"]["name"], "fcp")
        self.assertIn("inventory_project", data["provider"])
        # Single-file mode still runs the batch route over a one-take timeline.
        self.assertEqual(len(self.params("browser.appendClip")), 1)
        # The fake timeline carries B's words too; only A's window is kept.
        self.assertEqual([w["text"] for w in data["words"] if w["type"] == "word"], ["Hola", "mundo."])


if __name__ == "__main__":
    unittest.main()
