#!/usr/bin/env python3
"""Minimal JSON-RPC client for the SpliceKit bridge that runs inside Final Cut Pro.

The SpliceKit MCP server (/Applications/SpliceKit.app/Contents/Resources/mcp/server.py)
is a thin wrapper over a newline-delimited JSON-RPC 2.0 socket on 127.0.0.1:9876.
Helpers talk to that socket directly so they get raw JSON (the MCP tools
pretty-print and truncate) and stay unit-testable against a fake server.

Protocol notes mirrored from server.py `BridgeConnection`:
  - one request per line, one response per line
  - the server may emit unsolicited frames with a `method` key (events);
    those are skipped until a frame with our `id` arrives
  - responses can be large (FCPXML exports), hence the big recv buffer

Usage:
    from bridge import Bridge, BridgeDown
    b = Bridge()
    if b.alive():
        print(b.call("system.version"))

CLI:
    python helpers/fcp/bridge.py status      # exit 0 when the bridge answers
    python helpers/fcp/bridge.py call transcript.getState
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from typing import Any, Callable

DEFAULT_HOST = os.environ.get("SPLICEKIT_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("SPLICEKIT_PORT", "9876"))
RECV_CHUNK = 16 * 1024 * 1024


class BridgeDown(RuntimeError):
    """Raised when Final Cut Pro with SpliceKit is not reachable."""


class BridgeError(RuntimeError):
    """Raised when the bridge answered with a JSON-RPC error."""

    def __init__(self, method: str, error: Any):
        super().__init__(f"{method}: {error}")
        self.method = method
        self.error = error


class Bridge:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._id = 0

    # -- transport ---------------------------------------------------------
    def _connect(self) -> socket.socket:
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise BridgeDown(
                f"SpliceKit bridge not reachable at {self.host}:{self.port} "
                f"(is Final Cut Pro running with SpliceKit injected?): {e}"
            ) from e
        return s

    def call(self, method: str, /, _timeout: float | None = None, **params) -> Any:
        """Send one JSON-RPC request on a fresh socket and return its `result`.

        A fresh socket per call keeps helpers stateless between agent turns and
        avoids fighting the MCP server for the same connection.
        """
        self._id += 1
        req_id = self._id
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": req_id}
        sock = self._connect()
        if _timeout is not None:
            sock.settimeout(_timeout)
        buf = b""
        try:
            sock.sendall(json.dumps(payload).encode("utf-8") + b"\n")
            while True:
                while b"\n" not in buf:
                    chunk = sock.recv(RECV_CHUNK)
                    if not chunk:
                        raise BridgeDown(f"{method}: connection closed by SpliceKit")
                    buf += chunk
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" in frame or "id" not in frame:
                    continue  # event / notification frame
                if frame.get("id") != req_id:
                    continue  # stale answer from another caller
                if "error" in frame:
                    raise BridgeError(method, frame["error"])
                return frame.get("result", {})
        except socket.timeout as e:
            raise BridgeDown(f"{method}: timed out after {_timeout or self.timeout}s") from e
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # -- conveniences ------------------------------------------------------
    def alive(self) -> bool:
        """Cheap liveness probe (`bridge.alive` does not touch FCP's main thread)."""
        try:
            r = self.call("bridge.alive", _timeout=3.0)
        except (BridgeDown, BridgeError):
            return False
        return bool(r) and (r.get("alive", True) is not False)

    def version(self) -> dict:
        return self.call("system.version")

    def wait_until(self, predicate: Callable[[], bool], timeout: float,
                   interval: float = 2.0, what: str = "condition") -> None:
        deadline = time.monotonic() + timeout
        while True:
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
            time.sleep(interval)


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in {"status", "call"}:
        print(__doc__)
        return 2
    b = Bridge()
    if argv[1] == "status":
        try:
            v = b.version()
        except BridgeDown as e:
            print(json.dumps({"alive": False, "error": str(e),
                              "fallback": "bridge down → use helpers/render.py (ffmpeg path)"}))
            return 1
        print(json.dumps({"alive": True, **(v if isinstance(v, dict) else {"version": v})}))
        return 0
    method = argv[2]
    params = json.loads(argv[3]) if len(argv) > 3 else {}
    try:
        print(json.dumps(b.call(method, **params), indent=2, ensure_ascii=False))
    except (BridgeDown, BridgeError) as e:
        print(json.dumps({"error": str(e)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
