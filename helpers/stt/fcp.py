"""Final Cut Pro transcription through the SpliceKit bridge (Parakeet engine).

Verified against FCP 12.4 + SpliceKit 3.3.9: `transcript.open(fileURL=...)` always
fails and the `fcpNative` engine is broken, but the transcript panel transcribes
the clips on the CURRENT TIMELINE with `parakeet`. So one batch is:

    fcpxml.import(xml=<inventory project>)               # every take full length, built by
                                                         # edl_to_fcpxml (start timecode, LUT),
                                                         # in the "video-use · inventory" event
    project.open(name=<inventory project>)               # + reconcile the clip count
      (fallback when FCP drops clips: media.importFile → empty project → browser.appendClip)
    transcript.setEngine(engine="parakeet")
    transcript.open(forceRetranscribe=True)
    transcript.getState()                                # polled until "ready"
    transcript.close()

Words come back in TIMELINE seconds; each take's words are those inside its
[offset, offset + duration) window, shifted back to source seconds. The panel is
a singleton, so the provider is serial (`supports_parallel = False`) and batches
all pending takes into one timeline.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import quoteattr

from fcp.bridge import Bridge, BridgeError
from fcp.probe import COLORSPACE_REC709, format_name, probe
from fcp.timing import Timebase

from .base import OnDone, Provider, provider_meta
from .normalize import fcp_words_to_scribe, words_to_text

DEFAULT_ENGINE = "parakeet"
POLL_INTERVAL_S = 2.0
BASE_TIMEOUT_S = 120.0
PER_SECOND_TIMEOUT = 3.0
FCPXML_VERSION = "1.11"
INVENTORY_EVENT = "video-use · inventory"
RPC_TIMEOUT_S = 120.0


def engine_name() -> str:
    return os.environ.get("VIDEO_USE_FCP_ENGINE", "").strip() or DEFAULT_ENGINE


def inventory_project_name(videos_dir: Path, now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    return f"video-use · inventory {videos_dir.name} {stamp}"


def inventory_fcpxml(project: str, fps: str | None) -> str:
    """A project with an empty spine: the timeline the takes get appended to."""
    tb = Timebase.from_fps(fps or "30")
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE fcpxml>",
        f'<fcpxml version="{FCPXML_VERSION}">',
        "<resources>",
        f'<format id="r1" name={quoteattr(format_name(1920, 1080, tb.fps))} '
        f'frameDuration="{tb.frame_duration}" width="1920" height="1080" '
        f'colorSpace={quoteattr(COLORSPACE_REC709)}/>',
        "</resources>",
        "<library>",
        f"<event name={quoteattr(INVENTORY_EVENT)}>",
        f"<project name={quoteattr(project)}>",
        '<sequence format="r1" duration="0s" tcStart="0s" tcFormat="NDF" '
        'audioLayout="stereo" audioRate="48k">',
        "<spine/>",
        "</sequence>",
        "</project>",
        "</event>",
        "</library>",
        "</fcpxml>",
        "",
    ])


def _seconds(t: object) -> float | None:
    if isinstance(t, dict) and t.get("seconds") is not None:
        return float(t["seconds"])
    if isinstance(t, (int, float)):
        return float(t)
    return None


def _ok(method: str, result: object) -> dict:
    """SpliceKit reports some failures inside `result` instead of a JSON-RPC error."""
    r = result if isinstance(result, dict) else {}
    if r.get("status") == "error" or (r.get("error") and r.get("status") != "ok"):
        raise BridgeError(method, r.get("errorMessage") or r.get("error") or r)
    return r


class FcpProvider(Provider):
    name = "fcp"
    supports_parallel = False

    def __init__(self, bridge: Bridge | None = None, poll_interval: float = POLL_INTERVAL_S,
                 engine: str | None = None):
        self.bridge = bridge or Bridge()
        self.poll_interval = poll_interval
        self.engine = engine or engine_name()

    def transcribe(self, video: Path, *, audio_track: int = 0, language: str | None = None,
                   num_speakers: int | None = None, force: bool = False,
                   verbose: bool = False) -> dict:
        video = Path(video).resolve()
        res = self.transcribe_many([video], audio_track=audio_track, language=language,
                                   num_speakers=num_speakers, force=force, verbose=verbose)[video]
        if isinstance(res, Exception):
            raise res
        return res

    def transcribe_many(self, videos: list[Path], *, audio_track: int = 0,
                        language: str | None = None, num_speakers: int | None = None,
                        force: bool = False, verbose: bool = False, workers: int = 1,
                        on_done: OnDone | None = None) -> dict[Path, dict | Exception]:
        if audio_track != 0:
            raise ValueError(
                f"--audio-track {audio_track} is not supported by the FCP provider (it "
                "transcribes each clip's default audio on the timeline); use --provider scribe")
        videos = [Path(v).resolve() for v in videos]
        if not videos:
            return {}
        infos = [probe(v) for v in videos]
        project = inventory_project_name(videos[0].parent)

        offsets = self._build_timeline(videos, infos, project, verbose)
        total = sum(d for _, d in offsets)
        state = self._transcribe_timeline(BASE_TIMEOUT_S + PER_SECOND_TIMEOUT * total, verbose)

        words = state.get("words") or []
        results: dict[Path, dict | Exception] = {}
        for video, (offset, duration) in zip(videos, offsets):
            mine = [w for w in words
                    if w.get("startTime") is not None
                    and offset <= float(w["startTime"]) < offset + duration]
            sw = fcp_words_to_scribe(mine, offset=offset)
            for w in sw:  # a word cannot outlive its clip
                w["end"] = min(w["end"], duration)
            payload = {
                "language_code": state.get("language") or state.get("locale") or language,
                "text": words_to_text(sw),
                "words": sw,
                "provider": provider_meta(
                    "fcp", state.get("engine") or self.engine, None,
                    model=state.get("parakeetModel"), inventory_project=project,
                    timeline_offset=offset),
            }
            results[video] = payload
            if on_done:
                on_done(video, payload)
        return results

    # -- steps -------------------------------------------------------------
    def _build_timeline(self, videos: list[Path], infos: list, project: str,
                        verbose: bool) -> list[tuple[float, float]]:
        """Put every take on an inventory timeline. Returns (timeline offset, duration) per take."""
        try:
            return self._build_timeline_xml(videos, project, verbose)
        except (BridgeError, RuntimeError, ValueError, KeyError) as e:
            if verbose:
                print(f"  inventory via FCPXML failed ({e}); falling back to media import", flush=True)
            return self._build_timeline_append(videos, infos, project + " (append)", verbose)

    def _build_timeline_xml(self, videos: list[Path], project: str,
                            verbose: bool) -> list[tuple[float, float]]:
        from fcp import edl_to_fcpxml as builder   # lazy: heavy module, only for this route
        edl = {"version": 1,
               "sources": {f"T{i:03d}": str(v) for i, v in enumerate(videos)},
               "ranges": [{"source": f"T{i:03d}", "start": 0.0, "end": 1e9, "beat": v.stem}
                          for i, v in enumerate(videos)],
               "grade": "none", "overlays": [],
               "fcp": {"event": INVENTORY_EVENT, "project": project, "captions": "none"}}
        opt = builder.BuildOptions(captions="none", fade_s=0.0, event=INVENTORY_EVENT, project=project,
                                   probe_fn=lambda path: probe(path))   # late-bound: patchable in tests
        xml, plan = builder.build(edl, videos[0].parent / "edit", opt)
        if verbose:
            print(f"  importing the inventory project ({len(videos)} take(s)) into Final Cut Pro",
                  flush=True)
        _ok("fcpxml.import", self.bridge.call("fcpxml.import", _timeout=RPC_TIMEOUT_S,
                                              xml=xml, internal=True))
        self._open_project(project)
        st = self.bridge.call("timeline.getDetailedState", limit=500)
        spine = [it for it in (st.get("items") or []) if it.get("lane", 0) == 0]
        if len(spine) != len(videos):
            raise RuntimeError(f"FCP kept {len(spine)}/{len(videos)} takes on the inventory timeline")
        # the timeline's own positions are the clock Parakeet reports words on; the plan is the fallback
        out = []
        for it, c in zip(spine, plan.clips):
            start = _seconds(it.get("startTime"))
            dur = _seconds(it.get("duration"))
            out.append((c["out_start_s"] if start is None else start, c["duration_s"] if not dur else dur))
        return out

    def _build_timeline_append(self, videos: list[Path], infos: list, project: str,
                               verbose: bool) -> list[tuple[float, float]]:
        """Fallback: import the takes, open an empty inventory project, append them in order."""
        b = self.bridge
        if verbose:
            print(f"  importing {len(videos)} take(s) into Final Cut Pro", flush=True)
        r = _ok("media.importFile", b.call("media.importFile", _timeout=RPC_TIMEOUT_S,
                                           paths=[str(v) for v in videos], manageFileType=0))
        handles = self._match_handles(videos, r.get("imported") or [], r)

        _ok("fcpxml.import", b.call("fcpxml.import", _timeout=RPC_TIMEOUT_S,
                                    xml=inventory_fcpxml(project, infos[0].fps), internal=True))
        self._open_project(project)

        offsets: list[tuple[float | None, float]] = []
        for video, info, handle in zip(videos, infos, handles):
            ar = _ok("browser.appendClip", b.call("browser.appendClip", handle=handle))
            item = ((ar.get("placementDebug") or {}).get("matchedInsertedItem") or {})
            start = _seconds(item.get("startTime"))
            duration = _seconds(item.get("duration")) or info.duration
            offsets.append((start, duration))
        if any(s is None for s, _ in offsets):
            offsets = self._offsets_from_timeline(videos, offsets)
        return [(float(s), float(d)) for s, d in offsets]

    def _match_handles(self, videos: list[Path], imported: list[dict], raw: dict) -> list[str]:
        by_path = {str(Path(i.get("path", "")).resolve()): i.get("handle") for i in imported
                   if i.get("path")}
        by_name: dict[str, list[str]] = {}
        for i in imported:
            by_name.setdefault(i.get("name", ""), []).append(i.get("handle"))
        handles = []
        for v in videos:
            h = by_path.get(str(v)) or (by_name.get(v.stem) or [None]).pop(0)
            if not h:
                raise BridgeError("media.importFile", f"{v.name} was not imported: {raw}")
            handles.append(h)
        return handles

    def _open_project(self, project: str, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        last: object = None
        while True:
            try:
                r = self.bridge.call("project.open", name=project)
                if isinstance(r, dict) and r.get("status") == "ok":
                    return
                last = r
            except BridgeError as e:  # the imported project can take a moment to appear
                last = e
            if time.monotonic() >= deadline:
                raise BridgeError("project.open", f"inventory project {project!r}: {last}")
            time.sleep(self.poll_interval)

    def _offsets_from_timeline(self, videos: list[Path],
                               offsets: list[tuple[float | None, float]]):
        st = self.bridge.call("timeline.getDetailedState", limit=500)
        items = [it for it in (st.get("items") or []) if it.get("lane", 0) == 0]
        out, used = [], set()
        for video, (start, duration) in zip(videos, offsets):
            if start is None:
                for i, it in enumerate(items):
                    if i not in used and it.get("name") == video.stem:
                        used.add(i)
                        start = _seconds(it.get("startTime"))
                        duration = _seconds(it.get("duration")) or duration
                        break
            if start is None:
                raise BridgeError("timeline.getDetailedState",
                                  f"cannot find {video.stem!r} on the inventory timeline")
            out.append((start, duration))
        return out

    def _transcribe_timeline(self, timeout: float, verbose: bool) -> dict:
        b = self.bridge
        _ok("transcript.setEngine", b.call("transcript.setEngine", engine=self.engine))
        if verbose:
            print(f"  transcribing the inventory timeline in FCP ({self.engine})", flush=True)
        try:
            _ok("transcript.open", b.call("transcript.open", forceRetranscribe=True))
            return self._wait_ready(timeout)
        finally:
            try:
                b.call("transcript.close")
            except Exception:  # closing is best effort; the transcript is already in hand
                pass

    def _wait_ready(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            state = self.bridge.call("transcript.getState")
            state = state if isinstance(state, dict) else {}
            status = state.get("status")
            if status == "ready":
                return state
            if status == "error":
                raise BridgeError("transcript.getState",
                                  state.get("errorMessage") or state.get("error")
                                  or "transcription failed")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"FCP transcription not ready after {timeout:.0f}s (last status: {status})")
            time.sleep(self.poll_interval)
