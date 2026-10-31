"""helpers/fcp/bridge.py against a scripted fake JSON-RPC server on an ephemeral port."""
import json
import socket
import socketserver
import sys
import threading
import unittest
from pathlib import Path

FCP = Path(__file__).parents[1] / "helpers" / "fcp"
if str(FCP) not in sys.path:
    sys.path.insert(0, str(FCP))

from bridge import Bridge, BridgeDown, BridgeError  # noqa: E402


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline()
        if not line:
            return
        req = json.loads(line)
        self.server.requests.append(req)
        for frame in self.server.script(req):
            self.wfile.write((frame if isinstance(frame, bytes) else json.dumps(frame).encode()) + b"\n")


class ScriptedServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, script):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.script = script
        self.requests: list = []


def closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class BridgeCallTest(unittest.TestCase):
    def serve(self, script):
        server = ScriptedServer(script)
        th = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        th.start()

        def stop():
            server.shutdown()
            server.server_close()
        self.addCleanup(stop)
        return server, Bridge("127.0.0.1", server.server_address[1], timeout=2.0)

    def test_skips_notification_and_stale_frames(self):
        def script(req):
            return [
                {"jsonrpc": "2.0", "method": "timeline.changed", "params": {}},
                b"",                                   # blank line
                b"not json",
                {"jsonrpc": "2.0", "id": req["id"] + 100, "result": {"stale": True}},
                {"jsonrpc": "2.0", "result": {"no_id": True}},
                {"jsonrpc": "2.0", "id": req["id"], "result": {"answer": 42}},
            ]
        server, b = self.serve(script)
        self.assertEqual(b.call("system.version"), {"answer": 42})
        self.assertEqual(b.call("system.version"), {"answer": 42})   # ids advance per call
        ids = [r["id"] for r in server.requests]
        self.assertEqual(ids, [1, 2])

    def test_error_frame_raises_bridge_error(self):
        def script(req):
            return [{"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "nope"}}]
        _, b = self.serve(script)
        with self.assertRaises(BridgeError) as cm:
            b.call("does.not.exist")
        self.assertEqual(cm.exception.method, "does.not.exist")
        self.assertEqual(cm.exception.error["code"], -32601)
        self.assertFalse(b.alive())

    def test_keyword_named_method_is_a_param(self):
        def script(req):
            return [{"jsonrpc": "2.0", "id": req["id"], "result": {"echo": req["params"]}}]
        server, b = self.serve(script)
        out = b.call("system.callMethod", method="isAnyLibraryUpdating", className="X", _timeout=1.0)
        self.assertEqual(out, {"echo": {"method": "isAnyLibraryUpdating", "className": "X"}})
        self.assertEqual(server.requests[0]["method"], "system.callMethod")
        self.assertNotIn("_timeout", server.requests[0]["params"])

    def test_server_closes_without_answer(self):
        _, b = self.serve(lambda req: [{"jsonrpc": "2.0", "method": "event", "params": {}}])
        with self.assertRaises(BridgeDown):
            b.call("x")

    def test_alive(self):
        _, b = self.serve(lambda req: [{"jsonrpc": "2.0", "id": req["id"], "result": {"alive": True}}])
        self.assertTrue(b.alive())

    def test_closed_port_raises_bridge_down(self):
        b = Bridge("127.0.0.1", closed_port(), timeout=0.5)
        with self.assertRaises(BridgeDown):
            b.call("bridge.alive")
        self.assertFalse(b.alive())


if __name__ == "__main__":
    unittest.main()
